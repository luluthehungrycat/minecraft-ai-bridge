"""Durable executable controller tasks."""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.runtime.contracts import TaskStatus
from minecraft_ai_bridge.runtime.tasks import SqliteTaskStore, TaskStep
from tests.test_controller_service import setup as controller_setup


@pytest.fixture
def setup(tmp_path):
    yield from controller_setup.__wrapped__(tmp_path)


def test_store_excludes_another_runtime_and_recovers_unknown(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(path)
    task = store.create(
        "AIBot", "cred", "create-1", "test", [TaskStep(action="chat", params={"message": "hi"})]
    )
    with pytest.raises(ValueError, match="in use"):
        SqliteTaskStore(path)
    task = store.update(task, "task.started", status=TaskStatus.RUNNING, active_step=True)
    store.close()
    store = SqliteTaskStore(path)
    try:
        recovered = store.get(task.task_id)
        assert recovered.status is TaskStatus.PAUSED
        assert recovered.reason_code == "OUTCOME_UNKNOWN"
        assert recovered.next_step == 0
        assert store.pending_events()
    finally:
        store.close()


def test_store_create_is_idempotent_and_payload_bound(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    try:
        steps = [TaskStep(action="wait", params={"seconds": 0})]
        first = store.create("AIBot", "cred", "r", "test", steps)
        assert store.create("AIBot", "cred", "r", "test", steps).task_id == first.task_id
        with pytest.raises(ValueError, match="reused"):
            store.create("AIBot", "cred", "r", "other", steps)
        with pytest.raises(ValueError):
            TaskStep(action="wait", params={"seconds": 31})
        with pytest.raises(ValueError):
            TaskStep(action="craft_item", params={})
        with pytest.raises(ValueError):
            TaskStep(action="break_block", params={})
        with pytest.raises(ValueError):
            store.create(
                "AIBot",
                "cred",
                "x",
                "test",
                [TaskStep(action="chat", params={"message": "x" * 66000})],
            )
    finally:
        store.close()


@pytest.fixture
async def runtime(setup, tmp_path):
    from minecraft_ai_bridge.runtime import (
        ControllerService,
        EventJournal,
        SqliteActionOutcomeStore,
    )
    from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter

    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    outcomes = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3")
    journal = EventJournal(tmp_path / "events.sqlite3")
    service = ControllerService(
        McpqWorldAdapter(setup.world),
        setup.auth,
        "AIBot",
        outcome_store=outcomes,
        event_journal=journal,
        task_store=store,
    )
    yield service
    await service.aclose()
    journal.close()
    outcomes.close()
    store.close()


async def finished(service, token, task_id, status):
    async with asyncio.timeout(3):
        while True:
            task = await service.get_task(token, task_id)
            if task.status is status:
                return task
            await asyncio.sleep(0.01)


async def test_task_executes_after_create_and_persists_progress(runtime, setup):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "plan",
        "test",
        [
            {"action": "chat", "params": {"message": "one"}},
            {"action": "chat", "params": {"message": "two"}},
        ],
    )
    result = await finished(runtime, setup.controller, task.task_id, TaskStatus.SUCCEEDED)
    assert result.next_step == 2
    assert setup.world.chat_messages_sent == ["one", "two"]
    duplicate = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "plan",
        "test",
        [
            {"action": "chat", "params": {"message": "one"}},
            {"action": "chat", "params": {"message": "two"}},
        ],
    )
    assert duplicate.task_id == task.task_id
    assert setup.world.chat_messages_sent == ["one", "two"]
    with pytest.raises(PermissionError):
        await runtime.get_task(setup.observer, task.task_id)
    with pytest.raises(PermissionError):
        await runtime.get_task(setup.second_controller, task.task_id)


