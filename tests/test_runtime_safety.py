"""Deterministic tests for independently scheduled runtime safety."""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.bridge.self_preservation import (
    PreservationConfig,
    SelfPreservationLayer,
)
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.llm.models import LLMResponse
from minecraft_ai_bridge.minecraft.actions import ActionResult, ActionType
from minecraft_ai_bridge.minecraft.observer import InventorySlot, WorldState
from minecraft_ai_bridge.runtime import ActionService, SafetySupervisor
from minecraft_ai_bridge.runtime.action_service import execute_action as original_execute_action
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


@pytest.mark.asyncio
async def test_supervisor_keeps_polling_with_one_coalesced_pending_intervention() -> None:
    evaluations = 0
    three_evaluations = asyncio.Event()
    interventions: list[ActionResult] = []

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult:
        nonlocal evaluations
        evaluations += 1
        intervention = ActionResult(
            success=True,
            action=ActionType.HEAL,
            data={"evaluation": evaluations},
        )
        interventions.append(intervention)
        if evaluations >= 3:
            three_evaluations.set()
        return intervention

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(three_evaluations.wait(), timeout=0.5)

    assert evaluations >= 3
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)
    # One slot, holding the newest outcome rather than the first stale one.
    assert supervisor.take_nowait() is interventions[-1]
    assert supervisor.take_nowait() is None


@pytest.mark.asyncio
async def test_supervisor_pending_slot_keeps_unconsumed_failure_over_later_success() -> None:
    outcomes = iter([True, False, True])
    produced: list[ActionResult] = []
    done = asyncio.Event()

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        success = next(outcomes, None)
        if success is None:
            done.set()
            return None
        result = ActionResult(success=success, action=ActionType.ATTACK)
        produced.append(result)
        return result

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(done.wait(), timeout=0.5)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)

    assert supervisor.take_nowait() is produced[1]


