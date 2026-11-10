"""A restarted ControllerService resumes still-leased sessions (Phase C gate).

Sessions are cached in memory, while Authz keeps them active until their
lease ends. Without resuming, a service restart locked the controller out:
its session was unknown to the new process, and opening a new one was
refused as "already active" until the lease expired.
"""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import ActionStatus, ControllerService
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.test_controller_service import _request
from tests.test_controller_service import setup as controller_setup


@pytest.fixture
def setup(tmp_path):
    yield from controller_setup.__wrapped__(tmp_path)


def _restarted(setup, **kwargs) -> ControllerService:
    """A fresh service over the same Authz database: nothing cached."""
    return ControllerService(McpqWorldAdapter(setup.world), setup.auth, "AIBot", **kwargs)


async def test_restarted_service_resumes_the_holders_session(setup):
    session = await setup.service().open_session(setup.controller)
    restarted = _restarted(setup)

    result = await restarted.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "back"})
    )

    assert result.status is ActionStatus.SUCCEEDED, result
    assert setup.world.chat_messages_sent == ["back"]
    # The resumed session is a normal cached session from now on.
    renewed = await restarted.renew_session(setup.controller, session.session_id)
    assert renewed.session_id == session.session_id
    await restarted.close_session(setup.controller, session.session_id)
    assert setup.auth.resume_session("AIBot", session.session_id, renewed.controller_id) is None


async def test_restarted_service_can_renew_and_close_without_a_prior_action(setup):
    session = await setup.service().open_session(setup.controller)
    restarted = _restarted(setup)
    await restarted.renew_session(setup.controller, session.session_id)
    await restarted.close_session(setup.controller, session.session_id)
    # Released: the next controller can open immediately.
    await restarted.open_session(setup.second_controller)


async def test_another_credential_cannot_resume_a_session(setup):
    session = await setup.service().open_session(setup.controller)
    restarted = _restarted(setup)

    result = await restarted.submit_action(
        setup.second_controller, _request(session, ActionType.CHAT, {"message": "hijack"})
    )

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "SESSION_UNKNOWN"
    assert setup.world.chat_messages_sent == []
    with pytest.raises(PermissionError):
        await restarted.close_session(setup.second_controller, session.session_id)
    # Still the holder's: it resumes for the right credential afterwards.
    ok = await restarted.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "mine"})
    )
    assert ok.status is ActionStatus.SUCCEEDED


async def test_released_session_is_not_resumed(setup):
    service = setup.service()
    session = await service.open_session(setup.controller)
    await service.close_session(setup.controller, session.session_id)

    result = await _restarted(setup).submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "gone"})
    )

    assert result.reason_code == "SESSION_UNKNOWN"
    assert setup.world.chat_messages_sent == []


async def test_fenced_session_is_not_resumed(setup):
    service = setup.service()
    old = await service.open_session(setup.controller)
    await service.close_session(setup.controller, old.session_id)
    newer = await service.open_session(setup.controller)
    assert newer.generation > old.generation

    result = await _restarted(setup).submit_action(
        setup.controller, _request(old, ActionType.CHAT, {"message": "fenced"})
    )

    assert result.reason_code == "SESSION_UNKNOWN"
    assert setup.world.chat_messages_sent == []


async def test_expired_session_is_not_resumed(setup):
    short = ControllerService(
        McpqWorldAdapter(setup.world), setup.auth, "AIBot", session_lease_seconds=0.05
    )
    session = await short.open_session(setup.controller)
    await asyncio.sleep(0.1)

    result = await _restarted(setup).submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "late"})
    )

    assert result.reason_code == "SESSION_UNKNOWN"
    assert setup.world.chat_messages_sent == []


