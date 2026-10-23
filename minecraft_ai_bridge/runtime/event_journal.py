"""Durable, cursor-based event journal for RFC-0001 Event envelopes.

Event payload retention is bounded by ``max_events``. A compact event-ID
fingerprint index is retained after payload pruning so duplicate IDs remain
idempotent across the journal's lifetime; that index grows with unique event
IDs. This local service does not implement authorization, transport, or task
integration. The built-in safety supervisor is currently its only event
producer; other RFC-0001 event sources remain unimplemented.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .contracts import Event

__all__ = [
    "EventCursorExpiredError",
    "EventIdConflictError",
    "EventJournal",
    "EventJournalError",
    "EventRecord",
]

DEFAULT_MAX_EVENTS = 10_000
MAX_EVENTS_PER_FETCH = 1_000
MAX_CONSUMER_ID_LENGTH = 128
_CONSUMER_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class EventJournalError(Exception):
    """Base class for event journal errors."""


class EventIdConflictError(EventJournalError):
    """An event ID was reused with a different envelope."""


class EventCursorExpiredError(EventJournalError):
    """Retention removed events a consumer had not acknowledged."""

    def __init__(self, consumer_id: str, oldest_cursor: int) -> None:
        self.consumer_id = consumer_id
        self.oldest_cursor = oldest_cursor
        super().__init__(
            f"consumer {consumer_id!r} cursor expired; oldest retained cursor is "
            f"{oldest_cursor}; reset the consumer to resume with retained events"
        )


# Compatibility aliases for the initial draft API.
EventConflictError = EventIdConflictError
ConsumerCursorExpiredError = EventCursorExpiredError


@dataclass(frozen=True)
class EventRecord:
    """An event envelope paired with its stable journal cursor."""

    cursor: int
    event: Event


def _consumer_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CONSUMER_ID_LENGTH:
        raise ValueError(f"consumer_id must be a string of 1-{MAX_CONSUMER_ID_LENGTH} chars")
    if any(char not in _CONSUMER_ID_CHARS for char in value):
        raise ValueError("consumer_id may contain only ASCII letters, digits, '.', '_' and '-'")
    return value


def _positive_int(value: int, *, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _event_json(event: Event) -> tuple[Event, str, str]:
    """Return a validated snapshot, fixed-size fingerprint and canonical JSON."""
    if not isinstance(event, Event):
        raise TypeError("append() requires a contracts.Event instance")
    snapshot = Event.model_validate(deepcopy(event.model_dump(mode="python")))
    model_data = snapshot.model_dump(mode="json")
    canonical = json.dumps(
        model_data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()
    return snapshot, fingerprint, canonical


class EventJournal:
    """SQLite-backed append-only event journal with durable consumer cursors.

    Reads do not advance a consumer's cursor. Polling returns the same
    unacknowledged events until ``ack_event`` advances that consumer, giving
    at-least-once delivery. Retention expires consumers that lag behind
    pruned payloads; they must explicitly call ``reset_consumer`` and then
    resume from the oldest retained event.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        _positive_int(max_events, name="max_events")
        self._max_events = max_events
        self._lock = threading.RLock()
        self._closed = False
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._transaction():
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS event_ids (
                    event_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    cursor INTEGER NOT NULL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS consumers (
                    consumer_id TEXT PRIMARY KEY,
                    acked_cursor INTEGER NOT NULL,
                    expired INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS journal_meta (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )"""
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO journal_meta(name, value) VALUES ('pruned_through', 0)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event journal is closed")

    def close(self) -> None:
        """Close this journal instance; reopening its path restores state."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> EventJournal:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def append(self, event: Event) -> int:
        """Persist an event and return its monotonically increasing cursor.

        Repeating an identical event ID and envelope is idempotent, including
        after its payload was pruned. Reusing the ID with changed content fails.
        """
        snapshot, fingerprint, payload = _event_json(event)
        with self._transaction():
            existing = self._conn.execute(
                "SELECT fingerprint, cursor FROM event_ids WHERE event_id = ?",
                (snapshot.event_id,),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise EventIdConflictError(
                        f"event_id {snapshot.event_id!r} was already used with a different envelope"
                    )
                return int(existing["cursor"])

            result = self._conn.execute(
                "INSERT INTO events(event_id, fingerprint, payload) VALUES (?, ?, ?)",
                (snapshot.event_id, fingerprint, payload),
            )
            cursor = int(result.lastrowid)
            self._conn.execute(
                "INSERT INTO event_ids(event_id, fingerprint, cursor) VALUES (?, ?, ?)",
                (snapshot.event_id, fingerprint, cursor),
            )
            self._enforce_retention()
            return cursor

    def append_many(self, events: list[Event] | tuple[Event, ...]) -> list[int]:
        """Append a batch atomically, preserving idempotency per event ID."""
        if not isinstance(events, (list, tuple)):
            raise TypeError("append_many() requires a list or tuple of Event values")
        prepared = [_event_json(event) for event in events]
        cursors: list[int] = []
        with self._transaction():
            for snapshot, fingerprint, payload in prepared:
                existing = self._conn.execute(
                    "SELECT fingerprint, cursor FROM event_ids WHERE event_id = ?",
                    (snapshot.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise EventIdConflictError(
                            f"event_id {snapshot.event_id!r} was already used with a different "
                            "envelope"
                        )
                    cursors.append(int(existing["cursor"]))
                    continue
                result = self._conn.execute(
                    "INSERT INTO events(event_id, fingerprint, payload) VALUES (?, ?, ?)",
                    (snapshot.event_id, fingerprint, payload),
                )
                cursor = int(result.lastrowid)
                self._conn.execute(
                    "INSERT INTO event_ids(event_id, fingerprint, cursor) VALUES (?, ?, ?)",
                    (snapshot.event_id, fingerprint, cursor),
                )
                cursors.append(cursor)
            self._enforce_retention()
        return cursors

    def _enforce_retention(self) -> None:
        count = int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        excess = count - self._max_events
        if excess <= 0:
            return
        row = self._conn.execute(
            "SELECT cursor FROM events ORDER BY cursor LIMIT 1 OFFSET ?", (excess - 1,)
        ).fetchone()
        if row is not None:
            self._prune_through(int(row["cursor"]))

    def _prune_through(self, cursor: int) -> int:
        self._conn.execute("UPDATE consumers SET expired = 1 WHERE acked_cursor < ?", (cursor,))
        deleted = self._conn.execute("DELETE FROM events WHERE cursor <= ?", (cursor,)).rowcount
        self._conn.execute(
            "UPDATE journal_meta SET value = MAX(value, ?) WHERE name = 'pruned_through'",
            (cursor,),
        )
        return int(deleted)

    def get_events(self, consumer_id: str, *, limit: int = 100) -> list[EventRecord]:
        """Fetch a consumer's next unacknowledged events without advancing it."""
        consumer_id = _consumer_id(consumer_id)
        limit = _positive_int(limit, name="limit", maximum=MAX_EVENTS_PER_FETCH)
        with self._transaction():
            self._ensure_consumer(consumer_id)
            consumer = self._conn.execute(
                "SELECT acked_cursor, expired FROM consumers WHERE consumer_id = ?",
                (consumer_id,),
            ).fetchone()
            if consumer["expired"]:
                raise EventCursorExpiredError(consumer_id, self._oldest_cursor())
            rows = self._conn.execute(
                "SELECT cursor, payload FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?",
                (consumer["acked_cursor"], limit),
            ).fetchall()
        return [
            EventRecord(int(row["cursor"]), Event.model_validate_json(row["payload"]))
            for row in rows
        ]

    def ack_event(self, consumer_id: str, cursor: int) -> None:
        """Advance the durable consumer cursor through a processed event."""
        consumer_id = _consumer_id(consumer_id)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1:
            raise ValueError("cursor must be a positive integer")
        with self._transaction():
            self._ensure_consumer(consumer_id)
            consumer = self._conn.execute(
                "SELECT acked_cursor, expired FROM consumers WHERE consumer_id = ?",
                (consumer_id,),
            ).fetchone()
            if consumer["expired"]:
                raise EventCursorExpiredError(consumer_id, self._oldest_cursor())
            last_cursor = int(
                self._conn.execute("SELECT COALESCE(MAX(cursor), 0) FROM event_ids").fetchone()[0]
            )
            if cursor > last_cursor:
                raise ValueError("cannot acknowledge a cursor that has not been issued")
            self._conn.execute(
                "UPDATE consumers SET acked_cursor = MAX(acked_cursor, ?) WHERE consumer_id = ?",
                (cursor, consumer_id),
            )

    def reset_consumer(self, consumer_id: str) -> None:
        """Clear expiration and resume from the oldest retained event."""
        consumer_id = _consumer_id(consumer_id)
        with self._transaction():
            floor = self._pruned_through()
            self._conn.execute(
                """INSERT INTO consumers(consumer_id, acked_cursor, expired)
                   VALUES (?, ?, 0)
                   ON CONFLICT(consumer_id) DO UPDATE SET
                       acked_cursor = excluded.acked_cursor, expired = 0""",
                (consumer_id, floor),
            )

    def prune(self, *, keep: int) -> int:
        """Explicitly prune to at most keep events, expiring lagging consumers."""
        keep = _positive_int(keep, name="keep", maximum=MAX_EVENTS_PER_FETCH * 100)
        with self._transaction():
            count = int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            excess = count - keep
            if excess <= 0:
                return 0
            row = self._conn.execute(
                "SELECT cursor FROM events ORDER BY cursor LIMIT 1 OFFSET ?", (excess - 1,)
            ).fetchone()
            if row is None:
                return 0
            return self._prune_through(int(row["cursor"]))

    def last_cursor(self) -> int:
        """Return the highest cursor ever issued (including pruned events)."""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute("SELECT COALESCE(MAX(cursor), 0) FROM event_ids").fetchone()
            return int(row[0])

    def consumer_ack_cursor(self, consumer_id: str) -> int:
        """Return a consumer's durable ack cursor, creating it if absent."""
        consumer_id = _consumer_id(consumer_id)
        with self._transaction():
            self._ensure_open()
            self._ensure_consumer(consumer_id)
            row = self._conn.execute(
                "SELECT acked_cursor FROM consumers WHERE consumer_id = ?", (consumer_id,)
            ).fetchone()
            return int(row[0])

    def _ensure_consumer(self, consumer_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO consumers(consumer_id, acked_cursor, expired) VALUES (?, ?, 0)",
            (consumer_id, self._pruned_through()),
        )

    def _pruned_through(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM journal_meta WHERE name = 'pruned_through'"
        ).fetchone()
        return int(row[0])

    def _oldest_cursor(self) -> int:
        row = self._conn.execute("SELECT MIN(cursor) FROM events").fetchone()
        if row[0] is not None:
            return int(row[0])
        return self._pruned_through() + 1

    @property
    def retained_event_count(self) -> int:
        """Number of retained event payloads (excludes compact ID tombstones)."""
        with self._lock:
            self._ensure_open()
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
