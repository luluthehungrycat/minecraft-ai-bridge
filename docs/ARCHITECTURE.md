# Architecture

This document explains the design, rationale, and data flow of the Minecraft AI Bridge.

## Design Philosophy

The bridge is built on a **three-layer architecture** with strict separation of concerns:

1. **Minecraft Interface Layer** — talks to the game server
2. **LLM Abstraction Layer** — talks to language models
3. **Bridge Orchestration Layer** — the "brain" connecting everything

Each layer is independently testable, swappable, and extensible. The layers communicate through well-defined interfaces (abstract base classes, dataclasses, and pydantic models).

## Layer 1: Minecraft Interface

**Package**: `minecraft_ai_bridge.minecraft`  
**Entry points**: `McpqClient`, `execute_action()`, `Observer`

### McpqClient (`mc_api.py`)

An async wrapper around the MCPQ plugin's gRPC API. MCPQ (Minecraft Protobuf Queries) is a Paper plugin that exposes a gRPC interface for reading and writing world state and controlling players — no game client needed.

```python
client = McpqClient(host="localhost", port=1789, player_name="AIBot")
await client.connect()

# Direct world manipulation
await client.set_block("stone", 10, 64, 20)
block = await client.get_block(10, 64, 20)

# Player control
pos = await client.get_player_pos()
await client.teleport_player(10.0, 65.0, 20.0)

# Commands
result = await client.run_command_blocking("give @p diamond 1")
await client.post_to_chat("Hello from AI!")

# Discovery
players = await client.get_players_online()
time = await client.get_time()

await client.disconnect()
```

All gRPC calls are dispatched via `asyncio.to_thread()` to avoid blocking the event loop, since the generated gRPC stubs are synchronous.

### Action Handlers (`actions.py`)

24 atomic actions the LLM can invoke, organized by category:

- **Movement**: `move_to`, `move_forward`, `move_back`, `turn_left`, `turn_right`, `jump`, `teleport`
- **Interaction**: `break_block`, `place_block`, `interact`
- **Inventory**: `check_inventory`, `equip_item`, `craft_item`, `drop_item`
- **Combat**: `attack`
- **Information**: `scan`, `check_time`, `check_weather`, `check_health`, `check_position`, `list_players`
- **Communication**: `chat`
- **Meta**: `wait`, `done`

Each handler is an `async` function taking `(WorldAdapter, params: dict) -> ActionResult`:

```python
async def _place_block(mc: WorldAdapter, params: dict) -> ActionResult:
    x = params.get("x")
    y = params.get("y")
    z = params.get("z")
    block_type = params.get("block_type", "stone")
    await mc.set_block(block_type, int(x), int(y), int(z))
    return ActionResult(
        success=True,
        action=ActionType.PLACE_BLOCK,
        message=f"Placed {block_type} at ({x}, {y}, {z})",
    )
```

The `_HANDLERS` dict maps `ActionType` enum values to handler functions — simple dispatch, no reflection.

### World adapter (Phase A)

The runtime layer defines a transport-independent `WorldAdapter` Protocol in
`minecraft_ai_bridge/runtime/world_adapter.py`. `McpqWorldAdapter` is a
composition wrapper around an already-connected `McpqClient` — it owns no
connection state and delegates every call verbatim. The orchestrator creates
exactly one adapter around its connected client at startup and passes that
single instance to `ActionService`, `Observer`, `InventoryManager`,
`ChatCommandHandler`, and `SelfPreservationLayer`; built-in runtime reads
(position, player info, chat events, time, players, biome, block get, hurt
source) route through it. The raw `McpqClient` remains only for connection
lifecycle (`connect`/`disconnect`). This is a Phase A slice: the protocol
covers only the methods the runtime currently consumes and is not yet a full
backend contract for Mineflayer or any other transport. External MCP access is
unchanged and remains out of scope.

### Runtime contract schemas

minecraft_ai_bridge/runtime/contracts.py defines the RFC-0001 Phase A
contract vocabulary as Pydantic v2 models: SteveIdentity,
ControllerSession, Capabilities, Task, and Event, plus the action
envelope pair ActionRequestSchema/ActionResultSchema. The Schema suffix
avoids colliding with the existing dispatcher types
(runtime.action_service.ActionRequest and minecraft.actions.ActionResult),
which are unchanged; the schema pair is the canonical RFC contract shape
and is not converted to or from the dispatcher dataclasses in this slice.

These are schema definitions only. Strict validation covers identifiers,
status values, and timezone-aware timestamps. The schemas themselves do not
perform authorization, fencing, ownership, task scheduling, or MCP/network
transport. `McpqWorldAdapter.capabilities`
advertises the current MCPQ surface: simulated teleport movement,
privileged-simulated crafting, and world-edit support, while leaving live
version/dimension discovery and hazard sensors unclaimed. This is backend
ability reporting, not authorization, live world discovery, or an
independent safety guarantee. Those services remain RFC-0001 Phase B and
later work.

### Local authorization and controller fencing (Phase B foundation)