@pytest.mark.asyncio
async def test_action_service_serializes_backend_dispatches(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_dispatches = 0
    maximum_dispatches = 0

    async def track_dispatch(world, action, params):
        nonlocal active_dispatches, maximum_dispatches
        del world, params
        active_dispatches += 1
        maximum_dispatches = max(maximum_dispatches, active_dispatches)
        await asyncio.sleep(0)
        active_dispatches -= 1
        return ActionResult(success=True, action=action)

    monkeypatch.setattr(
        "minecraft_ai_bridge.runtime.action_service.execute_action",
        track_dispatch,
    )
    service = ActionService(McpqWorldAdapter(mock_mc))

    await asyncio.gather(
        service.execute(ActionType.CHAT, {"message": "second"}),
        service.execute(ActionType.CHAT, {"message": "third"}),
    )

    assert maximum_dispatches == 1


@pytest.mark.asyncio
async def test_action_service_wait_does_not_block_safety_dispatch(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_started = asyncio.Event()
    safety_dispatched = asyncio.Event()

    async def track_dispatch(world, action, params):
        del world, params
        if action is ActionType.WAIT:
            wait_started.set()
            await asyncio.sleep(30)
        else:
            safety_dispatched.set()
        return ActionResult(success=True, action=action)

    monkeypatch.setattr(
        "minecraft_ai_bridge.runtime.action_service.execute_action",
        track_dispatch,
    )
    safety_authority = object()
    service = ActionService(McpqWorldAdapter(mock_mc), safety_authority=safety_authority)
    wait_task = asyncio.create_task(service.execute(ActionType.WAIT, {"seconds": 30}))
    await asyncio.wait_for(wait_started.wait(), timeout=0.2)
    await asyncio.wait_for(
        service.execute(
            ActionType.HEAL,
            {"heal_item": "golden_apple"},
            safety_authority=safety_authority,
        ),
        timeout=0.2,
    )

    assert safety_dispatched.is_set()
    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task


@pytest.mark.asyncio
async def test_safety_dispatch_invalidates_queued_strategic_action(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heal_started = asyncio.Event()
    release_heal = asyncio.Event()
    strategic_dispatched = False

    async def track_dispatch(world, action, params):
        nonlocal strategic_dispatched
        del world, params
        if action is ActionType.HEAL:
            heal_started.set()
            await release_heal.wait()
        else:
            strategic_dispatched = True
        return ActionResult(success=True, action=action)

    monkeypatch.setattr(
        "minecraft_ai_bridge.runtime.action_service.execute_action",
        track_dispatch,
    )
    safety_authority = object()
    service = ActionService(McpqWorldAdapter(mock_mc), safety_authority=safety_authority)
    service.enable_safety_preemption()
    heal_task = asyncio.create_task(
        service.execute(
            ActionType.HEAL,
            {"heal_item": "golden_apple"},
            safety_authority=safety_authority,
            preemptive=True,
        )
    )
    await asyncio.wait_for(heal_started.wait(), timeout=0.2)
    strategic_task = asyncio.create_task(
        service.execute(ActionType.CHAT, {"message": "strategic"}, preemptible=True)
    )
    await asyncio.sleep(0)
    release_heal.set()

    heal_result, strategic_result = await asyncio.gather(heal_task, strategic_task)

    assert heal_result.success
    assert not strategic_result.success
    assert "preempt" in strategic_result.message.lower()
    assert not strategic_dispatched
    assert service.audit_records[-1].allowed is False

    service.acknowledge_safety_intervention()
    retry_result = await service.execute(
        ActionType.CHAT,
        {"message": "after reflex"},
        preemptible=True,
    )
    assert retry_result.success
    assert strategic_dispatched


class BlockingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def decide(self, system_prompt, messages, tool_choice="auto") -> LLMResponse:
        del system_prompt, messages, tool_choice
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return LLMResponse(action="chat", action_params={"message": "stale"})

    async def decompose_goal(self, goal):
        del goal
        return []


class BlockingDecompositionLLM(BlockingLLM):
    def __init__(self) -> None:
        super().__init__()
        self.decomposition_started = asyncio.Event()
        self.release_decomposition = asyncio.Event()

    async def decompose_goal(self, goal):
        del goal
        self.decomposition_started.set()
        await self.release_decomposition.wait()
        return []


class StrategicMutationDuringHealLLM(BlockingLLM):
    def __init__(self, heal_started: asyncio.Event) -> None:
        super().__init__()
        self._heal_started = heal_started
        self.decision_returned = asyncio.Event()

    async def decide(self, system_prompt, messages, tool_choice="auto") -> LLMResponse:
        del system_prompt, messages, tool_choice
        await self._heal_started.wait()
        self.decision_returned.set()
        return LLMResponse(action="chat", action_params={"message": "stale strategic action"})


@pytest.mark.asyncio
async def test_orchestrator_cancels_strategic_wait_for_safety_intervention() -> None:
    cancelled = asyncio.Event()
    intervention = ActionResult(success=True, action=ActionType.HEAL)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult:
        await asyncio.sleep(0.01)
        return intervention

    orch = Orchestrator.__new__(Orchestrator)
    orch._safety_supervisor = SafetySupervisor(observe, evaluate, interval=0.01)

    async def long_wait(_response: LLMResponse) -> ActionResult:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ActionResult(success=True, action=ActionType.WAIT)

    orch._act = long_wait
    stop_event = asyncio.Event()
    supervisor_task = asyncio.create_task(orch._safety_supervisor.run(stop_event))
    completed, reflex = await asyncio.wait_for(
        orch._wait_or_safety(LLMResponse(action="wait")), timeout=0.2
    )
    stop_event.set()
    await asyncio.wait_for(supervisor_task, timeout=0.2)

    assert completed is None
    assert reflex is intervention
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_run_safety_reflex_preempts_a_blocked_llm_decision(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.bridge.max_iterations = 1
    config.bridge.cycle_delay = 0
    orch = Orchestrator(config)
    llm = BlockingLLM()
    orch._llm = llm
    orch._goals._llm = llm

    async def no_connect() -> None:
        return None

    async def no_disconnect() -> None:
        return None

    async def no_goal(_goal: str) -> None:
        return None

    monkeypatch.setattr(orch, "_connect", no_connect)
    monkeypatch.setattr(orch, "_disconnect", no_disconnect)
    monkeypatch.setattr(orch._goals, "set_goal", no_goal)

    world = McpqWorldAdapter(mock_mc)
    safety_authority = object()
    service = ActionService(world, safety_authority=safety_authority)
    orch._mc = mock_mc
    orch._world = world
    orch._action_service = service
    orch._preservation = SelfPreservationLayer(
        world,
        orch._goals,
        orch._memory,
        PreservationConfig(),
        action_service=service,
        safety_authority=safety_authority,
    )

    observations = [
        WorldState(health=20.0, hunger=20),
        WorldState(
            health=2.0,
            hunger=20,
            inventory=[InventorySlot(item_id="golden_apple", count=1, slot=0)],
        ),
    ]

    async def observe() -> WorldState:
        state = (
            observations.pop(0)
            if observations
            else WorldState(
                health=2.0,
                hunger=20,
                inventory=[InventorySlot(item_id="golden_apple", count=1, slot=0)],
            )
        )
        orch._memory.record_observation(state)
        return state

    monkeypatch.setattr(orch, "_observe", observe)
    monkeypatch.setattr(orch, "_observe_for_safety", observe)

    run_task = asyncio.create_task(orch.run("stay alive"))
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    await asyncio.wait_for(run_task, timeout=2)

    assert llm.cancelled.is_set()
    assert any("effect give AIBot regeneration" in cmd for cmd in mock_mc.commands_ran)
    assert any(record.action is ActionType.HEAL for record in service.audit_records)
    assert not any("say stale" in cmd for cmd in mock_mc.commands_ran)


@pytest.mark.asyncio
async def test_reflex_invalidates_llm_mutation_queued_behind_heal(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.bridge.max_iterations = 1
    config.bridge.cycle_delay = 0
    orch = Orchestrator(config)
    heal_started = asyncio.Event()
    release_heal = asyncio.Event()
    strategic_dispatched = False

    async def controlled_dispatch(world, action, params):
        nonlocal strategic_dispatched
        if action is ActionType.HEAL:
            heal_started.set()
            await release_heal.wait()
        elif action is ActionType.CHAT:
            strategic_dispatched = True
        return await original_execute_action(world, action, params)

    monkeypatch.setattr(
        "minecraft_ai_bridge.runtime.action_service.execute_action",
        controlled_dispatch,
    )
    llm = StrategicMutationDuringHealLLM(heal_started)
    orch._llm = llm
    orch._goals._llm = llm

    async def no_connect() -> None:
        return None

    async def no_disconnect() -> None:
        return None

    async def no_goal(_goal: str) -> None:
        return None

    monkeypatch.setattr(orch, "_connect", no_connect)
    monkeypatch.setattr(orch, "_disconnect", no_disconnect)
    monkeypatch.setattr(orch._goals, "set_goal", no_goal)

    world = McpqWorldAdapter(mock_mc)
    safety_authority = object()
    service = ActionService(world, safety_authority=safety_authority)
    orch._mc = mock_mc
    orch._world = world
    orch._action_service = service
    orch._preservation = SelfPreservationLayer(
        world,
        orch._goals,
        orch._memory,
        PreservationConfig(),
        action_service=service,
        safety_authority=safety_authority,
    )
    decision_handled = asyncio.Event()
    original_decide_with_safety = orch._decide_with_safety

    async def tracked_decide_with_safety(messages):
        result = await original_decide_with_safety(messages)
        decision_handled.set()
        return result

    monkeypatch.setattr(orch, "_decide_with_safety", tracked_decide_with_safety)
    critical_state = WorldState(
        health=2.0,
        hunger=20,
        inventory=[InventorySlot(item_id="golden_apple", count=1, slot=0)],
    )

    async def observe() -> WorldState:
        orch._memory.record_observation(critical_state)
        return critical_state

    monkeypatch.setattr(orch, "_observe", observe)
    monkeypatch.setattr(orch, "_observe_for_safety", observe)
    run_task = asyncio.create_task(orch.run("stay alive"))
    await asyncio.wait_for(heal_started.wait(), timeout=0.5)
    await asyncio.wait_for(llm.decision_returned.wait(), timeout=0.5)
    await asyncio.wait_for(decision_handled.wait(), timeout=0.5)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release_heal.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert not strategic_dispatched
    assert any(record.action is ActionType.CHAT for record in service.audit_records)
    assert any(
        record.action is ActionType.CHAT and not record.allowed for record in service.audit_records
    )


@pytest.mark.asyncio
async def test_safety_supervisor_runs_while_goal_decomposition_is_blocked(
    mock_mc: MockMcpqClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.bridge.max_iterations = 1
    config.bridge.cycle_delay = 0
    orch = Orchestrator(config)
    llm = BlockingDecompositionLLM()
    orch._llm = llm
    orch._goals._llm = llm

    async def no_connect() -> None:
        return None

    async def no_disconnect() -> None:
        return None

    monkeypatch.setattr(orch, "_connect", no_connect)
    monkeypatch.setattr(orch, "_disconnect", no_disconnect)

    world = McpqWorldAdapter(mock_mc)
    safety_authority = object()
    service = ActionService(world, safety_authority=safety_authority)
    orch._mc = mock_mc
    orch._world = world
    orch._action_service = service
    orch._preservation = SelfPreservationLayer(
        world,
        orch._goals,
        orch._memory,
        PreservationConfig(),
        action_service=service,
        safety_authority=safety_authority,
    )
    critical_state = WorldState(
        health=2.0,
        hunger=20,
        inventory=[InventorySlot(item_id="golden_apple", count=1, slot=0)],
    )

    async def observe() -> WorldState:
        orch._memory.record_observation(critical_state)
        return critical_state

    monkeypatch.setattr(orch, "_observe", observe)
    monkeypatch.setattr(orch, "_observe_for_safety", observe)

    run_task = asyncio.create_task(orch.run("stay alive"))
    await asyncio.wait_for(llm.decomposition_started.wait(), timeout=0.5)
    for _ in range(100):
        if any(record.action is ActionType.HEAL for record in service.audit_records):
            break
        await asyncio.sleep(0.01)
    assert any(record.action is ActionType.HEAL for record in service.audit_records)
    llm.release_decomposition.set()
    await asyncio.wait_for(run_task, timeout=1)


class _NeverDecidingLLM(BlockingLLM):
    async def decide(self, system_prompt, messages, tool_choice="auto") -> LLMResponse:
        del system_prompt, messages, tool_choice
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ReadySupervisor:
    def __init__(self, result: ActionResult) -> None:
        self._result = result

    def take_nowait(self) -> ActionResult | None:
        return None

    async def wait_for_intervention(self) -> ActionResult:
        return self._result


@pytest.mark.asyncio
async def test_intervention_wins_over_simultaneous_authority_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.enabled = True
    orch = Orchestrator(config)
    orch._llm = _NeverDecidingLLM()
    intervention = ActionResult(success=False, action=ActionType.ATTACK)
    orch._safety_supervisor = _ReadySupervisor(intervention)  # type: ignore[assignment]
    orch._strategic_authority_lost = True
    orch._strategic_authority_lost_event.set()

    response, decided = await asyncio.wait_for(orch._decide_with_safety([]), timeout=1)
    assert response.action == "wait"
    assert decided is intervention

    completed, waited = await asyncio.wait_for(
        orch._wait_or_safety(LLMResponse(action="wait", action_params={"seconds": 60})),
        timeout=1,
    )
    assert completed is None
    assert waited is intervention


@pytest.mark.asyncio
async def test_deferred_strategy_records_consumed_reflex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orch = Orchestrator(AppConfig())
    orch._strategic_authority_lost = True
    reflex = ActionResult(success=False, action=ActionType.EAT, message="no food")

    await orch._defer_strategy(reflex)

    assert orch._last_result is reflex
    assert any("eat" in message.content for message in orch._memory.recent_messages())


@pytest.mark.asyncio
async def test_safety_polling_does_not_write_observations_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orch = Orchestrator(AppConfig())
    ticks = 0

    class ChangingObserver:
        async def observe(self) -> WorldState:
            nonlocal ticks
            ticks += 1
            # A live server's state changes between polls, defeating dedup.
            return WorldState(health=20.0 - ticks % 5)

    orch._observer = ChangingObserver()  # type: ignore[assignment]
    orch._memory.record_action("scan", {"success": True, "message": "", "data": {}})

    for _ in range(30):
        await orch._observe_for_safety()

    assert ticks == 30
    assert [entry.raw for entry in orch._memory._short_term][-1].startswith("Action: scan")
    assert len(orch._memory._short_term) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.MOVE_FORWARD, {"steps": 5}),
        (ActionType.MOVE_BACK, {"steps": 5}),
        (ActionType.SPRINT, {"steps": 5}),
        (ActionType.WALK_TO, {"x": 40, "z": 0}),
    ],
)
async def test_multi_step_actions_stop_when_a_reflex_is_waiting(
    mock_mc: MockMcpqClient, action, params
) -> None:
    from minecraft_ai_bridge.minecraft.actions import execute_action, safety_interrupt_scope

    mock_mc.set_position(0, 64, 0)
    with safety_interrupt_scope(lambda: True):
        result = await execute_action(mock_mc, action, params)

    assert not result.success
    assert result.data.get("interrupted") is True
    assert not any("^ ^ ^" in command for command in mock_mc.commands_ran)


@pytest.mark.asyncio
async def test_reflex_is_not_delayed_by_a_long_strategic_walk(
    mock_mc: MockMcpqClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reflex queued behind a running move_forward runs after one step, not 20."""
    mock_mc.set_position(0, 64, 0)
    original_pos = mock_mc.get_player_pos
    steps_seen = 0

    async def slow_pos():
        nonlocal steps_seen
        steps_seen += 1
        await asyncio.sleep(0.01)
        return await original_pos()

    monkeypatch.setattr(mock_mc, "get_player_pos", slow_pos)
    service = ActionService(McpqWorldAdapter(mock_mc))
    service.enable_safety_preemption()

    walk = asyncio.create_task(
        service.execute(ActionType.MOVE_FORWARD, {"steps": 20}, preemptible=True)
    )
    while steps_seen < 3:
        await asyncio.sleep(0.005)
    reflex = await asyncio.wait_for(
        service.execute(ActionType.CHAT, {"message": "reflex"}, preemptive=True), timeout=1
    )
    walked = await walk

    assert reflex.success
    assert walked.data.get("interrupted") is True
    assert walked.data["steps_taken"] < 10
    assert mock_mc.chat_messages_sent == ["reflex"]
