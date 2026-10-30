"""RFC-0001 Phase A contract schemas for the harness-agnostic runtime.

Transport-independent Pydantic v2 data contracts: Steve identity,
controller sessions, backend capabilities, tasks, events, and the action
request/result envelope. These models define and validate the shape of
contract data only. Nothing in this module performs authorization,
fencing, persistence, event journaling, task scheduling, MCP/network
I/O, or any other runtime behavior; authorization, controller ownership,
durable task/event services, and the MCP surface are RFC-0001 Phase B
and later work.

Name mapping: the RFC contract names ActionRequest and ActionResult
collide with existing runtime types the dispatcher relies on
(runtime.action_service.ActionRequest and minecraft.actions.ActionResult,
both unchanged here). The canonical RFC schemas therefore use the
explicit ActionRequestSchema and ActionResultSchema names. No automatic
conversion between these schemas and the runtime dataclasses exists in
this slice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from ..minecraft.actions import ActionType

__all__ = [
    "ActionRequestSchema",
    "ActionResultSchema",
    "ActionStatus",
    "BackendKind",
    "Capabilities",
    "ControllerSession",
    "CraftingMode",
    "EntityId",
    "Event",
    "EventSeverity",
    "MovementMode",
    "SessionStatus",
    "SteveIdentity",
    "Task",
    "TaskStatus",
]

MAX_JSON_PAYLOAD_DEPTH = 64
"""Maximum nesting depth accepted for JSON payload data.

Counts every container from the outer payload mapping itself (the dict
held by ActionRequestSchema.params or Event.payload) down to its
innermost value: each dict or list on the path, including the outer
payload mapping, is one level. Scalars do not add depth. Values nested
deeper than MAX_JSON_PAYLOAD_DEPTH levels are rejected before model
construction instead of risking unbounded recursion during validation or
a serialization failure at deep nesting.
"""


def _utcnow() -> datetime:
    """Return the current timezone-aware UTC time (default factory)."""

    return datetime.now(UTC)


EntityId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
"""Non-empty opaque identifier (IDs and dotted event type names)."""


def _require_utf8(value: str, /, *, what: str) -> str:
    """Reject strings that cannot survive JSON serialization as UTF-8."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{what} must be valid UTF-8 (no unpaired surrogates)") from exc
    return value