`runtime.AuthzService` stores Steve registrations, ownership bindings,
credential digests, scopes, revocation state, and the current controller
session/fencing generation in SQLite. Credentials are high-entropy bearer
values shown only by a creation result; only SHA-256 digests are persisted.
Admin credentials use a single bootstrap path; later admin grants,
provisioning, transfer, and cross-owner revocation require `admin:steves`.
The bootstrap call creates a global admin identity without a Steve binding.
It assumes trusted local setup code/operator access to the SQLite database
and process; it is not a remote enrollment mechanism. The call is refused
while any valid (unrevoked, unexpired) `admin:steves` credential exists, so it
cannot add a second bootstrap admin alongside a live provisioning admin.
`admin:world` remains independent: a world-only credential does not block
recovery, and bootstrap must include `admin:steves` even when no provisioning
admin remains. An admin cannot revoke its own credential while it is the last
valid `admin:steves` credential; first rotate it or grant and confirm a peer
admin. An `admin:world`-only credential may self-revoke without affecting
provisioning authority.
Unclaimed Steves use an atomic first-claim operation with a Steve-bound
`steve:control` credential. Sessions are exclusive per Steve, carry bounded
persisted leases for leased ownership, and are revalidated by the generated
ActionPolicy at each ActionService request, so credential revocation and
ownership transfer reject subsequent actions. The built-in CLI keeps its
existing default allow policy unless the optional `authz.enabled` setting is
true. When enabled, startup reads the controller bearer token only from
`AUTHZ_CONTROLLER_TOKEN`, authenticates it for the configured `steve_id`, and
acquires one controller session before startup provisioning. The configured
`session_lease_seconds` bounds each controller lease. The built-in orchestrator
renews it periodically only while the same persisted session, credential,
owner, and fencing generation remain valid; renewal never re-acquires or
resurrects a session and leased Steve ownership caps the session expiry. If
renewal is denied or cannot extend the lease, strategic work is suspended and
the independent safety supervisor remains active until an authorized `!stop`
or the `suspended_shutdown_seconds` grace period (default 300) ends, after
which the bridge shuts down. Renewal runs at a third of the lease and retries
storage errors within five seconds. Event-loop authorization checks never wait
on a contended local Authz lock: a check that cannot complete skips the current
strategic step and retries, and only a definitive denial or lease expiry
suspends strategy. The session is released on normal shutdown.
Shutdown completes renewal and safety-worker stops and Authz release/connection
close off the event loop before propagating repeated cancellation.
Invalid credentials, missing
`steve:control`, or an unavailable
session prevent startup mutations. This is local authz/session infrastructure
only: it adds no transport, network exposure, TLS, task persistence, or
backend-local watchdog, and does not itself fence already-dispatched backend
work. After an unclean process exit, a persisted session can block restart
until its lease expires. Once an operator confirms the old process is stopped,
an `admin:steves` administrator can deactivate and reactivate the Steve to
clear the stale session and advance its fencing generation.

ActionService classifies the MCPQ backend's command-simulated movement,
block/inventory changes, combat, eating, and healing as privileged-simulated
actions. Internal chat teleports and startup lifecycle mutations also require
the `admin:world` decision at ActionService; the internal caller capability
proves origin, not world-administration authority. A normal controller
session cannot invoke these actions alone. When Authz is enabled, configure
the separate `AUTHZ_WORLD_ADMIN_TOKEN` from an admin credential that has
`admin:world`; it is accepted only from the environment, and enabled Authz
refuses to start without it. Local mode without Authz and without a custom
policy retains the legacy behavior. A caller-supplied `action_policy` must
return `PolicyDecision(..., allow_privileged=True)` to permit these privileged
classes; a plain `PolicyDecision(True)` rejects them. The safety supervisor uses a distinct,
in-process capability for only bounded mob ATTACK, inventory-backed EAT, and
item-limited HEAL reflexes; it does not reuse strategic controller authority,
so session expiry or revocation does not disable those reactions. The
capability is not exposed through a transport. It does not create a backend
watchdog or support an administrative remote freeze; process shutdown
(including allowlisted `!stop`) stops the supervisor.
If the strategic controller session expires or is revoked, the orchestrator
suspends strategic work and remains in safety-only operation until an
authorized `!stop` or the bounded `authz.suspended_shutdown_seconds` grace
period ends; authorization denials do not trip the ordinary failure shutdown.
Lost authority is not restored in-process, so the grace period always ends in
shutdown (an offline-mode server has no authorized `!stop` path at all).
While suspended, chat polling accepts only the authenticated `!stop` command;
it ignores `!goal` and other commands that could restart strategic work.

Authorization decisions are re-read from persisted state on every check.
`steve:observe:self` and `steve:control` are Steve bound: the target must be
the credential's Steve and persisted ownership must be active, unexpired,
and still match the credential owner. `steve:observe:shared` is peer grant
bound: an `admin:steves` administrator records an explicit grant for each
named peer with `AuthzService.grant_peer_observation`; without that grant,
or if the originating Steve is no longer active and owned by its credential's
recorded owner, or the peer Steve becomes inactive or expires, the check fails
closed. `events:read` is service level and does not bind to the requested
Steve, but requires the originating Steve to remain active, owned by its
recorded owner, and unexpired. Credential authentication and the full scope
decision share both the process RLock and a SQLite read snapshot, so local or
other-connection revocation/rotation cannot mix database states within a
check. Peer grants are idempotent, record the granting admin principal, can
be listed or revoked by an `admin:steves` administrator, and migrate
atomically to a replacement credential during rotation. Admin scopes require
a global admin identity. Revoking another credential requires a global admin
holding `admin:steves`; shared Steve and owner bindings grant no revocation
authority. A credential may rotate or revoke itself, subject to the
last-admin guard above. Each effective peer-grant or peer-revocation change
also appends an immutable audit record with the holder Steve, peer Steve,
credential ID, actor principal, event, and UTC timestamp. Duplicate grants and
revocations that change no state add no audit record. An `admin:steves`-only
read API lists history by holder Steve ID, so it remains inspectable after
credential rotation. A versioned, transactional schema migration creates a
marked baseline grant entry for each active legacy grant that has no recorded
grant event. Its actor is the legacy row's `granted_by` value. The entry's
timestamp is when migration runs; the original grant time was not stored and
cannot be reconstructed. A schema migration marker makes the backfill run only
once, and later database opens and credential rotations do not create duplicate
baselines.

Steve IDs must be non-empty so a Steve registration cannot collide with the
empty Steve marker reserved for global admin identities. An `admin:steves`
administrator may reactivate a deactivated Steve; reactivation clears any
session and advances its fencing generation. Repeating reactivation on an
already active Steve is a no-op, and an expired ownership lease must be
recovered through administrator transfer before its scopes become usable.
Reactivation leaves the expired lease unchanged, allowing that subsequent
administrator transfer without renewing or reviving the expired grant.

Leased ownership is renewed in place by the current owner through
`renew_ownership`. A renewal must carry a timezone-aware expiry strictly in
the future and strictly later than the persisted one, so a lease can only be
extended and never shortened. Inactive, unclaimed, wrong-owner, and already
expired ownership are refused; a lapsed lease is recovered by an
administrator transfer under `admin:steves` rather than by renewal. A
renewal that changes the persisted expiry clears any active session and
advances the fencing generation, so a session bound to the previous lease
stops validating.

### Controller API (Phase B slice)

`runtime.ControllerService` is the transport-independent API through which
an external harness controls one Steve; an MCP or HTTP adapter only translates
requests into its calls. It composes the pieces above:

