"""ControllerService forgets sessions that can no longer be used (#26)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionStatus,
    AuthzService,
    ControllerService,
)
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")

    def credential(*scopes: str) -> str:
        return auth.create_credential("AIBot", "owner", set(scopes), admin_token=admin.token).token

    world = MockMcpqClient()
    world.set_position(0, 64, 0)
    yield auth, world, admin.token, credential("steve:control"), credential("steve:control")
    auth.close()


def _fence(auth, admin: str) -> None:
    """Fence the active session the way an admin clears a stale controller."""
    auth.deactivate_steve(admin, "AIBot")
    auth.reactivate_steve(admin, "AIBot")


def _service(auth, world, lease: float = 300) -> ControllerService:
    return ControllerService(McpqWorldAdapter(world), auth, "AIBot", session_lease_seconds=lease)


def _chat(session, request_id: str = "req-1", *, generation: int | None = None):
    return ActionRequestSchema(
        steve_id=session.steve_id,
        controller_session_id=session.session_id,
        generation=session.generation if generation is None else generation,
        request_id=request_id,
        action=ActionType.CHAT,
        params={"message": "hi"},
    )


async def test_sessions_stay_bounded_across_open_expire_cycles(setup):
    auth, world, _, token, _ = setup
    service = _service(auth, world, lease=0.02)

    for _ in range(25):
        await service.open_session(token)
        assert len(service._sessions) == 1
        await asyncio.sleep(0.03)

    # The last session expired without a new open; a request removes it.
    last = next(iter(service._sessions.values()))
    result = await service.submit_action(token, _chat(service._contract(last)))
    assert result.reason_code == "POLICY_REJECTED"
    assert service._sessions == {}


async def test_a_session_fenced_by_an_admin_is_forgotten_on_its_next_request(setup):
    auth, world, admin, token, _ = setup
    service = _service(auth, world)
    session = await service.open_session(token)
    _fence(auth, admin)

    result = await service.submit_action(token, _chat(session))

    assert result.reason_code == "POLICY_REJECTED"
    assert service._sessions == {}
    assert world.chat_messages_sent == []


async def test_a_failed_renewal_forgets_the_session(setup):
    auth, world, admin, token, _ = setup
    service = _service(auth, world)
    session = await service.open_session(token)
    _fence(auth, admin)

    with pytest.raises(PermissionError):
        await service.renew_session(token, session.session_id)
    assert service._sessions == {}
    # Later calls see an unknown session, as after close.
    with pytest.raises(PermissionError, match="not open"):
        await service.renew_session(token, session.session_id)


async def test_valid_sessions_are_kept_when_a_request_is_refused(setup):
    auth, world, _, token, other = setup
    service = _service(auth, world)
    session = await service.open_session(token)

    # Refused by policy (no world delegation), but the session is still valid.
    teleport = ActionRequestSchema(
        steve_id="AIBot",
        controller_session_id=session.session_id,
        generation=session.generation,
        request_id="tp",
        action=ActionType.TELEPORT,
        params={"x": 1, "y": 64, "z": 1},
    )
    refused = await service.submit_action(token, teleport)
    # Another credential presenting the session ID cannot evict it.
    foreign = await service.submit_action(other, _chat(session, "foreign"))
    stale = await service.submit_action(token, _chat(session, "stale", generation=999))
    with pytest.raises(PermissionError):
        await service.renew_session(other, session.session_id)

    assert refused.reason_code == "POLICY_REJECTED"
    assert foreign.reason_code == "SESSION_NOT_OWNED"
    assert stale.reason_code == "STALE_GENERATION"
    assert session.session_id in service._sessions
    ok = await service.submit_action(token, _chat(session, "after"))
    assert ok.status is ActionStatus.SUCCEEDED


async def test_an_entry_replaced_by_renewal_is_not_forgotten(setup):
    auth, world, _, token, _ = setup
    service = _service(auth, world)
    opened = await service.open_session(token)
    stale = service._sessions[opened.session_id]
    await asyncio.sleep(0.01)
    await service.renew_session(token, opened.session_id)

    # A request that raced the renewal validated the old lease and failed.
    assert not auth.validate_session(stale)
    await service._forget_if_invalid(stale)

    assert service._sessions[opened.session_id] is not stale
    assert auth.validate_session(service._sessions[opened.session_id])


async def test_an_unverifiable_session_is_kept(setup, monkeypatch):
    auth, world, admin, token, _ = setup
    service = _service(auth, world)
    session = await service.open_session(token)
    _fence(auth, admin)

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth, "validate_session", unavailable)
    await service._forget_if_invalid(service._sessions[session.session_id])

    assert session.session_id in service._sessions


async def test_expired_sessions_are_swept_by_any_request(setup):
    auth, world, _, token, other = setup
    service = _service(auth, world, lease=0.02)
    session = await service.open_session(token)
    await asyncio.sleep(0.03)

    # A request that never reaches the policy still sweeps expired leases.
    result = await service.submit_action(other, _chat(session, "foreign"))

    assert result.reason_code == "SESSION_NOT_OWNED"
    assert service._sessions == {}


async def test_a_renewal_during_validation_is_not_forgotten(setup, monkeypatch):
    auth, world, _, token, _ = setup
    service = _service(auth, world)
    opened = await service.open_session(token)
    stale = service._sessions[opened.session_id]
    validate = auth.validate_session
    fresh = auth.renew_session(stale, lease_seconds=600)

    def validate_while_renewed(session):
        # The renewal is stored while the stale entry is being checked.
        service._sessions[session.session_id] = fresh
        return validate(session)

    monkeypatch.setattr(auth, "validate_session", validate_while_renewed)
    await service._forget_if_invalid(stale)

    assert service._sessions[opened.session_id] is fresh
    assert validate(fresh)


@pytest.mark.parametrize("invalidate", ["fence", "expiry"])
async def test_a_late_open_does_not_evict_a_newer_session(setup, monkeypatch, invalidate):
    # Round-1 review of #35: an open whose response arrives after a newer
    # open completed must not replace the newer, still-valid session.
    auth, world, admin, older, newer = setup
    service = _service(auth, world, lease=0.2 if invalidate == "expiry" else 300)
    acquired = threading.Event()
    resume = threading.Event()
    acquire = auth.acquire_session

    def delayed_acquire(token, *args, **kwargs):
        session = acquire(token, *args, **kwargs)
        if token == older:
            acquired.set()
            assert resume.wait(10)
        return session

    monkeypatch.setattr(auth, "acquire_session", delayed_acquire)
    late = asyncio.create_task(service.open_session(older))
    try:
        assert await asyncio.to_thread(acquired.wait, 5)
        if invalidate == "fence":
            _fence(auth, admin)
        else:
            await asyncio.sleep(0.25)
        current = await service.open_session(newer)
        resume.set()
        await late

        result = await service.submit_action(newer, _chat(current, "current"))
        assert result.status is ActionStatus.SUCCEEDED, result
        assert list(service._sessions) == [current.session_id]
        await service.renew_session(newer, current.session_id)
    finally:
        resume.set()
        await asyncio.gather(late, return_exceptions=True)


async def test_a_late_renewal_does_not_restore_a_dropped_session(setup, monkeypatch):
    auth, world, admin, older, newer = setup
    service = _service(auth, world)
    first = await service.open_session(older)
    renewed = threading.Event()
    resume = threading.Event()
    renew = auth.renew_session

    def delayed_renew(session, **kwargs):
        result = renew(session, **kwargs)
        renewed.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(auth, "renew_session", delayed_renew)
    late = asyncio.create_task(service.renew_session(older, first.session_id))
    try:
        assert await asyncio.to_thread(renewed.wait, 5)
        _fence(auth, admin)
        current = await service.open_session(newer)
        resume.set()
        await late

        assert list(service._sessions) == [current.session_id]
    finally:
        resume.set()
        await asyncio.gather(late, return_exceptions=True)


async def test_pruning_during_a_renewal_does_not_lose_the_renewed_session(setup, monkeypatch):
    # Round-2 review of #35: a request with the old lease prunes the entry
    # after the renewal committed but before it returned.
    auth, world, _, token, _ = setup
    service = _service(auth, world)
    opened = await service.open_session(token)
    committed = threading.Event()
    resume = threading.Event()
    renew = auth.renew_session

    def delayed_renew(session, **kwargs):
        fresh = renew(session, **kwargs)
        committed.set()
        assert resume.wait(10)
        return fresh

    monkeypatch.setattr(auth, "renew_session", delayed_renew)
    pending = asyncio.create_task(service.renew_session(token, opened.session_id))
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        racing = await service.submit_action(token, _chat(opened, "during"))
        assert racing.reason_code == "POLICY_REJECTED"  # it used the old lease
        assert service._sessions == {}
        resume.set()
        await pending

        after = await service.submit_action(token, _chat(opened, "after"))
        assert after.status is ActionStatus.SUCCEEDED, after
        assert list(service._sessions) == [opened.session_id]
    finally:
        resume.set()
        await asyncio.gather(pending, return_exceptions=True)
