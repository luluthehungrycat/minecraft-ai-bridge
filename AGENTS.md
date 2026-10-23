# AGENTS.md — AI Handoff Notes

> This file is for AI agents working on this project. It captures what's been done, what needs work, design decisions, and conventions.

## Project Overview

**Minecraft AI Bridge** — an LLM-powered agent that plays Minecraft by connecting to a Paper server via the MCPQ plugin (gRPC). No game client needed. The agent receives high-level goals, decomposes them, then runs a think-act-observe loop using an LLM (OpenAI/Anthropic/Ollama/OpenRouter/OpenCode Server) to decide actions.

- **Language**: Python 3.11+
- **Async**: asyncio throughout
- **Config**: pydantic-settings (YAML + env vars)
- **Package**: `minecraft-ai-bridge` (PyPI-style, installable with pip)
- **Server**: Paper 26.2 build 123 + MCPQ plugin v2.2 + custom bot plugin
- **Docker**: itzg/minecraft-server image with plugins mounted

## What's Built (Complete)

### Minecraft Layer (`minecraft_ai_bridge/minecraft/`)
- `mc_api.py` — `McpqClient` async wrapper around MCPQ gRPC (all calls via `asyncio.to_thread`)
- `actions.py` — 36 `ActionType` enum values + handlers + `execute_action()` dispatcher (including `sprint`, A*-powered `walk_to`, and the internal-only `follow_teleport`, `provision_bot`, `prepare_safe_spawn`, `goto_teleport`, and `come_teleport` actions)
- `pathfinding.py` — A* pathfinder that scans a walkability grid via MCPQ `getBlock` and returns waypoints
- `observer.py` — `Observer` + `WorldState` dataclass, concurrent observation via `asyncio.gather`
- `rcon.py` — async RCON client (optional, unmaintained — MCPQ is primary)

### LLM Layer (`minecraft_ai_bridge/llm/`)
- `client.py` — `LLMClient` ABC + 5 implementations: OpenAI, Anthropic, Ollama, OpenRouter, OpenCode Server. Factory function `create_llm_client()`. `ACTION_TOOL` schema definition.
- `prompts.py` — `SYSTEM_PROMPT` string, `format_state()`, `format_goal()`
- `models.py` — `LLMResponse`, `Message`, `Role` pydantic models

### Bridge Layer (`minecraft_ai_bridge/bridge/`)
- `orchestrator.py` — `Orchestrator` class with `run()` and `_step()` think-act-observe loop. Auto-spawns the bot entity on connect, teleports to safe location.
- `goal_manager.py` — `GoalNode` tree, LLM-based decomposition, fallback plans for common goals (build, mine, farm, workshop, explore, generic). Mining pattern uses `\bore\b` to avoid matching "Explore" via substring.
- `memory.py` — `AgentMemory` with short-term (rolling deque) and long-term (facts set) memory
- `chat_commands.py` — Incoming chat command parser (`!stop`, `!status`, `!follow`, `!goto`, `!come`) for live agent control; `!goto`/`!come` teleports route through ActionService as internal actions with strict username validation
- `inventory_manager.py` — Structured inventory slot tracking with NBT parsing

### CLI (`minecraft_ai_bridge/main.py`, `__main__.py`)
- `minecraft-ai-bridge [OPTIONS] [GOAL]` CLI with `--verbose`, `--config`, `--version`, `--list-providers`
- Entry point registered in `pyproject.toml`

### Config (`minecraft_ai_bridge/config.py`)
- `AppConfig` with nested `MinecraftConfig`, `MCPQConfig`, `LLMConfig`, `BridgeConfig`, `AuthzConfig`, `GoalConfig`; AuthzConfig optionally gates Orchestrator sessions and accepts its bearer token only from the environment
- `from_yaml()` class method merges YAML + env vars with type coercion

