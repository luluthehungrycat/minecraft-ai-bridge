"""Durable request-ID outcomes for ActionService idempotency.

ActionService keeps request-ID outcomes in memory, so a client retrying after
a bridge restart could repeat a mutation. ``SqliteActionOutcomeStore`` keeps
them on disk. A request is marked ``dispatching`` before it can reach the
backend and completed afterwards; a marker left by a crash loads as an
``unknown`` outcome, so the request ID never dispatches again.

Records are bound to the authorized identity scope that created them, exactly
as in memory. A retry under a new controller session is therefore refused
rather than replayed, which is conservative: it never repeats a mutation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..minecraft.actions import ActionResult, ActionType

if TYPE_CHECKING:
    from .action_service import ActionOutcome

logger = logging.getLogger(__name__)

_DISPATCHING = "dispatching"


@dataclass(frozen=True)
class StoredActionOutcome:
    """A persisted request-ID record."""

    fingerprint: str
    identity_scope: tuple[str, ...]
    outcome: ActionOutcome


class SqliteActionOutcomeStore:
    """SQLite-backed request-ID outcomes with bounded retention.

    Methods are synchronous and thread-safe. ``claim`` runs before dispatch,
    so ActionService calls it from a worker thread; ``complete`` runs on
    terminal paths that cannot await and is a short local write.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = 10_000,
        max_age_seconds: float = 7 * 24 * 3600,
    ) -> None:
        if max_records < 1 or max_age_seconds <= 0:
            raise ValueError("outcome retention bounds must be positive")
        self._max_records = max_records
        self._max_age = timedelta(seconds=max_age_seconds)
        self._lock = threading.Lock()
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS action_outcomes ("
            "request_id TEXT PRIMARY KEY, action TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "identity_scope TEXT NOT NULL, status TEXT NOT NULL, allowed INTEGER, "
            "result TEXT, created_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS action_outcomes_created ON action_outcomes(created_at)"
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def load(self, request_id: str) -> StoredActionOutcome | None:
        """Return the stored record, treating an unfinished dispatch as unknown."""
        with self._lock:
            row = self._db.execute(
                "SELECT action, fingerprint, identity_scope, status, allowed, result, created_at "
                "FROM action_outcomes WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        action, fingerprint, scope_json, status, allowed, result_json, created_at = row
        if datetime.fromisoformat(created_at) < datetime.now(UTC) - self._max_age:
            return None
        from .action_service import ActionOutcome

        if status == _DISPATCHING:
            outcome = ActionOutcome(
                result=ActionResult(
                    success=False,
                    action=ActionType(action),
                    message=(
                        "A previous dispatch of this request_id did not record an "
                        "outcome; outcome unknown. The request will not re-dispatch."
                    ),
                    data={"request_id": request_id},
                ),
                status="unknown",
                allowed=True,
            )
        else:
            outcome = ActionOutcome(
                result=_decode_result(result_json),
                status=status,
                allowed=bool(allowed),
            )
        return StoredActionOutcome(fingerprint, tuple(json.loads(scope_json)), outcome)

    def claim(
        self,
        request_id: str,
        action: ActionType,
        fingerprint: str,
        identity_scope: tuple[str, ...],
    ) -> None:
        """Mark a request as possibly dispatched before it reaches the backend."""
        now = datetime.now(UTC)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM action_outcomes WHERE created_at < ?",
                    ((now - self._max_age).isoformat(),),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO action_outcomes "
                    "(request_id, action, fingerprint, identity_scope, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        action.value,
                        fingerprint,
                        json.dumps(identity_scope),
                        _DISPATCHING,
                        now.isoformat(),
                    ),
                )
                self._db.execute(
                    "DELETE FROM action_outcomes WHERE request_id IN ("
                    "SELECT request_id FROM action_outcomes ORDER BY created_at DESC "
                    "LIMIT -1 OFFSET ?)",
                    (self._max_records,),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def complete(self, request_id: str, outcome: ActionOutcome) -> None:
        """Record the terminal outcome of a claimed request."""
        with self._lock:
            self._db.execute(
                "UPDATE action_outcomes SET status=?, allowed=?, result=? WHERE request_id=?",
                (
                    outcome.status,
                    int(outcome.allowed),
                    _encode_result(outcome.result),
                    request_id,
                ),
            )


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


def _decode_result(raw: str | None) -> ActionResult:
    payload = json.loads(raw or "{}")
    return ActionResult(
        success=bool(payload.get("success", False)),
        action=ActionType(payload.get("action", ActionType.WAIT.value)),
        message=str(payload.get("message", "")),
        data=dict(payload.get("data") or {}),
    )


__all__ = ["SqliteActionOutcomeStore", "StoredActionOutcome"]
