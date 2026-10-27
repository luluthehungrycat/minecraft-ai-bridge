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


@pytest.mark.parametrize(
    "update",
    [
        {"action": "set_time"},
        {"params": [("message", "hi")]},
        {"params": {"message": object()}},
        {"controller_session_id": ["not", "hashable"]},
        {"request_id": None},
        {"request_id": 5},
        {"steve_id": ""},
    ],
)
async def test_unvalidated_request_instances_are_revalidated(setup, update):
    """model_construct/model_copy skip validation; the service must not trust them."""
    service = setup.service()
    session = await service.open_session(setup.controller)
    fields = {**dict(_request(session, ActionType.CHAT, {"message": "hi"})), **update}

    with pytest.raises(ValueError, match="invalid action request"):
        await service.submit_action(setup.controller, ActionRequestSchema.model_construct(**fields))
    assert setup.world.chat_messages_sent == []
    assert setup.world.commands_ran == []


async def test_revalidation_still_refuses_internal_actions(setup):
    service = setup.service(world_admin=True)
    session = await service.open_session(setup.controller)
    request = _request(session, ActionType.WAIT).model_copy(update={"action": "goto_teleport"})

    result = await service.submit_action(setup.controller, request)
    assert result.reason_code == "ACTION_NOT_EXPOSED"
    assert setup.world.commands_ran == []


async def test_unauthenticated_malformed_request_is_an_authentication_error(setup):
    service = setup.service()
    with pytest.raises(PermissionError):
        await service.submit_action(
            "mab-not-a-token", ActionRequestSchema.model_construct(action="set_time")
        )


async def test_revocation_while_queued_is_enforced_at_dispatch(setup, monkeypatch):
    """Policy runs after the dispatch lock, so a queued request sees revocation."""
    import asyncio

    from minecraft_ai_bridge.runtime import action_service

    service = setup.service()
    session = await service.open_session(setup.controller)
    first_running = asyncio.Event()
    release_first = asyncio.Event()
    original = action_service.execute_action

    async def slow_first(world, action, params):
        if not first_running.is_set():
            first_running.set()
            await release_first.wait()
        return await original(world, action, params)

    monkeypatch.setattr(action_service, "execute_action", slow_first)
    first = asyncio.create_task(
        service.submit_action(
            setup.controller, _request(session, ActionType.CHAT, {"message": "a"}, "req-a")
        )
    )
    await first_running.wait()
    second = asyncio.create_task(
        service.submit_action(
            setup.controller, _request(session, ActionType.CHAT, {"message": "b"}, "req-b")
        )
    )
    await asyncio.sleep(0.05)  # second is now waiting for the dispatch lock
    setup.auth.revoke_credential(setup.controller)
    release_first.set()

    assert (await first).status is ActionStatus.SUCCEEDED
    queued = await second
    assert queued.status is ActionStatus.REJECTED
    assert queued.reason_code == "POLICY_REJECTED"
    assert setup.world.chat_messages_sent == ["a"]


async def test_concurrent_sessions_do_not_share_authority(setup):
    """The ContextVar binding is per request even when requests interleave."""
    import asyncio

    service = setup.service()
    stale = await service.open_session(setup.controller)
    setup.auth.deactivate_steve(setup.admin, "AIBot")
    setup.auth.reactivate_steve(setup.admin, "AIBot")
    live = await service.open_session(setup.second_controller)

    requests = []
    for i in range(10):
        requests.append(
            service.submit_action(
                setup.controller,
                _request(stale, ActionType.CHAT, {"message": f"stale-{i}"}, f"s-{i}"),
            )
        )
        requests.append(
            service.submit_action(
                setup.second_controller,
                _request(live, ActionType.CHAT, {"message": f"live-{i}"}, f"l-{i}"),
            )
        )
    results = await asyncio.gather(*requests)

    assert [r.status for r in results[0::2]] == [ActionStatus.REJECTED] * 10
    assert [r.status for r in results[1::2]] == [ActionStatus.SUCCEEDED] * 10
    assert sorted(setup.world.chat_messages_sent) == sorted(f"live-{i}" for i in range(10))


async def test_unverifiable_session_fails_closed(setup, monkeypatch):
    """A storage error while validating the session rejects, never allows."""
    import sqlite3

    service = setup.service()
    session = await service.open_session(setup.controller)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(setup.auth, "validate_session", locked)

    result = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "x"})
    )
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "POLICY_REJECTED"
    assert setup.world.chat_messages_sent == []


async def test_request_id_cannot_replay_across_sessions(setup):
    service = setup.service()
    first = await service.open_session(setup.controller)
    done = await service.submit_action(
        setup.controller, _request(first, ActionType.CHAT, {"message": "once"})
    )
    await service.close_session(setup.controller, first.session_id)
    second = await service.open_session(setup.second_controller)

    replay = await service.submit_action(
        setup.second_controller, _request(second, ActionType.CHAT, {"message": "once"})
    )
    assert done.status is ActionStatus.SUCCEEDED
    assert replay.status is ActionStatus.REJECTED
    assert setup.world.chat_messages_sent == ["once"]


async def test_concurrent_requests_from_one_valid_session_all_run(setup):
    """Authz lock contention between requests must not read as an invalid session."""
    import asyncio

    service = setup.service()
    session = await service.open_session(setup.controller)
    results = await asyncio.gather(
        *(
            service.submit_action(
                setup.controller,
                _request(session, ActionType.CHAT, {"message": str(i)}, f"req-{i}"),
            )
            for i in range(20)
        )
    )

    assert [r.status for r in results] == [ActionStatus.SUCCEEDED] * 20
    assert len(setup.world.chat_messages_sent) == 20


async def test_request_fields_cannot_supply_world_admin_authority(setup):
    service = setup.service()  # no operator delegation
    session = await service.open_session(setup.controller)
    params = {
        "x": 5,
        "y": 64,
        "z": 5,
        "admin_token": setup.world_admin,
        "world_admin_token": setup.world_admin,
        "allow_privileged": True,
    }

    result = await service.submit_action(
        setup.controller, _request(session, ActionType.TELEPORT, params)
    )
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "POLICY_REJECTED"
    assert setup.world.commands_ran == []


async def test_action_id_names_the_logical_action_across_replays(setup):
    """RFC-0001: action IDs are durable, so a replay keeps the original ID."""
    service = setup.service()
    session = await service.open_session(setup.controller)
    request = _request(session, ActionType.CHAT, {"message": "hi"})

    first = await service.submit_action(setup.controller, request)
    replay = await service.submit_action(setup.controller, request)
    other = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "hi"}, "req-2")
    )

    assert first.action_id == replay.action_id
    assert other.action_id != first.action_id
    assert len(setup.world.chat_messages_sent) == 1 + 1