async def test_resumed_session_still_enforces_the_generation(setup):
    session = await setup.service().open_session(setup.controller)
    stale = _request(session, ActionType.CHAT, {"message": "stale"}).model_copy(
        update={"generation": session.generation - 1}
    )

    result = await _restarted(setup).submit_action(setup.controller, stale)

    assert result.reason_code == "STALE_GENERATION"
    assert setup.world.chat_messages_sent == []


async def test_request_id_replays_within_the_resumed_session(setup, tmp_path):
    """The same request ID after a restart returns the original result, once."""
    from minecraft_ai_bridge.runtime import SqliteActionOutcomeStore

    store_path = tmp_path / "outcomes.sqlite3"
    store = SqliteActionOutcomeStore(store_path)
    first = ControllerService(
        McpqWorldAdapter(setup.world), setup.auth, "AIBot", outcome_store=store
    )
    session = await first.open_session(setup.controller)
    original = await first.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "once"}, "r-1")
    )
    await first.action_service.flush()
    store.close()

    store = SqliteActionOutcomeStore(store_path)
    try:
        restarted = _restarted(setup, outcome_store=store)
        replay = await restarted.submit_action(
            setup.controller, _request(session, ActionType.CHAT, {"message": "once"}, "r-1")
        )
    finally:
        store.close()

    assert replay.status is original.status is ActionStatus.SUCCEEDED
    assert replay.action_id == original.action_id
    assert setup.world.chat_messages_sent == ["once"]


async def test_a_late_resume_does_not_undo_a_renewal(setup, monkeypatch):
    """Two requests resume at once; the slower one must keep the renewed lease."""
    import threading

    session = await setup.service().open_session(setup.controller)
    restarted = _restarted(setup)
    real_resume = setup.auth.resume_session
    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_first_resume(*args):
        nonlocal calls
        calls += 1
        snapshot = real_resume(*args)
        if calls == 1:
            started.set()
            release.wait(5)
        return snapshot

    monkeypatch.setattr(setup.auth, "resume_session", slow_first_resume)
    slow = asyncio.create_task(
        restarted.submit_action(
            setup.controller, _request(session, ActionType.CHAT, {"message": "slow"}, "r-slow")
        )
    )
    await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(0.01)  # the renewed lease must differ from the snapshot's
    renewed = await restarted.renew_session(setup.controller, session.session_id)
    release.set()

    result = await slow
    assert result.status is ActionStatus.SUCCEEDED, result
    # The renewed lease is still the cached one: later calls keep working.
    again = await restarted.renew_session(setup.controller, session.session_id)
    assert again.expires_at >= renewed.expires_at
    ok = await restarted.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "after"}, "r-after")
    )
    assert ok.status is ActionStatus.SUCCEEDED, ok


async def test_a_late_renewal_does_not_undo_a_newer_lease(setup, monkeypatch):
    """A renewal that returns late must not replace a lease renewed since."""
    import threading

    service = setup.service()
    session = await service.open_session(setup.controller)
    real_renew = setup.auth.renew_session
    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_first_renew(*args, **kwargs):
        nonlocal calls
        calls += 1
        renewed = real_renew(*args, **kwargs)
        if calls == 1:
            started.set()
            release.wait(5)
        return renewed

    monkeypatch.setattr(setup.auth, "renew_session", slow_first_renew)
    late = asyncio.create_task(service.renew_session(setup.controller, session.session_id))
    await asyncio.to_thread(started.wait, 5)
    # A request with the old lease is refused and prunes the cache...
    stale = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "old"}, "r-old")
    )
    assert stale.status is ActionStatus.REJECTED
    assert service._sessions == {}
    # ...then another renewal resumes the session and extends it again.
    await asyncio.sleep(0.01)
    newer = await service.renew_session(setup.controller, session.session_id)
    release.set()
    await late

    assert service._sessions[session.session_id].expires_at == newer.expires_at
    ok = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "after"}, "r-after")
    )
    assert ok.status is ActionStatus.SUCCEEDED, ok
    await service.renew_session(setup.controller, session.session_id)
