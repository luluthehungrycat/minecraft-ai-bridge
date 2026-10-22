# RFC-0001: Harness-Agnostic Minecraft Agent Runtime and Control Plane

- **Status:** Draft — for discussion; not yet accepted or implemented
- **Date:** 2026-10-22
- **Repository:** `luluthehungrycat/minecraft-ai-bridge`
- **Proposed path:** `docs/rfcs/RFC-0001-harness-agnostic-agent-runtime.md`
- **Scope:** Architectural direction and staged migration, not an implementation PR

## 1. Summary

Evolve **minecraft-ai-bridge** from a primarily self-contained LLM-driven Minecraft bot into a **harness-agnostic agent execution environment**. Keep its built-in Python CLI agent as an immediately usable reference frontend, while making external harnesses (Hermes Agent, Claude Code, Codex, OpenCode, OpenClaw, and others) first-class clients through an authenticated MCP server and a common runtime API.

The bridge, **not the calling agent**, retains authoritative control of actions, access policies, task state, safety and emergency responses. Each in-game Steve has an independent identity and controller credential; optional read-only grants permit multi-agent coordination. Ownership may be persistent or time-limited. A durable event bus notifies clients of unexpected events through polling, streams, and configured webhooks, without making safety depend on the external harness responding.

Support both the existing MCPQ-backed server-side fake player and an optional Mineflayer-backed protocol client, with backend capabilities made explicit. Establish extension points for deterministic reflex routines, an optional low-latency tactical decision model, reproducible benchmarks, and semantic Git-based Minecraft world versioning.

**Core invariant:** All normal agent-originated mutations must traverse the same authorized, safety-mediated action pipeline, regardless of whether they originate from the built-in loop, MCP, an HTTP client, or a future adapter.

## 2. Motivation and existing baseline

As documented in the current repository (v0.5.1, unreleased), the bridge already has:

- A Python 3.11+ async observe–think–act loop, multiple LLM providers, goal decomposition, persistent memory, and a CLI.
- MCPQ/gRPC access to a Paper server and a custom server-side fake-player plugin, **not** a Minecraft-protocol headless client.
- An action dispatcher, world observer, 2D-grid A* waypoint planner and incremental execute/teleport-based “WASD-like” movement. This is **not** equivalent to player-protocol movement physics.
- A `SelfPreservationLayer` invoked in the built-in orchestrator's observation loop. It can attack reactively, suggest fleeing, and manage hunger/healing, but its timing and placement do not yet constitute a backend-independent real-time safety authority. In particular, some fleeing is delegated back to the LLM.
- A documented deterministic test suite and server/LLM integration-test infrastructure. The RFC does **not** assert that those tests were run during its drafting.

These are assets to preserve and refactor incrementally. The present architectural hazard is that exposing low-level action handlers directly through MCP could bypass checks currently housed inside the built-in orchestrator.

## 3. Goals, non-goals, and principles

### Goals

1. The same authorized agent-facing semantics for built-in CLI and external harnesses; consistent results and comparable permissions.
2. Independently controllable Steves, one active strategic controller per Steve, and optional scoped read-only observations of peers.
3. Persistent or expiring controller ownership, safe reconnect and explicit handoff/revocation.
4. Fast emergency reactions that continue when an external agent is disconnected or slow; optional tactical model **cannot** override hard policy.
5. Backend-independence for MCPQ and Mineflayer, without pretending their capabilities or gameplay fidelity are identical.
6. Durable tasks, interrupts, events and replayable audit records; eventual semantic world diff/merge and agent benchmarking.
7. Low-friction standalone setup: `minecraft-ai-bridge "Build a house"` remains viable, with a documented server/service mode for MCP clients.

### Non-goals for RFC-0001 implementation

- Immediate parity of MCPQ and Mineflayer actions or immediate support for every Minecraft edition/version/mod.
- Rewriting the core Python planning loop in JavaScript.
- Guaranteeing survival against arbitrary game hazards or server failures.
- Replacing Minecraft's full simulation with a seed-derived deterministic world model.
- Implementing live multi-user Git merges in a running world in the first milestone.
- Depending on Flowloom, Hermes Agent or any specific external harness for basic operation.

### Design principles

