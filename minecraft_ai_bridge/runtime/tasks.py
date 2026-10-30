"""SQLite task checkpoints and transactional lifecycle-event outbox.

A runtime holds an OS lock for the lifetime of a file-backed store. A second
runtime opening the same task database fails closed. This avoids recovering
another process's live work as if it had crashed. All SQLite work is called
from worker threads by the task runner.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import Event, EventSeverity, Task, TaskStatus, TaskStep

TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class SqliteTaskStore:
    """Bounded durable plans, idempotent creation, checkpoints and event outbox."""

    def __init__(self, path: str | Path) -> None:
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
                    UNIQUE(steve_id, owner, request_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY, record TEXT NOT NULL
                );
            """)
            with self._lock:
                records = self._db.execute("SELECT record FROM tasks").fetchall()
                for (raw,) in records:
                    task = Task.model_validate_json(raw)
                    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                        self.update(
                            task,
                            "task.interrupted",
                            status=TaskStatus.PAUSED,
                            requested_operation=None,
                            reason_code="OUTCOME_UNKNOWN" if task.active_step else "RESTARTED",
                        )
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            if self._file_lock is not None:
                self._file_lock.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._file_lock is not None:
                self._file_lock.close()
                self._file_lock = None

    def create(
        self, steve_id: str, owner: str, request_key: str, title: str, steps: list[TaskStep]
    ) -> Task:
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
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT fingerprint, record FROM tasks "
                "WHERE steve_id=? AND owner=? AND request_key=?",
                (steve_id, owner, request_key),
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError("task request_id reused for a different plan")
                return Task.model_validate_json(existing[1])
            if self._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 10000:
                raise ValueError("task database is at capacity")
            for (raw,) in self._db.execute(
                "SELECT record FROM tasks WHERE steve_id=?", (steve_id,)
            ):
                if Task.model_validate_json(raw).status not in TERMINAL:
                    raise ValueError("Steve already has an unfinished task")
            task = Task(
                task_id=f"task-{uuid4()}",
                steve_id=steve_id,
                owner_controller_id=owner,
                title=title,
                steps=steps,
            )
            self._db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                (task.task_id, steve_id, owner, request_key, fingerprint, task.model_dump_json()),
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
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )
            )
            self._db.execute(
                "UPDATE tasks SET record=? WHERE task_id=?",
                (updated.model_dump_json(), task.task_id),
            )
            self._event(updated, event_type)
            return updated

    def _event(self, task: Task, event_type: str) -> None:
        if self._db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] >= 100000:
            raise ValueError("task event outbox is at capacity")
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
