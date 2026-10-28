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

This is not a transport, task service, or event API, and it does not run the
safety supervisor; those are separate slices.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from ..minecraft.actions import ActionType
from ..minecraft.observer import Observer
from ..minecraft.world_types import WorldAdapter
from .action_outcomes import SqliteActionOutcomeStore
from .action_service import (
    ActionClass,
    ActionRequest,
    ActionService,
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
    SessionStatus,
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
    ) -> None:
        if session_lease_seconds <= 0:
            raise ValueError("controller session lease must be positive")
        self._world = world
        self._authz = authz
        self._steve_id = steve_id
        self._lease_seconds = session_lease_seconds
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
        self._sessions[session.session_id] = session
        logger.info(
            "Controller session opened steve_id=%s credential_id=%s generation=%d",
            session.steve_id,
            session.credential_id,
            session.generation,
        )
        return self._contract(session)

    async def renew_session(self, token: str, session_id: str) -> ControllerSession:
        """Extend a still-valid session's lease; expired sessions stay expired."""
        session = await self._owned_session(token, session_id)
        renewed = await asyncio.to_thread(
            self._authz.renew_session, session, lease_seconds=self._lease_seconds
        )
        self._sessions[session_id] = renewed
        return self._contract(renewed)

    async def close_session(self, token: str, session_id: str) -> None:
        """Release the session so another controller can take over."""
        session = await self._owned_session(token, session_id)
        self._sessions.pop(session_id, None)
        await asyncio.to_thread(self._authz.release_session, session)

    # ── Observation ──────────────────────────────────────────────────

    async def get_capabilities(self, token: str) -> Capabilities:
        await self._require_observer(token)
        return self._world.capabilities

    async def get_state(self, token: str) -> dict[str, Any]:
        """Return a world-state snapshot as a JSON-compatible mapping."""
        await self._require_observer(token)
        state = await self._observer.observe()
        return dataclasses.asdict(state)

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
            )
        finally:
            self._current.reset(context)

        status = self._status(correlation_id, result.success)
        reason_code = {
            ActionStatus.REJECTED: "POLICY_REJECTED",
            ActionStatus.FAILED: "ACTION_FAILED",
        }.get(status)
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

__all__ = ["ControllerService"]
