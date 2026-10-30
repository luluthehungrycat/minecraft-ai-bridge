"""Sequential task worker; never persists bearer tokens or replays uncertain work."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from .action_service import ActionRequest, PolicyDecision
from .authz import ControllerSession
from .contracts import ActionRequestSchema, ActionStatus, Task, TaskStatus, TaskStep
from .tasks import TERMINAL, SqliteTaskStore

if TYPE_CHECKING:
    from .controller_service import ControllerService

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, controller: ControllerService, store: SqliteTaskStore) -> None:
        self.controller = controller
        self.store = store
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._task: Task | None = None
        self._operation: str | None = None
        self._epoch = 0
        self._stopping = False
        self._stop = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"task_worker_{id(self)}", default=None
        )

    def start(self) -> None:
        if self._pump is None and not self._stopping:
            self._pump = asyncio.create_task(self._publish_events(), context=contextvars.Context())

    async def _publish_events(self) -> None:
        while True:
            try:
                events = await asyncio.to_thread(self.store.pending_events)
                for event in events:
                    journal = self.controller._require_journal()
                    await asyncio.to_thread(journal.append, event)
                    await asyncio.to_thread(self.store.acknowledge_event, event.event_id)
            except (sqlite3.Error, ValueError):
                logger.exception("Task event publication failed; durable outbox retained")
            if self._stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 0.25)

    async def _session(self, token: str, session_id: str, generation: int) -> ControllerSession:
        session = await self.controller._owned_session(token, session_id)
        if isinstance(generation, bool) or generation != session.generation:
            raise PermissionError("stale task session generation")
        if not await asyncio.to_thread(self.controller._authz.validate_session, session):
            raise PermissionError("task controller session is no longer valid")
        return session

    async def _owned(self, token: str, task_id: str) -> Task:
        def read() -> Task:
            auth = self.controller._authz
            credential = auth.authenticate(token)
            if credential.steve_id != self.controller.steve_id or not auth.check_scope(
                token, "steve:control", steve_id=self.controller.steve_id
            ):
                raise PermissionError("task control for this Steve required")
            task = self.store.get(task_id)
            if (
                task.steve_id != self.controller.steve_id
                or task.owner_controller_id != credential.credential_id
            ):
                raise PermissionError("task belongs to another credential")
            return task

        return await asyncio.to_thread(read)

    async def get(self, token: str, task_id: str) -> Task:
        self.start()
        return await self._owned(token, task_id)

    async def create(
        self,
        token: str,
        session_id: str,
        generation: int,
        request_id: str,
        title: str,
        steps: list[dict[str, Any]],
    ) -> Task:
        session = await self._session(token, session_id, generation)
        validated = [TaskStep.model_validate(step) for step in steps]
        async with self._lock:
            if self._stopping:
                raise ValueError("task runtime is stopping")
            task = await asyncio.to_thread(
                self.store.create,
                self.controller.steve_id,
                session.credential_id,
                request_id,
                title,
                validated,
            )
            if task.status is TaskStatus.PENDING:
                self._launch(task, session)
        self.start()
        # Detached: the worker keeps its own copy of the plan.
        return task.model_copy(deep=True)

    async def manage(
        self, token: str, session_id: str, generation: int, task_id: str, operation: str
    ) -> Task:
        session = await self._session(token, session_id, generation)
        async with self._lock:
            task = await self._owned(token, task_id)
            if task.status in TERMINAL:
                return task
            if operation == "resume":
                if self._stopping:
                    raise ValueError("task runtime is stopping")
                if task.status is not TaskStatus.PAUSED:
                    raise ValueError("only paused tasks can resume")
                if task.active_step or task.reason_code == "OUTCOME_UNKNOWN":
                    raise ValueError("OUTCOME_UNKNOWN: reconcile externally and cancel this task")
                if self._worker is not None and not self._worker.done():
                    raise ValueError("previous task action is still stopping")
                task = await asyncio.to_thread(
                    self.store.update,
                    task,
                    "task.resumed",
                    status=TaskStatus.PENDING,
                    requested_operation=None,
                    reason_code=None,
                )
                self._launch(task, session)
            else:
                # Set the in-memory latch before awaiting the durable write.
                # The shared action policy and final dispatch guard see it.
                active = self._worker is not None and not self._worker.done()
                if active and self._task is not None and self._task.task_id == task_id:
                    if self._operation != "cancel":
                        self._operation = operation
                    task = await asyncio.to_thread(
                        self.store.update,
                        task,
                        "task.operation_requested",
                        requested_operation=self._operation,
                    )
                    self._task = task
                else:
                    task = await asyncio.to_thread(
                        self.store.update,
                        task,
                        "task.cancelled" if operation == "cancel" else "task.paused",
                        status=TaskStatus.CANCELLED if operation == "cancel" else TaskStatus.PAUSED,
                        requested_operation=None,
                    )
        self.start()
        # Detached: the worker keeps its own copy of the plan.
        return task.model_copy(deep=True)

    def _launch(self, task: Task, session: ControllerSession) -> None:
        if self._worker is not None and not self._worker.done():
            if self._task is not None and self._task.task_id == task.task_id:
                return
            raise ValueError("another task worker is still active")
        self._task = task
        self._operation = None
        self._epoch = self.controller.action_service.safety_generation
        self._worker = asyncio.create_task(self._run(session), context=contextvars.Context())

    def guard(self, request: ActionRequest) -> PolicyDecision | None:
        if request.trusted_safety:
            return None
        if self._worker is None or self._worker.done():
            return None
        if self._context.get() != (self._task.task_id if self._task else None):
            return PolicyDecision(False, "a task owns strategic execution; pause it first")
        if (
            self._operation
            or self._stopping
            or self.controller.action_service.safety_generation != self._epoch
        ):
            return PolicyDecision(False, "task interrupted before backend dispatch")
        return None

    async def _change(self, event: str, **changes: object) -> Task:
        # Caller holds _lock. Management and worker checkpoints cannot overwrite.
        assert self._task is not None
        self._task = await asyncio.to_thread(self.store.update, self._task, event, **changes)
        return self._task

    async def _run(self, session: ControllerSession) -> None:
        assert self._task is not None
        context = self._context.set(self._task.task_id)
        try:
            while True:
                async with self._lock:
                    assert self._task is not None
                    task = self._task
                    reason = (
                        "SAFETY_PREEMPTED"
                        if self.controller.action_service.safety_generation != self._epoch
                        else "SHUTDOWN"
                        if self._stopping
                        else "PAUSE_REQUESTED"
                    )
                    if self._operation or self._stopping or reason == "SAFETY_PREEMPTED":
                        await self._change(
                            "task.cancelled" if self._operation == "cancel" else "task.interrupted",
                            status=TaskStatus.CANCELLED
                            if self._operation == "cancel"
                            else TaskStatus.PAUSED,
                            requested_operation=None,
                            reason_code=reason,
                        )
                        return
                    if task.next_step >= len(task.steps):
                        await self._change(
                            "task.completed",
                            status=TaskStatus.SUCCEEDED,
                            active_step=False,
                            reason_code=None,
                        )
                        return
                    task = await self._change(
                        "task.started", status=TaskStatus.RUNNING, active_step=True
                    )
                    step = task.steps[task.next_step]
                    # A new ID is used only for a known-safe attempt. Recovery
                    # never launches a worker for an uncheckpointed attempt.
                    request = ActionRequestSchema(
                        steve_id=task.steve_id,
                        controller_session_id=session.session_id,
                        generation=session.generation,
                        request_id=f"{task.task_id}-{task.next_step}-{task.revision}",
                        task_id=task.task_id,
                        action=step.action,
                        params=step.params,
                    )
                result = await self._execute(session, request)
                async with self._lock:
                    if result is None:  # WAIT interrupted cooperatively; no world mutation.
                        await self._change("task.interrupted", active_step=False)
                        continue
                    if result.status is ActionStatus.SUCCEEDED:
                        await self._change(
                            "task.progress", active_step=False, next_step=task.next_step + 1
                        )
                    elif result.status is ActionStatus.REJECTED:
                        await self._change(
                            "task.interrupted",
                            status=TaskStatus.PAUSED,
                            active_step=False,
                            requested_operation=None,
                            reason_code=(
                                "SAFETY_PREEMPTED"
                                if self.controller.action_service.safety_generation != self._epoch
                                else "SHUTDOWN"
                                if self._stopping
                                else "PAUSE_REQUESTED"
                                if self._operation
                                else result.reason_code or "AUTHORITY_LOST"
                            ),
                        )
                        # A pending cancel must still terminate this task.
                        if self._operation == "cancel":
                            await self._change("task.cancelled", status=TaskStatus.CANCELLED)
                        return
                    else:
                        uncertain = result.status in {
                            ActionStatus.INTERRUPTED,
                            ActionStatus.CANCELLED,
                        }
                        await self._change(
                            "task.interrupted" if uncertain else "task.failed",
                            status=TaskStatus.PAUSED if uncertain else TaskStatus.FAILED,
                            active_step=uncertain,
                            reason_code="OUTCOME_UNKNOWN" if uncertain else "ACTION_FAILED",
                        )
                        return
        except (sqlite3.Error, ValueError, PermissionError):
            logger.exception("Task execution stopped; no further actions will be dispatched")
            # An intent may already have reached the backend. Keep it unknown.
            try:
                async with self._lock:
                    await self._change(
                        "task.interrupted", status=TaskStatus.PAUSED, reason_code="OUTCOME_UNKNOWN"
                    )
            except (sqlite3.Error, ValueError):
                logger.exception("Could not checkpoint interrupted task; restart will recover it")
        finally:
            self._context.reset(context)

    async def _execute(self, session: ControllerSession, request: ActionRequestSchema):
        # No token is captured from a transport request or persisted in storage.
        dispatch = asyncio.create_task(
            self.controller._submit_authenticated(session.credential_id, request)
        )
        if request.action.value != "wait":
            return await dispatch
        try:
            while not dispatch.done():
                if (
                    self._operation
                    or self._stopping
                    or self.controller.action_service.safety_generation != self._epoch
                ):
                    dispatch.cancel()  # WAIT only: no to_thread backend mutation.
                    with contextlib.suppress(asyncio.CancelledError):
                        await dispatch
                    return None
                await asyncio.wait({dispatch}, timeout=0.05)
            return await dispatch
        finally:
            if not dispatch.done():
                dispatch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatch

    async def close(self) -> None:
        # Taking the admission lock waits for an in-progress create or resume,
        # whose worker is then joined; later admissions see _stopping.
        async with self._lock:
            self._stopping = True
            worker = self._worker
        if worker is not None:
            await asyncio.shield(worker)
        self._stop.set()
        if self._pump is not None:
            await asyncio.shield(self._pump)