### Runtime (`minecraft_ai_bridge/runtime/`)
- `authz.py` — local SQLite credentials/scopes, global admin identities, Steve ownership/first-claim, exclusive controller sessions and fencing; trusted local bootstrap recovers only when no valid `admin:steves` credential remains, and `steve:observe:shared` requires explicit per-peer admin grants while `events:read` is service level. No transport or external endpoint.
- `world_adapter.py` — transport-independent `WorldAdapter` Protocol plus `McpqWorldAdapter` composition wrapper around a connected `McpqClient`; its read-only `capabilities` property advertises a conservative MCPQ profile (teleport-simulated movement, privileged-simulated crafting, world edit; no live version/dimension or hazard-sensor claims). The orchestrator creates exactly one adapter and passes it to ActionService, Observer, InventoryManager, ChatCommandHandler, and SelfPreservationLayer; the raw client is kept only for connection lifecycle. Phase A slice — capability reporting is not authorization or independent safety, and this is not yet a full backend contract; external MCP access out of scope.
- `action_service.py` — policy hook, bounded audit trace, and process-local request-ID idempotency for strategic actions, trusted startup provisioning, follow-mode teleport, chat-command teleports, and self-preservation mutating reflexes.
- `safety_supervisor.py` — independently scheduled preservation polling from goal decomposition onward with a single pending-intervention slot; the orchestrator cancels stale LLM decisions and strategic WAITs, and ActionService invalidates queued strategic actions when a reflex starts. It emits durable `safety.intervened` events with action and success only; all other RFC-0001 event producers remain open. This is a built-in runtime prerequisite slice, not backend watchdog safety or Phase C completion.
- `event_journal.py` — SQLite append-only Event persistence with monotonic cursors, event-ID idempotency, per-consumer at-least-once polling/acknowledgment, and bounded payload retention.
- `webhooks.py` — administrator-configured local webhook delivery over EventJournal, with isolated per-subscription consumers, strict journal-head ordering, ack-cursor reconciliation, durable retries/dead letters, HMAC signatures, bounded size/rate/retries, and HTTPS exact-host allowlisting. The default HTTPX pinned transport connects directly to checked public IPs while preserving TLS SNI and HTTP Host, with environment proxies and redirects disabled; callers may inject a compatible transport. No subscription-management endpoint or external controller authorization/admin config integration. This is not full Phase C.
- `contracts.py` — RFC-0001 Phase A contract schemas (Pydantic v2): SteveIdentity, ControllerSession, Capabilities, Task, Event, and the ActionRequestSchema/ActionResultSchema action envelope. Schema definitions only: strict IDs/statuses, timezone-aware timestamps; no authorization, persistence, event journal, task service, or MCP wiring (later phases). The Schema suffix avoids collision with the unchanged runtime ActionRequest/ActionResult dataclasses.

### Infrastructure
- `Dockerfile` — `python:3.13-slim`, pip installs the package
- `docker-compose.yml` — Paper server (pinned itzg Java 25 image with Paper 26.2 build 123) + bridge service. MCPQ on port 1789. Custom bot plugin mount. OPS env var with operator usernames.
- `scripts/download-plugins.sh` — downloads MCPQ v2.2 jar and builds the bot plugin
- `mcpq-config/config.yml` — MCPQ bound to `0.0.0.0:1789`
- `mcpq-plugins/` — mounted plugin directory
- `.env.example`, `.gitignore`, `config.yaml`

### Documentation
- `README.md` — complete project overview
- `ROADMAP.md` — current priorities, completed milestones, release plan
- `CHANGELOG.md` — release-by-release change log (Added / Changed / Fixed / Removed)
- `docs/ARCHITECTURE.md` — 3-layer architecture deep dive
- `docs/SETUP.md` — installation guide (Docker + local)
- `docs/CLI.md` — CLI reference
- `docs/AGENT.md` — agent internals (loop, actions, goals, memory)
- `docs/EXTENDING.md` — how to extend actions, providers, etc.
- `this file` — AGENTS.md handoff notes

## CHANGELOG.md — maintenance rule

> **For future agents (especially stateless sessions):** every PR
> that changes user-visible behaviour (anything beyond pure refactor,
> lint, or test cleanup) **MUST** add a line to `CHANGELOG.md` under
> the `## [Unreleased]` section. The PR description should reference
> the relevant subsection. This is enforced by the user, not CI.

The format uses `### Added`, `### Changed`, `### Fixed`, `### Removed`
subsections. When a version is tagged (e.g. `v0.5.1`), the
`[Unreleased]` section becomes `[X.Y.Z] - YYYY-MM-DD` and a new
empty `[Unreleased]` section is added above it. See the file for
examples and the conventions block at the top.

## Known Issues & Limitations

### Critical
- **Model compatibility**: Some OpenRouter models (esp. newer ones like `gpt-5-nano`) may not support tool calling. Test with models known to work: `openai/gpt-4o-mini`, `openai/gpt-4o`, `anthropic/claude-sonnet-4`.
- **No entity for MCPQ**: If the custom bot entity isn't spawned on connect, MCPQ player ops fail. The orchestrator has retry logic but it's not 100% reliable.

