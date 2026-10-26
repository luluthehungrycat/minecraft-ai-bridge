"""Shared policy boundary for mediated Minecraft actions.

The service wraps the existing dispatcher with action classification, policy
hooks, bounded in-memory audit, and process-local request-ID idempotency. Its
in-process lifecycle and safety capabilities are narrow trust boundaries for
the built-in orchestrator; this is not external transport authentication or
complete mediation of callers that still use the dispatcher directly.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isawaitable
from math import isfinite
from typing import Any
from uuid import uuid4

from ..minecraft.actions import (
    _HOSTILE_MOBS,
    ActionResult,
    ActionType,
    _is_food,
    _should_attack,
    execute_action,
    safety_interrupt_scope,
)
from ..minecraft.world_types import WorldAdapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionRequest:
    """A strategic action and its correlation metadata."""

    action: ActionType
    params: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trusted_internal: bool = False
    trusted_safety: bool = False


class ActionClass(StrEnum):
    """Authoritative runtime classification used before backend dispatch."""

    OBSERVATION = "observation"
    SURVIVAL = "survival"
    PRIVILEGED_SIMULATED = "privileged_simulated"
    INTERNAL_CONTROL = "internal_control"
    LIFECYCLE_INTERNAL = "lifecycle_internal"
    # Not listed in any class below; the gate rejects it before policy.
    UNCLASSIFIED = "unclassified"


_OBSERVATION_ACTIONS = frozenset(
    {
        ActionType.CHECK_INVENTORY,
        ActionType.SCAN_ENTITIES,
        ActionType.SCAN,
        ActionType.CHECK_TIME,
        ActionType.CHECK_WEATHER,
        ActionType.CHECK_HEALTH,
        ActionType.CHECK_HUNGER,
        ActionType.CHECK_POSITION,
        ActionType.LIST_PLAYERS,
    }
)
_PRIVILEGED_SIMULATED_ACTIONS = frozenset(
    {
        # The MCPQ implementation moves by issuing privileged teleport
        # commands, even when the action is named as ordinary movement.
        ActionType.MOVE_TO,
        ActionType.MOVE_FORWARD,
        ActionType.MOVE_BACK,
        ActionType.WALK_TO,
        ActionType.TURN_LEFT,
        ActionType.TURN_RIGHT,
        ActionType.JUMP,
        ActionType.SPRINT,
        ActionType.TELEPORT,
        # These handlers edit blocks/inventory or simulate survival effects
        # through server commands rather than genuine player interactions.
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
_INTERNAL_CONTROL_ACTIONS = frozenset(
    {
        ActionType.FOLLOW_TELEPORT,
        ActionType.GOTO_TELEPORT,
        ActionType.COME_TELEPORT,
    }
)
_LIFECYCLE_INTERNAL_ACTIONS = frozenset({ActionType.PROVISION_BOT, ActionType.PREPARE_SAFE_SPAWN})
# Actions with no world effect beyond chat. A new ActionType must be added to
# exactly one set; unlisted actions fail closed instead of defaulting here.
_SURVIVAL_ACTIONS = frozenset({ActionType.CHAT, ActionType.WAIT, ActionType.DONE})
_PRIVILEGED_AUTHORIZATION_CLASSES = frozenset(
    {
        ActionClass.PRIVILEGED_SIMULATED,
        ActionClass.INTERNAL_CONTROL,
        ActionClass.LIFECYCLE_INTERNAL,
        ActionClass.UNCLASSIFIED,
    }
)
_SAFETY_ACTIONS = frozenset({ActionType.ATTACK, ActionType.EAT, ActionType.HEAL})


def classify_action(action: ActionType) -> ActionClass:
    """Return the security class enforced by ActionService for an action."""
    if action in _OBSERVATION_ACTIONS:
        return ActionClass.OBSERVATION
    if action in _PRIVILEGED_SIMULATED_ACTIONS:
        return ActionClass.PRIVILEGED_SIMULATED
    if action in _INTERNAL_CONTROL_ACTIONS:
        return ActionClass.INTERNAL_CONTROL
    if action in _LIFECYCLE_INTERNAL_ACTIONS:
        return ActionClass.LIFECYCLE_INTERNAL
    if action in _SURVIVAL_ACTIONS:
        return ActionClass.SURVIVAL
    return ActionClass.UNCLASSIFIED


@dataclass(frozen=True)
class PolicyDecision:
    """Result returned by an action policy hook."""

    allowed: bool
    reason: str = ""
    allow_privileged: bool = False
    # Stable identity returned by trusted authorization policy, never copied
    # from untrusted action parameters.
    identity_scope: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ActionAuditRecord:
    """In-memory trace for one strategic action request."""

    correlation_id: str
    action: ActionType
    allowed: bool
    succeeded: bool
    reason: str = ""
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = None


ActionPolicy = Callable[[ActionRequest], PolicyDecision | Awaitable[PolicyDecision]]


@dataclass(frozen=True)
class ActionOutcome:
    """Terminal outcome stored for an explicit action request ID.

    ``status`` is ``"succeeded"``, ``"failed"``, ``"rejected"``,
    ``"policy_error"``, or ``"unknown"``. An ``"unknown"`` status means the
    backend dispatch was interrupted (for example by coroutine cancellation)
    after it started, so whether the mutation was applied cannot be known
    from this process.

    ``allowed`` records the permission decision made when this outcome was
    produced, so replay audits report the original decision instead of
    inferring it from ``status`` (a pre-dispatch cancellation stores
    ``status="failed"`` with ``allowed=False``, for example).
    """

    result: ActionResult
    status: str
    allowed: bool


@dataclass(frozen=True)
class _InflightEntry:
    """Fingerprint and completion future for a dispatch in progress."""

    fingerprint: str
    identity_scope: tuple[str, ...]
    future: asyncio.Future[ActionOutcome]


@dataclass(frozen=True)
class _TerminalEntry:
    """Fingerprint and outcome for a finished request ID."""

    fingerprint: str
    identity_scope: tuple[str, ...]
    outcome: ActionOutcome


def _validate_params(params: Mapping[str, Any]) -> None:
    """Reject action params that cannot be fingerprinted deterministically.

    Only JSON values are accepted: ``None``, booleans, integers, finite
    floats, strings, lists, and mappings with string keys. Anything else
    (for example ``bytes``, ``tuple``, or arbitrary objects) is refused
    with ``ValueError`` before policy evaluation or dispatch so that two
    distinct payloads can never be fingerprinted as equal: the canonical
    form keeps the concrete container type of every accepted value, so a
    JSON array and any other same-shaped container can never collide.
    """

    def _check(value: Any) -> None:
        if value is None or isinstance(value, bool | int | str):
            return
        if isinstance(value, float):
            if not isfinite(value):
                raise ValueError(
                    "action params values must be JSON values (None, bool, "
                    "int, finite float, str, list, or string-keyed mapping); "
                    f"got non-finite float {value!r}"
                )
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"action params mapping keys must be strings, got {type(key).__name__}"
                    )
                _check(item)
            return
        if isinstance(value, list):
            for item in value:
                _check(item)
            return
        raise ValueError(
            "action params values must be JSON values (None, bool, int, "
            "finite float, str, list, or string-keyed mapping); got "
            f"{type(value).__name__}"
        )

    _check(params)


def _request_fingerprint(action: ActionType, params: Mapping[str, Any]) -> str:
    """Return a canonical fingerprint of the action and its parameters.

    Params must already be validated by :func:`_validate_params`, so every
    value serializes deterministically and keeps its concrete container
    type: mappings are sorted by their string keys and lists stay lists.
    Distinct validated payloads always produce distinct fingerprints.
    """

    def _canonicalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: _canonicalize(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [_canonicalize(item) for item in value]
        return value

    canonical = json.dumps(
        {"action": str(action.value), "params": _canonicalize(dict(params))},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _allow_all(_request: ActionRequest) -> PolicyDecision:
    """Preserve current CLI behavior until a deployment supplies policy."""

    return PolicyDecision(allowed=True, allow_privileged=True)


class ActionService:
    """Policy hook, bounded audit trace, and process-local idempotency
    around the existing action dispatcher.

    Idempotency applies only when callers pass an explicit ``request_id``:
    identical action+params payloads replay the recorded terminal outcome,
    conflicting payloads are refused before any dispatch, and concurrent
    identical requests share one backend dispatch. Records are in-memory
    only: they are lost on process restart, and a request ID whose terminal
    record was evicted (or never stored because of a restart) may dispatch
    again. The default policy intentionally preserves existing behavior.
    The orchestrator's self-preservation layer routes its mutating reflexes
    (reflex ATTACK, auto EAT, auto HEAL), and the built-in chat command
    handler routes its !goto/!come teleports, through this boundary; other
    direct MCPQ mutations remain outside it, and this is not independent
    safety enforcement. Follow-mode teleports, trusted startup provisioning,
    and the chat-command teleports (goto_teleport/come_teleport) are routed
    through it as distinct internal actions, while read-only
    observation calls remain direct.

    Params passed with an explicit ``request_id`` must be JSON values
    (anything else, including tuples, raises ``ValueError`` before policy
    evaluation or dispatch). Accepted params are deep-copied into an
    independent snapshot before validation, fingerprinting, policy
    evaluation, and dispatch, so later caller-side mutation of the original
    mapping cannot change what the policy sees, what is dispatched, or what
    the fingerprint covers. With an explicit ``request_id``, the same
    validated snapshot is used for fingerprinting, policy evaluation, and
    backend dispatch, so the fingerprinted, policy-checked, and dispatched
    payload semantics are always identical. Calls without an explicit
    ``request_id`` are not validated, and their raw deep-copied snapshot is
    used for policy and dispatch exactly as before.
    """

    def __init__(
        self,
        world: WorldAdapter,
        policy: ActionPolicy | None = None,
        *,
        max_terminal_records: int = 1024,
        internal_authority: object | None = None,
        safety_authority: object | None = None,
    ) -> None:
        self._world = world
        self._policy = policy or _allow_all
        self._default_identity_scope = ("service", str(uuid4()))
        self._internal_authority = (
            internal_authority if internal_authority is not None else object()
        )
        self._safety_authority = safety_authority if safety_authority is not None else object()
        self._safety_identity_scope = ("safety", str(uuid4()))
        self._max_terminal_records = max_terminal_records
        # Serialize dispatch coroutines; WAIT is deliberately exempt so its
        # non-mutating sleep cannot block a safety reflex. Cancelling an
        # awaiter cannot stop MCPQ work already running in asyncio.to_thread,
        # so this lock cannot fence that work.
        self._dispatch_lock = asyncio.Lock()
        self._preemption_generation = 0
        self._acknowledged_preemption_generation = 0
        self._safety_preemption_enabled = False
        self._inflight: dict[str, _InflightEntry] = {}
        self._terminal: OrderedDict[str, _TerminalEntry] = OrderedDict()
        # Request IDs whose backend dispatch has started. A request ID stays
        # here after a cancellation so the cancellation handler can tell a
        # dispatch-interrupted outcome from a pre-dispatch one; it is removed
        # whenever that request reaches any terminal outcome (including a
        # cancellation-stored one), so a later reuse of the same ID starts
        # with clean dispatch state.
        self._dispatch_started: set[str] = set()
        self.audit_records: deque[ActionAuditRecord] = deque(maxlen=1024)

    async def execute(
        self,
        action: ActionType,
        params: Mapping[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        request_id: str | None = None,
        authority: object | None = None,
        safety_authority: object | None = None,
        preemptive: bool = False,
        preemptible: bool = False,
    ) -> ActionResult:
        """Evaluate policy, dispatch an allowed action, and record its result.

        With ``request_id=None`` this behaves exactly as before. With an
        explicit ``request_id``, results are replayed for identical
        action+params payloads, conflicting reuse fails without dispatching,
        and concurrent identical requests share a single dispatch (cancelling
        one coalesced waiter cancels only that waiter, not the shared
        dispatch or its other waiters). Params passed with an explicit
        ``request_id`` must be JSON values; anything else (including tuples)
        raises ``ValueError`` before policy evaluation or dispatch. Accepted
        are deep-copied into an independent snapshot before validation,
        fingerprinting, policy evaluation, and dispatch, so later caller-side
        mutation of the original mapping cannot change what the policy sees,
        what is dispatched, or what the fingerprint covers.
        """

        if preemptive and self._safety_preemption_enabled:
            self._preemption_generation += 1

        if request_id is None:
            request = ActionRequest(
                action=action,
                params=copy.deepcopy(dict(params or {})),
                correlation_id=correlation_id or str(uuid4()),
                trusted_internal=(
                    action in _INTERNAL_CONTROL_ACTIONS | _LIFECYCLE_INTERNAL_ACTIONS
                    and authority is self._internal_authority
                ),
                trusted_safety=(
                    action in _SAFETY_ACTIONS and safety_authority is self._safety_authority
                ),
            )
            if action is ActionType.WAIT:
                outcome = await self._dispatch(
                    request,
                    request_id=None,
                    preemptible=preemptible,
                )
            else:
                async with self._dispatch_lock:
                    outcome = await self._dispatch(
                        request,
                        request_id=None,
                        preemptible=preemptible,
                    )
            return outcome.result

        request = ActionRequest(
            action=action,
            params=copy.deepcopy(dict(params or {})),
            correlation_id=correlation_id or str(uuid4()),
            trusted_internal=(
                action in _INTERNAL_CONTROL_ACTIONS | _LIFECYCLE_INTERNAL_ACTIONS
                and authority is self._internal_authority
            ),
            trusted_safety=(
                action in _SAFETY_ACTIONS and safety_authority is self._safety_authority
            ),
        )
        _validate_params(request.params)
        fingerprint = _request_fingerprint(request.action, request.params)

        # Idempotency is an authorization-sensitive read. Check current
        # authority before consulting either cache and bind the record to an
        # identity emitted by the trusted policy, never by request params.
        decision, denied = await self._evaluate_policy(request, request_id=request_id)
        if denied is not None:
            return denied.result
        assert decision is not None
        identity_scope = decision.identity_scope or self._default_identity_scope

        entry = self._inflight.get(request_id)
        if entry is not None:
            if entry.identity_scope != identity_scope:
                return self._identity_conflict_result(request, request_id, in_flight=True)
            return await self._replay_inflight(
                entry, request, fingerprint, request_id, identity_scope
            )

        terminal = self._terminal.get(request_id)
        if terminal is not None:
            if terminal.identity_scope != identity_scope:
                return self._identity_conflict_result(request, request_id, in_flight=False)
            return self._replay_terminal(terminal, request, fingerprint, request_id)

        # No awaits occur between the checks above and this claim, so two
        # coroutines cannot both become the leader for one request_id.
        future: asyncio.Future[ActionOutcome] = asyncio.get_running_loop().create_future()
        self._inflight[request_id] = _InflightEntry(
            fingerprint=fingerprint,
            identity_scope=identity_scope,
            future=future,
        )

        try:
            if action is ActionType.WAIT:
                outcome = await self._dispatch(
                    request,
                    request_id=request_id,
                    preemptible=preemptible,
                    expected_identity_scope=identity_scope,
                )
            else:
                async with self._dispatch_lock:
                    outcome = await self._dispatch(
                        request,
                        request_id=request_id,
                        preemptible=preemptible,
                        expected_identity_scope=identity_scope,
                    )
        except asyncio.CancelledError:
            if request_id not in self._dispatch_started:
                # Cancellation arrived before the backend dispatch started
                # (for example while the policy hook was still running), so
                # no mutation can have been applied by this request. Record a
                # failed terminal outcome that says exactly that; never claim
                # a dispatch that did not happen and never record unknown.
                cancelled = ActionOutcome(
                    result=ActionResult(
                        success=False,
                        action=action,
                        message=(
                            "Action dispatch was cancelled before backend "
                            "dispatch started; no action was dispatched."
                        ),
                        data={
                            "correlation_id": request.correlation_id,
                            "request_id": request_id,
                        },
                    ),
                    status="failed",
                    allowed=False,
                )
                self._finish_inflight(request_id, cancelled)
                self._record(
                    request,
                    allowed=False,
                    succeeded=False,
                    reason=("cancelled before backend dispatch started; no action dispatched"),
                    request_id=request_id,
                )
                raise
            # The dispatch was interrupted after it was allowed to start.
            # execute_action runs MCPQ work via asyncio.to_thread, so the
            # underlying backend operation may still be running or may have
            # been applied; this process cannot know. Preserve an
            # outcome-unknown terminal record so the same request_id can
            # never repeat a possibly applied mutation.
            unknown = ActionOutcome(
                result=ActionResult(
                    success=False,
                    action=action,
                    message=(
                        "Action dispatch was interrupted after backend dispatch "
                        "started; outcome unknown. The same request_id will not "
                        "re-dispatch."
                    ),
                    data={
                        "correlation_id": request.correlation_id,
                        "request_id": request_id,
                    },
                ),
                status="unknown",
                allowed=True,
            )
            self._finish_inflight(request_id, unknown)
            self._record(
                request,
                allowed=True,
                succeeded=False,
                reason="dispatch interrupted after backend start; outcome unknown",
                request_id=request_id,
            )
            raise
        except Exception as exc:
            # execute_action converts handler exceptions into failed results,
            # so an exception here means the dispatch failed before a backend
            # result was produced. Record a failed terminal outcome.
            failed = ActionOutcome(
                result=ActionResult(
                    success=False,
                    action=action,
                    message=f"Action dispatch failed before completing: {exc}",
                    data={
                        "correlation_id": request.correlation_id,
                        "request_id": request_id,
                    },
                ),
                status="failed",
                allowed=True,
            )
            self._finish_inflight(request_id, failed)
            self._record(
                request,
                allowed=True,
                succeeded=False,
                reason=f"dispatch failed: {exc}",
                request_id=request_id,
            )
            raise
        outcome.result.data["request_id"] = request_id
        self._finish_inflight(request_id, outcome)
        return outcome.result

    @property
    def world(self) -> WorldAdapter:
        """Return the world adapter this service dispatches against.

        Read-only access for consumers that are handed the service alone
        (for example a harness constructing ChatCommandHandler without
        an orchestrator world reference) and still need the shared world
        surface for observations and chat replies.
        """

        return self._world

    def _safety_intervention_pending(self) -> bool:
        return self._preemption_generation != self._acknowledged_preemption_generation

    def acknowledge_safety_intervention(self) -> None:
        """Allow new strategic actions after the pending reflex is consumed."""
        self._acknowledged_preemption_generation = self._preemption_generation

    def enable_safety_preemption(self) -> None:
        """Enable the built-in supervisor's pre-dispatch invalidation gate."""
        self._safety_preemption_enabled = True

    def disable_safety_preemption(self) -> None:
        """Disable the runtime gate and retire any last intervention epoch."""
        self._acknowledged_preemption_generation = self._preemption_generation
        self._safety_preemption_enabled = False

    async def _replay_inflight(
        self,
        entry: _InflightEntry,
        request: ActionRequest,
        fingerprint: str,
        request_id: str,
        identity_scope: tuple[str, ...],
    ) -> ActionResult:
        """Coalesce onto an in-flight dispatch for the same request_id."""

        if entry.fingerprint != fingerprint:
            return self._conflict_result(request, request_id, in_flight=True)
        # Shield the shared future: cancelling this waiter must not cancel
        # the dispatch future that other coalesced waiters are awaiting.
        outcome = await asyncio.shield(entry.future)
        decision, denied = await self._evaluate_policy(request, request_id=request_id)
        if denied is not None:
            return denied.result
        assert decision is not None
        current_scope = decision.identity_scope or self._default_identity_scope
        if current_scope != identity_scope:
            return self._identity_conflict_result(request, request_id, in_flight=False)
        status = outcome.status
        self._record(
            request,
            allowed=outcome.allowed,
            succeeded=outcome.result.success,
            reason=f"replayed in-flight dispatch for request_id={request_id} (status={status})",
            request_id=request_id,
        )
        return self._result_from_outcome(outcome, request, request_id)

    def _replay_terminal(
        self,
        terminal: _TerminalEntry,
        request: ActionRequest,
        fingerprint: str,
        request_id: str,
    ) -> ActionResult:
        """Replay a stored terminal outcome for the same request_id."""

        if terminal.fingerprint != fingerprint:
            return self._conflict_result(request, request_id, in_flight=False)
        status = terminal.outcome.status
        self._record(
            request,
            allowed=terminal.outcome.allowed,
            succeeded=terminal.outcome.result.success,
            reason=f"replayed terminal dispatch for request_id={request_id} (status={status})",
            request_id=request_id,
        )
        return self._result_from_outcome(terminal.outcome, request, request_id)

    def _conflict_result(
        self, request: ActionRequest, request_id: str, *, in_flight: bool
    ) -> ActionResult:
        """Fail conflicting request_id reuse without dispatching."""

        state = "in flight" if in_flight else "completed"
        reason = (
            f"request_id {request_id!r} is already {state} with a different action or parameters"
        )
        self._record(
            request,
            allowed=False,
            succeeded=False,
            reason=reason,
            request_id=request_id,
        )
        result = ActionResult(success=False, action=request.action, message=reason)
        result.data["correlation_id"] = request.correlation_id
        result.data["request_id"] = request_id
        return result

    def _identity_conflict_result(
        self, request: ActionRequest, request_id: str, *, in_flight: bool
    ) -> ActionResult:
        """Refuse request-ID reuse by a different authorized identity."""
        state = "in flight" if in_flight else "completed"
        reason = f"request_id {request_id!r} is already {state} for another authorized identity"
        self._record(
            request,
            allowed=False,
            succeeded=False,
            reason=reason,
            request_id=request_id,
        )
        result = ActionResult(success=False, action=request.action, message=reason)
        result.data["correlation_id"] = request.correlation_id
        result.data["request_id"] = request_id
        return result

    def _result_from_outcome(
        self,
        outcome: ActionOutcome,
        request: ActionRequest,
        request_id: str,
    ) -> ActionResult:
        """Copy a stored outcome so callers cannot mutate shared state."""

        result = self._snapshot_result(outcome.result)
        result.data["correlation_id"] = request.correlation_id
        result.data["request_id"] = request_id
        result.data["replayed"] = True
        return result

    @staticmethod
    def _snapshot_result(result: ActionResult) -> ActionResult:
        """Return an isolated deep copy of a result for storage or replay."""

        return ActionResult(
            success=result.success,
            action=result.action,
            message=result.message,
            data=copy.deepcopy(result.data),
        )

    def _finish_inflight(self, request_id: str, outcome: ActionOutcome) -> None:
        """Resolve the in-flight entry and move its outcome to the terminal store."""

        # Every terminal path funnels through here (success, dispatch failure,
        # and both cancellation cases), so this is the single place that
        # retires the dispatch-started marker for the request ID.
        self._dispatch_started.discard(request_id)
        entry = self._inflight.pop(request_id, None)
        if entry is None:
            return
        # Snapshot before resolving the shared future. The leader still holds
        # the original outcome and returns its result to the caller, who may
        # mutate it; coalesced waiters read the payload carried by the future,
        # so that payload (and the terminal record) must be isolated.
        stored = ActionOutcome(
            result=self._snapshot_result(outcome.result),
            status=outcome.status,
            allowed=outcome.allowed,
        )
        if not entry.future.done():
            entry.future.set_result(stored)
        self._store_terminal(
            request_id,
            entry.fingerprint,
            entry.identity_scope,
            stored,
        )

    def _store_terminal(
        self,
        request_id: str,
        fingerprint: str,
        identity_scope: tuple[str, ...],
        outcome: ActionOutcome,
    ) -> None:
        """Store a terminal outcome, evicting oldest records when full.

        Only completed outcomes are stored here; in-flight entries live in a
        separate mapping and are never evicted. The stored outcome is an
        isolated snapshot, so later mutation of a returned result object
        cannot corrupt replays.
        """

        self._terminal[request_id] = _TerminalEntry(
            fingerprint=fingerprint,
            identity_scope=identity_scope,
            outcome=ActionOutcome(
                result=self._snapshot_result(outcome.result),
                status=outcome.status,
                allowed=outcome.allowed,
            ),
        )
        while len(self._terminal) > self._max_terminal_records:
            self._terminal.popitem(last=False)

    async def _dispatch(
        self,
        request: ActionRequest,
        *,
        request_id: str | None,
        preemptible: bool,
        expected_identity_scope: tuple[str, ...] | None = None,
    ) -> ActionOutcome:
        """Run policy evaluation, dispatch an allowed action, and audit it."""

        decision, denied = await self._evaluate_policy(request, request_id=request_id)
        if denied is not None:
            return denied
        assert decision is not None
        identity_scope = decision.identity_scope or self._default_identity_scope
        if expected_identity_scope is not None and identity_scope != expected_identity_scope:
            return self._rejection_outcome(
                request,
                request_id,
                "Action authorization identity changed before backend dispatch",
                status="rejected",
            )

        if (
            preemptible
            and request.action is not ActionType.WAIT
            and self._safety_preemption_enabled
            and self._preemption_generation != self._acknowledged_preemption_generation
        ):
            reason = "Action preempted by safety intervention before backend dispatch"
            result = ActionResult(success=False, action=request.action, message=reason)
            result.data["correlation_id"] = request.correlation_id
            if request_id is not None:
                result.data["request_id"] = request_id
            self._record(
                request,
                allowed=False,
                succeeded=False,
                reason=reason,
                request_id=request_id,
            )
            return ActionOutcome(result=result, status="preempted", allowed=False)

        if request_id is not None:
            # Mark the dispatch boundary before the backend call. Cancellation
            # before this point is a pre-dispatch cancellation; cancellation
            # after it is treated as an interrupted dispatch with unknown
            # outcome.
            self._dispatch_started.add(request_id)
        if preemptible and self._safety_preemption_enabled:
            # Multi-step handlers stop between steps once a reflex is waiting
            # for the dispatch lock, so a long walk cannot delay it.
            with safety_interrupt_scope(self._safety_intervention_pending):
                result = await execute_action(self._world, request.action, dict(request.params))
        else:
            result = await execute_action(self._world, request.action, dict(request.params))
        self._record(
            request,
            allowed=True,
            succeeded=result.success,
            reason="" if result.success else result.message,
            request_id=request_id,
        )
        status = "succeeded" if result.success else "failed"
        result.data.setdefault("correlation_id", request.correlation_id)
        return ActionOutcome(result=result, status=status, allowed=True)

    async def _evaluate_policy(
        self,
        request: ActionRequest,
        *,
        request_id: str | None,
    ) -> tuple[PolicyDecision | None, ActionOutcome | None]:
        """Evaluate current policy and audit denials/errors consistently."""
        action_class = classify_action(request.action)
        if action_class is ActionClass.UNCLASSIFIED:
            return None, self._rejection_outcome(
                request,
                request_id,
                f"{request.action.value} has no security classification",
                status="rejected",
            )
        if request.trusted_safety:
            if not self._safety_params_allowed(request.action, request.params):
                return None, self._rejection_outcome(
                    request,
                    request_id,
                    "Safety authority parameters exceed its deterministic action limits",
                    status="rejected",
                )
            return (
                PolicyDecision(
                    allowed=True,
                    reason="narrow built-in safety authority",
                    allow_privileged=True,
                    identity_scope=self._safety_identity_scope,
                ),
                None,
            )
        if (
            action_class in {ActionClass.INTERNAL_CONTROL, ActionClass.LIFECYCLE_INTERNAL}
            and not request.trusted_internal
        ):
            return None, self._rejection_outcome(
                request,
                request_id,
                f"{request.action.value} requires trusted runtime authority",
                status="rejected",
            )
        try:
            decision = self._policy(request)
            if isawaitable(decision):
                decision = await decision
        except Exception as exc:
            logger.exception("Action policy failed correlation_id=%s", request.correlation_id)
            return None, self._rejection_outcome(
                request,
                request_id,
                f"Action policy evaluation failed: {exc}",
                status="policy_error",
            )
        if not decision.allowed:
            return None, self._rejection_outcome(
                request,
                request_id,
                decision.reason or "Action rejected by policy",
                status="rejected",
            )
        if action_class in _PRIVILEGED_AUTHORIZATION_CLASSES and not decision.allow_privileged:
            return None, self._rejection_outcome(
                request,
                request_id,
                f"{request.action.value} requires privileged backend authorization "
                "(the action policy must return allow_privileged=True)",
                status="rejected",
            )
        return decision, None

    @staticmethod
    def _safety_params_allowed(action: ActionType, params: Mapping[str, Any]) -> bool:
        """Constrain the built-in emergency capability independently of caller policy."""
        if action is ActionType.ATTACK:
            target = params.get("entity_type")
            damage = params.get("damage_amount", 8)
            if (
                not isinstance(target, str)
                or isinstance(damage, bool)
                or not isinstance(damage, int)
            ):
                return False
            mob = target.lower().removeprefix("minecraft:")
            return mob in _HOSTILE_MOBS and _should_attack(mob) and 1 <= damage <= 8
        if action is ActionType.EAT:
            food = params.get("food_item")
            slot = params.get("slot")
            return bool(
                isinstance(food, str)
                and _is_food(food.lower().removeprefix("minecraft:"))
                and (
                    slot is None
                    or (isinstance(slot, int) and not isinstance(slot, bool) and 0 <= slot <= 35)
                )
            )
        if action is ActionType.HEAL:
            item = params.get("heal_item", "")
            slot = params.get("slot")
            return bool(
                isinstance(item, str)
                and item.lower().removeprefix("minecraft:")
                in {"golden_apple", "enchanted_golden_apple"}
                and (
                    slot is None
                    or (isinstance(slot, int) and not isinstance(slot, bool) and 0 <= slot <= 35)
                )
            )
        return False

    def _rejection_outcome(
        self,
        request: ActionRequest,
        request_id: str | None,
        reason: str,
        *,
        status: str,
    ) -> ActionOutcome:
        """Build and audit a fail-closed policy result."""
        self._record(
            request,
            allowed=False,
            succeeded=False,
            reason=reason,
            request_id=request_id,
        )
        result = ActionResult(success=False, action=request.action, message=reason)
        result.data["correlation_id"] = request.correlation_id
        if request_id is not None:
            result.data["request_id"] = request_id
        return ActionOutcome(result=result, status=status, allowed=False)

    def _record(
        self,
        request: ActionRequest,
        *,
        allowed: bool,
        succeeded: bool,
        reason: str,
        request_id: str | None = None,
    ) -> None:
        record = ActionAuditRecord(
            correlation_id=request.correlation_id,
            action=request.action,
            allowed=allowed,
            succeeded=succeeded,
            reason=reason,
            request_id=request_id,
        )
        self.audit_records.append(record)
        logger.info(
            "Action %s %s correlation_id=%s request_id=%s",
            request.action.value,
            "succeeded" if succeeded else "rejected" if not allowed else "failed",
            request.correlation_id,
            request_id if request_id is not None else "-",
        )
