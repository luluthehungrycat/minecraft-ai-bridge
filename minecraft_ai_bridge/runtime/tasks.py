"""SQLite task checkpoints and transactional lifecycle-event outbox.

A runtime holds an OS lock for the lifetime of a file-backed store. A second
runtime opening the same task database fails closed. This avoids recovering
another process's live work as if it had crashed. All SQLite work is called
from worker threads by the task runner.

Admission allows one pending or running task per Steve and one unfinished
task per credential, so a paused task blocks only its own credential.

Retention has two tiers, like the action outcome store. Finished tasks are
kept for ``max_age_seconds`` and the newest ``max_finished``; older ones
become tombstones that keep only the request key and plan fingerprint, so
the request ID still cannot start the plan again. Tombstones are deleted
after ``tombstone_max_age_seconds``, the overall idempotency window.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .contracts import Event, EventSeverity, Task, TaskStatus, TaskStep

TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})
_TERMINAL_VALUES = tuple(sorted(status.value for status in TERMINAL))
_ACTIVE_VALUES = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
# Unfinished tasks are bounded by one per credential; this caps them anyway.
MAX_UNFINISHED_TASKS = 10_000


def _finished_at(task: Task) -> str | None:
    """UTC ISO time a finished task ended, comparable as text; None while unfinished."""
    return task.updated_at.astimezone(UTC).isoformat() if task.status in TERMINAL else None


class SqliteTaskStore:
    """Bounded durable plans, idempotent creation, checkpoints and event outbox."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_finished: int = 10_000,
        max_age_seconds: float = 7 * 24 * 3600,
        tombstone_max_age_seconds: float = 365 * 24 * 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_finished < 1 or max_age_seconds <= 0 or tombstone_max_age_seconds < max_age_seconds:
            raise ValueError(
                "task retention bounds must be positive and tombstones must outlive finished tasks"
            )
        self._max_finished = max_finished
        self._max_age = timedelta(seconds=max_age_seconds)
        self._tombstone_max_age = timedelta(seconds=tombstone_max_age_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._file_lock = None
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = open(f"{Path(path).resolve()}.lock", "a+b")  # noqa: SIM115 - held until close
            try:
                fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._file_lock.close()
                raise ValueError("task database is already in use by another runtime") from None
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, steve_id TEXT NOT NULL,
                    owner TEXT NOT NULL, request_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, record TEXT NOT NULL,
                    status TEXT, finished_at TEXT,
                    UNIQUE(steve_id, owner, request_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_tombstones (
                    steve_id TEXT NOT NULL, owner TEXT NOT NULL,
                    request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    task_id TEXT NOT NULL, finished_at TEXT NOT NULL,
                    PRIMARY KEY(steve_id, owner, request_key)
                );
            """)
            with self._lock:
                self._add_status_columns()
                records = self._db.execute(
                    "SELECT record FROM tasks WHERE status IN (?, ?)", _ACTIVE_VALUES
                ).fetchall()
                for (raw,) in records:
                    task = Task.model_validate_json(raw)
                    unknown = "OUTCOME_UNKNOWN" if task.active_step else None
                    if task.requested_operation == "cancel":
                        # The caller already asked to cancel; never make it
                        # resumable again.
                        self.update(
                            task,
                            "task.cancelled",
                            status=TaskStatus.CANCELLED,
                            requested_operation=None,
                            reason_code=unknown,
                        )
                    else:
                        self.update(
                            task,
                            "task.interrupted",
                            status=TaskStatus.PAUSED,
                            requested_operation=None,
                            reason_code=unknown or "RESTARTED",
                        )
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            if self._file_lock is not None:
                self._file_lock.close()
            raise

    def _add_status_columns(self) -> None:
        """Add and backfill the status columns on databases created without them."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}
        if {"status", "finished_at"} <= columns:
            return
        with self._db:
            if "status" not in columns:
                self._db.execute("ALTER TABLE tasks ADD COLUMN status TEXT")
            if "finished_at" not in columns:
                self._db.execute("ALTER TABLE tasks ADD COLUMN finished_at TEXT")
            for task_id, raw in self._db.execute("SELECT task_id, record FROM tasks").fetchall():
                task = Task.model_validate_json(raw)
                self._db.execute(
                    "UPDATE tasks SET status=?, finished_at=? WHERE task_id=?",
                    (task.status.value, _finished_at(task), task_id),
                )

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._file_lock is not None:
                self._file_lock.close()
                self._file_lock = None

    def create(
        self,
        steve_id: str,
        owner: str,
        request_key: str,
        title: str,
        steps: list[TaskStep],
        *,
        admit_new: bool = True,
    ) -> Task:
        """Return the task for ``request_key``, creating it if it is new.

        With ``admit_new=False`` (a worker is still active) only an existing
        task is returned; a new one is refused as if a task were running.
        """
        # Revalidate instances that may have been constructed/copied unsafely.
        steps = [TaskStep.model_validate(dict(step)) for step in steps]
        if not 1 <= len(steps) <= 100:
            raise ValueError("tasks need between 1 and 100 steps")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("task request_id must have 1 to 128 characters")
        payload = json.dumps(
            {"title": title, "steps": [s.model_dump(mode="json") for s in steps]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > 65536:
            raise ValueError("task plan exceeds 64 KiB")
        fingerprint = hashlib.sha256(payload).hexdigest()
        with self._lock:
            # Committed on its own, so pruning holds even if admission fails.
            with self._db:
                self._prune(self._clock())
            return self._admit(steve_id, owner, request_key, title, steps, fingerprint, admit_new)

    def _admit(
        self,
        steve_id: str,
        owner: str,
        request_key: str,
        title: str,
        steps: list[TaskStep],
        fingerprint: str,
        admit_new: bool,
    ) -> Task:
        with self._db:
            existing = self._db.execute(
                "SELECT fingerprint, record FROM tasks "
                "WHERE steve_id=? AND owner=? AND request_key=?",
                (steve_id, owner, request_key),
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError("task request_id reused for a different plan")
                return Task.model_validate_json(existing[1])
            tombstone = self._db.execute(
                "SELECT fingerprint FROM task_tombstones "
                "WHERE steve_id=? AND owner=? AND request_key=?",
                (steve_id, owner, request_key),
            ).fetchone()
            if tombstone:
                if tombstone[0] != fingerprint:
                    raise ValueError("task request_id reused for a different plan")
                raise ValueError("task request_id was already used; its finished task has expired")
            unfinished = self._db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN (?, ?, ?)", _TERMINAL_VALUES
            ).fetchone()[0]
            if unfinished >= MAX_UNFINISHED_TASKS:
                raise ValueError("task database is at capacity")
            # Only admission is limited by the outbox: checkpoints and
            # restart recovery of admitted tasks must always be recordable,
            # and each admitted plan adds a bounded number of events.
            if self._db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] >= 100000:
                raise ValueError("task event outbox is at capacity; is the event journal failing?")
            if (
                not admit_new
                or self._db.execute(
                    "SELECT 1 FROM tasks WHERE steve_id=? AND status IN (?, ?)",
                    (steve_id, *_ACTIVE_VALUES),
                ).fetchone()
            ):
                raise ValueError("Steve already has a running task")
            # A paused task blocks only its own credential, which can always
            # cancel it; other credentials are never locked out by it.
            if self._db.execute(
                "SELECT 1 FROM tasks WHERE steve_id=? AND owner=? AND status NOT IN (?, ?, ?)",
                (steve_id, owner, *_TERMINAL_VALUES),
            ).fetchone():
                raise ValueError("this credential already has an unfinished task; cancel it first")
            task = Task(
                task_id=f"task-{uuid4()}",
                steve_id=steve_id,
                owner_controller_id=owner,
                title=title,
                steps=steps,
            )
            self._db.execute(
                "INSERT INTO tasks(task_id, steve_id, owner, request_key, fingerprint, record, "
                "status, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    task.task_id,
                    steve_id,
                    owner,
                    request_key,
                    fingerprint,
                    task.model_dump_json(),
                    task.status.value,
                ),
            )
            self._event(task, "task.created")
            return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            row = self._db.execute(
                "SELECT record FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown task")
            return Task.model_validate_json(row[0])

    def update(self, task: Task, event_type: str, **changes: object) -> Task:
        with self._lock, self._db:
            current = self.get(task.task_id)
            if current.revision != task.revision:
                raise ValueError("task checkpoint changed concurrently")
            updated = Task.model_validate(
                dict(
                    task.model_copy(
                        update={
                            **changes,
                            "revision": task.revision + 1,
                            "updated_at": self._clock(),
                        }
                    )
                )
            )
            self._db.execute(
                "UPDATE tasks SET record=?, status=?, finished_at=? WHERE task_id=?",
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    _finished_at(updated),
                    task.task_id,
                ),
            )
            self._event(updated, event_type)
            return updated

    def _prune(self, now: datetime) -> None:
        # Caller holds the lock and a transaction. Finished tasks past their
        # age, or beyond the newest max_finished, become tombstones;
        # tombstones past the tombstone age are deleted. Unfinished tasks
        # are never pruned.
        now = now.astimezone(UTC)
        expired = (
            "finished_at IS NOT NULL AND (finished_at < ? OR task_id NOT IN ("
            "SELECT task_id FROM tasks WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT ?))"
        )
        bounds = ((now - self._max_age).isoformat(), self._max_finished)
        self._db.execute(
            "INSERT OR REPLACE INTO task_tombstones "
            "SELECT steve_id, owner, request_key, fingerprint, task_id, finished_at "
            f"FROM tasks WHERE {expired}",
            bounds,
        )
        self._db.execute(f"DELETE FROM tasks WHERE {expired}", bounds)
        self._db.execute(
            "DELETE FROM task_tombstones WHERE finished_at < ?",
            ((now - self._tombstone_max_age).isoformat(),),
        )

    def _event(self, task: Task, event_type: str) -> None:
        event = Event(
            event_id=f"{task.task_id}-{task.revision}",
            type=event_type,
            severity=EventSeverity.INFO,
            source="task_service",
            steve_id=task.steve_id,
            task_id=task.task_id,
            payload={
                "status": task.status.value,
                "next_step": task.next_step,
                "revision": task.revision,
                "reason_code": task.reason_code,
            },
        )
        self._db.execute(
            "INSERT INTO task_events VALUES (?, ?)", (event.event_id, event.model_dump_json())
        )

    def pending_events(self) -> list[Event]:
        with self._lock:
            return [
                Event.model_validate_json(row[0])
                for row in self._db.execute(
                    "SELECT record FROM task_events ORDER BY rowid LIMIT 100"
                )
            ]

    def acknowledge_event(self, event_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM task_events WHERE event_id=?", (event_id,))
