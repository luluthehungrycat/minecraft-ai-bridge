# Minecraft AI Bridge

[![CI](https://github.com/luluthehermeticcrabBot/minecraft-ai-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/luluthehermeticcrabBot/minecraft-ai-bridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI](https://img.shields.io/badge/PyPI-coming%20soon-orange)](https://pypi.org/)

> An LLM-powered agent that connects to a Paper Minecraft server via the MCPQ plugin and executes goals by observing the world, reasoning with an LLM, and manipulating the world directly — no game client needed.

## Purpose

This project lets an LLM (OpenAI, Anthropic, Ollama, OpenRouter, or OpenCode Server) **play Minecraft autonomously**. Give it a high-level goal like "Build a house" or "Mine 10 iron ore" and the agent will:

1. **Decompose** the goal into sub-tasks
2. **Observe** the world (position, blocks, inventory, time, health)
3. **Think** — decide what action to take next using the LLM
4. **Act** — execute actions via the MCPQ plugin (place/break blocks, craft items, move, teleport, etc.)
5. **Remember** — store recent actions and discoveries in short and long-term memory
6. **Repeat** — until the goal is complete

The key difference from traditional RCON-based bridges: **MCPQ gives the agent direct world-manipulation APIs** — setBlock, getBlock, setSign, teleportPlayer, runCommand, postToChat — all over gRPC. No need to parse command output strings or keep a real Minecraft client logged in.

## Architecture

```
┌──────────────────────────────────────────────────┐
│           LLM Provider (the "brain")              │
│  OpenAI · Anthropic · Ollama · OpenRouter · OC    │
│  Goal decomposition · Action selection · Reasoning │
└──────────────────────┬───────────────────────────┘
                       │ actions + observations
┌──────────────────────▼───────────────────────────┐
│           Bridge Orchestrator                     │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Think   │→ │ Act          │→ │ Observe      │ │
│  │ (LLM)   │  │ (MCPQ calls) │  │ (MCPQ calls) │ │
│  └─────────┘  └──────────────┘  └──────────────┘ │
│  ┌─────────┐  ┌──────────────┐                    │
│  │ Goals   │  │ Memory       │                    │
│  │ Manager │  │ (short+long) │                    │
│  └─────────┘  └──────────────┘                    │
└──────────────────────┬───────────────────────────┘
                       │ gRPC (protobuf)
┌──────────────────────▼───────────────────────────┐
│        Paper Server + MCPQ Plugin                 │
│  setBlock · getBlock · setSign · teleportPlayer   │
│  getPlayer · getPlayerPos · runCommandBlocking    │
│  postToChat · getTime · getNbt                    │
└──────────────────────────────────────────────────┘
```

### Three Layers

| Layer | Package | Responsibility |
|-------|---------|----------------|
| **Minecraft Interface** | `minecraft_ai_bridge.minecraft` | MCPQ client wrapper, 36 action handlers, A* pathfinding, world observation (position, blocks, inventory, time, weather, health) |
| **LLM Abstraction** | `minecraft_ai_bridge.llm` | Unified interface for 5 LLM providers, tool-calling schema, system prompts |
| **Bridge Orchestration** | `minecraft_ai_bridge.bridge` | Think-act-observe loop, goal decomposition & tracking, short+long-term memory, chat commands, inventory manager |
| **Runtime** | `minecraft_ai_bridge.runtime` | Shared action gate, authorization and controller sessions, safety supervisor, durable event journal, durable tasks, webhooks |
| **Transport** | `minecraft_ai_bridge.transport` | `minecraft-ai-bridge-mcp` MCP server for external harnesses (optional `[mcp]` extra) |

## Quick Start

### 1. Prerequisites

- **Python 3.11+**
- **Docker** (recommended for the Paper server) or an existing Paper 26.2 build 123 server
- **An LLM API key**: OpenAI, Anthropic, OpenRouter, or a local Ollama / OpenCode Server

### 2. Clone and Install

```bash
git clone <repo-url>
cd minecraft-ai-bridge

# Basic install
pip install -e .

# With Anthropic support
pip install -e ".[anthropic]"

# With dev tools
pip install -e ".[dev]"
```

### 3. Download the MCPQ Plugin

```bash
chmod +x scripts/download-plugins.sh
./scripts/download-plugins.sh
```

This downloads the MCPQ v2.2 plugin and builds the bot plugin into `mcpq-plugins/`.

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your LLM API keys and Minecraft server details

cp config.yaml config.yaml
# Edit config.yaml with your provider, model, and server settings
```

Key configuration: set `mc_api.host` and `mc_api.port` to match your MCPQ plugin endpoint, and set `llm.provider` + `llm.model` to your chosen LLM.

### 5. Start the Paper Server (Docker)

```bash
docker compose up -d minecraft
```

The first startup downloads Paper + initializes the world (2-5 minutes). Watch logs:

```bash
docker compose logs -f minecraft
```

When you see `Done (XX.XXXs)!` the server is ready.

### 6. Run the Bridge

```bash
# Via Docker (config from docker-compose.yml env vars)
GOAL="Build a small wooden house by the lake" docker compose run --rm bridge

# Or locally against a remote server
minecraft-ai-bridge "Explore and find a village"

# Verbose mode (see LLM reasoning)
minecraft-ai-bridge --verbose "Mine 10 iron ore and smelt it into iron ingots"

# Custom config file
minecraft-ai-bridge --config my-config.yaml "Build a cobblestone bridge"
```

## CLI Reference

```
usage: minecraft-ai-bridge [-h] [-c CONFIG] [--verbose] [--version] [--list-providers] [goal]

LLM-powered AI agent that plays Minecraft.

positional arguments:
  goal                  High-level goal for the AI agent (e.g., 'Build a house')

options:
  -h, --help           Show this help message and exit
  -c, --config CONFIG  Path to config YAML file (default: config.yaml)
  --verbose            Enable verbose/debug logging
  --version            Show version and exit
  --list-providers     List supported LLM providers and exit
```

### List Providers

```bash
minecraft-ai-bridge --list-providers
```

Shows all 5 supported providers, their required env vars, and model naming conventions.

### MCP Server for External Harnesses

`minecraft-ai-bridge-mcp` lets an external agent harness (Claude Code, Codex, OpenCode, Hermes Agent and others) control the bot over MCP instead of the built-in LLM loop. It serves stdio or Streamable HTTP (loopback only by default) and needs the `[mcp]` extra:

```bash
pip install -e ".[mcp]"
MAB_CONTROLLER_TOKEN=... minecraft-ai-bridge-mcp --transport stdio
```

Every call goes through the same action gate as the built-in agent: scoped credentials, one exclusive controller session per bot, reach limits, and independent attack/eat/heal safety reflexes. Its tools cover observation, single actions, event polling and acknowledgement, and durable step plans (`minecraft_create_task` and friends). See [docs/CLI.md](docs/CLI.md#mcp-server-minecraft-ai-bridge-mcp) for credentials, flags, and the tool list.

## LLM Providers

| Provider | Models | Env Var | Optional Install |
|----------|--------|---------|------------------|
| **OpenAI** | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` | `OPENAI_API_KEY` or `LLM_OPENAI_API_KEY` | — |
| **Anthropic** | `claude-sonnet-4-20250514`, `claude-haiku-3-5` | `ANTHROPIC_API_KEY` or `LLM_ANTHROPIC_API_KEY` | `[anthropic]` |
| **Ollama** | `llama3`, `mixtral`, any local model | `LLM_OLLAMA_BASE_URL` (default: `http://localhost:11434`) | — |
| **OpenRouter** | `openai/gpt-4o`, `anthropic/claude-sonnet-4`, 200+ | `OPENROUTER_API_KEY` or `LLM_OPENROUTER_API_KEY` | — |
| **OpenCode Server** | `big-pickle`, or `providerID/modelID` | `LLM_OPencode_SERVER_URL` (default: `http://localhost:4096`) | — |

Set `LLM_PROVIDER` and `LLM_MODEL` in your env or config.yaml to switch.

## Docker

The `docker-compose.yml` provides two services:

```bash
# Download MCPQ and build the bot plugin

# Start the Paper server
docker compose up -d minecraft

# Run the bridge once
GOAL="Build a bridge" docker compose run --rm bridge

# Or run persistently (restarts on crash)
docker compose up -d bridge   # reads GOAL from docker-compose.yml or .env
```

The `minecraft` service is pre-configured with server operators (`OPS` env var) for the usernames `LuLuNyam`, `miau004258`, `AIBot`, and `TestBot` — required for world manipulation permissions.

### Environment for Docker

Set variables in `.env` at the project root:

```bash
OPENROUTER_API_KEY=sk-or-...
GOAL="Build a house"
```

Or pass inline:

```bash
LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini LLM_OPENAI_API_KEY=sk-... GOAL="Find diamonds" docker compose run --rm bridge
```

## Configuration

Configuration comes from two places, merged with **env vars taking priority**:

1. **`config.yaml`** — all settings in one file
2. **Environment variables** — override individual values

### config.yaml

```yaml
minecraft:
  host: localhost
  rcon_port: 25575           # Optional RCON fallback
  rcon_password: ""
  player_name: AIBot

mc_api:
  host: localhost            # MCPQ plugin host (Docker: minecraft)
  port: 1789                 # MCPQ plugin gRPC port
  player_name: AIBot         # In-game player name to control

llm:
  provider: openrouter       # openai | anthropic | ollama | openrouter | opencode_server
  model: openai/gpt-4o-mini # Provider-specific model ID
  temperature: 0.7
  max_tokens: 4096

bridge:
  max_iterations: 100        # Safety limit
  cycle_delay: 1.0           # Seconds between actions
  memory_window: 20          # Recent actions to remember
  verbose: true

goals:
  default: "Explore the world and gather resources"
  max_depth: 5
```

### Key Environment Variables

| Variable | Description |
|----------|-------------|
| `MC_API_HOST` | MCPQ plugin hostname |
| `MC_API_PORT` | MCPQ plugin port (default: 1789) |
| `MC_API_PLAYER_NAME` | In-game player name |
| `LLM_PROVIDER` | One of: openai, anthropic, ollama, openrouter, opencode_server |
| `LLM_MODEL` | Model ID for the chosen provider |
| `LLM_OPENAI_API_KEY` | OpenAI API key (or `OPENAI_API_KEY`) |
| `LLM_ANTHROPIC_API_KEY` | Anthropic API key (or `ANTHROPIC_API_KEY`) |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `LLM_OPencode_SERVER_URL` | OpenCode Server URL |
| `LLM_OPencode_SERVER_API_KEY` | OpenCode Server API key |

A full list of overrides is in `.env.example`.

## How the Agent Works

### The Think-Act-Observe Loop

On every turn the agent:

1. **Observe** — queries player position, health, inventory, time of day, and surroundings via MCPQ
2. **Think** — sends full context (goal, state, recent actions, memory, last result) to the LLM, which returns a structured action decision with step-by-step reasoning
3. **Act** — executes the chosen action via MCPQ (gRPC)
4. **Record** — stores action and result in short-term memory
5. **Repeat** — until the goal is complete or `max_iterations` is reached

### Goal Decomposition

When given a high-level goal, the agent first asks the LLM to break it into concrete sub-goals:

```
Build a wooden house
├── Gather wood (break trees)
├── Craft planks and a crafting table
├── Build a 5x5 wooden floor
├── Build walls 3 blocks high
├── Add a door
└── Build a roof
```

Sub-goals are tracked in a tree structure. The agent marks each one complete (via the `done` action) before moving to the next. If the LLM returns no sub-goals, **hardcoded fallback plans** match common patterns (build, mine, farm, explore, enchant).

### Action Space

| Category | Actions |
|----------|---------|
| Movement | `move_to`, `move_forward`, `move_back`, `turn_left`, `turn_right`, `jump`, `teleport` |
| Building | `break_block`, `place_block`, `interact` |
| Inventory | `check_inventory`, `equip_item`, `craft_item`, `drop_item` |
| Combat | `attack` |
| Information | `scan`, `check_time`, `check_weather`, `check_health`, `check_position`, `list_players` |
| Communication | `chat` |
| Meta | `wait`, `done` (signal sub-goal completion) |

### Memory

- **Short-term**: Rolling window of recent actions (configurable, default 20), injected into each LLM prompt as structured text
- **Long-term**: Notable facts extracted from observations (position data, discoveries), persisted across turns

## Project Structure

```
minecraft-ai-bridge/
├── config.yaml                     # Default configuration
├── docker-compose.yml              # Paper server + bridge
├── Dockerfile                      # Bridge container image
├── pyproject.toml                  # Package metadata & dependencies
├── .env.example                    # Environment variable reference
├── scripts/
│   └── download-plugins.sh         # MCPQ downloader and bot plugin builder
├── mcpq-plugins/                   # Mounted plugin directory
├── mcpq-config/                    # MCPQ plugin configuration
├── minecraft_ai_bridge/
│   ├── main.py                     # CLI entry point
│   ├── config.py                   # Pydantic config (YAML + env vars)
│   ├── minecraft/
│   │   ├── mc_api.py               # McpqClient async gRPC wrapper
│   │   ├── actions.py              # 36 action handlers + dispatcher
│   │   ├── pathfinding.py          # A* pathfinder
│   │   ├── observer.py             # World state observation
│   │   └── rcon.py                 # Async RCON client (optional fallback)
│   ├── llm/
│   │   ├── client.py               # LLM abstraction (5 providers)
│   │   ├── prompts.py              # System prompts & state formatting
│   │   └── models.py               # Pydantic data models
│   ├── bridge/
│   │   ├── orchestrator.py         # Main agent loop
│   │   ├── goal_manager.py         # Goal decomposition & tracking
│   │   ├── memory.py               # Short + long-term memory
│   │   ├── chat_commands.py        # Incoming chat command parser
│   │   └── inventory_manager.py    # Structured inventory tracking
│   ├── runtime/
│   │   ├── action_service.py       # Shared action gate, audit, idempotency
│   │   ├── authz.py                # Credentials, scopes, controller sessions
│   │   ├── controller_service.py   # Controller API for one bot
│   │   ├── safety_supervisor.py    # Independent safety reflexes
│   │   ├── event_journal.py        # Durable event journal
│   │   ├── tasks.py, task_runner.py # Durable bounded task plans
│   │   └── webhooks.py             # Signed webhook delivery
│   └── transport/
│       ├── mcp_server.py           # MCP tools over ControllerService
│       └── mcp_main.py             # minecraft-ai-bridge-mcp entry point
├── tests/
│   ├── conftest.py                 # MockMcpqClient, MockLLMClient, fixtures
│   ├── test_actions.py             # All 36 action handlers
│   ├── test_*.py                   # Observer, memory, goals, runtime, MCP, ... (see AGENTS.md)
│   ├── test_integration.py         # Full agent loop (real MCPQ + real LLM)
│   └── test_mcp_integration.py     # MCP server end to end against live MCPQ
├── docs/
│   ├── ARCHITECTURE.md             # Deep architecture dive
│   ├── SETUP.md                    # Detailed installation guide
│   ├── CLI.md                      # CLI reference
│   ├── AGENT.md                    # Agent internals
│   ├── EXTENDING.md                # How to extend the bridge
│   └── rfcs/                       # RFC-0001..0003 architecture proposals
├── examples/
│   └── goals.yaml                  # Example goals with sub-goals
└── AGENTS.md                       # AI handoff notes
```

## Project Status & Tracking

| Area | File | Purpose |
|------|------|---------|
| **Bugs** | `docs/bugs.md` | All known bugs, triaged and untriaged |
| **Improvements** | `docs/improvements.md` | Code quality, performance, maintainability |
| **Planned Features** | `docs/features/README.md` | Feature proposals with effort estimates |
| **WASD Movement** | `docs/features/wasd-movement.md` | Human-like walking movement (P2) |
| **Survival Mode** | `docs/features/survival-mode.md` | Hunting, defense, full survival (P3) |
| **OpenCode Skill** | `docs/features/opencode-skill.md` | OpenCode/Hermes integration (exploratory) |
| **CI/CD** | `.github/workflows/ci.yml` | GitHub Actions: lint, test, style checks |
| **Agent Diary** | `docs/agent-diary/` | Development decisions and progress log |

### Known Limitations

- **Movement**: Currently teleport-only (`/tp`). WASD-style physics-based movement is planned (see `docs/features/wasd-movement.md`). Note that MCPQ's `player.teleport(Vec3)` has a known issue — it reports success but may not actually move the player on some Paper/MCPQ versions. Fallback uses `/tp` command.
- **Crafting**: Uses `/give` (creative/OP mode). Proper survival crafting with recipe matching is planned.
- **Structure preservation**: The agent does not yet detect or respect existing player-built structures. Buildings, railroads, and NPC villages may be modified. This is a planned feature.
- **Inventory**: Raw NBT is shown to the LLM as the primary view. The structured `InventoryManager` is available for programmatic access but the LLM prompt currently includes both structured and raw formats.
- **Biomes**: The observer reports the current biome using authoritative server data; biome-specific strategies are not yet implemented.
- **Combat**: Basic `/damage`-based attack and hotbar weapon selection are implemented. Mob-specific strategies and armor management are not implemented.

## CI/CD

The project uses **GitHub Actions** for continuous integration. Every push and pull request to `master`/`main` runs deterministic checks:

| Job | What it checks |
|-----|---------------|
| **lint** | `ruff check` (code quality), `ruff format --check` (formatting), `mypy` (type checking) |
| **test** | Deterministic pytest suite across Python 3.11, 3.12, and 3.13 |
| **style** | `ruff check --select I` (import sorting) |
| **integration** | Opt-in provider-backed tests; requires `RUN_LIVE_INTEGRATION=true`, Docker, and `OPENROUTER_API_KEY`; provisions pinned Paper 26.2 build 123 and the bot plugin automatically |

The CI badge at the top of this README shows the current status of the `master` branch.

### PyPI (Future)

A PyPI release workflow will be added once the project stabilizes. The goal is to publish via `pypi-publish` on tagged commits. For now, install from source:

```bash
pip install -e "."
```

## Testing

The project has **878 deterministic tests** plus 14 live integration tests organized in two tiers:

| Tier | Count | Description | Dependencies |
|------|-------|-------------|--------------|
| **Deterministic** | 878 | Action handlers, NBT parsing, memory, goal fallbacks, config, chat commands, inventory manager, observer, orchestrator, runtime (action gate, authorization, sessions, safety, events, tasks, webhooks), and MCP adapter | `MockMcpqClient` / `MockLLMClient`; MCP tests need the `[mcp]` extra (in `[dev]`) |
| **Integration** | 14 | Full think-act-observe loop with real MCPQ + real LLM (12), and `minecraft-ai-bridge-mcp` over stdio and HTTP (2) | Paper 26.2 build 123 + MCPQ v2.2 + bot plugin + OpenRouter API key |

### Test Infrastructure

**`MockMcpqClient`** (in `tests/conftest.py`) simulates a full 3D world in memory with methods like `set_position()`, `set_block()`, `set_inventory()`, and `set_time()`. It records all commands and chat messages for assertion.

**`MockLLMClient`** returns pre-configured action queues for deterministic testing of the agent loop. Supports `responses: list[tuple[str, dict]]` for ordered action sequences and `set_decompose_return(subgoals)` for goal decomposition tests.

**Goal-verification helpers** (in `tests/test_integration.py`):
- `actions_taken(orch) → list[str]` — all action names from short-term memory
- `action_taken(orch, *names) → bool` — whether any given action was performed
- `position_reached(orch, x, y, z, tolerance) → bool` — whether observations show target coordinates

These helpers operate on in-memory Python dicts — essentially zero cost, no extra I/O.

### Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# All tests
pytest tests/

# Unit only
pytest tests/ -k "not integration"

# Integration only (Docker server + API key required)
pytest tests/test_integration.py -v --tb=short
```

Integration tests use real OpenRouter inference with `openai/gpt-oss-20b`. Ensure `OPENROUTER_API_KEY` is set and Docker is available; the tests provision pinned Paper 26.2 build 123 and the plugins. Unit tests are fully self-contained.

## Extending

See `docs/EXTENDING.md` for detailed guides on:

- **Adding a new action** — 5 steps from enum to handler to LLM schema
- **Adding a new LLM provider** — implement the `LLMClient` ABC
- **Custom goal decompositions** — extend or replace the fallback plans
- **Custom memory strategies** — plugin different memory backends

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| `No entity was found` for player ops | Fake player not spawned | Bridge auto-spawns one on connect; check `fp spawn` succeeded in logs |
| MCPQ connection refused | Wrong host/port or MCPQ not loaded | Verify `mc_api.host`/`port`, check server logs for `mcpq` startup |
| LLM returns 401 / auth error | Missing or invalid API key | Check env vars: `LLM_OPENAI_API_KEY`, `OPENROUTER_API_KEY`, etc. |
| Paper client/server version mismatch | Version mismatch | Ensure the client and server use the same current Minecraft release (the bridge uses headless MCPQ, no client needed) |
| LLM re-scans endlessly | World state unclear or player can't reach goal | Check player position; try a simpler goal; enable `--verbose` to see LLM reasoning |
| Bridge container exits immediately | MCPQ not reachable | Wait for Paper to fully start; check `docker compose logs minecraft` |

## License

MIT
