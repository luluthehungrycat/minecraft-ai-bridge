"""Durable request-ID outcomes for ActionService idempotency.

ActionService keeps request-ID outcomes in memory, so a client retrying after
a bridge restart could repeat a mutation. ``SqliteActionOutcomeStore`` keeps
them on disk with a small per-request state machine::

    reserved ──begin_dispatch──▶ dispatching ──complete──▶ terminal
        │                                                     │
        └─ stale (owner never dispatched): another claim     age ▼
           may take the reservation over                    expired (tombstone)

Every transition is a compare-and-set on the owner token issued by
``claim``, so two ActionService instances sharing one database cannot both
dispatch a request ID: only the owner can move ``reserved`` to
``dispatching``, and ``dispatching`` is never taken over. A row left in
``dispatching`` by a crash reads as an unknown outcome and never dispatches
again. This is at-most-once dispatch per request ID within the retention
window, not exactly-once execution: the Minecraft backend does not take part
in the SQLite transaction, so a crash after dispatch leaves the outcome
unknown.

Retention has two tiers. Full outcomes are kept for ``max_age_seconds`` and
the newest ``max_records``; older ones become tombstones that keep only the
request ID, action, fingerprint, and identity scope, so the request ID still
cannot be reused. Tombstones are deleted after ``tombstone_max_age_seconds``,
which is the overall idempotency window.

Records are bound to the authorized identity scope that created them, exactly
as in memory, so a retry under a new controller session is refused rather
than replayed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..minecraft.actions import ActionResult, ActionType

if TYPE_CHECKING:
    from .action_service import ActionOutcome

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_RESERVED = "reserved"
_DISPATCHING = "dispatching"
_EXPIRED = "expired"
_IN_PROGRESS = frozenset({_RESERVED, _DISPATCHING})


@dataclass(frozen=True)
class StoredActionOutcome:
    """A persisted request-ID record that is no longer in progress."""

    fingerprint: str
    identity_scope: tuple[str, ...]
    outcome: ActionOutcome


@dataclass(frozen=True)
class ClaimResult:
    """Result of ``claim``: exactly one field describes what happened.

    ``owner`` is set when this caller now holds the reservation. ``existing``
    is set when the request ID already has a terminal or expired record.
    ``in_progress`` is True when another owner holds it and may still
    dispatch, so the caller must neither dispatch nor cache a result.
    """

    owner: str | None = None
    existing: StoredActionOutcome | None = None
    in_progress: bool = False


class SqliteActionOutcomeStore:
    """SQLite-backed request-ID outcomes with bounded retention.

    Methods are synchronous and thread-safe; ActionService calls them from
    worker threads so SQLite I/O and lock waits never block the event loop.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = 10_000,
        max_age_seconds: float = 7 * 24 * 3600,
        tombstone_max_age_seconds: float = 365 * 24 * 3600,
        reservation_timeout_seconds: float = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            max_records < 1
            or max_age_seconds <= 0
            or reservation_timeout_seconds <= 0
            or tombstone_max_age_seconds < max_age_seconds
        ):
            raise ValueError(
                "outcome retention bounds must be positive and tombstones must "
                "outlive full outcomes"
            )
        self._max_records = max_records
        self._max_age = timedelta(seconds=max_age_seconds)
        self._tombstone_max_age = timedelta(seconds=tombstone_max_age_seconds)
        self._reservation_timeout = timedelta(seconds=reservation_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    def _init_schema(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        existing = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_outcomes'"
        ).fetchone()
        if version == SCHEMA_VERSION:
            return
        if version != 0 or existing:
            # Fail closed rather than guess at another version's semantics.
            self._db.close()
            raise RuntimeError(
                f"action outcome store schema version {version} is not supported "
                f"(expected {SCHEMA_VERSION}); move the database aside"
            )
        self._db.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE action_outcomes (
                request_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                action TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                identity_scope TEXT NOT NULL,
                status TEXT NOT NULL,
                allowed INTEGER,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX action_outcomes_status_created
                ON action_outcomes(status, created_at);
            PRAGMA user_version={SCHEMA_VERSION};
            COMMIT;
            """
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── State transitions ───────────────────────────────────────────

    def claim(
        self,
        request_id: str,
        action: ActionType,
        fingerprint: str,
        identity_scope: tuple[str, ...],
    ) -> ClaimResult:
        """Reserve a request ID if it has no record, or report the record.

        Never overwrites an existing record, except a ``reserved`` row older
        than the reservation timeout, whose owner never began dispatch.
        """
        now = self._clock()
        owner = str(uuid4())
        scope = json.dumps(identity_scope)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune(now)
                row = self._db.execute(
                    "SELECT status, updated_at FROM action_outcomes WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO action_outcomes (request_id, owner, action, fingerprint, "
                        "identity_scope, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            request_id,
                            owner,
                            action.value,
                            fingerprint,
                            scope,
                            _RESERVED,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    result = ClaimResult(owner=owner)
                elif row[0] == _RESERVED and _parse_time(row[1]) < now - self._reservation_timeout:
                    # The previous owner never began dispatch; begin_dispatch
                    # is a compare-and-set on owner, so it cannot dispatch now.
                    self._db.execute(
                        "UPDATE action_outcomes SET owner=?, action=?, fingerprint=?, "
                        "identity_scope=?, created_at=?, updated_at=? "
                        "WHERE request_id=? AND status=?",
                        (
                            owner,
                            action.value,
                            fingerprint,
                            scope,
                            now.isoformat(),
                            now.isoformat(),
                            request_id,
                            _RESERVED,
                        ),
                    )
                    result = ClaimResult(owner=owner)
                elif (
                    row[0] == _DISPATCHING and _parse_time(row[1]) < now - self._reservation_timeout
                ):
                    # Its owner most likely crashed mid-dispatch: report the
                    # outcome as unknown. The row is left as is and is never
                    # taken over, so the request cannot dispatch again.
                    result = ClaimResult(existing=self._read(request_id))
                elif row[0] in _IN_PROGRESS:
                    result = ClaimResult(in_progress=True)
                else:
                    result = ClaimResult(existing=self._read(request_id))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return result

    def begin_dispatch(self, request_id: str, owner: str) -> bool:
        """Move an owned reservation to ``dispatching``; False if it was lost."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE action_outcomes SET status=?, updated_at=? "
                "WHERE request_id=? AND owner=? AND status=?",
                (_DISPATCHING, self._clock().isoformat(), request_id, owner, _RESERVED),
            )
            return cursor.rowcount == 1

    def complete(self, request_id: str, owner: str, outcome: ActionOutcome) -> bool:
        """Record the terminal outcome of an owned request; False if not owned."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE action_outcomes SET status=?, allowed=?, result=?, updated_at=? "
                "WHERE request_id=? AND owner=? AND status IN (?, ?)",
                (
                    outcome.status,
                    int(outcome.allowed),
                    _encode_result(outcome.result),
                    self._clock().isoformat(),
                    request_id,
                    owner,
                    _RESERVED,
                    _DISPATCHING,
                ),
            )
            return cursor.rowcount == 1

    def load(self, request_id: str) -> StoredActionOutcome | None:
        """Return a non-in-progress record, for inspection and tests."""
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM action_outcomes WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or row[0] in _IN_PROGRESS:
                return None
            return self._read(request_id)

    # ── Internals (caller holds self._lock) ─────────────────────────

    def _prune(self, now: datetime) -> None:
        # Full outcomes past their age, or beyond the newest max_records,
        # become tombstones; tombstones and abandoned rows past the
        # tombstone age are deleted.
        self._db.execute(
            "UPDATE action_outcomes SET status=?, result=NULL "
            "WHERE status NOT IN (?, ?, ?) AND created_at < ?",
            (
                _EXPIRED,
                _EXPIRED,
                _RESERVED,
                _DISPATCHING,
                (now - self._max_age).isoformat(),
            ),
        )
        self._db.execute(
            "UPDATE action_outcomes SET status=?, result=NULL WHERE request_id IN ("
            "SELECT request_id FROM action_outcomes WHERE status NOT IN (?, ?, ?) "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (_EXPIRED, _EXPIRED, _RESERVED, _DISPATCHING, self._max_records),
        )
        self._db.execute(
            "DELETE FROM action_outcomes WHERE created_at < ?",
            ((now - self._tombstone_max_age).isoformat(),),
        )

    def _read(self, request_id: str) -> StoredActionOutcome | None:
        row = self._db.execute(
            "SELECT action, fingerprint, identity_scope, status, allowed, result "
            "FROM action_outcomes WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        action_raw, fingerprint, scope_json, status, allowed, result_json = row
        try:
            identity_scope = tuple(str(part) for part in json.loads(scope_json))
        except (TypeError, ValueError):
            logger.error("Corrupt identity scope for request_id %s", request_id)
            identity_scope = ("corrupt-record",)
        if status == _DISPATCHING:
            outcome = _unknown(
                action_raw,
                request_id,
                "A dispatch of this request_id did not record an outcome; outcome "
                "unknown. The request will not re-dispatch.",
            )
        elif status == _EXPIRED:
            outcome = _unknown(
                action_raw,
                request_id,
                "This request_id was already used; its outcome is no longer retained. "
                "The request will not re-dispatch.",
            )
        else:
            try:
                outcome = _decoded_outcome(status, allowed, result_json)
            except (TypeError, ValueError, KeyError):
                logger.error("Corrupt stored outcome for request_id %s", request_id)
                outcome = _unknown(
                    action_raw,
                    request_id,
                    "The stored outcome for this request_id is unreadable; outcome "
                    "unknown. The request will not re-dispatch.",
                )
        return StoredActionOutcome(fingerprint, identity_scope, outcome)


def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _action(raw: object) -> ActionType:
    try:
        return ActionType(str(raw))
    except ValueError:
        # Written by a version with an action this build does not know.
        return ActionType.WAIT


def _unknown(action_raw: object, request_id: str, message: str) -> ActionOutcome:
    from .action_service import ActionOutcome

    return ActionOutcome(
        result=ActionResult(
            success=False,
            action=_action(action_raw),
            message=message,
            data={"request_id": request_id},
        ),
        status="unknown",
        allowed=True,
    )


def _decoded_outcome(status: str, allowed: Any, raw: str | None) -> ActionOutcome:
    from .action_service import ActionOutcome

    payload = json.loads(raw) if raw else None
    if not isinstance(payload, dict) or not isinstance(payload.get("data", {}), dict):
        raise ValueError("stored result is not an object")
    result = ActionResult(
        success=bool(payload["success"]),
        action=_action(payload["action"]),
        message=str(payload.get("message", "")),
        data=dict(payload.get("data") or {}),
    )
    return ActionOutcome(result=result, status=status, allowed=bool(allowed))


def _encode_result(result: ActionResult) -> str:
    return json.dumps(
        {
            "success": result.success,
            "action": result.action.value,
            "message": result.message,
            "data": result.data,
        },
        default=str,
    )


__all__ = ["ClaimResult", "SqliteActionOutcomeStore", "StoredActionOutcome"]