- `open_session` acquires the Steve's exclusive Authz controller session for
  a `steve:control` credential; `renew_session` and `close_session` manage its
  lease. A session ID alone is not a bearer: every call must also present the
  token that opened it. The service keeps at most one cached session: opening
  a session drops every entry with a lower fencing generation (Authz has
  already fenced them), and a response that arrives after a newer session was
  cached leaves the cache alone because its session is already fenced.
  Renewals follow the same rule, so a renewal restores an entry that a request
  using the old lease pruned meanwhile, but never displaces a newer session.
  Expired leases are swept on each `submit_action`, and a session is dropped
  once a policy rejection or failed renewal is confirmed by Authz to mean the
  session itself is invalid. `close_session` forgets the entry only after
  Authz releases the session; a close that loses to a concurrent renewal
  fails with the stale lease and keeps the renewed entry, so retrying it
  succeeds (#36).
  Requests from other credentials cannot evict it. A dropped session ID then
  reads as unknown (`SESSION_UNKNOWN`).
- `get_capabilities` and `get_state` need `steve:observe:self` or
  `steve:control` for that Steve; observers never get a session.
- `submit_action` takes an `ActionRequestSchema` and returns an
  `ActionResultSchema`. It checks the Steve, session ownership, and fencing
  generation, then runs the action through the Steve's `ActionService` with
  the Authz policy bound to the requesting session (a `ContextVar`), so
  authorization, request-ID idempotency scoping, and audit match the CLI.
  Only actions on an explicit allowlist are exposed, so a new ActionType is
  unreachable until added; internal-control and lifecycle actions never are.
  Request-ID replay protection survives restarts when the service is given a
  `SqliteActionOutcomeStore` (`outcome_store=`); without one it is
  process-local, and a retry after a bridge restart can repeat a mutation.
  `action_id` names the logical action: it is derived from the Steve,
  credential, and request ID, so replays and retries, including after a
  restart, report the same ID.
- The service re-validates every `ActionRequestSchema` it receives, because
  Pydantic skips validation in `model_construct()` and
  `model_copy(update=...)`; malformed requests raise `ValueError` before
  dispatch. Transport adapters should still build requests with
  `model_validate`.
- Authorization is evaluated after the request acquires the Steve's dispatch
  lock, immediately before the backend call, so revocation or fencing that
  happens while a request is queued is enforced. The policy waits for the
  local Authz lock rather than treating contention as an invalid session. A
  revocation that lands after that check, or while MCPQ work is already
  running, cannot stop it.

Every MCPQ mutation is privileged-simulated, so it needs `admin:world`. That
authority comes only from the operator's `world_admin_token` passed to the
service, never from a request. A controller credential additionally needs the
delegation scope for the action's group, held on the session's own
credential and re-read on every request:

| Scope | Actions |
|-------|---------|
| `steve:world:move` | move_to, move_forward, move_back, walk_to, turn_left, turn_right, jump, sprint, teleport |
| `steve:world:edit` | break_block, place_block, interact |
| `steve:world:items` | equip_item, equip_best_weapon, craft_item, drop_item |
| `steve:world:combat` | attack, eat, heal |

Both keys are required: the token says the service may simulate privileged
actions at all, and the scope says this credential may request that group.
Without either, ordinary keys can observe, chat, and wait. An `admin:steves`
administrator grants scopes when creating the credential; changing them means
issuing a new credential. Privileged actions with no delegation scope
(internal and lifecycle actions) are always denied on this path. The
built-in CLI agent is trusted in-process and does not use delegation scopes.
Parameter validation still applies.

Targeted actions are also limited by `ReachLimits` (#25): `break_block`/
`place_block` with explicit coordinates within `edit_distance` (default 6
blocks, measured to the block the handler edits, `int()` of each coordinate), `teleport`/`move_to` destinations within `move_distance` (default
32, 3D), and `walk_to` destinations within the smaller of `move_distance` and
`walk_to`'s walking range (`WALK_TO_TELEPORT_DISTANCE`, 50 blocks horizontal),
so a controller's walk never falls through to the handler's long-distance
teleport. Destinations must also lie inside the world height range
-64..320, and all limits must be finite. Edits without coordinates act next
to the bot and relative steps are already bounded, so they are not checked.
Targeted coordinates that are not finite numbers are refused with
`INVALID_TARGET` rather than left to the handler, because handlers coerce
some values (for example `int(True)`); `walk_to`'s `y: null` means "keep the
current height", as in the handler.

The check is an `ActionService` dispatch guard: it runs after policy, under the
dispatch lock, immediately before the backend call. Every non-WAIT dispatch
through the same `ActionService` holds that lock, so no other request through
this controller service can move the bot between the position read and the
backend call, however many are queued. The lock does not fence anything
outside that service: another bridge process or a CLI agent driving the same
bot (Authz admits one controller session per Steve, but the unauthenticated
CLI is not a session), players, mobs, plugins, game physics such as falling or
knockback, or MCPQ work still running from an earlier dispatch that was
cancelled. Any of these can move the bot during the short window between the
position read and the backend command (about one MCPQ round trip), so reach
bounds where a controller can direct actions relative to the position just
read; it is not a guarantee about the bot's position when the command lands.
Replays of completed requests are answered
from the idempotency cache before the guard and return their original
outcome. A refusal is `rejected` with reason code `OUT_OF_REACH`, nothing is
dispatched, and it becomes that request ID's final outcome; an unreadable
position is refused too. The guard never applies to trusted internal or
safety requests, and the built-in CLI agent is not limited.

Authentication failures raise `PermissionError`; every other refusal
is a `rejected` result with a `reason_code` (`STEVE_MISMATCH`,
`SESSION_UNKNOWN`, `SESSION_NOT_OWNED`, `STALE_GENERATION`,
`ACTION_NOT_EXPOSED`, `OUT_OF_REACH`, `INVALID_TARGET`, `POLICY_REJECTED`).

With an `event_journal`, `get_events(token, limit=, reset_expired=)` and
`ack_events(token, cursor)` give each credential its own durable journal
consumer (`controller-<credential_id>`). They need `events:read` on a
credential bound to this Steve (the scope alone is service level, so the
binding check keeps other Steves' credentials out) and no session. Only
events whose `steve_id` is this Steve are delivered; unattributed events and
other Steves' events are skipped, and a run of skipped events with nothing
pending before it is acknowledged on the caller's behalf (at most ten
1,000-event journal pages per poll). Polls repeat events until they are
acknowledged. If retention dropped events the caller had not acknowledged,
polling raises `EventCursorExpiredError` unless `reset_expired` is set, which
resumes at the oldest retained event and reports `reset=True`. Opening and
releasing a session append a `controller.changed` event (`change`,
`controller_id`, `generation`); a journal failure is logged and never fails
the session call. Fencing by an administrator records no event yet.

`ControllerSafety` runs preservation reflexes beside the MCP transport using
its ControllerService's ActionService and a process-local safety authority.
Attack/eat/heal do not require an external controller session. Reflexes
invalidate queued controller actions (`SAFETY_PREEMPTED`), even after the
reflex is acknowledged; multi-step movement stops between steps and reports
`SAFETY_INTERRUPTED`. Events carry the Steve ID. With no goal tree, flee and
find-food subgoals are unavailable; overwhelming threats fall back to attack.
Shutdown stops polling and flushes safety events before closing the action
outcome store or journal. This is in-process safety, not a backend watchdog.

With a `task_store` (`SqliteTaskStore`, which also requires the durable
outcome store and event journal), `create_task`/`get_task`/`pause_task`/
`resume_task`/`cancel_task` run caller-supplied plans of up to 100 steps
(64 KiB) in a service-owned worker, independent of the MCP request. Steps
are limited to single-dispatch actions: chat, done, WAIT up to 30 seconds,
the position/health/hunger/inventory/time/weather/player checks, and
`teleport`/`move_to`/`break_block`/`place_block` with explicit finite
coordinates. Each step runs through `submit_action`'s gate under the
creating session (allowlist, delegation, reach limits, safety preemption),
with a fresh request ID per attempt. A Steve runs one task at a time, and
each credential has at most one unfinished task: a paused task blocks only
its own credential (which can cancel it), never other controllers. While a
worker runs, other strategic actions are refused. Management calls need
`steve:control`, the task's own credential, and a current session; `get_task`
needs no session. Bearer tokens are never stored.

The store checkpoints before and after every step (`next_step`,
`active_step`, `revision` compare-and-set) and holds an OS file lock, so a
second runtime on the same file fails closed. Pause and cancel are
cooperative: they are latched immediately, refuse a queued step before it
reaches the backend (also re-checked after the awaited reach read), interrupt
WAIT, and otherwise take effect when the running step finishes; completed
effects are not undone. A safety intervention between or during steps pauses
the task (`SAFETY_PREEMPTED`). A failed step fails the task; a refused one
pauses it with the refusal's reason. On restart, unfinished tasks become
`paused`: `RESTARTED` when no step was in flight, which can be resumed under
a new session of the same credential, or `OUTCOME_UNKNOWN` when a step may
have reached the backend, which can never be resumed (reconcile, cancel, and
create a new plan). A cancel requested before the restart becomes
`cancelled`. Shutdown waits for an in-progress create or resume before
joining the worker. A full event outbox (100,000 unpublished events) refuses
new tasks but never blocks checkpoints or restart recovery. Finished tasks
are kept for 7 days and the newest 10,000, then pruned at admission to
tombstones that keep only the request key and plan fingerprint until 365
days, so a retried `request_id` never starts its plan again. Lifecycle events (`task.created`, `task.started`,
`task.progress`, `task.interrupted`, `task.completed`, `task.failed`,
`task.cancelled`, ...) are written to an outbox in the same transaction as
each checkpoint and published to the journal with deterministic event IDs,
carrying only status, step index, revision, and reason code. Completion means
every step reported success, not that the caller's goal holds in the world.

### MCP transport (Phase B slice)

`minecraft_ai_bridge.transport.mcp_server.build_mcp_server` exposes one
`ControllerService` to MCP clients through thirteen tools:
`minecraft_get_capabilities`, `minecraft_get_state`, `minecraft_open_session`,
`minecraft_renew_session`, `minecraft_close_session`,
`minecraft_submit_action`, `minecraft_get_events`, `minecraft_ack_events`,
`minecraft_create_task`, `minecraft_get_task`, `minecraft_pause_task`,
`minecraft_resume_task`, and `minecraft_cancel_task`. Each tool makes exactly one `ControllerService`
call, so authentication, session fencing, delegation scopes, the exposed-action
allowlist, parameter validation, and idempotency are enforced there and nowhere
else. The adapter only decides which bearer token a call carries:

- Streamable HTTP: the `Authorization: Bearer <token>` header of the HTTP
  request carrying the call. A missing header, another scheme, or an invalid
  token is refused; there is no fallback to any other token. Requests are
  stateless (`stateless_http=True`), since controller sessions live in
  `ControllerService`, not in the MCP transport.
- stdio: the controller token in `MAB_CONTROLLER_TOKEN`, supplied by the
  harness that launches the process.

The `minecraft-ai-bridge-mcp` command (`transport/mcp_main.py`) connects MCPQ,
`AuthzService`, a `SqliteActionOutcomeStore`, and an `EventJournal`
(`--event-journal`, defaulting to `bridge.event_journal_path`), then serves stdio or
Streamable HTTP. HTTP binds to `127.0.0.1` by default; any non-loopback
address requires `--allow-remote` plus at least one `--allowed-host`, and the
server does not terminate TLS itself. Host and Origin validation against
DNS rebinding is always configured explicitly (`transport_security_for`):
SDK 2.3 only enables it on its own for the exact hosts `127.0.0.1`,
`localhost`, and `::1`, leaving other binds unprotected. Loopback binds accept
loopback Host and Origin values; other binds accept only the configured hosts
and origins, and with no origins configured only requests without an Origin
header. Startup registers each resource's cleanup as it is opened
(`AsyncExitStack`), so a failure at any step, or in another cleanup, still
closes everything opened before it. Refusals become
MCP tool errors (`unauthorized: ...`, `invalid request: ...`); controller-level
rejections are returned as `rejected` results with their `reason_code`.

Limitations: the bot entity must already exist (the server does not summon
it); safety cannot undo already-dispatched backend work; tasks run only bounded
single-dispatch steps.

### Durable event journal (Phase C slice)

`runtime.EventJournal` persists validated Event envelopes in SQLite with WAL
and full synchronous commits. Each append receives a monotonically increasing
cursor. Repeating an identical event ID and envelope returns its original
cursor; reusing the ID with different content raises a conflict. Event payload
rows are retained up to the configured bound; a compact event-ID fingerprint
index remains after payload pruning so old IDs cannot be silently reused.

Consumers poll from their durable acknowledgment cursor. Fetching does not
advance the cursor, so unacknowledged events are delivered again after retry or
restart (at-least-once). When retention removes an unacknowledged event, that
consumer is marked expired and polling/acknowledgment fails until
`reset_consumer()` resumes at the oldest retained event. Reset can skip expired
history and is an explicit recovery choice. There is no authentication/ACL
layer in the journal itself; `ControllerService` adds per-credential polling
for one Steve's events (and the MCP tools over it). There is no SSE delivery.
Producers are the built-in `SafetySupervisor`, `ControllerService`
(`controller.changed`), and the task runner, which publishes `task.*` events
from its durable outbox. For the supervisor: each ordinary
`safety.intervened` event contains the action name and success flag, using HIGH
for successful interventions and CRITICAL for failed or denied interventions.
Writes run off the event loop and are drained during orderly shutdown. Other
RFC-0001 event producers remain unimplemented. The compact event-ID index
grows with unique IDs even though event payload retention is bounded.
If the journal sink is slow, the safety producer keeps one write in flight and
coalesces pending safety-event reporting into one bounded summary. The summary
contains counts by safety action and outcome; any coalesced failure keeps the
summary at CRITICAL severity and is represented as a failed action, even when
a later intervention succeeds. This bounds event backlog without erasing a
critical failure behind a later success.

### Outbound webhook delivery (local service slice)

`runtime.WebhookService` layers administrator-supplied
`WebhookSubscription` values over the journal. There is no subscription
creation endpoint: the caller supplies the configured subscription list and
secret material. Each subscription polls a distinct `webhook-{id}` journal
consumer. SQLite stores a stable UUIDv5 delivery ID (subscription ID plus
event ID), attempt state, retry deadline, response status, and dead-letter
reason. A successful 2xx response advances the consumer cursor. Exhausting
configured attempts, an oversized event body, or an event lost to journal
retention records a terminal dead letter and advances the cursor. `deliveries()`
and `dead_letters()` expose state. Retry delay doubles from its configured
initial value up to its cap; attempt and per-minute request limits are enforced.
Concurrent `run_once()` calls on one service are serialized so stale sends
cannot overwrite a delivery's terminal state.

The body is UTF-8 JSON with recursively sorted keys, no insignificant spaces,
and no non-finite numbers. `X-Webhook-Timestamp` is Unix seconds;
`X-Webhook-Signature` is `sha256=` followed by lowercase hex HMAC-SHA256 over
`timestamp + "." + exact_body_bytes`; `X-Webhook-Delivery-Id` is stable across
retries. Receivers should validate the signature and timestamp window and
deduplicate by delivery ID. If a receiver accepts a request and the process
crashes before the local success/ack commit, a retry can deliver the event
again. Delivery is at least once, never exactly once.

URLs must use HTTPS on port 443, contain no userinfo or fragment, and match an
exact administrator allowlisted hostname. Literal and DNS-resolved non-global
addresses are rejected, DNS answers are capped, and redirects are disabled.
The default `HttpxPinnedTransport` tries only those checked addresses, using an
IP-literal URL for the connection while setting the original hostname as HTTP
`Host` and HTTPX's documented `sni_hostname` extension for TLS SNI and
certificate hostname verification. Its HTTPX transport and client both disable
environment proxy configuration; connection limits are one with no keep-alive,
and a process-wide limit bounds concurrent pinned sends. Connect/read/write/pool
plus whole-operation timeouts are bounded. Tests
inject HTTPX `MockTransport` through the same request construction path. A
custom `PinnedWebhookTransport` remains a trusted security boundary; the
service cannot inspect whether an injected implementation honors its contract.
DNS and transport errors are recorded and retried under the configured policy
without falling back to ordinary hostname resolution. Endpoint allowlisting is
not authorization for who may configure subscriptions. External controller
authorization and admin configuration integration remain absent; this bounded
local service does not complete RFC-0001 Phase C.

DNS lookup uses a single process-wide daemon worker with nonblocking admission,
so saturation cannot accumulate queued resolver jobs or threads. At capacity a
lookup is rejected immediately and recorded through the normal retry path.
Each admitted lookup has an async deadline; timeout or cancellation does not
release its capacity slot until the underlying resolver finishes. A timed-out
result is discarded, and the configured transport is never called for that
lookup. Retry interval values must be finite positive numbers, with the maximum
at least the initial delay.

The worker processes only the journal head for each subscription. A backoff
at an earlier cursor blocks later sends and acknowledgments; event-type filters
skip only the current head. Since delivery state and journal acknowledgments
live in separate SQLite databases, startup reconciliation replays durable
`delivered`/`dead_letter` state into the consumer cursor. A crash after remote
acceptance but before terminal state is recorded can still cause a duplicate;
the stable delivery ID supports receiver-side deduplication.
If journal retention expires a consumer between remote acceptance and local
acknowledgment, reconciliation resets the consumer to the oldest retained
cursor. Pending delivery rows at or below that floor are dead-lettered as
expired because their event bodies are gone. Already delivered rows remain
terminal and are not sent again, after which processing resumes from retained
events.

### Initial runtime action gate

The built-in orchestrator sends its strategic LLM-selected actions through
`minecraft_ai_bridge.runtime.ActionService`. The service provides an async
policy hook, correlation IDs, and a bounded in-memory audit trace before
calling the existing dispatcher. Its default policy allows actions to retain
the current standalone CLI behavior.

This is a Phase A slice, not yet a complete mutation boundary. Most
direct MCPQ access remains outside it. The self-preservation layer's
mutating reflexes (reflex ATTACK, auto EAT, auto HEAL) are routed
through the orchestrator's shared ActionService, but a layer constructed
directly without one keeps the previous dispatcher behavior. Follow-mode
teleports, the two trusted startup provisioning steps (bot summon and
safe-spawn platform preparation), and the built-in chat handler's
!goto/!come teleports are routed through the action gate as distinct
internal actions, while read-only observation calls (including the
layer's SCAN_ENTITIES and damage-source checks) stay direct. A chat
handler constructed without an orchestrator ActionService falls back
to a local ActionService bound to its resolved world adapter; a harness
exposing only a raw MCPQ client is wrapped in McpqWorldAdapter, so
legacy _mc-only harnesses keep polling, status, and chat replies
working. A harness exposing only an ActionService (with no adapter and
no raw client) resolves the world from that service, so the same
polling, status, and reply behavior is preserved. The mutation still
crosses the gate. External MCP clients must
not be exposed until the
remaining mutation paths and independent safety requirements are
covered.

At this boundary, backend command-simulated movement, world/inventory edits,
combat, and simulated consumption/healing are classified as privileged and
require `admin:world` under Authz; ordinary controller sessions cannot reach
them by selecting a different ActionType. Classification is an explicit
allowlist: `chat`, `wait`, and `done` are the only survival actions, and an
ActionType missing from every class is rejected before policy evaluation
instead of defaulting to survival. Handlers that build server commands from
request strings validate them first: player targets as plain usernames, and
block and item names as plain IDs without block states or NBT data, amounts
as integers from 1 to 64, and teleport coordinates as finite numbers. Local
mode preserves existing CLI behavior. Safety reflexes use a distinct bounded capability described above.

### Independently scheduled preservation (safety prerequisite slice)

`runtime.SafetySupervisor` periodically obtains a world observation and
evaluates the orchestrator's existing `SelfPreservationLayer` in a task
separate from the strategic LLM loop. If a mutating reflex arrives while the
LLM is deciding, the orchestrator cancels and awaits that decision, then
dispatches the reflex through the shared `ActionService`. A one-item pending
slot bounds unconsumed result backlog; the supervisor continues observing and
evaluating while that result is pending. The slot holds the newest result,
except that a later success never replaces an unconsumed failure. Goal
decomposition is covered by the supervisor as well.
Strategic WAIT actions are interruptible and do not hold
the ActionService dispatch lock; other dispatch coroutines are serialized.
Multi-step strategic actions (`walk_to`, `move_forward`, `move_back`,
`sprint`) check between steps whether a reflex is waiting for that lock and
stop with an `interrupted` result, so a reflex waits at most one step plus any
MCPQ call already in flight. Single-step actions still run to completion.
When a safety reflex starts, the shared action service invalidates strategic
requests that have not crossed the backend dispatch boundary; it re-enables
them after the intervention is consumed. Cancellation can release that lock
while MCPQ work in an `asyncio.to_thread` call continues, so this is not
backend mutation fencing.

The safety capability permits only bounded mob attacks and inventory-backed
eating/healing. EAT and item-backed HEAL require a positive server response
confirming one item was cleared before applying effects; item-free generic
HEAL is not available through that capability. ATTACK executes at the bot and
targets only curated attackable mob types within six blocks.

This makes preservation checks independent of LLM latency while the runtime
and backend calls remain responsive. It is not the RFC-0001 Phase C safety
service: there is no watchdog for a blocked or disconnected backend; cancelling
an awaiting coroutine does not stop an MCPQ operation already running in
`asyncio.to_thread`, whose outcome can remain uncertain; and flee-to-safety
still asks the LLM to choose an action. These limitations do not authorize
external MCP control.

The orchestrator routes follow-mode teleports through the gate as the
internal `follow_teleport` action, which is not exposed to the LLM action
space. When a `!follow` target is more than four blocks away, the
orchestrator passes only the target name; the handler validates it as a
plain Minecraft username (letters, digits, and underscores; entity selectors
and command metacharacters are rejected) before embedding it in the teleport
command.

Startup provisioning is routed the same way: the `/botsummon` bot summon and
the teleport-plus-3x3-platform safe-spawn preparation are the internal
`provision_bot` and `prepare_safe_spawn` actions, also hidden from the LLM
action space. The bot player name is validated as a strict Minecraft
username before command interpolation, and failed provisioning results are
logged deliberately instead of being treated as success.

The built-in chat handler routes its !goto and !come teleports through
the orchestrator's shared ActionService as the internal `goto_teleport`
and `come_teleport` actions, also hidden from the LLM action space. The
!goto target and the !come sender are untrusted chat-derived strings, so
both are validated with the same strict Minecraft username validator
(letters, digits, and underscores; entity selectors and command
metacharacters are rejected) before they are interpolated into teleport
commands. ActionService and the dispatcher report failures as failed
results rather than raising, so the handler surfaces failure results to
the chat explicitly. Direct/test harness construction without an
orchestrator-provided service uses a local ActionService bound to the
handler's MCPQ client as the default, so there is no raw command bypass.
Service-only construction (no adapter, no raw client) resolves the world
from the supplied service. Protected chat commands (`!goal`, `!stop`,
`!goto`, `!follow`, and `!come`) additionally require the MCPQ event's
player metadata to match an explicit UUID or name allowlist. A supplied UUID
takes precedence over the name. The current MCPQ ChatEvent exposes only the
server-supplied player name, so name authorization is enabled only when
`chat_control.server_online_mode` explicitly asserts that Paper uses
`online-mode=true`; the bridge cannot verify this for separately managed
servers. Offline-mode names, formatted chat markup, and sender strings passed
to direct dispatch do not grant authority. `!status` and `!help` remain public
read-only commands.

### Request idempotency

`ActionService.execute` accepts an optional `request_id`. Calls that omit it
behave exactly as before. With an explicit `request_id` the service keeps a
process-local, bounded terminal registry (plus separate in-flight entries,
which are never evicted). An optional `SqliteActionOutcomeStore` makes it
durable with a per-request state machine, `reserved` → `dispatching` →
terminal:

- `claim` inserts a `reserved` row only if the request ID has none, so two
  ActionService instances sharing a database cannot both own a request. An
  existing terminal record replays (or conflicts); a request reserved or
  dispatching elsewhere is refused without dispatching and without caching.
- `begin_dispatch` moves the row to `dispatching` with a compare-and-set on
  the owner token, under the dispatch lock and before policy evaluation, so
  it is persisted before any backend call can start. A reservation whose
  owner crashed while waiting for the lock can be taken over after
  `reservation_timeout_seconds` (default 300); the old owner's
  compare-and-set then fails and it does not dispatch, and it can no longer
  record an outcome for the row. Takeover and `begin_dispatch` are each
  atomic in SQLite, so when they race exactly one owner reaches
  `dispatching`. A `dispatching` row is never taken over.
- `complete` records the terminal outcome off the event loop in a tracked
  task. The caller's result is returned only after the write; cancellation
  does not abandon it, and `ActionService.flush()` /
  `ControllerService.aclose()` wait for pending writes at shutdown. A row
  left in `dispatching` (a crash after dispatch, or a failed write) reads as
  an unknown outcome and never dispatches again.

This is at-most-once dispatch per request ID within the retention window,
not exactly-once execution: the backend is not part of the SQLite
transaction, so a crash after dispatch leaves the outcome unknown. Full
outcomes are kept for `max_age_seconds` (7 days) and the newest
`max_records` (10,000); older ones become tombstones holding only the request
ID, fingerprint, and identity scope, so the ID still cannot be reused.
Tombstones are deleted after `tombstone_max_age_seconds` (365 days), which
bounds the idempotency window; after that an ID may dispatch again. Records
keep their identity scope, so a retry from a new controller session is
refused rather than replayed. Unreadable or incompatible records read as
unknown outcomes, and the store refuses to open a database with another
schema version.

- An identical action+params payload replays the stored terminal result
  instead of dispatching again. Replayed results are copies flagged with
  `data["replayed"] = True` and carry both the original `request_id` and a
  fresh `correlation_id` for the replay event.
- Params passed with an explicit `request_id` must be JSON values (`None`,
  bool, int, finite float, str, list, or string-keyed mapping, recursively).
  Tuples and non-finite floats raise `ValueError` before policy evaluation
  or dispatch; this is what makes fingerprint comparison exact for every
  accepted payload. Calls without `request_id` are not validated.
- A different action or params payload under the same ID is rejected
  (`success=False`) before any dispatch.
- Concurrent identical requests coalesce onto a single backend dispatch.
  Cancelling one coalesced caller cancels only that caller; the shared
  dispatch and the remaining waiters are unaffected.
  Waiters receive an isolated snapshot of the dispatch outcome, so the
  dispatching caller mutating its returned result cannot alter what
  coalesced waiters or later replays see.
- If the dispatching coroutine is cancelled after the backend dispatch
  started, the service records an `"unknown"` terminal outcome for that ID
  (MCPQ work runs in a worker thread, so the backend operation may still
  complete or may already have applied). Replaying that ID returns the
  unknown-outcome failure instead of re-dispatching. This is an audit and
  replay guard, not a backend cancellation guarantee.
  A cancellation that arrives before the backend dispatch started (for
  example while the policy hook was still running) records a `"failed"`
  outcome stating that nothing was dispatched, and does not claim an
  unknown mutation.
- The dispatch-started marker is cleared when the request reaches any
  terminal outcome, so a later reuse of the same ID after eviction is
  classified as a fresh pre-dispatch request rather than an interrupted
  one.
- Replay audits record the permission decision that was made for the
  original dispatch (persisted on the stored outcome) instead of inferring
  it from the outcome status. A replay of a pre-dispatch-cancellation
  record is therefore audited as not allowed.
- Accepted params are deep-copied into an independent snapshot before
  validation, fingerprinting, policy evaluation, and dispatch. A caller
  mutating the original mapping after calling `execute` cannot change what
  the policy sees, what is dispatched, or what the fingerprint covers;
  this applies to calls with and without an explicit `request_id`.
- Records live in memory only and are lost on process restart; a request ID
  whose record was evicted or never stored may dispatch again.
- Stored outcomes are isolated from the result objects returned to callers,
  so mutating a returned result cannot corrupt later replays.
- Policy-rejected results carry the caller's `correlation_id` (and the
  `request_id`, when one was supplied) in `result.data`, matching the
  metadata already present on dispatch failures and replay results.

Idempotency applies only to ActionService calls that pass an explicit
`request_id`; current built-in callers do not persist or replay IDs across
restart. This does not make other MCPQ call sites idempotent and does not
provide durable or cross-process replay protection.

### Observer (`observer.py`)

Gathers world state on each turn:

```python
class WorldState:
    position: tuple[float, float, float] | None
    health: float
    inventory: str         # raw NBT data string
    inventory_manager: InventoryManager  # structured slot tracking
    time: str               # game time string
    players: list[str]
    scan_data: dict         # nearby blocks, cardinal samples
    raw_command_outputs: dict  # all raw responses for debugging

The observer uses `asyncio.gather()` to query position, inventory, health, time, and players concurrently. Raw NBT inventory data is parsed into structured slot information via `InventoryManager.update_from_nbt()`.

### RCON Client (`rcon.py`, optional fallback)

An async RCON client is included as a fallback for admin commands, but MCPQ replaces all gameplay functionality. The RCON implementation uses a custom `asyncio`-compatible read loop to avoid hangs on Python 3.13+.

## Layer 2: LLM Abstraction

**Package**: `minecraft_ai_bridge.llm`  
**Entry point**: `create_llm_client(AppConfig)`

### LLMClient Protocol (`client.py`)

Abstract base class with a single method:

```python
class LLMClient(ABC):
    @abstractmethod
    async def decide(
        self,
        system_prompt: str,
        messages: list[Message],
    ) -> LLMResponse:
        ...
```

`LLMResponse` contains the action name, params dict, and reasoning:

```python
class LLMResponse(BaseModel):
    action: str
    action_params: dict[str, Any]
    reasoning: str
```

### Five Provider Implementations

| Provider | Class | Backend SDK | Features |
|----------|-------|-------------|----------|
| **OpenAI** | `OpenAIClient` | `openai` library | Tool calling, streaming support |
| **Anthropic** | `AnthropicClient` | `anthropic` library | Tool calling via `anthropic.types.ToolUseBlock` |
| **Ollama** | `OllamaClient` | `httpx` HTTP calls | OpenAI-compatible chat endpoint, tool calling |
| **OpenRouter** | `OpenRouterClient` | `openai` library (custom base_url) | Same as OpenAI, extra headers for referer/title |
| **OpenCode Server** | `OpenCodeServerClient` | `httpx` HTTP calls | Session-based API: POST /sessions, then POST /session/{id}/message |

### System Prompts (`prompts.py`)

The agent receives a detailed system prompt that explains:
- Its role as an AI playing Minecraft
- All available actions with descriptions and parameter schemas
- The JSON output format for tool calls
- Rules of conduct (prefer scanning before moving, don't spam, etc.)

State is formatted in `format_state()` — a human-readable summary of position, health, inventory, time, and surrounding blocks.

### Action Tool Schema

The LLM is given an `ACTION_TOOL` definition in `client.py` that describes each action as a JSON Schema function call. This is the mechanism that lets structured action decisions work across all providers:

```python
ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "act",
        "description": "Choose the next action to perform.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in ActionType],
                    "description": "..."
                },
                "action_params": { ... },
                "reasoning": {
                    "type": "string",
                    "description": "..."
                }
            },
            "required": ["action", "action_params", "reasoning"]
        }
    }
}
```

## Layer 3: Bridge Orchestration

**Package**: `minecraft_ai_bridge.bridge`  
**Entry points**: `Orchestrator`, `GoalManager`, `AgentMemory`, `ChatCommandParser`, `InventoryManager`

### Orchestrator (`orchestrator.py`)

The main agent loop. On construction it creates LLM client, MCPQ client, observer, memory, and goal manager. The `run()` method:

1. **Connects** to MCPQ, spawns the bot entity if needed, teleports to safe coordinates
2. **Decomposes** the goal into sub-goals via the LLM (with fallback plans)
3. **Loops** through think-act-observe until the goal is complete

Each step:

```
┌──────────┐    ┌──────────┐    ┌──────────┐
│ Observe  │───→│  Think   │───→│   Act    │
│ (MCPQ)   │    │ (LLM)    │    │ (MCPQ)   │
└──────────┘    └──────────┘    └──────────┘
     │               │               │
     │               │               │
      ▼               ▼               ▼