async def test_pause_resume_and_cancel_wait_prevents_next_step(runtime, setup):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "plan",
        "wait then chat",
        [
            {"action": "wait", "params": {"seconds": 30}},
            {"action": "chat", "params": {"message": "never"}},
        ],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    await runtime.pause_task(setup.controller, session.session_id, session.generation, task.task_id)
    await finished(runtime, setup.controller, task.task_id, TaskStatus.PAUSED)
    await runtime.resume_task(
        setup.controller, session.session_id, session.generation, task.task_id
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    await runtime.cancel_task(
        setup.controller, session.session_id, session.generation, task.task_id
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.CANCELLED)
    assert setup.world.chat_messages_sent == []


async def test_cancel_while_queued_never_reaches_backend(runtime, setup):
    session = await runtime.open_session(setup.controller)
    await runtime.action_service._dispatch_lock.acquire()
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "queued",
        "queued",
        [{"action": "chat", "params": {"message": "never"}}],
    )
    try:
        await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
        await runtime.cancel_task(
            setup.controller, session.session_id, session.generation, task.task_id
        )
    finally:
        runtime.action_service._dispatch_lock.release()
    result = await finished(runtime, setup.controller, task.task_id, TaskStatus.CANCELLED)
    assert not result.active_step
    assert setup.world.chat_messages_sent == []


async def test_safety_epoch_between_steps_pauses_and_revoked_owner_cannot_resume(runtime, setup):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "safety",
        "test",
        [
            {"action": "wait", "params": {"seconds": 30}},
            {"action": "chat", "params": {"message": "never"}},
        ],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    runtime.action_service._preemption_generation += 1
    runtime.action_service.acknowledge_safety_intervention()
    paused = await finished(runtime, setup.controller, task.task_id, TaskStatus.PAUSED)
    assert paused.reason_code == "SAFETY_PREEMPTED"
    assert setup.world.chat_messages_sent == []
    setup.auth.deactivate_steve(setup.admin, "AIBot")
    with pytest.raises(PermissionError):
        await runtime.resume_task(
            setup.controller, session.session_id, session.generation, task.task_id
        )


async def test_unrelated_actions_are_blocked_during_task(runtime, setup):
    from minecraft_ai_bridge.minecraft.actions import ActionType
    from minecraft_ai_bridge.runtime import ActionStatus
    from tests.test_controller_service import _request

    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "wait",
        "test",
        [{"action": "wait", "params": {"seconds": 30}}],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    result = await runtime.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "no"})
    )
    assert result.status is ActionStatus.REJECTED
    await runtime.cancel_task(
        setup.controller, session.session_id, session.generation, task.task_id
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.CANCELLED)
    assert setup.world.chat_messages_sent == []


async def test_pause_while_queued_reports_the_pause(runtime, setup):
    session = await runtime.open_session(setup.controller)
    await runtime.action_service._dispatch_lock.acquire()
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "queued-pause",
        "queued",
        [{"action": "chat", "params": {"message": "later"}}],
    )
    try:
        await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
        await runtime.pause_task(
            setup.controller, session.session_id, session.generation, task.task_id
        )
    finally:
        runtime.action_service._dispatch_lock.release()
    paused = await finished(runtime, setup.controller, task.task_id, TaskStatus.PAUSED)
    assert paused.reason_code == "PAUSE_REQUESTED"
    assert not paused.active_step
    assert setup.world.chat_messages_sent == []

    await runtime.resume_task(
        setup.controller, session.session_id, session.generation, task.task_id
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.SUCCEEDED)
    assert setup.world.chat_messages_sent == ["later"]


async def test_a_task_recovered_without_an_active_step_resumes_on_a_new_runtime(setup, tmp_path):
    from minecraft_ai_bridge.runtime import (
        ControllerService,
        EventJournal,
        SqliteActionOutcomeStore,
    )
    from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter

    credential = setup.auth.authenticate(setup.controller).credential_id
    path = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(path)
    task = store.create(
        "AIBot",
        credential,
        "restart",
        "test",
        [
            {"action": "chat", "params": {"message": "done before"}},
            {"action": "chat", "params": {"message": "after restart"}},
        ],
    )
    # Step 0 completed and was checkpointed; the process then stopped.
    task = store.update(task, "task.progress", status=TaskStatus.RUNNING, next_step=1)
    store.close()

    store = SqliteTaskStore(path)
    outcomes = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3")
    journal = EventJournal(tmp_path / "events.sqlite3")
    service = ControllerService(
        McpqWorldAdapter(setup.world),
        setup.auth,
        "AIBot",
        outcome_store=outcomes,
        event_journal=journal,
        task_store=store,
    )
    try:
        recovered = await service.get_task(setup.controller, task.task_id)
        assert recovered.status is TaskStatus.PAUSED
        assert recovered.reason_code == "RESTARTED"
        session = await service.open_session(setup.controller)
        await service.resume_task(
            setup.controller, session.session_id, session.generation, task.task_id
        )
        await finished(service, setup.controller, task.task_id, TaskStatus.SUCCEEDED)
        assert setup.world.chat_messages_sent == ["after restart"]
    finally:
        await service.aclose()
        journal.close()
        outcomes.close()
        store.close()


