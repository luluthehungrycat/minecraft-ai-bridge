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