┌──────────────────────────────────────────┐
│              Record (Memory)              │
└──────────────────────────────────────────┘
```

**Failure resilience**: The orchestrator tracks `_consecutive_failures` — incremented on every failed action and reset on success. When the counter reaches `_max_failures` (default 5), the agent shuts down gracefully. Exceptions in `run()` are also caught and increment the counter, with exponential backoff sleep for crash recovery.

### Goal Manager (`goal_manager.py`)

Manages a tree of goals and sub-goals:

```python
class GoalNode:
    description: str
    completed: bool
    sub_goals: list[GoalNode]
```

- **Goal decomposition**: Sends the high-level goal to the LLM with instructions to return a JSON array of sub-goals
- **Fallback plans**: If the LLM fails or returns nothing, matches the goal text against patterns — "build" → 11-step construction plan, "mine" → 12-step mining plan, "farm" → 10-step farming plan, "enchant"/"workshop" → 9-step workshop plan, "explore"/"scout" → 8-step exploration plan, plus a generic 6-step fallback
    - **Progress tracking**: `is_complete` property, `mark_current_complete()` method, tree traversal for next uncompleted goal
    - **Fallback regex safety**: Mining pattern uses `\bore\b` (word boundaries) so "Explore" doesn't accidentally match the mining plan via the "ore" substring

### Chat Command Parser (`chat_commands.py`)

Parses incoming Minecraft chat messages for live agent control:

| Command | Effect |
|---------|--------|
| `!stop` | Sets shutdown flag, agent stops after current action |
| `!status` | Returns goal progress and last action summary via chat |
| `!follow` | Sets follow-player mode for coordinated building/travel |

The parser uses `_COMMAND_RE = r"<([^>]+)>\s+((?:/[!\u00a7]?|!)\w+)(.*)"` to match both `/!status` (with slash) and `!status` (without slash) formats.

### Inventory Manager (`inventory_manager.py`)

Structured tracking of the player's 41-slot inventory (0-8 hotbar, 9-35 main, 36-39 armor, 40 offhand, 41-? extra):

- `update_from_nbt(nbt_data)` — parses raw NBT from `/data get entity @p Inventory`
- `slot_summary()` — human-readable summary for LLM prompts (hotbar + armor + offhand)
- `get_slot(slot)` / `find_item(item_type)` — structured lookups
- Slot regex handles negative offhand slot indices (`-106`) that Paper reports

### Memory (`memory.py`)

Two-tier memory system backed by SQLite:

```python
class AgentMemory:
    def __init__(self, window: int = 20):
        self._actions: deque[ActionRecord]  # short-term, rolling window
        self._facts: set[str]              # long-term, notable discoveries
        self._observations: deque[ObservationRecord]