### Performance
- **Scanner limited to radius 16**: The `scan` action caps at r=16 to avoid MCPQ rate limiting. For scanning large areas, needs chunk-based iteration.
- **Movement system**: Step-by-step WASD-like movement via execute-based commands with collision detection, hazard avoidance, and A* pathfinding. Slow for very long distances (>50 blocks falls back to teleport).
- **Inventory tracking**: Inventory parsed into structured `InventorySlot` objects; observer grabs via `/data get entity @p Inventory` and inventory manager tracks slots.

### Paper / MCPQ
- **Paper pin**: Paper 26.2 build 123 with MCPQ v2.2. Docker pins the Java 25 server image by digest; update the Paper version/build and image digest together.
- **Bot plugin**: Custom `mc-bot-plugin-1.0.0.jar` provides the bot entity. Built in `bot-plugin/` with Gradle + paperweight-userdev (Java 25). Provides `/botsummon <name>` command that creates a ServerPlayer entity MCPQ can detect.
- **Plugin version pinning**: MCPQ jar is downloaded from GitHub releases. The bot plugin and Paperweight dev bundle are pinned; update them deliberately together.
- **Known Paper 26.x issues**:
  - `time query daytime` throws CommandException — use `time query day` instead (fixed in bridge code)
  - `setblock` commands via MCPQ may have array-related issues (mitigated in MCPQ client)
  - `defaultgamemode` command format changed (use `gamemode` instead)

### Docker
- **Health check timing**: First Paper startup takes 2-5 minutes. The health check has a 240s start period. The bridge retries for 20 attempts with backoff.
- **config.yaml volume mount**: Mounted read-only at `/app/config.yaml`. Changes require `docker compose restart bridge`.

### Code Quality
- **Tests**: 478 tests collected in total — 466 deterministic unit tests plus 12 integration tests. Unit tests use `MockMcpqClient` for deterministic MCPQ simulation. Integration tests use a real MCPQ server + real LLM (OpenRouter `openai/gpt-oss-20b`) for end-to-end validation. The stack uses pinned Paper 26.2 build 123. Run with `pytest tests/`.
- **Mypy**: The CI job is wired but skipped by default; enable it with the `MYPY_ENABLED` repository variable after SDK typing issues are resolved.
- **gRPC stubs are synchronous**: MCPQ generated stubs block; dispatched via `asyncio.to_thread`. Not ideal but works.
- **RCON client is unmaintained**: Since the MCPQ migration, `rcon.py` isn't tested. Consider removing or marking deprecated.

## Design Decisions Made

### Why MCPQ over RCON?
MCPQ gives structured world manipulation (setBlock, getBlock, getPlayerPos) that RCON can't do. RCON only allows running commands and parsing string output.

### Why the custom bot plugin over pyCraft?
pyCraft maxes at Minecraft 1.18.1 (protocol 754) and is unmaintained. The custom bot plugin creates a ServerPlayer server-side only — no network protocol needed.

### Why asyncio?
The bridge is I/O-bound (LLM API calls, MCPQ gRPC, rate-limit delays). asyncio allows concurrent observation queries without thread overhead.

### Why docker-compose?
Simplifies the Paper setup (plugin downloads, config mounting, network). The itzg/minecraft-server image handles Paper installation automatically.

### Why hardcoded fallback plans?
Some LLMs (especially smaller/local ones) can't reliably output structured JSON for goal decomposition. Fallback plans ensure the bridge works without a high-quality LLM.

## Priority Work Items

### P0: None (Testing Complete)
The 0.5.0 release completed the testing milestone:
- **Unit tests** (466): MockMcpqClient covers all 36 actions, observer parsing, memory, goal fallback plans, config, inventory manager, chat commands, pathfinding, the runtime action gate, the safety supervisor, the event journal, webhook delivery, and the local authorization service
- **Integration tests** (12): Full think-act-observe loop with real MCPQ server + real LLM inference (OpenRouter `openai/gpt-oss-20b`). Covers teleport, exploration, disconnect handling, fallback decomposition, failure counting, memory recording, chat command stop, and inventory creation.
- **Goal-verification assertions**: Integration tests verify agent behavior via:
  - `action_taken(orch, *names) → bool` — checks if the agent performed a specific action (parses `MemoryEntry.raw`)
  - `actions_taken(orch) → list[str]` — all action names from short-term memory
  - `position_reached(orch, x, y, z, tolerance) → bool` — checks observations for target coordinates
  - These helpers are essentially free (in-memory dict parsing, no extra I/O)
- **Real LLM tests**: Most integration tests use real OpenRouter inference; the rest use `MockLLMClient` for deterministic edge cases (fallback plans, failure counting). The exact split is tracked in `tests/test_integration.py`.

### P1: Observer Improvements
- Parse inventory NBT into structured slot data (not raw command output)
- Parse health numeric value from `/data get entity @p Health`
- Add biome detection (without the noisy `/locate biome` command — maybe via client seed + chunk coordinates)

