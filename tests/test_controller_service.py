"""RFC-0001 Phase B gate for the transport-independent controller API."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionStatus,
    AuthzService,
    BackendKind,
    ControllerService,
)
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


@dataclass
class Setup:
    auth: AuthzService
    world: MockMcpqClient
    admin: str
    controller: str
    second_controller: str
    observer: str
    world_admin: str

    def service(self, *, world_admin: bool = False) -> ControllerService:
        return ControllerService(
            McpqWorldAdapter(self.world),
            self.auth,
            "AIBot",
            world_admin_token=self.world_admin if world_admin else None,
        )


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")

    def credential(scopes: set[str]) -> str:
        return auth.create_credential("AIBot", "owner", scopes, admin_token=admin.token).token

    world = MockMcpqClient()
    world.set_position(0, 64, 0)
    yield Setup(
        auth=auth,
        world=world,
        admin=admin.token,
        controller=credential({"steve:control", "steve:observe:self"}),
        second_controller=credential({"steve:control"}),
        observer=credential({"steve:observe:self"}),
        world_admin=auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token,
    )
    auth.close()


def _request(session, action: ActionType, params=None, request_id: str = "req-1"):
    return ActionRequestSchema(
        steve_id=session.steve_id,
        controller_session_id=session.session_id,
        generation=session.generation,
        request_id=request_id,
        action=action,
        params=params or {},
    )


async def test_only_one_controller_session_per_steve(setup):
    service = setup.service()
    first = await service.open_session(setup.controller)

    with pytest.raises(PermissionError, match="already active"):
        await service.open_session(setup.second_controller)

    await service.close_session(setup.controller, first.session_id)
    second = await service.open_session(setup.second_controller)
    assert second.generation > first.generation


async def test_observer_reads_but_cannot_control(setup):
    service = setup.service()
    capabilities = await service.get_capabilities(setup.observer)
    assert capabilities.backend is BackendKind.MCPQ
    assert "health" in await service.get_state(setup.observer)

    with pytest.raises(PermissionError):
        await service.open_session(setup.observer)

    session = await service.open_session(setup.controller)
    result = await service.submit_action(
        setup.observer, _request(session, ActionType.CHAT, {"message": "hi"})
    )
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "SESSION_NOT_OWNED"
    assert setup.world.chat_messages_sent == []


async def test_unknown_token_is_an_authentication_error(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)

    with pytest.raises(PermissionError):
        await service.get_state("mab-not-a-token")
    with pytest.raises(PermissionError):
        await service.submit_action("mab-not-a-token", _request(session, ActionType.WAIT))


async def test_privileged_actions_need_operator_world_admin(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)

    moved = await service.submit_action(
        setup.controller, _request(session, ActionType.TELEPORT, {"x": 5, "y": 64, "z": 5})
    )
    assert moved.status is ActionStatus.REJECTED
    assert moved.reason_code == "POLICY_REJECTED"
    assert setup.world.commands_ran == []

    chatted = await service.submit_action(
        setup.controller,
        _request(session, ActionType.CHAT, {"message": "hello"}, request_id="req-2"),
    )
    assert chatted.status is ActionStatus.SUCCEEDED
    assert setup.world.chat_messages_sent


async def test_operator_world_admin_allows_privileged_actions(setup):
    service = setup.service(world_admin=True)
    session = await service.open_session(setup.controller)

    moved = await service.submit_action(
        setup.controller, _request(session, ActionType.TELEPORT, {"x": 5, "y": 64, "z": 5})
    )
    assert moved.status is ActionStatus.SUCCEEDED, moved.message


@pytest.mark.parametrize(
    "action", [ActionType.GOTO_TELEPORT, ActionType.PROVISION_BOT, ActionType.PREPARE_SAFE_SPAWN]
)
async def test_internal_actions_are_never_exposed(setup, action):
    service = setup.service(world_admin=True)
    session = await service.open_session(setup.controller)

    result = await service.submit_action(setup.controller, _request(session, action))
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "ACTION_NOT_EXPOSED"
    assert setup.world.commands_ran == []


async def test_fenced_session_cannot_mutate_after_takeover(setup):
    service = setup.service()
    stale = await service.open_session(setup.controller)
    # An admin clears the session, e.g. after the old process was stopped.
    setup.auth.deactivate_steve(setup.admin, "AIBot")
    setup.auth.reactivate_steve(setup.admin, "AIBot")
    await service.open_session(setup.second_controller)

    result = await service.submit_action(
        setup.controller, _request(stale, ActionType.CHAT, {"message": "stale"})
    )
    assert result.status is ActionStatus.REJECTED
    assert setup.world.chat_messages_sent == []


async def test_envelope_must_match_session_and_steve(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)

    stale = _request(session, ActionType.WAIT).model_copy(
        update={"generation": session.generation - 1}
    )
    wrong_steve = _request(session, ActionType.WAIT).model_copy(update={"steve_id": "OtherBot"})
    unknown = _request(session, ActionType.WAIT).model_copy(
        update={"controller_session_id": "not-a-session"}
    )

    results = [
        await service.submit_action(setup.controller, r) for r in (stale, wrong_steve, unknown)
    ]
    assert [r.reason_code for r in results] == [
        "STALE_GENERATION",
        "STEVE_MISMATCH",
        "SESSION_UNKNOWN",
    ]


async def test_replayed_request_id_does_not_repeat_the_mutation(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)
    request = _request(session, ActionType.CHAT, {"message": "once"})

    first = await service.submit_action(setup.controller, request)
    replay = await service.submit_action(setup.controller, request)

    assert first.status is replay.status is ActionStatus.SUCCEEDED
    assert len(setup.world.chat_messages_sent) == 1


async def test_closed_session_is_unusable(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)
    await service.close_session(setup.controller, session.session_id)

    result = await service.submit_action(setup.controller, _request(session, ActionType.WAIT))
    assert result.reason_code == "SESSION_UNKNOWN"
    with pytest.raises(PermissionError):
        await service.renew_session(setup.controller, session.session_id)


def test_exposed_actions_are_an_explicit_safe_allowlist():
    from minecraft_ai_bridge.runtime.action_service import ActionClass, classify_action
    from minecraft_ai_bridge.runtime.controller_service import _EXPOSED_ACTIONS

    exposable = {ActionClass.OBSERVATION, ActionClass.SURVIVAL, ActionClass.PRIVILEGED_SIMULATED}
    assert {classify_action(a) for a in _EXPOSED_ACTIONS} <= exposable
    assert ActionType.GOTO_TELEPORT not in _EXPOSED_ACTIONS


async def test_action_outside_allowlist_is_not_exposed(setup, monkeypatch):
    from minecraft_ai_bridge.runtime import controller_service

    monkeypatch.setattr(
        controller_service,
        "_EXPOSED_ACTIONS",
        controller_service._EXPOSED_ACTIONS - {ActionType.CHAT},
    )
    service = setup.service()
    session = await service.open_session(setup.controller)

    result = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "hi"})
    )
    assert result.reason_code == "ACTION_NOT_EXPOSED"
    assert setup.world.chat_messages_sent == []


async def test_revoked_credential_loses_access_immediately(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)
    setup.auth.revoke_credential(setup.controller)

    with pytest.raises(PermissionError):
        await service.submit_action(setup.controller, _request(session, ActionType.WAIT))
    with pytest.raises(PermissionError):
        await service.get_state(setup.controller)


async def test_expired_lease_is_rejected(setup):
    import asyncio

    service = ControllerService(
        McpqWorldAdapter(setup.world), setup.auth, "AIBot", session_lease_seconds=0.05
    )
    session = await service.open_session(setup.controller)
    await asyncio.sleep(0.1)

    result = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "late"})
    )
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "POLICY_REJECTED"
    assert setup.world.chat_messages_sent == []


async def test_world_admin_does_not_bypass_parameter_validation(setup):
    service = setup.service(world_admin=True)
    session = await service.open_session(setup.controller)

    result = await service.submit_action(
        setup.controller, _request(session, ActionType.PLACE_BLOCK, {"block_type": "stone{}"})
    )
    assert result.status is ActionStatus.FAILED
    assert setup.world.commands_ran == []
