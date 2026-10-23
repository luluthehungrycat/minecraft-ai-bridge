"""Transport-independent runtime services.

Contracts defines the RFC-0001 Phase A Pydantic schemas. This package also
provides the action gate, local SQLite authorization and controller fencing,
durable event journal, and local webhook sender. The authorization service
does not provide transport/MCP wiring, task persistence, or backend watchdog
safety; those capabilities remain outside this slice.
"""

from .action_service import (
    ActionAuditRecord,
    ActionClass,
    ActionOutcome,
    ActionPolicy,
    ActionRequest,
    ActionService,
    PolicyDecision,
    classify_action,
)
from .authz import (
    SCOPES,
    AuthzService,
    CreatedCredential,
    Credential,
    PeerGrantAuditEntry,
)
from .authz import (
    ControllerSession as AuthzControllerSession,
)
from .contracts import (
    ActionRequestSchema,
    ActionResultSchema,
    ActionStatus,
    BackendKind,
    Capabilities,
    ControllerSession,
    CraftingMode,
    EntityId,
    Event,
    EventSeverity,
    MovementMode,
    SessionStatus,
    SteveIdentity,
    Task,
    TaskStatus,
)
from .event_journal import EventCursorExpiredError, EventIdConflictError, EventJournal, EventRecord
from .safety_supervisor import SafetySupervisor
from .webhooks import (
    HttpxPinnedTransport,
    PinnedWebhookTransport,
    WebhookDelivery,
    WebhookError,
    WebhookService,
    WebhookSubscription,
)
from .world_adapter import McpqWorldAdapter, WorldAdapter

__all__ = [
    "ActionAuditRecord",
    "ActionClass",
    "ActionOutcome",
    "ActionPolicy",
    "ActionRequest",
    "ActionRequestSchema",
    "ActionResultSchema",
    "ActionService",
    "AuthzService",
    "AuthzControllerSession",
    "Credential",
    "CreatedCredential",
    "SCOPES",
    "ActionStatus",
    "BackendKind",
    "Capabilities",
    "ControllerSession",
    "CraftingMode",
    "EntityId",
    "Event",
    "EventCursorExpiredError",
    "EventIdConflictError",
    "EventJournal",
    "EventRecord",
    "EventSeverity",
    "McpqWorldAdapter",
    "MovementMode",
    "PeerGrantAuditEntry",
    "PolicyDecision",
    "classify_action",
    "SessionStatus",
    "SafetySupervisor",
    "SteveIdentity",
    "Task",
    "TaskStatus",
    "WorldAdapter",
    "WebhookDelivery",
    "WebhookError",
    "HttpxPinnedTransport",
    "PinnedWebhookTransport",
    "WebhookService",
    "WebhookSubscription",
]
