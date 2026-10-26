"""Durable request-ID idempotency across ActionService restarts (#23)."""

from __future__ import annotations

import sqlite3

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionService,
    AuthzService,
    ControllerService,
    PolicyDecision,
    SqliteActionOutcomeStore,
)
from minecraft_ai_bridge.runtime.action_service import _request_fingerprint
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


def _service(world: MockMcpqClient, store: SqliteActionOutcomeStore, scope=("controller", "a")):
    def policy(_request):
        return PolicyDecision(True, identity_scope=scope)

    return ActionService(McpqWorldAdapter(world), policy=policy, outcome_store=store)


@pytest.fixture
def world():
    return MockMcpqClient()


async def test_outcome_replays_after_restart(tmp_path, world):
    path = tmp_path / "outcomes.sqlite3"
    first = SqliteActionOutcomeStore(path)
    done = await _service(world, first).execute(
        ActionType.CHAT, {"message": "once"}, request_id="r1"
    )
    first.close()

    second = SqliteActionOutcomeStore(path)  # a fresh process
    replay = await _service(world, second).execute(
        ActionType.CHAT, {"message": "once"}, request_id="r1"
    )
    second.close()

    assert done.success and replay.success
    assert replay.data.get("replayed") is True
    assert world.chat_messages_sent == ["once"]


async def test_crash_mid_dispatch_is_unknown_and_never_redispatched(tmp_path, world):
    path = tmp_path / "outcomes.sqlite3"
    store = SqliteActionOutcomeStore(path)
    params = {"message": "maybe"}
    # Simulate a process that claimed the request and died before completing it.
    store.claim(
        "r1", ActionType.CHAT, _request_fingerprint(ActionType.CHAT, params), ("controller", "a")
    )

    result = await _service(world, store).execute(ActionType.CHAT, params, request_id="r1")
    store.close()

    assert not result.success
    assert "outcome unknown" in result.message
    assert world.chat_messages_sent == []


async def test_conflicting_replay_after_restart_is_refused(tmp_path, world):
    path = tmp_path / "outcomes.sqlite3"
    store = SqliteActionOutcomeStore(path)
    await _service(world, store).execute(ActionType.CHAT, {"message": "a"}, request_id="r1")
    store.close()

    store = SqliteActionOutcomeStore(path)
    result = await _service(world, store).execute(
        ActionType.CHAT, {"message": "b"}, request_id="r1"
    )
    store.close()

    assert not result.success
    assert world.chat_messages_sent == ["a"]


async def test_other_identity_cannot_replay_after_restart(tmp_path, world):
    path = tmp_path / "outcomes.sqlite3"
    store = SqliteActionOutcomeStore(path)
    await _service(world, store).execute(ActionType.CHAT, {"message": "a"}, request_id="r1")
    store.close()

    store = SqliteActionOutcomeStore(path)
    result = await _service(world, store, scope=("controller", "b")).execute(
        ActionType.CHAT, {"message": "a"}, request_id="r1"
    )
    store.close()

    assert not result.success
    assert "another authorized identity" in result.message
    assert world.chat_messages_sent == ["a"]


async def test_unpersisted_claim_is_not_dispatched(tmp_path, world, monkeypatch):
    store = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3")
    service = _service(world, store)

    def broken_claim(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "claim", broken_claim)
    refused = await service.execute(ActionType.CHAT, {"message": "x"}, request_id="r1")
    monkeypatch.undo()
    retried = await service.execute(ActionType.CHAT, {"message": "x"}, request_id="r1")
    store.close()

    assert not refused.success
    assert retried.success
    assert world.chat_messages_sent == ["x"]


def test_retention_is_bounded(tmp_path):
    store = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3", max_records=2)
    for i in range(4):
        store.claim(f"r{i}", ActionType.CHAT, "fp", ("controller", "a"))
    kept = [i for i in range(4) if store.load(f"r{i}") is not None]
    store.close()
    assert kept == [2, 3]

    with pytest.raises(ValueError):
        SqliteActionOutcomeStore(tmp_path / "other.sqlite3", max_records=0)


async def test_controller_retry_after_restart_never_repeats_a_mutation(tmp_path, world):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    token = auth.create_credential(
        "AIBot", "owner", {"steve:control"}, admin_token=admin.token
    ).token
    path = tmp_path / "outcomes.sqlite3"

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
    assert (await before.submit_action(token, request(session))).status.value == "succeeded"
    await before.close_session(token, session.session_id)
    store.close()

    store = SqliteActionOutcomeStore(path)  # bridge restarted
    after = controller(store)
    session = await after.open_session(token)
    retry = await after.submit_action(token, request(session))
    store.close()
    auth.close()

    # The new session is a different authorized identity, so the retry is
    # refused rather than replayed; either way the chat is not repeated.
    assert retry.status.value == "rejected"
    assert world.chat_messages_sent == ["once"]