def _validate_json_value(value: Any, /, depth: int = 1) -> Any:
    """Return the input unchanged when it is exactly JSON-serializable.

    Accepts only null, bool, int, finite float, UTF-8-encodable str,
    lists, and string-keyed mappings; nested values and mapping keys
    are validated recursively. Tuples, sets, non-finite floats, lone
    surrogates, non-string mapping keys, and arbitrary objects are
    rejected instead of coerced. Every dict or list counts as one level
    towards MAX_JSON_PAYLOAD_DEPTH (starting at the whole payload
    mapping); the depth check applies only to containers, so scalar
    leaves may sit just beyond the last container level, nesting stays
    bounded, and a successfully constructed contract payload always
    survives model_dump_json(); Pydantic passes the depth default
    explicitly, and bound-instance defaults are not shared between
    sibling containers.
    """

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("payload values must be finite floats")
        return value
    if isinstance(value, str):
        return _require_utf8(value, what="payload strings")
    if isinstance(value, list):
        if depth > MAX_JSON_PAYLOAD_DEPTH:
            raise ValueError(f"payload values must nest at most {MAX_JSON_PAYLOAD_DEPTH} levels")
        return [_validate_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise ValueError("payload mapping keys must be strings")
            _require_utf8(key, what="payload mapping keys")
        if depth > MAX_JSON_PAYLOAD_DEPTH:
            raise ValueError(f"payload values must nest at most {MAX_JSON_PAYLOAD_DEPTH} levels")
        return {key: _validate_json_value(item, depth + 1) for key, item in value.items()}
    raise ValueError(f"payload values must be JSON-compatible; got {type(value).__name__}")


def _validate_json_mapping(value: Any, /) -> Any:
    """Validate a whole payload mapping before Pydantic dict coercion.

    The dict[str, ...] annotation otherwise laxly coerces non-dict
    inputs and non-string keys (for example a list of pairs becomes a
    dict and a bytes key may normalize into an existing str key,
    losing an entry); rejecting the raw input first preserves the
    exact mapping. Values are validated recursively by
    _validate_json_value with container depth bounded by
    MAX_JSON_PAYLOAD_DEPTH, counted from this outer mapping.
    """

    if not isinstance(value, dict):
        raise ValueError("payload must be a string-keyed JSON object mapping")
    for key in value:
        if not isinstance(key, str):
            raise ValueError("payload mapping keys must be strings")
        _require_utf8(key, what="payload mapping keys")
    return {key: _validate_json_value(item, 2) for key, item in value.items()}


JsonValue = Annotated[Any, AfterValidator(_validate_json_value)]
JsonPayload = Annotated[dict[str, JsonValue], BeforeValidator(_validate_json_mapping)]


class BackendKind(StrEnum):
    """Backend hosting a Steve in-game identity (RFC-0001 sections 5-6)."""

    MCPQ = "mcpq"
    MINEFLAYER = "mineflayer"


class MovementMode(StrEnum):
    """Movement fidelity a backend can offer (RFC-0001 section 6)."""

    PHYSICAL = "physical"
    TELEPORT_SIMULATED = "teleport_simulated"
    UNSUPPORTED = "unsupported"


class CraftingMode(StrEnum):
    """Crafting fidelity a backend can offer (RFC-0001 section 6)."""

    VANILLA = "vanilla"
    PRIVILEGED_SIMULATED = "privileged_simulated"
    UNSUPPORTED = "unsupported"


class ActionStatus(StrEnum):
    """Action lifecycle states distinguished by RFC-0001 section 6."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """Task lifecycle states for the task contract (RFC-0001 section 9)."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventSeverity(StrEnum):
    """Event severity scale for the event envelope (RFC-0001 section 8)."""

    LOW = "low"
    INFO = "info"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SessionStatus(StrEnum):
    """Controller session lifecycle states (RFC-0001 section 5)."""

    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ContractModel(BaseModel):
    """Base for RFC-0001 contract schemas; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class SteveIdentity(ContractModel):
    """Stable logical identity of one Steve and its backend binding (section 5).

    A key is a credential, not part of the identity; credentials are
    deliberately absent from this schema until Phase B defines them.
    """

    steve_id: EntityId
    backend: BackendKind
    backend_identity: str | None = None
    display_name: str = Field(default="", max_length=256)
    created_at: AwareDatetime = Field(default_factory=_utcnow)
    active: bool = True


class ControllerSession(ContractModel):
    """One controller session bound to a Steve (RFC-0001 section 5).

    Schema record only: fencing enforcement, ownership expiry, takeover,
    and transfer policy are not implemented in this slice. generation is
    the monotonic fencing generation the RFC reserves for stale-command
    rejection in later phases.
    """

    session_id: EntityId
    steve_id: EntityId
    controller_id: EntityId
    generation: int = Field(ge=0)
    started_at: AwareDatetime = Field(default_factory=_utcnow)
    expires_at: AwareDatetime | None = None
    status: SessionStatus = SessionStatus.ACTIVE


class Capabilities(ContractModel):
    """Advertised backend capabilities and limitations (RFC-0001 section 6).

    Declarative backend ability and limitation data, not a controller
    authorization grant. Adapter-reported values may describe the
    implemented interface without discovering live world metadata or
    guaranteeing independent safety; the model itself infers nothing.
    """

    backend: BackendKind
    movement: MovementMode
    crafting: CraftingMode
    world_edit: bool = False
    max_interaction_distance: float | None = Field(default=None, ge=0)
    supported_versions: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    hazard_sensors: bool = False


class ActionRequestSchema(ContractModel):
    """Transport-independent action request envelope (RFC-0001 section 6).

    Canonical RFC contract shape. The runtime dispatcher still uses the
    frozen runtime.action_service.ActionRequest dataclass; no automatic
    conversion exists in this slice.
    params must be exactly JSON-compatible (null, bool, int, finite
    float, str, lists, string-keyed mappings, recursively); tuples,
    non-finite floats, non-string keys, unpaired surrogates, and
    arbitrary objects are rejected rather than coerced. The whole
    mapping is validated before Pydantic lax dict coercion, so
    non-dict inputs and non-string keys cannot be normalized.
    JSON nesting is bounded by MAX_JSON_PAYLOAD_DEPTH, counted from the
    params mapping itself, so model_dump_json() is guaranteed only for
    payloads within this depth.
    """

    steve_id: EntityId
    controller_session_id: EntityId
    generation: int = Field(ge=0)
    request_id: EntityId
    action: ActionType
    params: JsonPayload = Field(default_factory=dict)
    task_id: EntityId | None = None
    expected_state_revision: int | None = Field(default=None, ge=0)
    requested_at: AwareDatetime = Field(default_factory=_utcnow)


class ActionResultSchema(ContractModel):
    """Transport-independent action result envelope (RFC-0001 section 6).

    status carries the RFC terminal/intermediate distinction;
    reason_code, safety_action, state_revision, and resumable mirror the
    RFC example response fields. The runtime dispatcher still returns
    minecraft.actions.ActionResult dataclass instances; no automatic
    conversion exists in this slice.
    """

    action_id: EntityId
    steve_id: EntityId
    request_id: EntityId | None = None
    status: ActionStatus
    reason_code: str | None = Field(default=None, max_length=128)
    message: str = ""
    task_id: EntityId | None = None
    safety_action: str | None = Field(default=None, max_length=64)
    state_revision: int | None = Field(default=None, ge=0)
    resumable: bool = False
    completed_at: AwareDatetime | None = None


class TaskStep(ContractModel):
    """A bounded, single-dispatch action in an external controller's plan."""

    action: ActionType
    params: JsonPayload = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_action(self) -> TaskStep:
        allowed = {
            ActionType.CHAT,
            ActionType.WAIT,
            ActionType.DONE,
            ActionType.CHECK_POSITION,
            ActionType.CHECK_HEALTH,
            ActionType.CHECK_HUNGER,
            ActionType.CHECK_INVENTORY,
            ActionType.CHECK_TIME,
            ActionType.CHECK_WEATHER,
            ActionType.LIST_PLAYERS,
            ActionType.TELEPORT,
            ActionType.MOVE_TO,
            ActionType.BREAK_BLOCK,
            ActionType.PLACE_BLOCK,
        }
        if self.action not in allowed:
            raise ValueError("action is not supported in bounded tasks")
        if self.action is ActionType.WAIT:
            seconds = self.params.get("seconds", 2.0)
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not isfinite(seconds)
                or not 0 <= seconds <= 30
            ):
                raise ValueError("task WAIT seconds must be finite and between 0 and 30")
        if self.action in {
            ActionType.TELEPORT,
            ActionType.MOVE_TO,
            ActionType.BREAK_BLOCK,
            ActionType.PLACE_BLOCK,
        }:
            for axis in ("x", "y", "z"):
                value = self.params.get(axis)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not isfinite(value)
                ):
                    raise ValueError("task targets require explicit finite numeric coordinates")
        return self