- **Server-enforced, not prompt-enforced:** Authorization, policy and safety do not rely on what tool schemas or system prompts tell an LLM.
- **Single mutation authority:** Every privileged backend action is reachable only through policy-controlled services; avoid exporting raw `runCommand`, `setBlock`, `/give`, or `/tp` to ordinary survival controllers.
- **Fail closed for new agent commands; degrade safely for ongoing gameplay:** If an ownership check or critical observation fails, reject or pause unsafe commands. The backend-local watchdog attempts an appropriate safe state; do not promise safety after total process/server failure.
- **Separate identity, credential, ownership, and session:** They are different concepts and must not be conflated.
- **Explicit backend capabilities:** Tool discovery exposes supported operations and whether they are physical, simulated, or privileged.
- **Observable decisions:** Rejections, interventions, edits, and failures have structured reasons, provenance and correlation IDs.

## 4. Architecture

```text
CLI reference agent         MCP clients (Hermes, Codex, etc.)     Future HTTP clients
      |                                  |                                |
      +---------------------+------------+--------------------------------+
                            |
             Authenticated transport/API adapters
                      (MCP / internal API)
                            |
        +-------------------v-----------------------+
        |          Shared agent runtime             |
        | identities / scopes / controller sessions |
        | task state / capability discovery         |
        | event journal / subscriptions / telemetry |
        | validated commands + action scheduler     |
        +-------------------+-----------------------+
                            |
          +-----------------v----------------------+
          | Mandatory policy + safety supervisor   |
          | hard constraints / emergency preempt   |
          | tactical selector (optional, bounded)  |
          +-----------------+----------------------+
                            |
               Backend-specific action adapter
                  /                     \
           MCPQ + Paper             Mineflayer sidecar
           server-side bot          Minecraft protocol bot
                  \                     /
                   Minecraft Java world

Future sibling subsystem: snapshot/semantic-world-versioning service
receives authoritative checkpoints and attributed edit events.
```

The Python process hosts the control plane. A Mineflayer integration, if used, is a **Node.js sidecar** implementing a narrow authenticated internal protocol, not a duplicate strategic agent. Its local reflex/watchdog code runs close to physics/gameplay events. Backend selection is per Steve; a Steve is not simultaneously controlled by two independent player implementations. The existing Paper fake player and a Mineflayer protocol client may be distinct in-game identities and require migration/spawn semantics.

### Required internal boundaries

- `AgentRuntime`: lifecycle for Steves, tasks, controllers and events.
- `ActionService`: validates and schedules commands; sole public mutation entry point.
- `SafetySupervisor`: mandatory action gate, hazard interrupts, recovery and backend-watchdog coordination.
- `WorldAdapter`: normalized observations/actions plus capability advertisement; implementations `McpqAdapter` and optional `MineflayerAdapter`.
- `EventService`: append-only events, cursors, subscriptions, delivery adapters.
- `AuthzService`: credentials, access grants, ownership and fencing.
- `BuiltinAgentFrontend`: existing goal manager/LLM client refactored to use `ActionService`, not raw MCPQ handlers.

Use Pydantic models for schemas and adapter contracts. An incremental refactor is preferred over renaming all existing packages immediately.

## 5. Steve identity, credentials and ownership

Each `steve_id` is stable and bound to one selected backend instance/player identity. A key is a **credential**, not the Steve's identity. At least the following scopes are proposed:

| Scope | Meaning |
|---|---|
| `steve:observe:self` | Inspect this Steve's state and authorized history |
| `steve:control` | Submit permitted actions and manage own tasks |
| `steve:observe:shared` | Read specifically shared fields for named peer Steves |
| `events:read` | Retrieve authorized events / maintain a subscription |
| `admin:steves` | Provision, reassign, revoke and configure bots; separate admin identity |
| `admin:world` | Privileged world edits/snapshots, separately granted |

Default configuration: one controller key for one Steve, with `observe:self` and `control`; optional independent observer keys/grants. Scope checks happen **on every request and event read**, including stream replay. Peer visibility is opt-in, field-filtered, world/dimension scoped, and excludes secrets, raw credentials and private task context unless explicitly published.

Supported ownership modes:

- `persistent`: assigned controller remains owner across disconnections, restarts and indefinite idle periods until explicit revoke/transfer; **not** an instruction to keep playing forever.
- `leased`: configured long or short expiry plus renewal; reconnect need not require reassignment.
- `unclaimed`: no strategic controller until claimed by an authorized principal.

