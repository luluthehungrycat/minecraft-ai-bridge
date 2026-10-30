# Changelog

All notable changes to the Minecraft AI Bridge are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## Conventions

- **Added** — new functionality
- **Changed** — changes to existing functionality
- **Fixed** — bug fixes
- **Removed** — removed features (rare; we deprecate before deleting)

Versions follow [Semantic Versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`. The "Unreleased" section tracks work merged to
`master` that has not yet been tagged.

## Maintenance rule

Every PR that changes user-visible behaviour (anything beyond pure
refactor, lint, or test cleanup) MUST add a line to this file under
"## [Unreleased]". The PR description should reference the relevant
section. This is enforced by the AGENTS.md "CHANGELOG.md" rule.

---

## [Unreleased]

### Added

- `minecraft-ai-bridge-mcp` runs durable tasks: `minecraft_create_task`
  starts a plan of up to 100 bounded steps (chat, waits, observations, and
  targeted moves and block edits) that runs independently of the MCP
  request, through the same authorization, reach, and safety checks as single
  actions. `minecraft_get_task`, `minecraft_pause_task`,
  `minecraft_resume_task`, and `minecraft_cancel_task` manage it. Progress
  is checkpointed in `--task-store` (default `data/tasks.sqlite3`) and
  published as `task.*` events; unfinished tasks pause on safety
  interventions and restarts, and a step whose outcome is unknown is never
  replayed (#18).

- MCP-controlled Steves now run independent attack/eat/heal safety reflexes,
  journal attributed interventions, and refuse stale queued actions with
  `SAFETY_PREEMPTED`. Partially executed movement reports `SAFETY_INTERRUPTED`
  instead; completed backend work cannot be undone (#18).

- `minecraft-ai-bridge-mcp` serves the bot's events: `minecraft_get_events`
  polls a durable per-credential cursor (events repeat until acknowledged)
  and `minecraft_ack_events` acknowledges them. Both need `events:read` on a
  credential bound to the Steve, and only that Steve's events are delivered.
  Opening and releasing a controller session now records a
  `controller.changed` event. `--event-journal` selects the journal file
  (default `bridge.event_journal_path`) (#18).

### Fixed

- `!follow` now rejects targets that are not plain Minecraft usernames (such as
  `@a` selectors) and replies with an error, and follow mode never runs its
  position read with an unvalidated target (#21).
- `ControllerService` no longer keeps expired or fenced controller sessions
  in memory indefinitely. Opening a session drops older-generation entries
  (a late response for an already-fenced session never displaces a newer
  one), expired leases are swept on each action request, and a session is
  dropped once a rejected request or failed renewal is confirmed to be
  caused by the session no longer being valid (#26).
- `McpqClient.connect` now cleans up the MCPQ client created by each failed
  attempt instead of leaving it to garbage collection.
- `McpqClient.disconnect` now shuts down the MCPQ client explicitly. It
  previously only dropped the reference, leaving cleanup to garbage
  collection, which could run inside another thread's gRPC call and deadlock
  the bridge (reproduced as a hang in the integration suite).
- Safety reflexes no longer wait for a long strategic movement to finish.
  `walk_to`, `move_forward`, `move_back`, and `sprint` stop between steps once
  a reflex is waiting and report `interrupted` (#22).
- Fixed the 12 mypy errors in `llm/client.py`, `rcon.py`, and
  `goal_manager.py`, and made mypy a required CI step (#2). Goal
  decomposition now returns an empty list when a provider's JSON holds a
  non-list `subgoals` value, and non-function tool calls fall back to text
  parsing instead of raising.

- Fixed automatic healing repeatedly selecting golden carrots that the
  safety-authorized HEAL action rejects. Golden carrots remain eligible for
  ordinary auto-consumption; healing now selects only supported golden apples.

- Preserved failed safety interventions when a slow journal coalesces event
  writes. The bounded summary records outcome counts by action and remains
  CRITICAL if any coalesced intervention failed.

- Combined Authz and caller-supplied action policies so a caller's explicit
  `allow_privileged=False` restriction cannot be overridden by world-admin
  authorization. Also restored collection of the preservation observation
  boundary regression test.

- Protected in-game chat control commands with a default-deny allowlist based
  on MCPQ event player metadata. Configure UUIDs where available, or explicit
  authenticated player names for current MCPQ ChatEvent; all control commands
  require an explicit online-mode assertion because MCPQ exposes no UUID.
  Offline-mode names are forgeable and denied. `!status` and `!help` remain
  public. See `docs/AGENT.md` for single-player setup.

- Added `AUTHZ_WORLD_ADMIN_TOKEN` for a separately granted `admin:world`
  credential. Authz now requires world-admin authorization for internal chat
  teleports and startup lifecycle mutations as well as simulated gameplay;
  the internal caller capability alone proves origin, not administrative
  permission. Complex chat allowlist environment values are decoded as JSON
  arrays before config validation.

- When the configured controller session expires or is revoked, the
  orchestrator now suspends strategic work and remains safety-only until an
  explicit runtime stop. Authorization loss no longer counts toward the
  consecutive-failure shutdown threshold. Suspended chat polling accepts only
  authenticated `!stop`, and stop acknowledgement is time-bounded. Blocked
  initial or chat-triggered goal decomposition is canceled when authority is
  lost, and a reassignment is not acknowledged as successful.
- The built-in orchestrator renews its finite controller session while its
  persisted credential, ownership, and fencing generation remain valid.
  Renewal cannot revive expired authority; losing the lease suspends strategic
  work while the safety supervisor remains active. Renewal and event-loop
  session checks fail fast under Authz/SQLite lock contention instead of
  blocking safety polling.
- Shutdown now completes renewal and safety-worker stops and Authz release/close
  before propagating repeated cancellation; blocking SQLite cleanup runs off the
  asyncio event loop.
- `move_to` and `teleport` now require finite numeric coordinates. They were
  interpolated unvalidated into a console `tp` command.
- `place_block`, `craft_item`, and `drop_item` now accept only plain block
  and item IDs (optional `minecraft:` namespace) and amounts from 1 to 64.
  These values were interpolated unvalidated into server commands, so block
  or item data in a request could make a placed block run its own command.
- Action security classification now fails closed. Only `chat`, `wait`,
  and `done` are survival actions; an ActionType missing from every class is
  rejected before policy instead of defaulting to survival, which an
  ordinary controller session could use without `admin:world`.
- The CLI exits with code 3 when it shut down after losing controller
  authority, instead of 0, so supervisors and scripts can tell it apart
  from a normal finish.
- Safety-only operation after losing controller authority is now bounded by
  `authz.suspended_shutdown_seconds` (default 300). Previously the bridge
  idled forever when no authorized `!stop` was possible, e.g. on offline-mode
  servers. Chat poll failures while suspended are now logged.
- A session check that cannot complete because of Authz lock or SQLite
  contention now defers the current strategic step and retries, instead of
  permanently suspending strategy. Only a definitive denial or lease expiry
  suspends it.
- Enabled Authz now requires `AUTHZ_WORLD_ADMIN_TOKEN` at startup. Without
  it, bot provisioning and safe spawn were silently rejected and the bridge
  started with no bot entity.
- A safety intervention that completes in the same tick as authority loss
  is now recorded in memory instead of being discarded.
- The safety supervisor's single pending slot now holds the newest
  intervention, and a later success never replaces an unconsumed failure.
  Previously the strategic loop saw only the oldest, stale result.
- Safety supervisor polls no longer write observations to the agent's
  short-term memory. On a live server every 0.25s poll differed from the
  last, so polls evicted the action history the LLM sees within seconds.
- The `openrouter` provider now honours `llm.openrouter_base_url` instead of
  always calling openrouter.ai, so OpenAI-compatible routers such as Requesty
  work. Integration tests use Requesty when only `REQUESTY_API_KEY` is set.
- Controller session renewal now runs at a third of the lease instead of
  every five seconds, and retries storage errors within five seconds.

### Changed

- `ControllerService` no longer grants privileged-simulated actions to every
  controller session when the operator supplies `world_admin_token`. A
  controller credential also needs the delegation scope for the action's
  group: `steve:world:move`, `steve:world:edit`, `steve:world:items`, or
  `steve:world:combat` (#24). The built-in CLI agent is unaffected.

- **Migration:** custom `action_policy` callables must now return
  `PolicyDecision(..., allow_privileged=True)` to permit privileged-simulated,
  internal-control, and lifecycle actions (bot provisioning, safe spawn, follow,
  `!goto`/`!come`). A plain `PolicyDecision(True)` rejects them, and the
  rejection message now says so. Bridges without a custom policy are unaffected.

### Added

- Added reach limits for external controllers (#25): `ControllerService` rejects
  block edits with explicit coordinates more than 6 blocks from the bot, and
  `teleport`/`move_to`/`walk_to` destinations more than 32 blocks away or outside
  y -64..320, with reason code `OUT_OF_REACH` and nothing dispatched.
  `minecraft-ai-bridge-mcp` exposes `--max-edit-distance` and
  `--max-move-distance`. The check runs under the dispatch lock right before
  the backend call, so it is serialized with the controller's other requests
  (movement by other processes, players, or physics is not fenced), and
  replays of completed requests return their original outcome.
  `walk_to` is capped at its 50-block walking range so it never turns into a
  teleport, and limits must be finite. Targets whose coordinates are not
  finite numbers are refused with `INVALID_TARGET` instead of being passed to
  the handler. The built-in CLI agent is not limited.

- Added an MCP server for external agent harnesses: `minecraft-ai-bridge-mcp`
  (install with `pip install minecraft-ai-bridge[mcp]`) serves one Steve over
  stdio or Streamable HTTP with six tools over `ControllerService`. HTTP calls
  authenticate with their own `Authorization: Bearer` header and bind to
  loopback unless `--allow-remote` is given, which also requires
  `--allowed-host`; Host and Origin are always validated against DNS
  rebinding. stdio uses `MAB_CONTROLLER_TOKEN`. See `docs/CLI.md`.

- Added `runtime.SqliteActionOutcomeStore` for durable request-ID
  idempotency. Pass it to `ActionService` or `ControllerService` as
  `outcome_store=`; a retry after a bridge restart replays the stored outcome,
  and a dispatch interrupted by a crash is reported as unknown instead of
  running again (#23). Requests move `reserved` → `dispatching` → terminal
  with owner compare-and-set, so instances sharing a database dispatch a
  request ID at most once; outcome writes run off the event loop; expired
  outcomes leave tombstones for a 365-day idempotency window.
- `ControllerService` results now carry a stable `action_id` for the logical
  action, shared by replays and retries, and `ControllerService.aclose()`
  waits for pending outcome writes.

- Added `runtime.ControllerService`, a transport-independent controller API
  for one Steve (RFC-0001 Phase B slice). It opens exclusive Authz controller
  sessions, serves capabilities and state to observers, and runs
  `ActionRequestSchema` requests through the same action gate as the CLI.
  Privileged MCPQ actions need an operator-supplied world-admin token, which
  then applies to every controller session on that service. Only explicitly
  allowlisted actions are exposed. Requests are re-validated at the service
  boundary, and authorization is checked at dispatch time without treating
  Authz lock contention as an invalid session. No MCP transport yet.

- Added `compose.podman.yml`, a rootless Podman override for the Paper
  server, and documented building the bot plugin in a Java 25 container in
  `docs/SETUP.md`.
- Added `MC_BIND_ADDRESS` to `docker-compose.yml` to choose the host address
  for published ports (default `0.0.0.0`, unchanged).

- Separated built-in survival reflexes from strategic controller sessions with
  an in-process safety capability restricted to bounded mob attacks and
  inventory-backed eating/healing. Effects now require server-confirmed item
  consumption, item-free generic healing is denied, and combat selectors are
  bot-relative within six blocks. Authz classifies MCPQ command-simulated
  movement and mutations as privileged and requires the separate
  `admin:world` credential when Authz is enabled, while legacy local mode
  remains compatible.

- Added an opt-in local AuthzService gate for the built-in Orchestrator.
  Set `authz.enabled`, `authz.steve_id`, and the finite
  `authz.session_lease_seconds` in configuration, and provide the scoped
  bearer token only through `AUTHZ_CONTROLLER_TOKEN`. Startup requires a valid
  `steve:control` credential and exclusive session before provisioning;
  subsequent ActionService requests revalidate that session. Simulated and
  internal world mutations additionally require the separate
  `AUTHZ_WORLD_ADMIN_TOKEN`. Shutdown releases the session. The gate is
  disabled by default. Its built-in heartbeat renews only the existing valid
  session; it never reacquires a session after expiration or revocation. If a
  process exits uncleanly, startup reports an active
  session; after confirming the prior process stopped, an administrator can
  deactivate and reactivate the Steve to clear stale session state. This adds
  no external transport or backend watchdog.

- Added a local SQLite AuthzService foundation for scoped credentials,
  per-Steve ownership, exclusive controller sessions, and monotonic fencing.
  Plaintext credentials are returned once and only SHA-256 digests persist;
  global admin identities use one trusted local bootstrap and `admin:world`
  remains distinct from `admin:steves`. The ActionPolicy binding checks
  current session/credential state on each request. This adds no transport,
  network exposure, TLS, task persistence, or backend-local watchdog and does
  not complete RFC-0001 Phase B.

- Added in-place lease renewal for owned Steves through
  `AuthzService.renew_ownership`. A renewal must carry a timezone-aware
  expiry strictly in the future and strictly later than the persisted one,
  so a lease is only ever extended. Inactive, unclaimed, wrong-owner, and
  already expired ownership are refused; a lapsed lease is recovered by an
  administrator transfer under `admin:steves`. A renewal clears any active
  session and advances the fencing generation, so a session bound to the
  previous lease stops validating.

- Added append-only SQLite audit history for effective peer-observation
  grants and revocations. Entries include the holder and peer Steve IDs, the
  credential ID, actual administrator principal, event, and UTC timestamp.
  Duplicate no-op grant/revoke calls add no entries. An `admin:steves`-only
  API reads history by holder Steve ID, including after credential rotation.
  A one-time transactional migration records active legacy grants with an
  explicit unknown-original-time marker.

### Fixed

- Fixed attack target injection by restricting targeted and untargeted
  `/damage` selectors to curated attackable mob types. Player names,
  selector fragments, unknown types, and protected or critical mobs are
  rejected without issuing a command.

- Fixed last-admin lockout and unusable declared scopes in the local
  AuthzService. Bootstrap can recover when no valid `admin:steves` credential
  remains, even if an independent `admin:world` credential survives; bootstrap
  itself must include `admin:steves` and stays closed while provisioning
  authority exists. The last valid `admin:steves`
  credential cannot self-revoke until a replacement peer is confirmed.
  `steve:observe:shared` now requires an explicit administrator grant for
  each named peer Steve, while `events:read` is service level and requires a
  valid credential bound to an active Steve without binding the queried
  target. `steve:observe:self` and `steve:control` remain bound to the
  credential's Steve and persisted owner. This remains local authorization:
  there is no external MCP or network transport, network exposure, TLS,
  backend watchdog or fencing of dispatched work, task persistence,
  dispatch-time capability filtering. The built-in Orchestrator can opt into
  the local gate through `AuthzConfig`.

- Fixed additional Phase B authorization edge cases. Shared observation and
  `events:read` require the originating Steve to remain active, owned by its
  recorded owner, and unexpired; shared observation also requires an explicit
  active-peer grant. Scope authentication and evaluation share the process
  lock and a SQLite read snapshot, rotation migrates peer grants atomically,
  peer grants are
  idempotent and revocable with the granting admin principal recorded, and a
  Steve can be reactivated by an administrator without reviving an expired
  lease. Steve IDs cannot be empty. Failed SQLite commits are rolled back
  before the service lock is released. An `admin:world`-only credential may
  self-revoke without affecting the last-admin:steves guard.

- Fixed scope checks that ignored a Steve's current owner. Steve-bound scope
  checks now re-read persisted state and fail closed unless the Steve is
  active, its ownership is unexpired, and the persisted owner still matches
  the credential, so an ownership transfer immediately invalidates the former
  owner's credentials for scope checks as well as for session acquisition.

- Fixed revocation that could be performed by any credential sharing a
  Steve and owner with the target. A non-admin credential can no longer revoke
  a different credential; revoking another credential, including an admin
  credential, requires a global admin holding `admin:steves`. A presented
  credential may still rotate or revoke itself.

- Fixed admin credentials that could neither rotate nor revoke themselves.
  Admin rotation preserves the principal, scopes, and expiry, revokes the old
  token exactly once, and returns a working replacement; a valid admin
  credential can also revoke itself without passing its token twice. An
  `admin:world` credential still cannot act as `admin:steves`.

- Fixed a write-lock leak when a transaction could not start. A failed
  `BEGIN IMMEDIATE` now releases the service lock and attempts a safe
  rollback, so a transient database lock surfaces as an error instead of
  blocking every later call.

- Added administrator-configured outbound webhook delivery over the local
  EventJournal. Per-subscription cursors and durable pending/retry/delivered/
  dead-letter state provide at-least-once delivery, HMAC signatures, bounded
  retries/body size/rate, and offline-injectable HTTP transport. Endpoints
  require HTTPS and an exact host allowlist and reject unsafe resolved IPs;
  cursor order is preserved across retries and terminal state is reconciled
  with the journal after restart. The default HTTPX pinned transport connects
  directly to checked addresses while preserving TLS SNI and HTTP Host, with
  environment proxies and redirects disabled; compatible custom pinned
  transports remain injectable.
  There is no subscription management endpoint or external controller
  authorization/admin configuration integration. DNS and transport-pin errors
  remain visible and retry under the configured policy; DNS lookup uses a
  process-wide single-worker limit with immediate rejection at capacity.
  Timed-out lookups retain capacity until they finish, preventing thread
  buildup, while their results are discarded. Expired journal consumers
  reset to retained history, with
  lost pending payloads dead-lettered and prior terminal deliveries not replayed.
  Concurrent delivery passes on one service are serialized so a late duplicate
  cannot overwrite terminal state. Retry interval configuration rejects
  non-finite values. This is not full RFC-0001 Phase C completion.

- Added a SQLite-backed `EventJournal` for the RFC-0001 Event envelope.
  Events have durable monotonic cursors and idempotent event IDs; consumer
  polling is at-least-once until acknowledgment, and bounded payload
  retention expires lagging consumers explicitly so they must reset before
  resuming. Compact event-ID fingerprints remain after payload pruning to
  preserve duplicate detection. This local journal does not add auth, MCP
  transport, webhooks, safety event producers, or task integration.

- Added a built-in `SafetySupervisor` that periodically evaluates the existing
  self-preservation layer independently of the strategic LLM loop. A pending
  intervention preempts and cancels an outstanding LLM decision before a stale
  strategic action can dispatch; its single pending result is coalesced while
  safety observations and evaluation continue. Safety event writes are bounded
  to one active and one latest coalesced event. A strategic WAIT is
  interruptible and does not hold the ActionService dispatch lock. Other dispatch coroutines are
  serialized, and strategic requests queued but not yet dispatched are
  invalidated when a reflex starts. A canceled awaiter can still leave MCPQ
  thread work running.
  This is a runtime safety prerequisite slice, not completion of RFC-0001
  Phase C: it does not watchdog a blocked/disconnected backend, guarantee that
  an already-dispatched MCPQ call stopped on coroutine cancellation, or make
  flee decisions independent of the LLM.

- Added the `safety.intervened` event producer to the built-in
  `SafetySupervisor`. Events contain only the actual action and success flag,
  use HIGH severity on success and CRITICAL on failure/denial, and are
  appended to the configured persistent SQLite journal off the event loop.
  Pending writes are drained during orderly shutdown, and a reflex evaluation
  already in progress at shutdown is still recorded when it completes. Other
  RFC-0001 event producers remain unimplemented.

- `McpqWorldAdapter` now advertises a read-only `Capabilities` profile for
  simulated teleport movement, privileged-simulated crafting, and world-edit
  support. It leaves live version/dimension discovery and hazard sensors
  unclaimed; capability reporting does not grant authorization or provide
  independent safety assurance.

- Added RFC-0001 Phase A contract schemas in
  `minecraft_ai_bridge/runtime/contracts.py`: Pydantic v2 models for
  SteveIdentity, ControllerSession, Capabilities, Task, and Event,
  plus the ActionRequestSchema/ActionResultSchema action envelope (the
  Schema suffix avoids colliding with the unchanged runtime
  ActionRequest/ActionResult dataclasses). Strict identifier/status
  validation, timezone-aware timestamps, and safe defaults; these are
  schema definitions only: no authorization, fencing, persistence, event
  journal, task service, MCP transport, or conversion to the dispatcher
  dataclasses is included (later-phase RFC-0001 work).
- Added a transport-independent `WorldAdapter` Protocol and `McpqWorldAdapter`
  composition wrapper in `minecraft_ai_bridge/runtime/world_adapter.py`
  (RFC-0001 Phase A). The orchestrator creates exactly one adapter around its
  connected `McpqClient` and passes it to `ActionService`, `Observer`,
  `InventoryManager`, `ChatCommandHandler`, and `SelfPreservationLayer`;
  built-in runtime reads (position/info, hurt source, chat events,
  run-as-player and blocking commands, teleport, get/set block, chat post,
  time, online players, biome, connected) route through the adapter. The raw
  client remains only for connection lifecycle. Action handler, helper,
  dispatcher, pathfinding, and ActionService inputs are annotated against
  the transport-independent WorldAdapter contract rather than McpqClient;
  McpqWorldAdapter itself stays typed against McpqClient. Public APIs and
  direct mock construction are preserved; the protocol is a Phase A slice
  covering only the consumed method set, not a Mineflayer or other-backend
  contract, and no external MCP surface is added.

- Routed the built-in chat handler's !goto and !come teleport mutations
  through ActionService as two new internal-only actions (goto_teleport
  and come_teleport), so policy and audit distinguish chat-issued
  teleports from LLM strategic teleports. The target and sender names
  are untrusted chat-derived strings and are validated with the same
  strict Minecraft username validator used by follow-mode teleports and
  bot provisioning before command interpolation. Chat responses and
  error handling are preserved: ActionService and the dispatcher report
  failures as failed results rather than raising, so the handler now
  surfaces those results to the chat instead of only catching exceptions.
  A handler constructed without an orchestrator ActionService falls back
  to a local ActionService bound to its resolved world adapter, and a
  harness exposing only a raw MCPQ client has that client wrapped in
  McpqWorldAdapter automatically, so direct/test harness construction
  keeps working without a raw command bypass and legacy _mc-only
  harnesses keep polling, status, and chat replies working. Both new
  identities are rejected if a model response names them, joining the
  existing internal-action guard. Routing and input validation do not
  add authenticated chat sender authorization: RFC-0001 ownership and
  authorization requirements remain open.

- Introduced a policy hook and bounded in-memory audit trace for strategic
  actions routed from the built-in agent loop through `ActionService`.
  The default policy preserves existing behavior; external MCP access and
  complete mediation of non-strategic mutation paths are not part of this
  increment.
- Added process-local idempotency for `ActionService` calls that pass an
  explicit `request_id`: identical action+params payloads replay the stored
  terminal outcome, conflicting reuse is rejected without dispatching, and
  concurrent identical requests share a single backend dispatch. A dispatch
  interrupted after backend work started preserves an outcome-unknown
  terminal record so the same ID cannot repeat a possibly applied mutation.
  Records are in-memory only and are lost on process restart; calls without
  `request_id` keep the previous behavior.
  - Idempotency hardening: coalesced waiters no longer cancel the shared
    dispatch when one caller is cancelled; cancellation before backend
    dispatch started records a failed (not unknown) outcome; terminal
    records are isolated from returned results so caller mutation cannot
    corrupt replays; explicit-ID params must be JSON values only
    (`ValueError` otherwise) so distinct payloads can never fingerprint
    as equal; coalesced waiters now receive a snapshot of the outcome
    instead of the leader-owned result object; dispatch-started markers
    are cleared on every terminal outcome (including cancellation and
    eviction paths), preventing misclassification after ID reuse; and
    replay audits use the permission decision recorded with the original
    outcome instead of inferring it from status alone.
  - Fingerprint collision fix: canonical fingerprints previously folded
    tuples into lists, so a tuple and a JSON list that serialize to the
    same JSON both validated and fingerprinted equal under one
    `request_id`, replaying the first payload and skipping policy for the
    changed one. Explicit-ID params now accept JSON values only (tuples
    and non-finite floats raise `ValueError`), and the fingerprint
    preserves the exact validated payload, so the fingerprinted,
    policy-checked, and dispatched semantics always match.
  Accepted params are deep-copied before policy evaluation and
  fingerprinting so caller-side mutation of the original mapping cannot
  desynchronize the dispatched payload from the recorded one, and
  policy-rejected results now carry correlation (and request-ID) metadata
  consistent with other failure paths.

- Added an internal `follow_teleport` action and routed the built-in
  orchestrator's follow-mode teleport through `ActionService` so policy can
  identify it as a distinct action. The `!follow` target is validated as a
  plain Minecraft username (letters, digits, and underscores; entity
  selectors and command metacharacters are rejected) before it is embedded
  in the teleport command. Follow-mode behavior is otherwise unchanged:
  teleports happen only when the target is more than four blocks away, and
  read-only observation calls remain direct.
- Routed the orchestrator's trusted startup provisioning (the `/botsummon`
  bot summon and the teleport-plus-3x3-platform safe-spawn preparation)
  through `ActionService` as two additional internal actions
  (`provision_bot` and `prepare_safe_spawn`) so policy and audit distinguish
  startup provisioning from ordinary gameplay. The bot player name is
  validated as a strict Minecraft username before command interpolation.
  Retry timing, poll behavior, and degraded-start logging are unchanged;
  a failed provisioning result is logged deliberately instead of silently
  treated as success. Neither provisioning action is exposed to the LLM
  action space.
- Routed the self-preservation layer's mutating reflexes — reflex ATTACK,
  auto EAT, and auto HEAL — through the orchestrator's shared ActionService
  so they use the same policy hook and bounded audit trace as LLM-selected
  actions. Heuristic decision order and default behavior are unchanged, and
  read-only observation calls (SCAN_ENTITIES, LastHurtByEntity damage-source
  check) remain direct. A layer constructed directly without an
  ActionService keeps the previous dispatcher behavior, so this is still
  not complete mediation of the mutation surface, independent safety
  enforcement, or MCP/auth coverage.
- Added deterministic acceptance tests for the RFC-0001 Phase A action gate
  (`tests/test_runtime_action_gate.py`) covering strategic actions,
  follow-mode teleport, startup provisioning and safe-spawn preparation,
  chat-command teleports, and self-preservation reflexes, including policy
  denial paths (audited rejection without world mutation), internal-action
  protection, observation exclusion, and a production-wiring test that runs
  `Orchestrator._connect()` against the mock MCPQ client and asserts one
  shared ActionService across chat, self-preservation, and startup audit.
  - Internal-action guard: `provision_bot`, `prepare_safe_spawn`, and the
    existing `follow_teleport` are rejected at the LLM dispatch boundary in
    `_act` even if a model response names them, so text/JSON parsing cannot
    reach the trusted provisioning or follow identities with the same
    policy/audit action type as orchestrator-issued calls.
  - Platform placement preserves the previous per-write failure suppression:
    a failed corner no longer skips the remaining eight platform blocks.
  - Platform placement failures are now reported: `prepare_safe_spawn`
    attempts all nine support blocks independently and returns a failed
    result with per-block error detail when any required platform block
    cannot be placed, so the orchestrator retry loop treats an incomplete
    platform as a failure instead of success.

- **Survival series — survival mode tuning** (slice 3 of post-survival work):
  - `ActionType.HEAL` action handler that applies `/effect give @p
    regeneration 30 2` plus golden-apple-specific bonus effects
    (absorption 120s, instant_health II, fire_resistance, resistance
    for enchanted golden apples). Generic heal (no item) just
    applies regeneration.
  - **Auto-heal** in `SelfPreservationLayer` — when health drops
    below `health_critical_threshold` (default 4.0) AND a healing
    item is in inventory, triggers the HEAL action without waiting
    for the LLM. Priority: enchanted_golden_apple > golden_apple >
    golden_carrot.
  - **Critical health threshold** (`critical_health_threshold`,
    default 2.0) — below this, the agent always flees even from a
    single mob.
  - **Day/night awareness** — when the in-game time enters the
    night range (13 000–24 000 ticks), records a memory fact so
    the LLM sees it on the next turn and can choose defensive action.
  - `enable_auto_heal` and `enable_day_night_awareness` flags
    (both default True).
  - SYSTEM_PROMPT updated to document the new `heal` action.
- **Survival series — context-aware combat** (PR #9, slice 2 of post-survival work):
  - `_MOB_THREAT_LEVELS` table in `minecraft/actions.py` — per-mob
    threat classification (`low` / `medium` / `high` / `critical`)
    for all 28 detected hostile mobs plus extras. Creeper = high,
    zombie = low, skeleton = medium, warden = critical.
  - `_MOB_BLACKLIST` frozenset — mobs the agent must never attack:
    iron_golem, villager, wandering_trader, tamed wolf, tamed cat,
    tamed parrot, tamed equines (horse, donkey, mule, llama,
    trader_llama, ocelot), friendly aquatics (axolotl, dolphin,
    turtle, squid, glow_squid, frog), neutral but not aggressive
    (fox, bee, goat, strider, armadillo), and passive farm animals
    (pig, cow, sheep, chicken, mooshroom, rabbit).
  - `_get_threat_level()`, `_is_blacklisted()`, `_should_attack()`
    helpers. `_should_attack` returns False for blacklisted OR
    critical mobs.
  - `scan_entities` action returns three new structured fields:
    `detailed` (list of `{type, threat, should_attack}` per mob),
    `blacklisted` (mobs in range that must not be attacked),
    `too_dangerous` (critical mobs in range that should trigger
    flee). Backward compatible — `mobs` and `mobs_nearby` still work.
  - **Self-preservation layer** now respects the blacklist.
    Reflex attack uses `_pick_target()` to choose the highest-threat
    attackable mob, skipping blacklisted and critical ones. Threat
    assessment counts only attackable mobs against the flee
    threshold. A critical mob in range triggers flee even alone.
  - SYSTEM_PROMPT updated to document the new scan_entities fields.
- **Survival series — auto-consume food** (PR #8, slice 1 of post-survival work):
  - `ActionType.EAT` action handler. Takes `food_item` (e.g. `bread`,
    `golden_apple`) and an optional `slot` parameter. Equips the food
    to the player's hand, applies a saturation effect via
    `/effect give @p saturation` to restore hunger immediately, and
    clears one of the food items from inventory. Headless-friendly:
    no client interaction required to consume.
  - `_FOOD_ITEMS` table in `minecraft/actions.py` — 32 edible items
    with (item_id, hunger_restored, saturation_points) entries,
    covering tier 1 (enchanted_golden_apple) through tier 7 (cake).
    Sorted by saturation, descending.
  - `_is_food()` and `_food_value()` helpers. Strips the
    `minecraft:` namespace before lookup so both `bread` and
    `minecraft:bread` are recognised.
  - **Auto-consume** in `SelfPreservationLayer` — when hunger is
    below `hunger_critical_threshold` AND the player has food in
    inventory, the layer picks the highest-saturation food and
    triggers the EAT action **without waiting for the LLM**. Closes
    the "find food and then wait for the LLM to eat it" loop that
    the v0.5.1 design had.
  - `PreservationConfig.enable_auto_consume` flag (default True).
  - SYSTEM_PROMPT updated to document the new `eat` action.
- **Survival series — hunger observation** (PR #5):
  `ActionType.CHECK_HUNGER` action handler and `WorldState.hunger`
  field. The LLM can now see its food level (0–20) and react
  accordingly. The format_state prompt includes `Hunger: N/20`.
- **Survival series — combat capability** (PR #6):
  - `ActionType.SCAN_ENTITIES` action that detects 28 common hostile
    mob types within a configurable radius (default 16, capped).
    Returns both a per-mob presence map and a flat list of detected
    mobs.
  - `attack` action now accepts a `damage_amount` parameter (default
    4, clamped to [1, 64]) and returns a structured `target_hit`
    boolean so the LLM can branch on success without parsing strings.
- **Survival series — self-preservation loop** (PR #7):
  - `SelfPreservationLayer` (new module
    `bridge/self_preservation.py`) — runs after observation, before
    the LLM thinks. Detects health drops via
    `/data get entity @p LastHurtByEntity` so environmental damage
    (fall, cactus, fire, lava, drowning) does **not** trigger a
    reflex attack.
  - **Reflex attack**: when a hostile mob is in range and the
    threat is manageable (≤ `max_fightable_mobs`, health above
    `flee_health_threshold` with ≤ 1 hostile), attack the nearest
    mob without waiting for the LLM.
  - **Reflex flee**: when the threat is too high (many hostiles,
    or low health + multiple hostiles), inject an URGENT
    "Flee to safety" sub-goal at the front of the goal list and
    defer to the LLM. The original task naturally resumes when the
    agent is safe (that's the regroup).
  - **Find-food injection**: when `state.hunger < 6`, inject an
    URGENT "Find and eat food" sub-goal (no duplicates).
  - **Memory hint**: when health < 4, record a fact so the LLM
    sees the warning in its next context window.
- **Fallback plans** in `bridge/goal_manager.py`:
  - "Find and eat food" — 9-step plan for food/hunger-related
    goals. Action-verb-specific pattern so it doesn't shadow
    the existing farm plan.
  - "Flee to safety" — 8-step plan for flee/escape/retreat
    goals. Teaches the LLM to scan for hostiles, sprint away,
    find shelter, and regroup on the original task.
- Added `equip_best_weapon`, which selects the strongest supported melee
  weapon in the hotbar, avoids redundant replacement when it is already
  held, and reports inventory or equip failures without false success.

### Changed
- Docker uses a pinned Paper 26.2 build 123 with the pinned Java 25 server image, and the bot plugin uses the matching pinned Paperweight dev bundle. The plugin setup script builds and copies it automatically; upgrade the Paper version/build and image digest together.

- `bridge/orchestrator.py` calls `self._preservation.evaluate(world)`
  after `_observe()` and before the LLM decides. If the layer
  returns an `ActionResult`, that result is used as the action for
  the turn and the LLM is skipped. Bookkeeping (recording, failure
  tracking, termination check) runs unchanged.
- `minecraft_ai_bridge/minecraft/mc_api.py` exposes a new
  `get_hurt_by_entity() -> bool` method that reads the
  `LastHurtByEntity` NBT field via
  `/data get entity @p LastHurtByEntity`.
- The reflex-attack path in the orchestrator no longer triggers on
  environmental damage (fall, cactus, fire, etc.). This is a
  behavioral change from the first draft of the self-preservation
  layer, which reflex-attacked on any health drop.
- Observer hardening now parses inventory SNBT independent of field order,
  preserves valid partial compounds, rejects invalid health values without
  guessing, and reports custom max-health output separately.
- Biome detection now uses authoritative `/execute if biome` probes only,
  executes them in the configured player's dimension, covers common
  Overworld, Nether, and End biomes, and caches successful and `"unknown"`
  results per chunk to avoid repeated bounded probe sequences.
- Prompt context is now bounded by recent-entry and character budgets.
  Failed LLM-selected actions include an explicit recovery hint and receive
  at most one guarded retry decision on the next turn; identical retries
  are rejected without repeating side effects.
- Live integration CI is now opt-in via the `RUN_LIVE_INTEGRATION`
  repository variable and requires a configured Paper/MCPQ environment plus
  `OPENROUTER_API_KEY`; deterministic checks remain mandatory on every PR.

### Fixed

- Fixed the chat handler's service-only construction path: a harness
  exposing an ActionService without a world adapter or raw MCPQ client
  previously accepted the service but resolved no world, silently disabling
  chat-event polling, !status, and chat replies. ActionService now exposes
  a read-only world property, and ChatCommandHandler resolves the world
  adapter from the supplied service when the orchestrator has neither
  _world nor _mc; the shared service remains the teleport path.
- Survival preservation now scans curated hostile mobs plus critical threats, and auto-consume, auto-heal, and day/night awareness operate independently of the legacy reflex flags. Tick 24000 is correctly treated as daytime.
- PR #7 (initial) had a reflex attack that fired on **any** health
  drop, including fall damage, cactus damage, and lava damage.
  This caused the agent to charge a nearby mob because it tripped
  on a cactus — fixing this required a damage-source check
  (`LastHurtByEntity`) and a flee path for high-threat scenarios.
- `equip_best_weapon` now skips malformed inventory records instead of
  raising during weapon selection; nonzero item damage remains metadata-only
  until max-durability data is available.
- Fixed the RFC-0001 Phase A contract schemas to reject non-JSON payload
  values at validation time: `ActionRequestSchema.params` and
  `Event.payload` now enforce the exact JSON domain recursively (null,
  bool, int, finite float, str, lists, string-keyed mappings) and reject
  tuples, non-finite floats, non-string mapping keys, and arbitrary
  objects without coercion, so successfully constructed contracts always
  serialize via `model_dump_json()`.
- Hardened the same payload validation against Pydantic lax dict
  coercion and invalid Unicode: `ActionRequestSchema.params` and
  `Event.payload` now validate the entire input mapping before Pydantic
  coercion, rejecting non-dict inputs (e.g. lists of pairs) and
  non-string keys (bytes, int, bool) instead of normalizing them, and
  every string and mapping key must encode as UTF-8, so lone surrogates
  can no longer construct a contract that fails `model_dump_json()`.
- Bounded JSON payload nesting in `ActionRequestSchema.params` and
  `Event.payload` with `MAX_JSON_PAYLOAD_DEPTH` (64): every dict or
  list counts as one level starting at the outer payload mapping itself,
  deeper structures are rejected before model construction, and the
  `model_dump_json()` serialization guarantee now explicitly applies
  only to payloads within this depth.

### Removed

- Removed the unreachable standalone `blacklisted` field from `scan_entities`; blacklist safety remains represented by each detected entity's `detailed[].should_attack` value.

---

## [0.5.1] — 2026-07-11

### Added

- `ROADMAP.md` — current priorities, completed milestones, release
  plan, and testing strategy.
- Tracking issue #2 for re-enabling `mypy` in CI.

### Changed

- CI restructured: `lint`, `Unit Tests`, and `Integration Tests`
  jobs run separately. Unit tests run on every PR; integration
  tests run on pushes to `main`/`master` with the
  `OPENROUTER_API_KEY` secret configured.
- 54 pre-existing `ruff check` warnings resolved (44 auto-fixed,
  10 manual). `ruff check`, `ruff format`, and `ruff check
  --select I` are now all clean.
- `ActionType` and `Role` migrated from `(str, Enum)` to
  `StrEnum` (Python 3.11+ idiom, ruff UP042). `str(X.MEMBER)` now
  returns the value instead of the name — no existing call sites
  depend on the name form.
- `_move_forward` no longer auto-steps over hazards (lava, fire,
  cactus, etc.). Real safety fix.
- Stale test assertions (`test_turn_left/right/jump`,
  `test_move_forward_hazard`) updated to match current command
  format. These were silently failing on master before the fix.

### Removed

- `RCONDisconnected` exception renamed to
  `RCONDisconnectedError` to satisfy `ruff` N818 (Error suffix).
  The class is internal to `rcon.py` (no usages outside the file)
  and `rcon.py` is documented as unmaintained, so this is a
  internal-only rename.

---

## [0.5.0] — 2026-07-06

### Added

- A* pathfinding for human-like walking via
  `minecraft_ai_bridge/minecraft/pathfinding.py`.
- Sprint action for faster forward movement (1.0-block steps
  with reduced collision checking).
- Execute-based WASD movement — replaces teleport-based movement
  with physics-based step-by-step walking that respects collisions
  and avoids hazards.
- Ollama fallback: when JSON mode is rejected, the Ollama client
  retries without forced JSON format instead of failing the
  request.
- 160 unit tests covering the full action surface (up from ~30
  in v0.4.0).