async def test_task_lifecycle_events_reach_the_journal(runtime, setup, tmp_path):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "events",
        "test",
        [{"action": "chat", "params": {"message": "hi"}}],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.SUCCEEDED)
    journal = runtime._require_journal()
    async with asyncio.timeout(3):
        while True:
            types = [
                record.event.type
                for record in journal.get_events("test-reader", limit=100)
                if record.event.task_id == task.task_id
            ]
            if "task.completed" in types:
                break
            await asyncio.sleep(0.05)
    assert types[0] == "task.created"
    assert "task.progress" in types


def test_a_full_outbox_refuses_new_tasks_but_never_blocks_recovery(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(path)
    task = store.create("AIBot", "cred", "full", "test", [TaskStep(action="wait", params={})])
    task = store.update(task, "task.started", status=TaskStatus.RUNNING)
    with store._db:
        store._db.executemany(
            "INSERT INTO task_events VALUES (?, ?)",
            ((f"filler-{i}", "{}") for i in range(100_000)),
        )
    store.close()

    store = SqliteTaskStore(path)
    try:
        assert store.get(task.task_id).status is TaskStatus.PAUSED
        with pytest.raises(ValueError, match="outbox is at capacity"):
            store.create("Other", "cred", "next", "test", [TaskStep(action="wait", params={})])
    finally:
        store.close()


def test_a_cancel_requested_before_a_restart_stays_cancelled(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(path)
    task = store.create("AIBot", "cred", "c", "test", [TaskStep(action="wait", params={})])
    task = store.update(
        task, "task.operation_requested", status=TaskStatus.RUNNING, requested_operation="cancel"
    )
    store.close()

    store = SqliteTaskStore(path)
    try:
        recovered = store.get(task.task_id)
        assert recovered.status is TaskStatus.CANCELLED
        assert recovered.reason_code is None
    finally:
        store.close()


async def test_close_waits_for_an_admission_in_progress(runtime, setup, monkeypatch):
    import threading

    session = await runtime.open_session(setup.controller)
    store = runtime._tasks.store
    committed = threading.Event()
    resume = threading.Event()
    create = store.create

    def slow_create(*args, **kwargs):
        task = create(*args, **kwargs)
        committed.set()
        assert resume.wait(10)
        return task

    monkeypatch.setattr(store, "create", slow_create)
    creating = asyncio.create_task(
        runtime.create_task(
            setup.controller,
            session.session_id,
            session.generation,
            "late",
            "test",
            [{"action": "wait", "params": {"seconds": 30}}],
        )
    )
    assert await asyncio.to_thread(committed.wait, 5)
    closing = asyncio.create_task(runtime.stop_tasks())
    await asyncio.sleep(0.05)
    assert not closing.done()  # waits for the admission, not just the old worker
    resume.set()
    task = await creating
    async with asyncio.timeout(5):
        await closing
    assert runtime._tasks._worker is not None and runtime._tasks._worker.done()
    stopped = await runtime.get_task(setup.controller, task.task_id)
    assert stopped.status is TaskStatus.PAUSED
    assert stopped.reason_code == "SHUTDOWN"


async def test_a_returned_task_is_detached_from_the_running_plan(runtime, setup):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "detached",
        "test",
        [{"action": "chat", "params": {"message": "kept"}}],
    )
    task.steps.clear()
    done = await finished(runtime, setup.controller, task.task_id, TaskStatus.SUCCEEDED)
    assert done.next_step == 1
    assert setup.world.chat_messages_sent == ["kept"]


def _finish(store, task, status=TaskStatus.SUCCEEDED):
    return store.update(task, "task.completed", status=status)


def test_a_paused_task_blocks_only_its_own_credential(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    try:
        step = [TaskStep(action="chat", params={"message": "hi"})]
        a = store.create("AIBot", "cred-a", "a1", "test", step)
        with pytest.raises(ValueError, match="running task"):
            store.create("AIBot", "cred-b", "b1", "test", step)
        a = store.update(a, "task.interrupted", status=TaskStatus.PAUSED, reason_code="X")
        # Another credential is not locked out by A's paused task ...
        b = store.create("AIBot", "cred-b", "b1", "test", step)
        _finish(store, b)
        # ... but A must cancel its own unfinished task before starting another.
        with pytest.raises(ValueError, match="cancel it first"):
            store.create("AIBot", "cred-a", "a2", "test", step)
        _finish(store, a, TaskStatus.CANCELLED)
        assert store.create("AIBot", "cred-a", "a2", "test", step).status is TaskStatus.PENDING
    finally:
        store.close()


def test_admit_new_false_answers_only_an_idempotent_retry(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    try:
        step = [TaskStep(action="chat", params={"message": "hi"})]
        first = _finish(store, store.create("AIBot", "cred", "r1", "test", step))
        again = store.create("AIBot", "cred", "r1", "test", step, admit_new=False)
        assert again.task_id == first.task_id
        with pytest.raises(ValueError, match="running task"):
            store.create("AIBot", "cred", "r2", "test", step, admit_new=False)
    finally:
        store.close()


def test_finished_tasks_are_pruned_to_tombstones_that_keep_the_request_id(tmp_path):
    from datetime import UTC, datetime, timedelta

    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3", max_finished=2, clock=lambda: now[0])
    try:
        step = [TaskStep(action="chat", params={"message": "hi"})]
        ids = []
        for key in ("r1", "r2", "r3"):
            ids.append(_finish(store, store.create("AIBot", "cred", key, "test", step)).task_id)
            now[0] += timedelta(seconds=1)
        # Admission prunes beyond the newest two finished tasks.
        store.create("AIBot", "cred", "r4", "test", step)
        with pytest.raises(ValueError, match="unknown task"):
            store.get(ids[0])
        assert store.get(ids[1]).status is TaskStatus.SUCCEEDED
        with pytest.raises(ValueError, match="already used"):
            store.create("AIBot", "cred", "r1", "test", step)
        with pytest.raises(ValueError, match="different plan"):
            store.create("AIBot", "cred", "r1", "other", step)

        # Age also prunes; the unfinished r4 task is never pruned.
        now[0] += timedelta(days=8)
        with pytest.raises(ValueError, match="running task"):
            store.create("AIBot", "cred", "r5", "test", step)
        with pytest.raises(ValueError, match="unknown task"):
            store.get(ids[2])

        # After the idempotency window the request ID is free again.
        now[0] += timedelta(days=366)
        store.update(
            store.get(store.create("AIBot", "cred", "r4", "test", step).task_id),
            "task.cancelled",
            status=TaskStatus.CANCELLED,
        )
        assert store.create("AIBot", "cred", "r1", "test", step).status is TaskStatus.PENDING
    finally:
        store.close()


def test_more_than_the_retained_number_of_tasks_can_be_created(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3", max_finished=3)
    try:
        step = [TaskStep(action="chat", params={"message": "hi"})]
        for i in range(10):
            _finish(store, store.create("AIBot", "cred", f"r{i}", "test", step))
        rows = store._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert rows <= 4
    finally:
        store.close()


def test_a_database_without_status_columns_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "tasks.sqlite3"
    store = SqliteTaskStore(path)
    step = [TaskStep(action="chat", params={"message": "hi"})]
    paused = store.update(
        store.create("AIBot", "cred-a", "a1", "test", step),
        "task.interrupted",
        status=TaskStatus.PAUSED,
    )
    store.close()
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE old AS SELECT task_id, steve_id, owner, request_key, fingerprint, record "
        "FROM tasks; DROP TABLE tasks; ALTER TABLE old RENAME TO tasks;"
    )
    db.close()
    store = SqliteTaskStore(path)
    try:
        assert store.get(paused.task_id).status is TaskStatus.PAUSED
        with pytest.raises(ValueError, match="cancel it first"):
            store.create("AIBot", "cred-a", "a2", "test", step)
        assert store.create("AIBot", "cred-b", "b1", "test", step).status is TaskStatus.PENDING
    finally:
        store.close()


async def test_another_controller_can_run_tasks_after_a_paused_task_is_abandoned(runtime, setup):
    session = await runtime.open_session(setup.controller)
    abandoned = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "abandoned",
        "test",
        [
            {"action": "wait", "params": {"seconds": 30}},
            {"action": "chat", "params": {"message": "never"}},
        ],
    )
    await finished(runtime, setup.controller, abandoned.task_id, TaskStatus.RUNNING)
    await runtime.pause_task(
        setup.controller, session.session_id, session.generation, abandoned.task_id
    )
    await finished(runtime, setup.controller, abandoned.task_id, TaskStatus.PAUSED)
    await runtime.close_session(setup.controller, session.session_id)

    other = await runtime.open_session(setup.second_controller)
    task = await runtime.create_task(
        setup.second_controller,
        other.session_id,
        other.generation,
        "next",
        "test",
        [{"action": "chat", "params": {"message": "hello"}}],
    )
    await finished(runtime, setup.second_controller, task.task_id, TaskStatus.SUCCEEDED)
    assert setup.world.chat_messages_sent == ["hello"]
    still = await runtime.get_task(setup.controller, abandoned.task_id)
    assert still.status is TaskStatus.PAUSED


async def _cancel_after_commit(runtime, monkeypatch, method, call):
    """Cancel ``call`` after the store ``method`` commits but before it returns."""
    import threading

    store = runtime._tasks.store
    committed = threading.Event()
    release = threading.Event()
    original = getattr(store, method)

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(store, method, slow)
    pending = asyncio.create_task(call)
    assert await asyncio.to_thread(committed.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(store, method, original)


async def test_a_cancelled_create_still_runs_the_committed_task(runtime, setup, monkeypatch):
    session = await runtime.open_session(setup.controller)
    await _cancel_after_commit(
        runtime,
        monkeypatch,
        "create",
        runtime.create_task(
            setup.controller,
            session.session_id,
            session.generation,
            "cancelled-caller",
            "test",
            [{"action": "chat", "params": {"message": "ran"}}],
        ),
    )
    async with asyncio.timeout(3):
        while setup.world.chat_messages_sent != ["ran"]:
            await asyncio.sleep(0.01)
    # Nothing is left pending: the next task is admitted and runs.
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "next",
        "test",
        [{"action": "chat", "params": {"message": "next"}}],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.SUCCEEDED)


async def test_a_cancelled_resume_still_launches_the_worker(runtime, setup, monkeypatch):
    session = await runtime.open_session(setup.controller)
    task = await runtime.create_task(
        setup.controller,
        session.session_id,
        session.generation,
        "resume",
        "test",
        [
            {"action": "wait", "params": {"seconds": 30}},
            {"action": "chat", "params": {"message": "never"}},
        ],
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    await runtime.pause_task(setup.controller, session.session_id, session.generation, task.task_id)
    await finished(runtime, setup.controller, task.task_id, TaskStatus.PAUSED)
    await _cancel_after_commit(
        runtime,
        monkeypatch,
        "update",
        runtime.resume_task(setup.controller, session.session_id, session.generation, task.task_id),
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.RUNNING)
    worker = runtime._tasks._worker
    assert worker is not None and not worker.done()
    await runtime.cancel_task(
        setup.controller, session.session_id, session.generation, task.task_id
    )
    await finished(runtime, setup.controller, task.task_id, TaskStatus.CANCELLED)
    assert setup.world.chat_messages_sent == []
