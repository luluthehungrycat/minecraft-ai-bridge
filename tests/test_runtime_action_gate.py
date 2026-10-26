"""Deterministic acceptance tests for the RFC-0001 Phase A action gate.

Every built-in mutation entry point (strategic LLM actions, follow-mode
teleport, trusted startup provisioning, chat-command teleports, and the
self-preservation layer mutating reflexes ATTACK/EAT/HEAL) must evaluate
the shared policy hook, dispatch only when allowed, and leave a bounded
audit record behind. Read-only observation calls stay outside the gate
by design (Phase A scope).

All tests use the deterministic in-memory MCPQ mock; no live server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from minecraft_ai_bridge.bridge.chat_commands import ChatCommandHandler
from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.bridge.self_preservation import (
    PreservationConfig,
    SelfPreservationLayer,
)
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.llm.models import LLMResponse
from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.minecraft.observer import InventorySlot, WorldState
from minecraft_ai_bridge.runtime import ActionService, PolicyDecision
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


class PolicyDeny:
    """Minimal request object exposing only the identity the policy sees."""

    def __init__(self, action: ActionType) -> None:
        self.action = action


def _seen_actions(service: ActionService) -> list[str]:
    return [record.action.value for record in service.audit_records]


def _assert_rejected(
    service: ActionService,
    record_action: ActionType,
    reason_substring: str,
) -> None:
    record = service.audit_records[-1]
    assert record.action is record_action
    assert record.allowed is False
    assert record.succeeded is False
    assert reason_substring in record.reason


def _assert_succeeded(service: ActionService, record_action: ActionType) -> None:
    record = service.audit_records[-1]
    assert record.action is record_action
    assert record.allowed is True
    assert record.succeeded is True


def deny_all(request: Any) -> PolicyDecision:
    return PolicyDecision(allowed=False, reason="test denial of " + request.action.value)


def logged_denier(log: PolicyLog) -> Any:
    """Deny every request while still recording it, like a real policy."""

    def _deny(request: Any) -> PolicyDecision:
        log.actions.append(request.action)
        return PolicyDecision(allowed=False, reason="test denial of " + request.action.value)

    return _deny


@dataclass
class PolicyLog:
    actions: list[ActionType] = field(default_factory=list)

    def hook(self, request: Any) -> PolicyDecision:
        self.actions.append(request.action)
        return PolicyDecision(allowed=True, allow_privileged=True)


class RecordingLLM:
    def __init__(self, responses: list[tuple[str, dict[str, Any]]]) -> None:
        self._responses = responses
        self._index = 0

    async def decide(
        self,
        system_prompt: str,
        messages: list,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        del system_prompt, messages, tool_choice
        if self._index >= len(self._responses):
            return LLMResponse(action="done", action_params={}, reasoning="exhausted")
        action, params = self._responses[self._index]
        self._index += 1
        return LLMResponse(action=action, action_params=params, reasoning="test")

    async def decompose_goal(self, goal: str) -> list[dict[str, Any]]:
        del goal
        return []


@pytest_asyncio.fixture
async def gate_env(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Orchestrator, ActionService, PolicyLog]]:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.bridge.max_iterations = 10
    config.bridge.cycle_delay = 0
    orch = Orchestrator(config)
    policy_log = PolicyLog()
    orch._llm = RecordingLLM([("done", {"message": "complete"})])
    orch._goals._llm = orch._llm
    orch._goals.set_goal_from_subgoals("Gate test", [{"description": "stay alive"}])
    orch._mc = mock_mc
    orch._world = McpqWorldAdapter(mock_mc)
    orch._internal_action_authority = object()
    orch._safety_action_authority = object()
    service = ActionService(
        orch._world,
        policy=policy_log.hook,
        internal_authority=orch._internal_action_authority,
        safety_authority=orch._safety_action_authority,
    )
    orch._action_service = service

    async def observe() -> WorldState:
        state = WorldState(position=(0.0, 65.0, 0.0), health=20.0, hunger=20)
        orch._memory.record_observation(state)
        return state

    orch._observe = observe  # type: ignore[method-assign]
    return orch, service, policy_log


@pytest.mark.asyncio
class TestStrategicActionGate:
    async def test_llm_action_dispatches_through_service_and_audits(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        orch._llm = RecordingLLM([("chat", {"message": "gate online"})])
        orch._goals._llm = orch._llm

        await orch._step()

        assert policy_log.actions == [ActionType.CHAT]
        _assert_succeeded(service, ActionType.CHAT)
        assert any("gate online" in msg for msg in orch._mc.chat_messages_sent)

    async def test_policy_denial_blocks_strategic_action(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        service._policy = logged_denier(policy_log)

        result = await orch._act(LLMResponse(action="chat", action_params={"message": "nope"}))

        assert result.success is False
        assert result.data["correlation_id"]
        assert policy_log.actions == [ActionType.CHAT]
        _assert_rejected(service, ActionType.CHAT, "test denial")
        assert not any("nope" in msg for msg in orch._mc.chat_messages_sent)

    async def test_allowed_action_executes_world_mutation(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, _log = gate_env
        params = {"x": 3, "y": 64, "z": 3, "block_type": "stone"}

        result = await orch._act(LLMResponse(action="place_block", action_params=params))

        assert result.success is True
        _assert_succeeded(service, ActionType.PLACE_BLOCK)
        assert orch._mc._block_map.get((3, 64, 3)) == "stone"


@pytest.mark.asyncio
class TestInternalActionProtection:
    async def test_llm_cannot_invoke_internal_action_identities(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        internal_names = [
            "follow_teleport",
            "provision_bot",
            "prepare_safe_spawn",
            "goto_teleport",
            "come_teleport",
        ]

        for name in internal_names:
            result = await orch._act(LLMResponse(action=name, action_params={}))
            assert result.success is False, name
            assert result.action is ActionType.WAIT
            assert "Internal action not available" in result.message

        # Rejection happens before policy evaluation or dispatch.
        assert policy_log.actions == []
        assert _seen_actions(service) == []

    async def test_caller_policy_can_restrict_authz_privileged_allow(
        self, mock_mc: MockMcpqClient
    ) -> None:
        authority = object()

        def authz_policy(_request: Any) -> PolicyDecision:
            return PolicyDecision(
                allowed=True,
                allow_privileged=True,
                identity_scope=("controller", "test-controller"),
            )

        def caller_policy(_request: Any) -> PolicyDecision:
            return PolicyDecision(allowed=True, allow_privileged=False)

        service = ActionService(
            mock_mc,
            policy=Orchestrator._combine_action_policies(authz_policy, caller_policy),
            internal_authority=authority,
        )

        result = await service.execute(
            ActionType.PROVISION_BOT,
            {"player_name": "AIBot"},
            authority=authority,
        )

        assert not result.success
        assert "privileged backend authorization" in result.message
        assert mock_mc.commands_ran == []
        _assert_rejected(service, ActionType.PROVISION_BOT, "privileged backend authorization")


async def _fake_pos_response(command: str) -> str:
    del command
    return "[10.0d, 65.0d, 10.0d]"


@pytest.mark.asyncio
class TestFollowTeleportGate:
    async def test_follow_mode_routes_through_service_policy_and_audit(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        mock_mc._run_command_blocking = _fake_pos_response
        orch._follow_target = "TargetPlayer"
        orch._turn += 1

        await orch._step()

        assert ActionType.FOLLOW_TELEPORT in policy_log.actions
        assert any(
            record.action is ActionType.FOLLOW_TELEPORT and record.allowed
            for record in service.audit_records
        )
        assert any("run tp" in cmd for cmd in mock_mc.commands_ran)


@pytest.mark.asyncio
class TestStartupProvisioningGate:
    async def test_internal_action_requires_service_capability(
        self, mock_mc: MockMcpqClient
    ) -> None:
        service = ActionService(mock_mc)

        denied = await service.execute(
            ActionType.PROVISION_BOT,
            {"player_name": "Bot"},
        )

        assert denied.success is False
        assert "trusted runtime authority" in denied.message
        assert not any("botsummon" in command for command in mock_mc.commands_ran)

    async def test_provision_bot_routes_through_service(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env

        result = await orch._provision_bot("ProvisionedBot")

        assert result.success is True
        assert policy_log.actions == [ActionType.PROVISION_BOT]
        _assert_succeeded(service, ActionType.PROVISION_BOT)
        assert any("botsummon ProvisionedBot" in cmd for cmd in orch._mc.commands_ran)

    async def test_policy_denies_startup_provisioning(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        service._policy = logged_denier(policy_log)

        result = await orch._provision_bot("ProvisionedBot")

        assert result.success is False
        assert policy_log.actions == [ActionType.PROVISION_BOT]
        _assert_rejected(service, ActionType.PROVISION_BOT, "test denial")
        assert not any("botsummon" in cmd for cmd in orch._mc.commands_ran)


@pytest.mark.asyncio
class TestStartupSafeSpawnGate:
    async def test_prepare_safe_spawn_routes_through_service(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc

        async def instant_sleep(_seconds: float) -> None:
            return None

        monkey_sleep = pytest.MonkeyPatch()
        monkey_sleep.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", instant_sleep)
        try:
            await orch._prepare_safe_spawn()
        finally:
            monkey_sleep.undo()

        assert policy_log.actions == [ActionType.PREPARE_SAFE_SPAWN]
        _assert_succeeded(service, ActionType.PREPARE_SAFE_SPAWN)
        assert any("teleport to (0.0, 65.0, 0.0)" in cmd for cmd in mock_mc.commands_ran)
        assert mock_mc._pos == (0.0, 65.0, 0.0)
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                assert mock_mc._block_map.get((dx, 64, dz)) == "dirt"

    async def test_policy_denial_blocks_safe_spawn_writes(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        service._policy = logged_denier(policy_log)

        async def instant_sleep(_seconds: float) -> None:
            return None

        monkey_sleep = pytest.MonkeyPatch()
        monkey_sleep.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", instant_sleep)
        try:
            await orch._prepare_safe_spawn()
        finally:
            monkey_sleep.undo()

        # Each of the three startup retry attempts re-evaluates the policy
        # and records its own rejection; none of them reaches dispatch.
        assert policy_log.actions == [ActionType.PREPARE_SAFE_SPAWN] * 3
        _assert_rejected(service, ActionType.PREPARE_SAFE_SPAWN, "test denial")
        assert not any("teleport" in cmd for cmd in mock_mc.commands_ran)
        assert mock_mc._pos == (0.0, 65.0, 0.0)
        assert mock_mc._block_map == {}


class _BareHarness:
    def __init__(self, mc: MockMcpqClient) -> None:
        self._mc = mc
        self._world = None
        self._action_service = None
        self._action_policy = None
        self._internal_action_authority = object()
        self._stop_requested = False
        self._follow_target = None
        self._turn = 0


@pytest.mark.asyncio
class TestChatCommandGate:
    async def test_chat_goto_teleport_shares_service_policy_and_audit(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        orch._config.chat_control.allowed_names = ["SomePlayer"]
        orch._config.chat_control.server_online_mode = True
        handler = ChatCommandHandler(orch)

        await handler.handle_command(
            "!goto",
            "TargetPlayer",
            "SomePlayer",
            trusted_player=SimpleNamespace(name="SomePlayer"),
        )

        assert policy_log.actions == [ActionType.GOTO_TELEPORT]
        assert handler._action_service is service
        _assert_succeeded(service, ActionType.GOTO_TELEPORT)
        assert any("tp AIBot TargetPlayer" in cmd for cmd in orch._mc.commands_ran)

    async def test_chat_come_teleport_shares_service_policy_and_audit(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        orch._config.chat_control.allowed_names = ["SomePlayer"]
        orch._config.chat_control.server_online_mode = True
        handler = ChatCommandHandler(orch)

        await handler.handle_command(
            "!come",
            "",
            "SomePlayer",
            trusted_player=SimpleNamespace(name="SomePlayer"),
        )

        assert policy_log.actions == [ActionType.COME_TELEPORT]
        _assert_succeeded(service, ActionType.COME_TELEPORT)

    async def test_chat_handler_without_shared_service_uses_orchestrator_policy(
        self, mock_mc: MockMcpqClient
    ) -> None:
        harness = _BareHarness(mock_mc)
        harness._config = AppConfig()
        harness._config.chat_control.allowed_names = ["SomePlayer"]
        harness._config.chat_control.server_online_mode = True
        harness._action_policy = lambda request: deny_all(PolicyDeny(request.action))
        handler = ChatCommandHandler(harness)

        await handler.handle_command(
            "!goto",
            "TargetPlayer",
            "SomePlayer",
            trusted_player=SimpleNamespace(name="SomePlayer"),
        )

        record = handler._action_service.audit_records[-1]
        assert record.action is ActionType.GOTO_TELEPORT
        assert record.allowed is False
        assert "test denial" in record.reason
        assert not any("tp" in cmd for cmd in mock_mc.commands_ran)


def _make_layer(
    orch: Orchestrator,
    service: ActionService | None,
) -> SelfPreservationLayer:
    return SelfPreservationLayer(
        mc=orch._world,
        goal_manager=orch._goals,
        memory=orch._memory,
        config=PreservationConfig(),
        action_service=service,
        safety_authority=orch._safety_action_authority,
    )


@pytest.mark.asyncio
class TestSelfPreservationGate:
    async def test_reflex_attack_routes_through_service(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        mock_mc.set_hostile_mobs(["zombie"])
        layer = _make_layer(orch, service)

        await layer.evaluate(WorldState(health=20.0, hunger=20))
        result = await layer.evaluate(WorldState(health=15.0, hunger=20))

        assert result is not None
        assert result.action is ActionType.ATTACK
        assert policy_log.actions == []
        assert any("damage" in cmd for cmd in mock_mc.commands_ran)
        _assert_succeeded(service, ActionType.ATTACK)

    async def test_auto_eat_routes_through_service(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        layer = _make_layer(orch, service)
        world = WorldState(
            health=20.0,
            hunger=2,
            inventory=[InventorySlot(item_id="bread", count=3, slot=0)],
        )

        result = await layer.evaluate(world)

        assert result is not None
        assert result.action is ActionType.EAT
        assert policy_log.actions == []
        assert any("saturation" in cmd for cmd in mock_mc.commands_ran)
        assert mock_mc.commands_ran.index("clear AIBot minecraft:bread 1") < next(
            index for index, command in enumerate(mock_mc.commands_ran) if "saturation" in command
        )
        _assert_succeeded(service, ActionType.EAT)

    async def test_auto_heal_routes_through_service(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        layer = _make_layer(orch, service)
        world = WorldState(
            health=2.0,
            hunger=20,
            inventory=[
                InventorySlot(item_id="golden_apple", count=1, slot=0),
            ],
        )

        result = await layer.evaluate(world)

        assert result is not None
        assert result.action is ActionType.HEAL
        assert result.success is True
        assert policy_log.actions == []
        _assert_succeeded(service, ActionType.HEAL)
        assert mock_mc.commands_ran.index("clear AIBot minecraft:golden_apple 1") < next(
            index for index, command in enumerate(mock_mc.commands_ran) if "regeneration" in command
        )
        assert any("regeneration" in cmd for cmd in mock_mc.commands_ran)

    async def test_controller_policy_denial_does_not_disable_reflex_heal(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        service._policy = logged_denier(policy_log)
        layer = _make_layer(orch, service)
        world = WorldState(
            health=2.0,
            hunger=20,
            inventory=[
                InventorySlot(item_id="golden_apple", count=1, slot=0),
            ],
        )

        result = await layer.evaluate(world)

        assert result is not None
        assert result.success is True
        assert policy_log.actions == []
        _assert_succeeded(service, ActionType.HEAL)
        assert any("effect give" in cmd for cmd in mock_mc.commands_ran)

    async def test_controller_policy_denial_does_not_disable_reflex_attack(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        mock_mc.set_hostile_mobs(["zombie"])
        service._policy = logged_denier(policy_log)
        layer = _make_layer(orch, service)

        await layer.evaluate(WorldState(health=20.0, hunger=20))
        result = await layer.evaluate(WorldState(health=15.0, hunger=20))

        assert result is not None
        assert result.success is True
        assert policy_log.actions == []
        _assert_succeeded(service, ActionType.ATTACK)
        assert any("damage" in cmd for cmd in mock_mc.commands_ran)

    async def test_observations_stay_outside_policy_and_audit(
        self, gate_env: tuple[Orchestrator, ActionService, PolicyLog]
    ) -> None:
        orch, service, policy_log = gate_env
        mock_mc = orch._mc
        mock_mc.set_hostile_mobs(["zombie"])
        layer = _make_layer(orch, service)

        await layer.evaluate(WorldState(health=20.0, hunger=20))
        result = await layer.evaluate(WorldState(health=15.0, hunger=20))

        # The reflex receives dedicated safety authorization and an audit
        # record; the hurt-source probe and entity scan stay read-only.
        assert result is not None
        assert result.action is ActionType.ATTACK
        assert policy_log.actions == []
        assert [r.action for r in service.audit_records] == [ActionType.ATTACK]


@pytest.mark.asyncio
async def test_safety_authority_requires_a_consumed_item_before_healing_or_eating(
    mock_mc: MockMcpqClient, monkeypatch
) -> None:
    safety_authority = object()
    service = ActionService(mock_mc, safety_authority=safety_authority)
    commands: list[str] = []

    async def missing_item(command: str) -> str:
        commands.append(command)
        if command.startswith("clear "):
            return "No items were found matching that predicate"
        return "Given effect to AIBot"

    monkeypatch.setattr(mock_mc, "run_as_player", missing_item)

    generic_heal = await service.execute(ActionType.HEAL, {}, safety_authority=safety_authority)
    missing_apple = await service.execute(
        ActionType.HEAL,
        {"heal_item": "golden_apple"},
        safety_authority=safety_authority,
    )
    missing_bread = await service.execute(
        ActionType.EAT,
        {"food_item": "bread"},
        safety_authority=safety_authority,
    )

    assert not generic_heal.success
    assert not missing_apple.success
    assert not missing_bread.success
    assert commands == [
        "clear @p minecraft:golden_apple 1",
        "clear @p minecraft:bread 1",
    ]
    assert not any(command.startswith("effect give") for command in commands)


@pytest.mark.asyncio
class TestProductionWiring:
    async def test_connect_installs_one_shared_service_across_subsystems(
        self, mock_mc: MockMcpqClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = AppConfig()
        orch = Orchestrator(config)

        # Bind the production McpqClient constructor to the deterministic
        # mock so _connect runs its real wiring path with no live server.
        monkeypatch.setattr(
            "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
            lambda **_kwargs: mock_mc,
        )

        async def instant_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", instant_sleep)

        # The bot entity is absent on the first presence probe, so _connect
        # takes the real provisioning path; afterwards it is present.
        pos_calls = {"count": 0}

        async def player_pos() -> tuple[float, float, float] | None:
            pos_calls["count"] += 1
            if pos_calls["count"] == 1:
                return None
            return (0.0, 65.0, 0.0)

        monkeypatch.setattr(mock_mc, "get_player_pos", player_pos)

        await orch._connect()

        service = orch._action_service
        assert service is not None
        assert orch._world is not None
        assert orch._world._client is mock_mc
        # The service orchestrator installed is the same object the chat
        # handler and the self-preservation layer reference.
        assert orch._cmd_handler is not None
        assert orch._cmd_handler._action_service is service
        assert orch._preservation is not None
        assert orch._preservation._action_service is service
        # Startup provisioning was recorded by that same shared service.
        provision_records = [
            record for record in service.audit_records if record.action is ActionType.PROVISION_BOT
        ]
        assert provision_records
        assert all(record.allowed for record in provision_records)
        assert any("botsummon AIBot" in cmd for cmd in mock_mc.commands_ran)


def test_every_action_type_has_an_explicit_security_class():
    from minecraft_ai_bridge.runtime.action_service import ActionClass, classify_action

    unclassified = [a.value for a in ActionType if classify_action(a) is ActionClass.UNCLASSIFIED]
    assert unclassified == [], "classify new ActionTypes in action_service.py"


@pytest.mark.asyncio
async def test_unclassified_action_fails_closed_before_policy(mock_mc: MockMcpqClient):
    from enum import StrEnum

    from minecraft_ai_bridge.runtime import ActionService, PolicyDecision
    from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter

    class FutureAction(StrEnum):
        SET_TIME = "set_time"

    policy_calls: list[object] = []

    def allow_everything(request):
        policy_calls.append(request)
        return PolicyDecision(True, allow_privileged=True)

    service = ActionService(McpqWorldAdapter(mock_mc), policy=allow_everything)
    result = await service.execute(FutureAction.SET_TIME, {})  # type: ignore[arg-type]

    assert not result.success
    assert "no security classification" in result.message
    assert policy_calls == []
    assert mock_mc.commands_ran == []
