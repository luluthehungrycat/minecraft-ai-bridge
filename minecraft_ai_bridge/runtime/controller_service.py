"""Transport-independent controller API for one Steve (RFC-0001 Phase B).

External harnesses reach a Steve through ``ControllerService``; an MCP or HTTP
adapter only translates requests into these calls. Each call authenticates the
caller's bearer token again, and mutating calls also require the exclusive
controller session that token opened. The service builds the Steve's
``ActionService`` with the Authz policy bound to whichever session is making
the current request, so authorization, idempotency scoping, and audit use the
same gate as the built-in CLI.

Privileged-simulated MCPQ actions (movement, block and inventory edits,
combat) need ``admin:world``. That authority comes only from the operator's
``world_admin_token`` given to this service, never from a client request.
Without it, ordinary controller keys can observe, chat, and wait. With it,
*every* controller session on this service gets those privileged actions,
including teleporting and editing blocks at any coordinates; configure it only
when all controller credentials for this Steve are trusted at that level.
Only allowlisted actions are exposed; internal and lifecycle actions never are.

With an ``event_journal``, credentials holding ``events:read`` for this Steve
poll its events through a durable per-credential cursor (at-least-once:
events repeat until acknowledged), and opening or releasing a session records
a ``controller.changed`` event. Only events attributed to this Steve are
delivered; unattributed events and other Steves' events are skipped.

This is not a transport or task service. It does not run the safety
supervisor itself; ``bridge.controller_safety.ControllerSafety`` does, sharing
this service's ActionService.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import sqlite3
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from ..minecraft.actions import WALK_TO_TELEPORT_DISTANCE, ActionType, _finite_coordinate
from ..minecraft.observer import Observer
from ..minecraft.world_types import WorldAdapter
from .action_outcomes import SqliteActionOutcomeStore
from .action_service import (
    ActionClass,
    ActionRequest,
    ActionService,
    GuardRefusal,
    PolicyDecision,
    classify_action,
)
from .authz import AuthzService
from .authz import ControllerSession as AuthzControllerSession
from .contracts import (
    ActionRequestSchema,
    ActionResultSchema,
    ActionStatus,
    Capabilities,
    ControllerSession,
    Event,
    EventSeverity,
    SessionStatus,
)
from .event_journal import (
    MAX_EVENTS_PER_FETCH,
    EventCursorExpiredError,
    EventJournal,
    EventJournalError,
    EventRecord,
)

logger = logging.getLogger(__name__)

# Explicit allowlist of actions a controller may request. A new ActionType is
# not exposed until it is added here; internal-control, lifecycle, and
# unclassified actions must never be.
_EXPOSED_ACTIONS = frozenset(
    {
        # Observation
        ActionType.CHECK_INVENTORY,
        ActionType.SCAN_ENTITIES,
        ActionType.SCAN,
        ActionType.CHECK_TIME,
        ActionType.CHECK_WEATHER,
        ActionType.CHECK_HEALTH,
        ActionType.CHECK_HUNGER,
        ActionType.CHECK_POSITION,
        ActionType.LIST_PLAYERS,
        # Survival
        ActionType.CHAT,
        ActionType.WAIT,
        ActionType.DONE,
        # Privileged-simulated on MCPQ; still require admin:world from policy
        ActionType.MOVE_TO,
        ActionType.MOVE_FORWARD,
        ActionType.MOVE_BACK,
        ActionType.WALK_TO,
        ActionType.TURN_LEFT,
        ActionType.TURN_RIGHT,
        ActionType.JUMP,
        ActionType.SPRINT,
        ActionType.TELEPORT,
        ActionType.BREAK_BLOCK,
        ActionType.PLACE_BLOCK,
        ActionType.INTERACT,
        ActionType.EQUIP_ITEM,
        ActionType.EQUIP_BEST_WEAPON,
        ActionType.CRAFT_ITEM,
        ActionType.DROP_ITEM,
        ActionType.EAT,
        ActionType.HEAL,
        ActionType.ATTACK,
    }
)
_EXPOSABLE_CLASSES = frozenset(
    {ActionClass.OBSERVATION, ActionClass.SURVIVAL, ActionClass.PRIVILEGED_SIMULATED}
)
_OBSERVE_SCOPES = ("steve:observe:self", "steve:control")
_EVENTS_SCOPE = "events:read"
# Journal pages read per poll while skipping events this Steve may not see,
# so one poll stays bounded even after a long run of other Steves' events.
_MAX_SKIP_PAGES = 10


@dataclass(frozen=True)
class EventPage:
    """One poll of a credential's event cursor.

    ``events`` repeat on later polls until acknowledged. ``reset`` is true
    when retention had dropped unacknowledged events and the cursor was
    moved to the oldest retained event, so some events were lost.
    """

    events: list[EventRecord]
    reset: bool = False


@dataclass(frozen=True)
class ReachLimits:
    """How far from the bot an external controller may act (#25).

    ``edit_distance`` bounds block edits with explicit coordinates (vanilla
    reach is about 4.5-5 blocks). ``move_distance`` bounds teleport and
    move_to (3D) and walk_to (horizontal) destinations, which must also lie
    within the world height range ``min_y``..``max_y``.
    """

    edit_distance: float = 6.0
    move_distance: float = 32.0
    min_y: float = -64.0
    max_y: float = 320.0

    def __post_init__(self) -> None:
        values = (self.edit_distance, self.move_distance, self.min_y, self.max_y)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("reach limits must be finite numbers")
        if not (self.edit_distance > 0 and self.move_distance > 0 and self.min_y < self.max_y):
            raise ValueError("reach limits must be positive and min_y below max_y")


# Actions whose parameters name an absolute target, by reach rule.
_REACH_KINDS: dict[ActionType, str] = {
    ActionType.TELEPORT: "move",
    ActionType.MOVE_TO: "move",
    ActionType.WALK_TO: "walk",
    ActionType.BREAK_BLOCK: "edit",
    ActionType.PLACE_BLOCK: "edit",
}


class ControllerService:
    """Authenticated observation and action API for a single Steve."""

    def __init__(
        self,
        world: WorldAdapter,
        authz: AuthzService,
        steve_id: str,
        *,
        world_admin_token: str | None = None,
        session_lease_seconds: float = 300,
        safety_authority: object | None = None,
        outcome_store: SqliteActionOutcomeStore | None = None,
        reach_limits: ReachLimits | None = None,
        event_journal: EventJournal | None = None,
    ) -> None:
        if session_lease_seconds <= 0:
            raise ValueError("controller session lease must be positive")
        self._world = world
        self._authz = authz
        self._steve_id = steve_id
        self._lease_seconds = session_lease_seconds
        self._reach = reach_limits or ReachLimits()
        self._journal = event_journal
        self._sessions: dict[str, AuthzControllerSession] = {}
        # The requesting session for the current task. ActionService's policy
        # reads it, so concurrent requests from different sessions cannot see
        # each other's authority.
        self._current: ContextVar[AuthzControllerSession | None] = ContextVar(
            f"controller_session_{steve_id}", default=None
        )
        self._authz_policy = authz.action_policy(
            self._current.get,
            admin_token=(world_admin_token or "").strip() or None,
            blocking=True,
            require_delegation=True,
        )
        self._action_service = ActionService(
            world,
            policy=self._policy,
            safety_authority=safety_authority,
            outcome_store=outcome_store,
            dispatch_guard=self._reach_guard,
        )
        self._observer = Observer(world)

    async def aclose(self) -> None:
        """Wait for pending durable outcome writes; call before shutdown."""
        await self._action_service.flush()

    @property
    def steve_id(self) -> str:
        """The Steve this service controls."""
        return self._steve_id

    @property
    def action_service(self) -> ActionService:
        """The Steve's action gate, for sharing with an in-process safety layer."""
        return self._action_service

    # ── Sessions ─────────────────────────────────────────────────────

    async def open_session(self, token: str) -> ControllerSession:
        """Acquire the Steve's exclusive controller session for ``token``.

        Raises ``PermissionError`` if the credential lacks ``steve:control``
        for this Steve or another controller session is active.
        """

        def acquire() -> AuthzControllerSession:
            credential = self._authz.authenticate(token)
            return self._authz.acquire_session(
                token,
                self._steve_id,
                owner_id=credential.owner_id,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            )

        session = await asyncio.to_thread(acquire)
        self._publish(session)
        logger.info(
            "Controller session opened steve_id=%s credential_id=%s generation=%d",
            session.steve_id,
            session.credential_id,
            session.generation,
        )
        await self._record_controller_change(session, "opened")
        return self._contract(session)

    async def renew_session(self, token: str, session_id: str) -> ControllerSession:
        """Extend a still-valid session's lease; expired sessions stay expired."""
        session = await self._owned_session(token, session_id)
        try:
            renewed = await asyncio.to_thread(
                self._authz.renew_session, session, lease_seconds=self._lease_seconds
            )
        except PermissionError:
            await self._forget_if_invalid(session)
            raise
        # Also restores an entry pruned while the renewal was in flight (a
        # request with the old lease saw it as invalid).
        self._publish(renewed)
        return self._contract(renewed)

    async def close_session(self, token: str, session_id: str) -> None:
        """Release the session so another controller can take over."""
        session = await self._owned_session(token, session_id)
        self._sessions.pop(session_id, None)
        await asyncio.to_thread(self._authz.release_session, session)
        await self._record_controller_change(session, "released")

    # ── Observation ──────────────────────────────────────────────────

    async def get_capabilities(self, token: str) -> Capabilities:
        await self._require_observer(token)
        return self._world.capabilities

    async def get_state(self, token: str) -> dict[str, Any]:
        """Return a world-state snapshot as a JSON-compatible mapping."""
        await self._require_observer(token)
        state = await self._observer.observe()
        return dataclasses.asdict(state)

    # ── Events ───────────────────────────────────────────────────────

    async def get_events(
        self, token: str, *, limit: int = 100, reset_expired: bool = False
    ) -> EventPage:
        """Return the caller's next unacknowledged events for this Steve.

        Requires ``events:read`` on a credential bound to this Steve; no
        session is needed. Polling does not advance the cursor; call
        ``ack_events`` after processing. If retention dropped events the
        caller had not acknowledged, this raises ``EventCursorExpiredError``
        unless ``reset_expired`` is set, which resumes from the oldest
        retained event and reports ``reset=True``.
        """
        journal = self._require_journal()
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_EVENTS_PER_FETCH:
            raise ValueError(f"limit must be between 1 and {MAX_EVENTS_PER_FETCH}")
        consumer = await self._event_consumer(token)

        def poll() -> EventPage:
            reset = False
            for _ in range(_MAX_SKIP_PAGES):
                try:
                    records = journal.get_events(consumer, limit=MAX_EVENTS_PER_FETCH)
                except EventCursorExpiredError:
                    if not reset_expired or reset:
                        raise
                    journal.reset_consumer(consumer)
                    reset = True
                    continue
                visible: list[EventRecord] = []
                skip_through = 0
                for record in records:
                    if record.event.steve_id == self._steve_id:
                        visible.append(record)
                        if len(visible) == limit:
                            break
                    else:
                        skip_through = record.cursor
                if visible or not records:
                    return EventPage(visible, reset)
                # The page held only events this caller never sees, so
                # acknowledging them hides nothing.
                journal.ack_event(consumer, skip_through)
            return EventPage([], reset)

        return await asyncio.to_thread(poll)

    async def ack_events(self, token: str, cursor: int) -> None:
        """Acknowledge every event up to and including ``cursor``."""
        journal = self._require_journal()
        consumer = await self._event_consumer(token)
        await asyncio.to_thread(journal.ack_event, consumer, cursor)

    # ── Actions ──────────────────────────────────────────────────────

    async def submit_action(self, token: str, request: ActionRequestSchema) -> ActionResultSchema:
        """Run one action through the Steve's gate under the caller's session.

        Authentication failures raise ``PermissionError``. A request that does
        not satisfy ``ActionRequestSchema`` raises ``ValueError`` before any
        dispatch. Every other refusal is returned as a ``rejected`` result with
        a ``reason_code``.
        """
        credential = await asyncio.to_thread(self._authz.authenticate, token)
        request = self._revalidate(request)
        session = self._sessions.get(request.controller_session_id)
        self._drop_expired()
        rejection = self._envelope_rejection(credential.credential_id, request, session)
        if rejection is not None:
            return self._result(
                credential.credential_id, request, ActionStatus.REJECTED, *rejection
            )
        assert session is not None

        correlation_id = str(uuid4())
        context = self._current.set(session)
        try:
            result = await self._action_service.execute(
                request.action,
                request.params,
                correlation_id=correlation_id,
                request_id=request.request_id,
                # Yields to safety reflexes when preemption is enabled
                # (ControllerSafety); otherwise it has no effect.
                preemptible=True,
            )
        finally:
            self._current.reset(context)

        status = self._status(correlation_id, result.success)
        if result.success:
            reason_code = None
        elif result.data.get("interrupted") is True:
            status = ActionStatus.INTERRUPTED
            reason_code = "SAFETY_INTERRUPTED"
        elif isinstance(result.data.get("reason_code"), str):
            # A dispatch-guard refusal (e.g. OUT_OF_REACH) or a safety
            # preemption (SAFETY_PREEMPTED), including when replayed from
            # the idempotency cache. Neither reached the backend.
            status = ActionStatus.REJECTED
            reason_code = result.data["reason_code"]
        else:
            reason_code = {
                ActionStatus.REJECTED: "POLICY_REJECTED",
                ActionStatus.FAILED: "ACTION_FAILED",
            }.get(status)
            if reason_code == "POLICY_REJECTED":
                await self._forget_if_invalid(session)
        return self._result(credential.credential_id, request, status, reason_code, result.message)

    # ── Helpers ──────────────────────────────────────────────────────

    async def _policy(self, request: ActionRequest) -> PolicyDecision:
        """Evaluate the blocking Authz policy in a worker thread.

        The default Authz policy denies when its lock is busy, which would
        reject concurrent requests from a valid session because each request
        authenticates under that lock. The blocking variant waits instead, so
        it runs off the event loop. ``asyncio.to_thread`` copies the context,
        so the requesting session is still the one bound in ``_current``. A
        storage error still fails closed through ActionService.
        """
        return await asyncio.to_thread(self._authz_policy, request)

    async def _reach_guard(self, request: ActionRequest) -> GuardRefusal | None:
        """ActionService dispatch guard: refuse targets out of reach.

        Runs under the dispatch lock immediately before the backend call, so
        no other request through this service can move the bot in between; it
        is never applied to replays of completed requests. Movement from
        outside this service (other processes, players, physics) is not
        fenced; see docs/ARCHITECTURE.md.
        """
        try:
            message = await self._reach_violation(request.action, request.params)
        except ValueError as exc:
            # Fail closed: handlers coerce some values this parser rejects
            # (e.g. int(True)), so an unparsed target must never pass.
            return GuardRefusal("INVALID_TARGET", f"target coordinates are not valid: {exc}")
        return None if message is None else GuardRefusal("OUT_OF_REACH", message)

    async def _reach_violation(self, action: ActionType, params: Mapping[str, Any]) -> str | None:
        """Return why a targeted action is out of reach, or None if it is in reach.

        Only actions with absolute targets are checked; relative steps and
        edits next to the bot are already bounded. Raises ``ValueError`` for
        coordinates that are not finite numbers, which the guard refuses.
        """
        kind = _REACH_KINDS.get(action)
        if kind is None:
            return None
        if kind == "edit" and not all(axis in params for axis in ("x", "y", "z")):
            return None
        # Same defaults as the handlers: move_to treats missing axes as 0, and
        # walk_to treats a missing or null y as "keep the current height".
        x = _finite_coordinate(params.get("x", 0), "x")
        z = _finite_coordinate(params.get("z", 0), "z")
        if kind == "walk" and params.get("y") is None:
            y = None
        else:
            y = _finite_coordinate(params.get("y", 0), "y")
        position = await self._world.get_player_pos()
        if position is None:
            return "the bot's position is unknown, so reach cannot be checked"
        limits = self._reach
        if kind == "edit":
            assert y is not None
            # The handlers edit the block at int(coordinate); measure that
            # block, not the fractional target, so truncation cannot push an
            # edit past the limit.
            distance = math.dist(position, (int(x), int(y), int(z)))
            if distance > limits.edit_distance:
                return (
                    f"target is {distance:.1f} blocks away; edits are limited to "
                    f"{limits.edit_distance:g}"
                )
            return None
        if y is not None and not limits.min_y <= y <= limits.max_y:
            return f"target y={y:g} is outside {limits.min_y:g}..{limits.max_y:g}"
        if kind == "move" and y is not None:
            distance = math.dist(position, (x, y, z))
            limit = limits.move_distance
        else:
            distance = math.dist((position[0], position[2]), (x, z))
            # Beyond its walking range walk_to teleports instead; a
            # controller's walk must stay a walk.
            limit = (
                min(limits.move_distance, WALK_TO_TELEPORT_DISTANCE)
                if kind == "walk"
                else limits.move_distance
            )
        if distance > limit:
            noun = "walking" if kind == "walk" else "movement"
            return f"target is {distance:.1f} blocks away; {noun} is limited to {limit:g}"
        return None

    @staticmethod
    def _revalidate(request: ActionRequestSchema) -> ActionRequestSchema:
        """Validate the request's field values again at the trust boundary.

        Pydantic does not validate ``model_construct()`` or
        ``model_copy(update=...)``, so an instance's type alone does not prove
        its fields are valid.
        """
        try:
            return ActionRequestSchema.model_validate(dict(request))
        except (ValidationError, TypeError) as exc:
            raise ValueError(f"invalid action request: {exc}") from None

    def _envelope_rejection(
        self,
        credential_id: str,
        request: ActionRequestSchema,
        session: AuthzControllerSession | None,
    ) -> tuple[str, str] | None:
        if request.steve_id != self._steve_id:
            return "STEVE_MISMATCH", f"this service controls {self._steve_id!r}"
        if session is None:
            return "SESSION_UNKNOWN", "controller session is not open on this service"
        if credential_id != session.credential_id:
            return "SESSION_NOT_OWNED", "controller session belongs to another credential"
        if request.generation != session.generation:
            return "STALE_GENERATION", "request generation does not match the session"
        if (
            request.action not in _EXPOSED_ACTIONS
            or classify_action(request.action) not in _EXPOSABLE_CLASSES
        ):
            return "ACTION_NOT_EXPOSED", f"{request.action.value} is not a controller action"
        return None

    def _publish(self, session: AuthzControllerSession) -> None:
        """Cache ``session`` as the only entry unless a newer one is cached.

        Authz allows one session per Steve and raises the Steve's generation
        on every acquisition and fence, so ``session`` has fenced every lower
        generation. Open and renew responses can arrive out of order; if a
        higher generation is already cached, ``session`` is already fenced
        and must not displace it.
        """
        if all(cached.generation <= session.generation for cached in self._sessions.values()):
            self._sessions = {session.session_id: session}

    def _drop_expired(self) -> None:
        """Forget sessions whose lease has passed; Authz never revives them."""
        now = datetime.now(UTC)
        for session_id, session in list(self._sessions.items()):
            if session.expires_at is not None and session.expires_at <= now:
                del self._sessions[session_id]

    async def _forget_if_invalid(self, session: AuthzControllerSession) -> None:
        """Forget ``session`` if Authz no longer considers it valid.

        Depends only on the session's own state, never on who is asking, so
        another credential cannot evict it. The identity check after
        validation keeps an entry that a concurrent renewal replaced.
        """
        if self._sessions.get(session.session_id) is not session:
            return  # already gone or replaced; skip the read
        try:
            valid = await asyncio.to_thread(self._authz.validate_session, session)
        except sqlite3.Error:
            return  # unverifiable now; a later rejection or expiry removes it
        if not valid and self._sessions.get(session.session_id) is session:
            del self._sessions[session.session_id]

    def _require_journal(self) -> EventJournal:
        if self._journal is None:
            raise ValueError("this service does not record events")
        return self._journal

    async def _event_consumer(self, token: str) -> str:
        """Return the caller's journal consumer after checking ``events:read``.

        The credential must be bound to this Steve; ``events:read`` alone is
        service level and would otherwise admit another Steve's credential.
        """

        def consumer() -> str:
            credential = self._authz.authenticate(token)
            if credential.steve_id != self._steve_id or not self._authz.check_scope(
                token, _EVENTS_SCOPE, steve_id=self._steve_id
            ):
                raise PermissionError(f"{_EVENTS_SCOPE} for this Steve required")
            return f"controller-{credential.credential_id}"

        return await asyncio.to_thread(consumer)

    async def _record_controller_change(self, session: AuthzControllerSession, change: str) -> None:
        """Journal a ``controller.changed`` event; never fails the session call."""
        if self._journal is None:
            return
        event = Event(
            event_id=f"evt-{uuid4()}",
            type="controller.changed",
            severity=EventSeverity.INFO,
            steve_id=self._steve_id,
            source="controller_service",
            payload={
                "change": change,
                "controller_id": session.credential_id,
                "generation": session.generation,
            },
        )
        try:
            await asyncio.to_thread(self._journal.append, event)
        except (EventJournalError, sqlite3.Error):
            logger.exception("Failed to record controller.changed event")

    def _token_owns(self, token: str, session: AuthzControllerSession) -> bool:
        try:
            credential = self._authz.authenticate(token)
        except PermissionError:
            return False
        return credential.credential_id == session.credential_id

    async def _owned_session(self, token: str, session_id: str) -> AuthzControllerSession:
        session = self._sessions.get(session_id)
        if session is None or not await asyncio.to_thread(self._token_owns, token, session):
            raise PermissionError("controller session is not open for this credential")
        return session

    async def _require_observer(self, token: str) -> None:
        def allowed() -> bool:
            return any(
                self._authz.check_scope(token, scope, steve_id=self._steve_id)
                for scope in _OBSERVE_SCOPES
            )

        if not await asyncio.to_thread(allowed):
            raise PermissionError("observe scope for this Steve required")

    def _status(self, correlation_id: str, success: bool) -> ActionStatus:
        if success:
            return ActionStatus.SUCCEEDED
        # Rejections and backend failures share the ActionResult shape; the
        # audit record for this call records whether policy allowed it.
        # Replays add no record and keep the failed status.
        for record in reversed(self._action_service.audit_records):
            if record.correlation_id == correlation_id:
                return ActionStatus.FAILED if record.allowed else ActionStatus.REJECTED
        return ActionStatus.FAILED

    def _result(
        self,
        credential_id: str,
        request: ActionRequestSchema,
        status: ActionStatus,
        reason_code: str | None,
        message: str,
    ) -> ActionResultSchema:
        return ActionResultSchema(
            action_id=_action_id(request.steve_id, credential_id, request.request_id),
            steve_id=request.steve_id,
            request_id=request.request_id,
            status=status,
            reason_code=reason_code,
            message=message,
            task_id=request.task_id,
            completed_at=datetime.now(UTC),
        )

    @staticmethod
    def _contract(session: AuthzControllerSession) -> ControllerSession:
        return ControllerSession(
            session_id=session.session_id,
            steve_id=session.steve_id,
            controller_id=session.credential_id,
            generation=session.generation,
            expires_at=session.expires_at,
            status=SessionStatus.ACTIVE,
        )


def _action_id(steve_id: str, credential_id: str, request_id: str) -> str:
    """Return the stable ID of the logical action a request ID names.

    RFC-0001 treats action IDs as durable across reconnects, so replays and
    retries of one request ID from one credential share an ID, including
    after a restart. It is derived rather than stored, so it needs no
    persistence.
    """
    name = "\x00".join((steve_id, credential_id, request_id))
    return f"act-{uuid5(_ACTION_ID_NAMESPACE, name)}"


_ACTION_ID_NAMESPACE = UUID("6f0d3c1e-6a0b-5c5e-9a8e-4a1d5f2b7c90")

__all__ = ["ControllerService", "EventPage", "ReachLimits"]
