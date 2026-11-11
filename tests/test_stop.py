"""RFC-0001 Phase D: a controller stop that never waits for the dispatch lock."""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.minecraft import actions
from minecraft_ai_bridge.minecraft.actions import (
    ActionResult,
    ActionType,
    _interrupted_result,
    _safety_interrupted,
)
from minecraft_ai_bridge.runtime import ActionStatus, AuthzService, ControllerService
from minecraft_ai_bridge.runtime.action_service import ActionService
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient
from tests.test_controller_service import _request


class Stall:
    """A multi-step handler that runs until interrupted or released."""

    def __init__(self, action: ActionType) -> None:
        self.action = action
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.steps = 0

    async def __call__(self, mc, params) -> ActionResult:
        self.started.set()
        while not self.release.is_set():
            if _safety_interrupted():
                return _interrupted_result(self.action, self.steps)
            self.steps += 1
            await asyncio.sleep(0.005)
        return ActionResult(success=True, action=self.action, message="finished")


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")

    def credential(scopes: set[str]) -> str:
        return auth.create_credential("AIBot", "owner", scopes, admin_token=admin.token).token

    controller = credential({"steve:control", "steve:observe:self"})
    observer = credential({"steve:observe:self"})
    service = ControllerService(McpqWorldAdapter(MockMcpqClient()), auth, "AIBot")  # type: ignore[arg-type]
    yield service, controller, observer
    auth.close()


async def test_stop_ends_a_running_action_without_waiting_for_it(setup, monkeypatch):
    service, token, _ = setup
    stall = Stall(ActionType.CHAT)
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, stall)
    session = await service.open_session(token)

    running = asyncio.create_task(
        service.submit_action(token, _request(session, ActionType.CHAT, {"message": "x"}, "a"))
    )
    await asyncio.wait_for(stall.started.wait(), 2)
    stopped = await asyncio.wait_for(
        service.submit_action(token, _request(session, ActionType.STOP, {}, "s")), 2
    )
    result = await asyncio.wait_for(running, 2)

    assert stopped.status is ActionStatus.SUCCEEDED
    assert "1 running" in stopped.message
    assert result.status is ActionStatus.INTERRUPTED
    assert result.reason_code == "STOP_REQUESTED"
    assert "stop request" in result.message
    assert not stall.release.is_set()


async def test_stop_with_nothing_running_is_a_recorded_no_op(setup):
    service, token, _ = setup
    session = await service.open_session(token)

    result = await service.submit_action(token, _request(session, ActionType.STOP))

    assert result.status is ActionStatus.SUCCEEDED
    assert "Nothing was running" in result.message
    record = service.action_service.audit_records[-1]
    assert record.action is ActionType.STOP and record.allowed and record.succeeded


async def test_stop_does_not_end_a_later_action(setup, monkeypatch):
    service, token, _ = setup
    stall = Stall(ActionType.CHAT)
    monkeypatch.setitem(actions._HANDLERS, ActionType.CHAT, stall)
    session = await service.open_session(token)
    await service.submit_action(token, _request(session, ActionType.STOP, {}, "s"))

    later = asyncio.create_task(
        service.submit_action(token, _request(session, ActionType.CHAT, {"message": "x"}, "b"))
    )
    await asyncio.wait_for(stall.started.wait(), 2)
    await asyncio.sleep(0.03)
    stall.release.set()
    result = await asyncio.wait_for(later, 2)

    assert result.status is ActionStatus.SUCCEEDED


async def test_repeated_stop_with_one_request_id_is_harmless(setup):
    service, token, _ = setup
    session = await service.open_session(token)
    request = _request(session, ActionType.STOP, {}, "same")

    first = await service.submit_action(token, request)
    second = await service.submit_action(token, request)

    assert first.status is second.status is ActionStatus.SUCCEEDED


async def test_stop_needs_a_valid_session(setup):
    service, token, observer = setup
    session = await service.open_session(token)

    stale = _request(session, ActionType.STOP).model_copy(
        update={"generation": session.generation + 1}
    )
    result = await service.submit_action(token, stale)
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "STALE_GENERATION"

    result = await service.submit_action(observer, _request(session, ActionType.STOP))
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "SESSION_NOT_OWNED"

    await service.close_session(token, session.session_id)
    result = await service.submit_action(token, _request(session, ActionType.STOP))
    assert result.status is ActionStatus.REJECTED


async def test_policy_denial_refuses_stop():
    from minecraft_ai_bridge.runtime.action_service import PolicyDecision

    service = ActionService(
        McpqWorldAdapter(MockMcpqClient()),  # type: ignore[arg-type]
        policy=lambda _request: PolicyDecision(False, "no authority"),
    )

    result = await service.interrupt()

    assert not result.success
    assert service.audit_records[-1].allowed is False


async def test_stop_never_cuts_a_safety_reflex_short(monkeypatch):
    stall = Stall(ActionType.EAT)
    monkeypatch.setitem(actions._HANDLERS, ActionType.EAT, stall)
    safety = object()
    service = ActionService(McpqWorldAdapter(MockMcpqClient()), safety_authority=safety)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_safety_params_allowed", lambda *_: True)

    reflex = asyncio.create_task(
        service.execute(ActionType.EAT, {"item": "bread"}, safety_authority=safety)
    )
    await asyncio.wait_for(stall.started.wait(), 2)
    stopped = await service.interrupt()
    await asyncio.sleep(0.03)
    stall.release.set()
    result = await asyncio.wait_for(reflex, 2)

    assert stopped.success
    assert result.success


async def test_stop_rewrites_a_real_multi_step_handlers_result(monkeypatch):
    """move_forward checks between steps; a stop mid-walk ends it there."""
    world = MockMcpqClient()
    service = ActionService(McpqWorldAdapter(world))  # type: ignore[arg-type]
    original = world.run_as_player
    calls = 0

    async def slow(command: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            await service.interrupt()
        return await original(command)

    monkeypatch.setattr(world, "run_as_player", slow)

    result = await service.execute(ActionType.MOVE_FORWARD, {"steps": 10})

    assert not result.success
    assert result.data["interrupted"] is True
    assert result.data["interrupt_reason"] == "stop"
    assert "stop request" in result.message
    assert result.data["steps_taken"] < 10