One active **controller session** per Steve, even if a key was copied to several processes. A new session/takeover advances a monotonically increasing **fencing generation**. Every queued or long-running command is bound to its generation; stale commands fail after reassignment. A controller may gracefully release control; administrator transfer can force-fence a prior controller. An ownership transfer has a deterministic policy for pending work (`pause`, `cancel`, or `handoff`), default `pause`.

Store only hashed API keys, show plaintext at creation, support rotation/revocation and audit access. Store durable state transactionally (SQLite acceptable initially). Use TLS or trusted local sockets, never expose unauthenticated control over public network interfaces. Rate-limit sessions/commands; distinguish operational roles from Minecraft server operators.

A separate admin stop/freeze facility must always be available. A disconnected controller cannot issue new commands; depending on configured idle policy, Steve stays safe-idle, shelters, or performs explicitly permitted upkeep.

## 6. Unified action contract and MCP surface

The transport-independent `ActionService` is the authoritative API. MCP is **one adapter**, not the core execution engine. Recommended initial MCP transport is an authenticated Streamable HTTP endpoint for remote clients, with stdio as an optional local wrapper if useful. Verify client support and SDK compatibility during implementation.

Proposed high-level MCP tool groups (names illustrative, to be reconciled against existing `ActionType` definitions):

- `minecraft.get_capabilities`, `minecraft.get_state`, `minecraft.scan`, `minecraft.get_inventory`
- `minecraft.submit_action` or named action tools such as `minecraft.move_to`, `minecraft.dig`, `minecraft.place_block`, `minecraft.interact`, `minecraft.craft`, `minecraft.attack`
- `minecraft.get_task`, `minecraft.create_task`, `minecraft.pause_task`, `minecraft.resume_task`, `minecraft.cancel_task`
- `minecraft.get_events`, `minecraft.ack_event`, `minecraft.get_shared_peer_state`
- Optional `minecraft.get_action_status` and `minecraft.cancel_action` for operations that last longer than a request

Do **not** expose arbitrary console commands, OP commands, broad server-side NBT mutations, direct MCPQ handles, raw Mineflayer control states or policy-disabling controls on ordinary controller keys. Administrative capabilities belong on a separately authorized interface, preferably a separate service/network boundary. A client cannot bypass hard safety by sending `avoid_hazards=false`, changing tool names, constructing raw payloads or replaying old requests.

Long-running actions are asynchronous and support cancellation. Request fields include `steve_id`, `controller_session_id`, `generation`, `request_id` (idempotency), action type, validated parameters, task association and expected-state revision when relevant. Responses distinguish `accepted`, `running`, `succeeded`, `failed`, `rejected`, `interrupted`, and `cancelled`. Task/action IDs are durable across agent reconnects.

Example **proposed** response:

```json
{
  "action_id": "act-123",
  "steve_id": "steve-a",
  "status": "interrupted",
  "reason_code": "HOSTILE_ENTITY_NEARBY",
  "task_id": "build-001",
  "safety_action": "RETREAT",
  "state_revision": 482,
  "resumable": true
}
```

Capability discovery must report backend and limitations, e.g. `movement: physical|teleport_simulated`, `crafting: vanilla|privileged_simulated|unsupported`, `world_edit: true|false`, interaction distance, supported versions, dimensions, hazard-sensor availability. Unsupported actions return explicit errors; they never silently escalate to admin commands.

## 7. Safety, reflexes and tactical models

**All controllers, including the built-in loop, use the same action gate.** Refactor the current `SelfPreservationLayer` into independently scheduled, testable safety services; preserve its proven heuristics where appropriate, but stop depending on the built-in LLM turn loop to notice imminent threats.

Three decision levels:

1. **Hard policy / deterministic reflex layer** (mandatory): allow/deny mutations, collision and hazard checks, life-threatening interrupt rules, emergency stop/retreat where supported, starvation/health protections, and watchdogs. Backend-local real-time reactions are preferable where practical.
2. **Tactical selector** (optional): a constrained, local low-latency classifier/policy network/decision model (Jev/SystemOne-like) chooses *among authorized short-term actions*. Validate every suggestion through level 1; impose inference budgets, stale-observation checks and deterministic fallback.
3. **Strategic agent** (internal or external LLM): goal decomposition, crafting/building plans, resource allocation and replanning. Its requests may be preempted or denied.