```

- **Short-term**: A `deque` of recent `ActionRecord` objects (action name, success, message, timestamp). Injected into every LLM prompt as a structured text block.
- **Long-term**: A `set` of fact strings stored in SQLite. Extracted from position data, discoveries, and other notable events. Persists across the entire session.
- **Summaries**: `short_term_summary` property formats recent actions for prompts, `notable_facts()` returns all long-term facts.
- **Thread safety**: Uses `threading.local()` for connection caching. The `close()` method properly deletes the thread-local attribute so subsequent calls reopen a fresh connection rather than returning a stale closed one.

## Data Flow Diagram

```
User CLI ("Build a house")
    │
    ▼
Orchestrator.run()
    │
    ├── config.yaml + env vars ──→ AppConfig
    │
    ├── create_llm_client(config) ──→ LLMClient (OpenAI/Anthropic/...)
    │
    ├── McpqClient.connect() ──→ Paper Server (gRPC / MCPQ plugin)
    │
    ├── Observer(client) ──→ reads world state
    │
    ├── GoalManager.set_goal("Build a house")
    │       │
    │       ├── LLM: decompose ──→ ["Gather wood", "Craft planks", ...]
    │       └── fallback: match patterns
    │
    └── Loop (max_iterations):
            │
            ├── Observer.observe() ──→ WorldState
            │
            ├── Build context (goal + state + memory + last result)
            │
            ├── LLM.decide(system_prompt, context) ──→ LLMResponse
            │       │
            │       └── { action: "place_block", params: {...}, reasoning: "..." }
            │
            ├── execute_action(mc, action, params) ──→ ActionResult
            │
            ├── memory.record_action(action, result)
            │
            └── if action == "done" → mark sub-goal complete
                                    → if all done → exit loop
```

## Key Design Decisions

### Why MCPQ instead of RCON?

RCON is text-based and limited to running commands and reading string output. MCPQ provides:
- **Direct block manipulation**: `setBlock(x, y, z, type)`, `getBlock(x, y, z)`
- **Player entity control**: `getPlayer()`, `teleportPlayer()`, `getPlayerPos()`
- **Structured data**: Protobuf instead of parsing command output
- **No client needed**: The plugin runs server-side

### Why the included bot plugin + MCPQ (not a full bot)?

MCPQ needs a `ServerPlayer` entity to control. The included `mc-bot-plugin` creates a lightweight server-side player entity — no game client connection needed. This is more reliable and simpler than maintaining a full client bot (like pyCraft).

### Why asyncio?

The bridge spends most of its time waiting — for LLM API responses, for MCPQ gRPC calls, for rate-limit delays. asyncio allows concurrent observation queries (`asyncio.gather`) and clean timeouts without threads.

### Why pydantic-settings for config?

All configuration is merged from YAML + environment variables with proper type coercion (string "1789" → int 1789). The `from_yaml()` class method loads YAML then re-creates pydantic models with env var overrides applied, ensuring types are correct even when values come from string env vars.