### P2: LLM Prompt Optimization
- Trim context window: long-term memory should be summarized, not dumped raw
- Add "last action failed because..." hints to help the LLM recover
- Implement retry with different params when action fails

### P3: Player Movement
- Replace `/tp` with WASD-style movement simulation for survival mode
- Maybe control through MCPQ if the plugin adds it, or use Minecraft's `/execute` with `facing` + `move`

### P4: Infrastructure
- Set up GitHub Actions CI (lint + type check + test) — **partial**: CI runs lint + unit tests on every PR; integration tests gated on `OPENROUTER_API_KEY` secret. See `.github/workflows/ci.yml`.
- Re-enable `mypy` in CI — **tracking**: mypy is currently skipped due to strict OpenAI/Anthropic SDK type signatures in `llm/client.py`. Job is wired up but no-op until `MYPY_ENABLED` repo variable is set. Track re-enablement as a follow-up issue.
- Add `mypy` configuration to `pyproject.toml`
- Consider adding `pre-commit` hooks
- Publish to PyPI as `minecraft-ai-bridge`

## Code Conventions

- **Imports**: standard library → third-party → local. Absolute imports preferred (`from .module import X`).
- **Type hints**: use `from __future__ import annotations` everywhere. Full type annotations required.
- **Logging**: use `logger = logging.getLogger(__name__)` per module. No print statements.
- **Error handling**: catch specific exceptions, not `Exception`/`BaseException`. Log with `logger.exception()` for traceback.
- **Async patterns**: all IO is async. gRPC sync calls via `asyncio.to_thread`. LLM HTTP via `httpx.AsyncClient` or `openai.AsyncOpenAI`.
- **Config**: pydantic models with `SettingsConfigDict(env_prefix=...)`. `field_validator` for fallback env vars.
- **Dataclasses**: use `@dataclass` for simple data containers, `BaseModel` (pydantic) for validated config.

## Key File Map

| File | Purpose |
|------|---------|
| `minecraft_ai_bridge/main.py` | CLI entry point, argument parsing |
| `minecraft_ai_bridge/config.py` | All config models and YAML/env loading |
| `minecraft_ai_bridge/minecraft/mc_api.py` | MCPQ gRPC client wrapper |
| `minecraft_ai_bridge/minecraft/actions.py` | ActionType enum + 36 handlers + dispatcher |
| `minecraft_ai_bridge/minecraft/world_types.py` | Type alias re-exporting `WorldAdapter` (real Protocol under type checking, `object` at runtime) for the dispatcher, observer, action helpers, and pathfinding without a runtime circular import |
| `minecraft_ai_bridge/minecraft/observer.py` | World state observation |
| `minecraft_ai_bridge/minecraft/rcon.py` | RCON client (legacy, unused) |
| `minecraft_ai_bridge/llm/client.py` | LLMClient ABC + 5 providers + ACTION_TOOL |
| `minecraft_ai_bridge/llm/prompts.py` | System prompt, state formatting |
| `minecraft_ai_bridge/llm/models.py` | LLMResponse, Message, Role models |
| `minecraft_ai_bridge/bridge/orchestrator.py` | Main agent loop, connection, step logic |
| `minecraft_ai_bridge/runtime/world_adapter.py` | Transport-independent `WorldAdapter` Protocol + `McpqWorldAdapter` composition wrapper and conservative MCPQ capability profile (Phase A slice) |
| minecraft_ai_bridge/runtime/contracts.py | RFC-0001 Phase A Pydantic v2 contract schemas (identity, session, capabilities, task, event, action envelope); schemas only, later-phase services intentionally absent |
| `minecraft_ai_bridge/runtime/action_service.py` | Policy hook, bounded audit trace, and process-local request-ID idempotency for strategic agent actions, trusted startup provisioning, the follow-mode teleport, the chat-command teleports (goto_teleport/come_teleport), and the self-preservation layer's mutating reflexes (ATTACK/EAT/HEAL); read-only observations stay direct (Phase A slice) |
| `minecraft_ai_bridge/runtime/safety_supervisor.py` | Independent periodic self-preservation evaluator from goal decomposition onward, with bounded pending intervention and strategic decision/WAIT preemption; ActionService invalidates queued strategic actions at intervention start, but dispatched backend work remains uncertain (safety prerequisite slice) |
| `minecraft_ai_bridge/runtime/event_journal.py` | SQLite durable append-only event journal with monotonic cursors, idempotent event IDs, consumer polling/ack, retention expiry/reset, and bounded payload retention; event-ID fingerprint tombstones persist beyond payload retention |
| `minecraft_ai_bridge/runtime/webhooks.py` | Administrator-configured webhook sender with durable ordered retries/dead letters, restart reconciliation, signatures and endpoint checks; default HTTPX transport pins checked IPs with original TLS/Host identity |
| `minecraft_ai_bridge/runtime/authz.py` | Local SQLite credentials, scopes, global admin identities, Steve ownership with lease renewal, exclusive controller sessions and fencing (RFC-0001 Phase B foundation) |
| `minecraft_ai_bridge/bridge/goal_manager.py` | Goal tree, LLM decomposition, fallback plans |
| `minecraft_ai_bridge/bridge/memory.py` | Short+long term memory |
| `minecraft_ai_bridge/bridge/chat_commands.py` | Incoming chat command parser (!stop, !status, !follow, !goto, !come); goto/come teleports route through ActionService with username validation |
| `minecraft_ai_bridge/bridge/inventory_manager.py` | Structured inventory slot tracking |
| `config.yaml` | Default configuration |
| `docker-compose.yml` | Paper server + bridge services |
| `docs/*.md` | Full documentation set |
| `tests/conftest.py` | MockMcpqClient, MockLLMClient, test fixtures |
| `tests/test_*.py` | Unit + integration tests (478 total: 466 unit + 12 integration) |
| `AGENTS.md` | This file |