Treat fast response as a measured requirement, not a guaranteed model property. Nominal Minecraft server ticks are approximately 50 ms, but load, network, sidecar and model latency vary. Define performance thresholds experimentally. Safety coverage must be documented separately for MCPQ's simulated/server-command movement versus Mineflayer's physical gameplay. An MCPQ bridge subprocess cannot magically prevent damage while its whole process is down; ensure the backend can enter the safest supported idle/stop state and report degraded protection.

Interrupt lifecycle: `running -> interrupted -> emergency_active -> safe|failed -> awaiting_replan -> resumed|revised|cancelled`. If emergency action is impossible, log/notify and fail closed for ordinary follow-on actions. Resumption checks conditions and plan validity; do not blindly replay stale placements/movement.

Safety policies have **hard constraints** and **configurable preferences**. Do not make normal combat/mining impossible by treating all risk as forbidden. Only a clearly authenticated administrator may change hard-policy configuration, and only according to deployment policy; controller tools cannot.

## 8. Event bus, external wakeups and webhooks

Publish structured events for `player.attacked`, `player.health_critical`, `player.starving`, `player.died`, `world.explosion`, `world.structure_damaged`, `task.interrupted`, `task.completed`, `controller.changed`, `backend.disconnected`, `safety.intervened`, and `world.snapshot_created`, among others.

Common envelope:

```json
{
  "event_id": "evt-4821",
  "schema_version": 1,
  "steve_id": "steve-a",
  "task_id": "build-001",
  "type": "world.structure_damaged",
  "severity": "high",
  "occurred_at": "2026-10-22T11:00:00Z",
  "source": "minecraft_server",
  "causal_action_id": null,
  "payload": {
    "cause": "creeper_explosion",
    "position": [125, 64, -32],
    "affected_blocks": 18
  }
}
```

The example is illustrative; damage attribution may be unknown and must not be fabricated. Capture an authoritative event timestamp, source and confidence/visibility scope when possible. Avoid inferring a specific mob purely from changed blocks without evidence.

Event delivery options:

- **Durable polling:** `get_events(cursor, limit)` and `ack_event` for any MCP-capable harness; at-least-once semantics, deduplication and bounded retention.
- **Stream:** authenticated SSE/WebSocket where helpful, using stable cursor-based recovery.
- **Outbound webhooks:** administrator-configured subscriptions with signature verification (e.g. HMAC), timestamp, delivery ID, retry with exponential backoff, delivery/dead-letter visibility, URL allowlisting, SSRF protection, body size and rate limits.
- **Harness-specific wake-up adapters:** explicitly opt-in connectors that can reliably enqueue a prompt/event to a particular harness or persistent agent profile.

**Critical limitation:** MCP server notifications do not mean all clients will wake a dormant agent or interrupt model generation. The bridge must never depend on this to stop a creeper attack. If a harness cannot receive push events, polling plus a documented adapter is the fallback. Event storms should be aggregated/rate-limited without losing critical incidents. Enforce peer-observation ACLs for delivery and replay.

## 9. Tasks, multi-agent collaboration and provenance

Tasks are durable runtime objects with owner, status, goal tree/steps, checkpoints, optional spatial reservation and event cursor. Tasks may outlive an MCP connection. Strategic controller ownership governs who can change the task; the bridge owns state transitions and emergency interruption independently.

For multiple Steves working on a shared build:

- Share opt-in, read-only peer state: position/dimension, broad health/threat status, published task summary, claimed region, and progress. Use explicit grants.
- Allow shared team objectives and coordination messages as a later feature; avoid forcing Flowloom or a particular agent framework.
- Prevent simultaneous conflicting edits through spatial work zones/reservations and optimistic block- or region-revision checks. Reservations are advisories unless enforced by the ActionService.
- Record **intent** (requested actions), **result** (backend acknowledged) and **observation** (world actually changed) separately. A creeper, human player or another bot can modify the world after an agent's placement.
- Apply safety checks to collaboration tools and task delegation; a controller cannot gain another Steve's permissions through delegated commands.

## 10. World versioning extension: Git-friendly semantic changes

World version control is a **related but separately staged** subsystem. Its first implementation should not be a precondition for the MCP/control-plane MVP.

Desired interfaces: snapshot/import, semantic diff, annotated change history, region/block-level blame, validation, branch/fork/merge/reapply and export. Prefer a canonical Git-facing representation of chunks/patches plus an explicit import/export step over pretending `.mca` is text or that Git automatically splits Anvil files through a clean filter.

