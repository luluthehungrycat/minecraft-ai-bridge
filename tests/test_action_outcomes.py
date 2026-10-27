"""Durable request-ID idempotency across ActionService restarts and instances."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionResult, ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionService,
    AuthzService,
    ControllerService,
    PolicyDecision,
    SqliteActionOutcomeStore,
)
from minecraft_ai_bridge.runtime.action_service import ActionOutcome, _request_fingerprint
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient

SCOPE = ("controller", "a")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _service(world: MockMcpqClient, store: SqliteActionOutcomeStore, scope=SCOPE):
    def policy(_request):
        return PolicyDecision(True, identity_scope=scope)

    return ActionService(McpqWorldAdapter(world), policy=policy, outcome_store=store)


def _chat(service: ActionService, message: str, request_id: str = "r1"):
    return service.execute(ActionType.CHAT, {"message": message}, request_id=request_id)


def _fp(message: str) -> str:
    return _request_fingerprint(ActionType.CHAT, {"message": message})


@pytest.fixture
def world():
    return MockMcpqClient()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "outcomes.sqlite3"


# ── Restart replay ───────────────────────────────────────────────────


async def test_outcome_replays_after_restart(path, world):
    first = SqliteActionOutcomeStore(path)
    done = await _chat(_service(world, first), "once")
    first.close()

    second = SqliteActionOutcomeStore(path)  # a fresh process
    replay = await _chat(_service(world, second), "once")
    second.close()

    assert done.success and replay.success
    assert replay.data.get("replayed") is True
    assert world.chat_messages_sent == ["once"]


async def test_conflicting_replay_after_restart_is_refused(path, world):
    store = SqliteActionOutcomeStore(path)
    await _chat(_service(world, store), "a")
    store.close()

    store = SqliteActionOutcomeStore(path)
    result = await _chat(_service(world, store), "b")
    store.close()

    assert not result.success
    assert world.chat_messages_sent == ["a"]


async def test_other_identity_cannot_replay_after_restart(path, world):
    store = SqliteActionOutcomeStore(path)
    await _chat(_service(world, store), "a")
    store.close()

    store = SqliteActionOutcomeStore(path)
    result = await _chat(_service(world, store, scope=("controller", "b")), "a")
    store.close()

    assert not result.success
    assert "another authorized identity" in result.message
    assert world.chat_messages_sent == ["a"]


# ── F1: retention never lets a used request_id dispatch again ───────


async def test_capacity_eviction_keeps_a_tombstone(path, world):
    store = SqliteActionOutcomeStore(path, max_records=2)
    service = _service(world, store)
    for i in range(3):
        await _chat(service, f"m{i}", request_id=f"r{i}")
    store.close()

    store = SqliteActionOutcomeStore(path, max_records=2)
    retry = await _chat(_service(world, store), "m0", request_id="r0")
    store.close()

    assert not retry.success
    assert "no longer retained" in retry.message
    assert world.chat_messages_sent.count("m0") == 1


async def test_expired_outcome_keeps_a_tombstone_until_the_window_ends(path, world):
    clock = Clock()
    store = SqliteActionOutcomeStore(
        path, max_age_seconds=60, tombstone_max_age_seconds=3600, clock=clock
    )
    await _chat(_service(world, store), "once")
    store.close()

    clock.advance(seconds=120)  # past full-outcome retention
    store = SqliteActionOutcomeStore(
        path, max_age_seconds=60, tombstone_max_age_seconds=3600, clock=clock
    )
    within = await _chat(_service(world, store), "once")
    store.close()

    clock.advance(seconds=3600)  # past the idempotency window
    store = SqliteActionOutcomeStore(
        path, max_age_seconds=60, tombstone_max_age_seconds=3600, clock=clock
    )
    after = await _chat(_service(world, store), "once")
    store.close()

    assert not within.success and "no longer retained" in within.message
    # Documented bound: after the tombstone window the ID may be reused.
    assert after.success
    assert world.chat_messages_sent == ["once", "once"]


def test_retention_bounds_are_validated(tmp_path):
    with pytest.raises(ValueError):
        SqliteActionOutcomeStore(tmp_path / "a.sqlite3", max_records=0)
    with pytest.raises(ValueError):
        SqliteActionOutcomeStore(
            tmp_path / "b.sqlite3", max_age_seconds=60, tombstone_max_age_seconds=30
        )


# ── F3: atomic claims across instances ───────────────────────────────


def test_claim_never_overwrites_another_owner(path):
    a = SqliteActionOutcomeStore(path)
    b = SqliteActionOutcomeStore(path)  # independent connection

    first = a.claim("r1", ActionType.CHAT, _fp("x"), SCOPE)
    second = b.claim("r1", ActionType.CHAT, _fp("x"), SCOPE)

    assert first.owner is not None
    assert second.owner is None and second.in_progress
    assert not b.begin_dispatch("r1", "not-the-owner")
    assert a.begin_dispatch("r1", first.owner)
    a.close()
    b.close()


def test_concurrent_claims_from_separate_connections_have_one_winner(path):
    SqliteActionOutcomeStore(path).close()  # create the schema once
    stores = [SqliteActionOutcomeStore(path) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    owners: list[str | None] = []

    def claim(store: SqliteActionOutcomeStore) -> None:
        barrier.wait()
        owners.append(store.claim("r1", ActionType.CHAT, _fp("x"), SCOPE).owner)

    threads = [threading.Thread(target=claim, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()

    assert sum(owner is not None for owner in owners) == 1


async def test_two_services_sharing_a_store_dispatch_once(path, world):
    a = SqliteActionOutcomeStore(path)
    b = SqliteActionOutcomeStore(path)

    results = await asyncio.gather(_chat(_service(world, a), "x"), _chat(_service(world, b), "x"))
    c = SqliteActionOutcomeStore(path)
    late = await _chat(_service(world, c), "x")
    for store in (a, b, c):
        store.close()

    # Exactly one dispatch; the other instance is refused (still in progress)
    # or replays the stored outcome, depending on timing.
    assert sum(r.success and not r.data.get("replayed") for r in results) == 1
    assert late.success and late.data.get("replayed") is True
    assert world.chat_messages_sent == ["x"]


# ── F4: reserved -> dispatching -> terminal ──────────────────────────


async def test_crash_while_waiting_for_the_lock_is_retryable_after_timeout(path, world):
    clock = Clock()
    store = SqliteActionOutcomeStore(path, reservation_timeout_seconds=30, clock=clock)
    crashed = store.claim("r1", ActionType.CHAT, _fp("hi"), SCOPE)  # never began dispatch

    fresh = await _chat(_service(world, store), "hi")
    clock.advance(seconds=31)
    retried = await _chat(_service(world, store), "hi")
    store.close()

    assert not fresh.success and "reserved or dispatching" in fresh.message
    assert retried.success
    assert world.chat_messages_sent == ["hi"]
    # The original owner lost the reservation and can no longer dispatch.
    assert crashed.owner is not None


async def test_crash_mid_dispatch_is_unknown_and_never_redispatched(path, world):
    clock = Clock()
    store = SqliteActionOutcomeStore(path, reservation_timeout_seconds=30, clock=clock)
    crashed = store.claim("r1", ActionType.CHAT, _fp("maybe"), SCOPE)
    assert crashed.owner is not None
    assert store.begin_dispatch("r1", crashed.owner)  # then the process died

    soon = await _chat(_service(world, store), "maybe")
    clock.advance(days=30)
    reopened = SqliteActionOutcomeStore(path, reservation_timeout_seconds=30, clock=clock)
    later = await _chat(_service(world, reopened), "maybe")
    reopened.close()
    store.close()

    assert not soon.success
    assert not later.success and "outcome unknown" in later.message
    assert world.chat_messages_sent == []


async def test_lost_reservation_is_not_dispatched(path, world, monkeypatch):
    store = SqliteActionOutcomeStore(path)
    service = _service(world, store)
    monkeypatch.setattr(store, "begin_dispatch", lambda *_args: False)

    result = await _chat(service, "x")
    store.close()

    assert not result.success
    assert "reservation was lost" in result.message
    assert world.chat_messages_sent == []


# ── F2: persistence never blocks the event loop ─────────────────────


async def test_slow_completion_does_not_block_the_event_loop(path, world, monkeypatch):
    store = SqliteActionOutcomeStore(path)
    original = store.complete
    entered = threading.Event()
    release = threading.Event()

    def slow_complete(request_id: str, owner: str, outcome: ActionOutcome) -> bool:
        entered.set()
        release.wait(timeout=5)
        return original(request_id, owner, outcome)

    monkeypatch.setattr(store, "complete", slow_complete)
    task = asyncio.create_task(_chat(_service(world, store), "x"))

    ticks = 0
    while not entered.is_set():
        await asyncio.sleep(0)
    for _ in range(50):
        ticks += 1
        await asyncio.sleep(0)  # the loop keeps running while the write waits

    assert not task.done(), "the leader returns only after its outcome is durable"
    release.set()
    result = await task
    store.close()

    assert ticks == 50
    assert result.success
    assert SqliteActionOutcomeStore(path).load("r1") is not None


async def test_cancelled_leader_still_persists_its_outcome(path, world, monkeypatch):
    store = SqliteActionOutcomeStore(path)
    original = store.complete
    entered = threading.Event()
    release = threading.Event()

    def slow_complete(request_id: str, owner: str, outcome: ActionOutcome) -> bool:
        entered.set()
        release.wait(timeout=5)
        return original(request_id, owner, outcome)

    monkeypatch.setattr(store, "complete", slow_complete)
    service = _service(world, store)
    task = asyncio.create_task(_chat(service, "x"))
    while not entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await service.flush()
    stored = store.load("r1")
    store.close()

    assert stored is not None and stored.outcome.status == "succeeded"


async def test_failed_completion_write_reads_as_unknown(path, world, monkeypatch):
    clock = Clock()
    store = SqliteActionOutcomeStore(path, reservation_timeout_seconds=30, clock=clock)

    def broken(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "complete", broken)
    first = await _chat(_service(world, store), "x")
    monkeypatch.undo()
    clock.advance(seconds=31)
    retry = await _chat(_service(world, store), "x")
    store.close()

    assert first.success
    assert not retry.success and "outcome unknown" in retry.message
    assert world.chat_messages_sent == ["x"]


async def test_unpersisted_claim_is_not_dispatched(path, world, monkeypatch):
    store = SqliteActionOutcomeStore(path)
    service = _service(world, store)

    def broken_claim(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "claim", broken_claim)
    refused = await _chat(service, "x")
    monkeypatch.undo()
    retried = await _chat(service, "x")
    store.close()

    assert not refused.success
    assert retried.success
    assert world.chat_messages_sent == ["x"]


# ── Corrupt and incompatible records ─────────────────────────────────


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("result", "not json"),
        ("result", '{"success": true}'),
        ("result", '{"success": true, "action": "teleport_v2", "data": {}}'),
        ("identity_scope", "{"),
    ],
)
async def test_corrupt_records_fail_closed(path, world, column, value):
    store = SqliteActionOutcomeStore(path)
    await _chat(_service(world, store), "x")
    store.close()
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE action_outcomes SET {column}=? WHERE request_id='r1'", (value,))

    store = SqliteActionOutcomeStore(path)
    retry = await _chat(_service(world, store), "x")
    store.close()

    assert world.chat_messages_sent == ["x"]
    if column == "result" and "teleport_v2" in value:
        assert retry.data.get("replayed") is True  # unknown action, valid record
    else:
        assert not retry.success


def test_unsupported_schema_versions_fail_closed(tmp_path):
    newer = tmp_path / "newer.sqlite3"
    with sqlite3.connect(newer) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="schema version"):
        SqliteActionOutcomeStore(newer)

    unversioned = tmp_path / "unversioned.sqlite3"
    with sqlite3.connect(unversioned) as db:
        db.execute("CREATE TABLE action_outcomes (request_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="schema version"):
        SqliteActionOutcomeStore(unversioned)


# ── Controller integration ───────────────────────────────────────────


async def test_controller_retry_after_restart_never_repeats_a_mutation(tmp_path, path, world):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    token = auth.create_credential(
        "AIBot", "owner", {"steve:control"}, admin_token=admin.token
    ).token

    def controller(store):
        return ControllerService(McpqWorldAdapter(world), auth, "AIBot", outcome_store=store)

    def request(session):
        return ActionRequestSchema(
            steve_id="AIBot",
            controller_session_id=session.session_id,
            generation=session.generation,
            request_id="req-1",
            action=ActionType.CHAT,
            params={"message": "once"},
        )

    store = SqliteActionOutcomeStore(path)
    before = controller(store)
    session = await before.open_session(token)
    first = await before.submit_action(token, request(session))
    await before.close_session(token, session.session_id)
    await before.aclose()
    store.close()

    store = SqliteActionOutcomeStore(path)  # bridge restarted
    after = controller(store)
    session = await after.open_session(token)
    retry = await after.submit_action(token, request(session))
    store.close()
    auth.close()

    assert first.status.value == "succeeded"
    # The new session is a different authorized identity, so the retry is
    # refused rather than replayed; either way the chat is not repeated.
    assert retry.status.value == "rejected"
    assert retry.action_id == first.action_id
    assert world.chat_messages_sent == ["once"]


# ── Reservation takeover vs. a delayed original owner ────────────────


def test_delayed_owner_cannot_dispatch_or_complete_after_takeover(path):
    clock = Clock()
    a = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    b = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    first = a.claim("r1", ActionType.CHAT, _fp("x"), SCOPE)

    clock.advance(seconds=301)  # A is stalled past the reservation timeout
    second = b.claim("r1", ActionType.CHAT, _fp("x"), SCOPE)

    assert first.owner and second.owner and first.owner != second.owner
    assert not a.begin_dispatch("r1", first.owner)  # A wakes up: fenced
    assert b.begin_dispatch("r1", second.owner)
    stale = ActionOutcome(
        result=ActionResult(success=True, action=ActionType.CHAT), status="succeeded", allowed=True
    )
    assert not a.complete("r1", first.owner, stale)  # cannot overwrite B's row
    # A dispatching row is never taken over, however old it gets.
    clock.advance(days=30)
    late = a.claim("r1", ActionType.CHAT, _fp("x"), SCOPE)
    assert late.owner is None
    a.close()
    b.close()


async def test_owner_stalled_behind_its_dispatch_lock_aborts_after_takeover(path, world):
    clock = Clock()
    a_store = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    b_store = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    a = _service(world, a_store)
    b = _service(world, b_store)

    # Hold A's dispatch lock so A's request reserves, then waits.
    await a._dispatch_lock.acquire()
    stalled = asyncio.create_task(_chat(a, "x"))
    while a_store.load("r1") is None and "r1" not in a._durable_owners:
        await asyncio.sleep(0)

    clock.advance(seconds=301)
    taken = await _chat(b, "x")  # B takes the stale reservation over
    a._dispatch_lock.release()
    aborted = await stalled
    a_store.close()
    b_store.close()

    assert taken.success
    assert not aborted.success and "reservation was lost" in aborted.message
    assert world.chat_messages_sent == ["x"]


def _race_takeover(
    a: SqliteActionOutcomeStore, b: SqliteActionOutcomeStore, request_id: str, owner: str
) -> dict[str, bool]:
    """Run A's begin_dispatch and B's takeover at the same moment."""
    barrier = threading.Barrier(2)
    outcome: dict[str, bool] = {}

    def original_owner() -> None:
        barrier.wait()
        outcome["a"] = a.begin_dispatch(request_id, owner)

    def takeover() -> None:
        barrier.wait()
        taken = b.claim(request_id, ActionType.CHAT, _fp("x"), SCOPE)
        outcome["b"] = taken.owner is not None and b.begin_dispatch(request_id, taken.owner)

    threads = [threading.Thread(target=original_owner), threading.Thread(target=takeover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcome


def test_begin_dispatch_and_takeover_race_has_exactly_one_winner(path):
    clock = Clock()
    a = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    b = SqliteActionOutcomeStore(path, reservation_timeout_seconds=300, clock=clock)
    for i in range(300):
        claim = a.claim(f"r{i}", ActionType.CHAT, _fp("x"), SCOPE)
        assert claim.owner is not None
        clock.advance(seconds=301)  # the reservation is now stale
        outcome = _race_takeover(a, b, f"r{i}", claim.owner)
        assert outcome["a"] != outcome["b"], (i, outcome)  # exactly one dispatches
    a.close()
    b.close()