## Environment Info

- **Python**: 3.11+ required (built on 3.13)
- **OS**: Linux (Docker: python:3.13-slim)
- **Paper**: 26.2 build 123 via pinned itzg Java 25 image
- **MCPQ plugin**: v2.2
- **Custom bot plugin**: v1.0.0, built from `bot-plugin/` with Java 25 and Paperweight
- **Docker compose**: v2 format

## Test Infrastructure

### MockMcpqClient (`tests/conftest.py`)
Full in-memory MCPQ mock simulating a 3D world. Methods:
- `set_position()`, `set_block()`, `set_block_map()` — configure the world
- `set_inventory()`, `set_time()`, `set_players()`, `set_biome()`, `set_player_nbt()` — configure state
- `commands_ran`, `chat_messages_sent`, `last_command()` — assert on actions
- `assert_command_contains(substring)`, `assert_chat_contains(substring)` — convenience assertions

### MockLLMClient (`tests/conftest.py`)
Returns pre-configured action queues. Supports:
- `responses: list[tuple[str, dict]]` — ordered action responses
- `set_decompose_return(subgoals)` — override goal decomposition
- `prompts_received` — introspection of prompts sent

### Goal-Verification Assertions
Helpers in `tests/test_integration.py`:
- `actions_taken(orch) → list[str]` — all actions from short-term memory
- `action_taken(orch, *names) → bool` — whether any given action was taken
- `position_reached(orch, x, y, z, tol) → bool` — whether observations show target coords

### Test Organization
| Directory | Tests | Description |
|-----------|-------|-------------|
| `tests/test_actions.py` | 104 | All 36 action handlers with MockMcpqClient |
| `tests/test_self_preservation.py` | 60 | Self-preservation reflex heuristics and dispatch |
| `tests/test_observer.py` | 43 | NBT parsing, inventory parsing, observation formatting |
| `tests/test_memory.py` | 25 | Short/long-term memory, dedup, persistence |
| `tests/test_goal_manager.py` | 39 | Goal decomposition, fallback plans patterns |
| `tests/test_chat_commands.py` | 17 | Chat command parsing and dispatch |
| `tests/test_prompts.py` | 17 | Prompt and state formatting |
| `tests/test_inventory_manager.py` | 12 | Inventory manager slot tracking |
| `tests/test_pathfinding.py` | 10 | A* pathfinding |
| `tests/test_orchestrator.py` | 6 | Orchestrator loop and wiring |
| `tests/test_authz_orchestrator.py` | 7 | Optional built-in Orchestrator Authz configuration, session gating, revocation, and cleanup |
| `tests/test_authz_service.py` | 53 | Local credentials, scopes, ownership, sessions, fencing, peer-grant audit migration |
| `tests/test_event_journal.py` | 8 | Durable event journal |
| `tests/test_runtime_action_gate.py` | 19 | Action gate, policy, and idempotency |
| `tests/test_runtime_safety.py` | 8 | Independently scheduled preservation |
| `tests/test_safety_events.py` | 14 | Durable safety intervention events |
| `tests/test_webhooks.py` | 24 | Webhook delivery, retries, and endpoint checks |
| `tests/test_integration.py` | 12 | Full agent loop with real MCPQ + real LLM |