Support three edit sources:

1. **Live Mineflayer gameplay** — player-level intent and observed changes within visible/loaded chunks.
2. **Live MCPQ administration/creative editing** — privileged edit operations with correlation IDs and checked world-side effects.
3. **Offline Anvil/NBT processing** — full-save snapshots and semantic chunk diffs; ensure the server is quiesced or a consistent backup/snapshot is taken before reading.

Track dimension, coordinates, block states, block entities, inventories/entities as applicable, Minecraft data version, generator settings, datapacks/mods, server/world identity, hashes and provenance. Distinguish world observations from confirmed edits; unseen chunks cannot be assumed unchanged.

**Seed-aware optimization is optional, not the baseline**: a world seed alone is not sufficient to reconstruct a byte- or block-identical generated world. Generation version, settings, dimensions, datapacks/mods, chunk generation state and nondeterministic/dynamic activity matter. Start with reliable materialized snapshots (possibly chunk-level deduplication); later implement generated baselines plus sparse mutations only when hashes/semantic validation prove equivalent. Cache regeneration and fall back to saved chunk data whenever equivalence fails.

Semantic merge must detect overlapping block/state/NBT/entity changes; non-overlap does not always mean gameplay-safe (e.g. redstone, gravity, water). Require a post-merge validation and optional Minecraft server smoke test. Never hot-merge an unvalidated offline save into a live world.

Potential follow-up RFC: **RFC-0002: Semantic World Snapshots, Diffs and Git Integration**.

## 11. Benchmarking and reproducibility

The built-in CLI agent remains the minimal reference implementation, not a privileged special case. External harnesses are the primary interoperability target. Benchmarks must disclose and pin:

- Minecraft version/server build, plugins/mods/datapacks, difficulty and gamerules, world snapshot/seed, starting Steve state and allocated resources.
- Backend (`MCPQ` simulated/privileged versus `Mineflayer` physical), available tools and permission policy.
- Model/provider/harness/version, prompt/task specification, memory policy, max steps, runtime and inference budget.
- Safety policy version, actual interventions, event delivery characteristics and failures.

Report task success (prefer independently validated world-state outcomes), completion time/ticks, tool calls, tokens/cost when available, damage/deaths, unsafe/rejected actions, preemptions, recovery and reproducibility. Compare agents **within the same backend/permission profile** first; treat cross-backend comparisons as different benchmark tracks. Keep full action/event traces, but redact secrets and any sensitive player chat.

## 12. Operations, trust boundaries and threats

A critical part of implementation is reducing raw backend authority. The current MCPQ plugin and server console can have privileges above a survival bot. Restrict access to MCPQ/gRPC, RCON, server console, plugin configuration, writable world saves, container runtime and admin keys so external harness processes cannot bypass ActionService. Prefer a private network or Unix socket between internal services. Review the current plugin bind (`0.0.0.0:1789` in repository documentation) before any remotely exposed deployment.

Threat model includes stolen/copied keys; malicious MCP clients and prompt injection in chat/signs/books/world data; stale controller sessions; replayed requests; webhook SSRF; oversized scans/DoS; unsafe raw commands; ambiguous actor attribution; corrupted saves; and compromised third-party plugins. Treat world/player-generated text as untrusted data. Apply payload limits, idempotency, bounded actions, timeouts, path validation and audit logs.

On control-plane restart, restore assignments, task checkpoints and event cursors, reconcile actual world state, and **never replay a non-idempotent command without an explicit reconciliation check**. On backend disconnect, mark capability/safety degradation, pause pending mutations and emit an event.

## 13. Staged delivery and acceptance criteria

### Phase A — contracts and compatibility (first implementation target)

- Define `SteveIdentity`, `ControllerSession`, `Capabilities`, `ActionRequest`, `ActionResult`, `Task`, and `Event` schemas.
- Extract the existing dispatch path into a shared `ActionService`; route **the built-in CLI** through it first.
- Introduce `WorldAdapter` wrapping existing MCPQ without breaking the working CLI.
- Add authorization hooks, a single mutation choke point, idempotency and audit tracing.
- **Gate:** deterministic tests prove built-in CLI cannot bypass the shared action path; existing action behavior remains compatible.

### Phase B — MCP and ownership