class Task(ContractModel):
    """Durable sequential action plan and conservative execution checkpoint.

    An active step without a completed checkpoint has an unknown outcome
    after restart and cannot be resumed automatically or explicitly.
    """

    task_id: EntityId
    steve_id: EntityId
    title: str = Field(min_length=1, max_length=256)
    status: TaskStatus = TaskStatus.PENDING
    owner_controller_id: EntityId | None = None
    created_at: AwareDatetime = Field(default_factory=_utcnow)
    updated_at: AwareDatetime = Field(default_factory=_utcnow)
    steps: list[TaskStep] = Field(default_factory=list, max_length=100)
    next_step: int = Field(default=0, ge=0)
    active_step: bool = False
    revision: int = Field(default=0, ge=0)
    requested_operation: str | None = None
    reason_code: str | None = None


class Event(ContractModel):
    """Structured event envelope (RFC-0001 section 8).

    The local EventJournal persists these envelopes; the local webhook service
    can deliver them to administrator-configured destinations. steve_id and task_id are
    optional because RFC-0001 notes attribution may be unknown; payload
    content is validated as a JSON-compatible mapping but is never
    inferred or fabricated. JSON nesting is bounded by
    MAX_JSON_PAYLOAD_DEPTH, counted from the payload mapping itself, so
    model_dump_json() is guaranteed only for payloads within this depth.
    """

    event_id: EntityId
    type: EntityId
    severity: EventSeverity
    occurred_at: AwareDatetime = Field(default_factory=_utcnow)
    steve_id: EntityId | None = None
    task_id: EntityId | None = None
    causal_action_id: EntityId | None = None
    source: str = Field(default="minecraft_server", min_length=1, max_length=128)
    schema_version: int = Field(default=1, ge=1)
    payload: JsonPayload = Field(default_factory=dict)