- Authenticated MCP service; scope-filtered capabilities and observations; exclusive controller sessions.
- Persistent/leased/unclaimed ownership, rotation/revocation, fencing, disconnect policy.
- **Gate:** two concurrent controllers cannot both mutate the same Steve; observers cannot mutate; privileged MCPQ commands are inaccessible to ordinary keys.

### Phase C — safety separation and events

- Extract `SelfPreservationLayer` from the LLM turn loop into a runtime supervisor; backend watchdog/interrupt integration.
- Durable event journal, polling/replay, webhook subscriptions, delivery retries and safe defaults.
- **Gate:** emergency response is triggered without any LLM answer and continues across an external-client disconnect *within the supported backend's documented failure model*; duplicate events/replayed commands do not cause duplicate mutations.

### Phase D — Mineflayer backend

- Optional Node.js sidecar and protocol-backed Steve; plugin-assisted physical movement/pathfinding/inventory/crafting.
- Backend capability parity matrix; verify Mineflayer's actual compatible game versions and offline/online authentication modes at integration time.
- **Gate:** simple observe/move/dig/place/stop tasks pass through the **same** Python `ActionService`; policies remain enforced during network/sidecar faults.

### Phase E — coordination, benchmarks and world history

- Scoped peer observations, collaborative tasks and spatial edit coordination.
- Reference benchmark harness and reproducible results.
- Separate world-versioning implementation/RFC for semantic snapshots, seed-assisted optimization and merges.
- **Gate:** two Steves collaborate without unauthorized cross-control, and two benchmark clients can run comparable tasks using identical backend/tool profiles.

Do not bundle all phases into a single feature PR. Deliver small reviewable PRs, preserve project-specific changelog/test conventions and update `AGENTS.md`, documentation and roadmap at each accepted milestone.

## 14. Open design questions

1. Should controller API credentials identify a named harness installation, or are per-Steve opaque bearer keys sufficient for the first release? **Proposed:** opaque scoped keys plus independent controller-session identity initially.
2. What exactly constitutes one Steve when moving from a Paper fake-player entity to a Mineflayer protocol client? **Proposed:** stable logical `steve_id`, backend-specific in-game identity and explicit migration; never run two backends as one live actor.
3. Should the initial MCP service support local stdio, Streamable HTTP, or both? **Proposed:** Streamable HTTP for remote/persistent harnesses, optional stdio wrapper later.
4. Where should emergency logic execute for MCPQ versus Mineflayer, and what failure guarantees can be tested? **Proposed:** shared policy plus backend-local fast paths/watchdogs where supported.
5. How much peer state is shared by default? **Proposed:** none beyond explicitly authorized team/public summaries.
6. What webhook/harness adapters can genuinely wake a persistent profile? **Proposed:** optional adapters; do not assume native MCP notifications guarantee wakeup.
7. Which world changes need block-accurate provenance, and when are snapshot checks required? **Proposed:** establish action/event IDs now; design full semantic storage separately.
8. Should tactical decision models be configurable per Steve, per world, or both? **Proposed:** per-Steve policies bounded by world/server hard constraints.

## 15. Decision requested

Accept the **direction**, not a monolithic implementation: make Minecraft AI Bridge a harness-neutral, safe, authenticated Minecraft execution service with a fully functional built-in reference agent, opt-in external MCP clients, multi-Steve ownership, persistent events and interchangeable backends. Proceed first with **Phase A** as an independently reviewable implementation plan. Track Phases B–E in linked GitHub issues once this RFC is reviewed.

**Status remains DRAFT until explicitly accepted. No code or repository changes are implied by this document.**

## References and existing design documents

- [Repository README](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/README.md)
- [Current architecture](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/docs/ARCHITECTURE.md)
- [Self-preservation implementation](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/minecraft_ai_bridge/bridge/self_preservation.py)
- [WASD-like movement proposal/current status](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/docs/features/wasd-movement.md)
- [Survival-mode design](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/docs/features/survival-mode.md)
- [Project roadmap](https://github.com/luluthehungrycat/minecraft-ai-bridge/blob/master/ROADMAP.md)
- [Mineflayer](https://github.com/PrismarineJS/mineflayer) and [mineflayer-pathfinder](https://github.com/PrismarineJS/mineflayer-pathfinder)
- [Model Context Protocol specification](https://modelcontextprotocol.io/specification/latest)