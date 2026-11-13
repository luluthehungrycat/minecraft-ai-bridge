# CLI Reference

## minecraft-ai-bridge

The main bridge CLI — connects to a Paper server via MCPQ and runs the AI agent.

### Usage

```bash
minecraft-ai-bridge [OPTIONS] [GOAL]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `goal` | string | High-level goal for the AI agent. If omitted, uses the default from config. |

### Options

| Option | Description |
|--------|-------------|
| `-h, --help` | Show help message and exit |
| `-c, --config CONFIG` | Path to configuration YAML file (default: `config.yaml`) |
| `--verbose` | Enable verbose/debug logging (shows LLM reasoning, all action results) |
| `--version` | Show version (e.g., `minecraft-ai-bridge v0.2.0`) and exit |
| `--list-providers` | List all supported LLM providers with their requirements and exit |

### Examples

```bash
# Basic goal
minecraft-ai-bridge "Build a small wooden house"

# Verbose mode — see what the LLM is thinking
minecraft-ai-bridge --verbose "Mine 10 iron ore and smelt it"

# Use a different config file
minecraft-ai-bridge -c ~/minecraft-configs/survival.yaml "Find diamonds"

# List what LLM providers are available
minecraft-ai-bridge --list-providers

# No goal — uses config default
minecraft-ai-bridge

# Via python -m
python -m minecraft_ai_bridge "Build a farm"
```

## Provider List Output

Running `--list-providers` shows:

```
Supported LLM providers:

  openai          — OpenAI GPT-4o / GPT-4o-mini etc.
                    Requires: OPENAI_API_KEY env var
  anthropic       — Anthropic Claude Sonnet / Haiku
                    Requires: ANTHROPIC_API_KEY env var
                    Install:  pip install minecraft-ai-bridge[anthropic]
  ollama          — Local models (Llama 3, Mistral, etc.)
                    Requires: ollama server running (default http://localhost:11434)
  openrouter      — OpenRouter proxy (200+ models)
                    Requires: OPENROUTER_API_KEY env var
                    Models: openai/gpt-4o, anthropic/claude-sonnet-4, ...
  opencode_server — Attachable OpenCode inference server
                    Requires: opencode server running (default http://localhost:4096)
                    Default model: big-pickle
```

## Environment Variables

All configuration is driven by environment variables. The full list:

### Minecraft / MCPQ

| Variable | Default | Description |
|----------|---------|-------------|
| `MINECRAFT_HOST` | `localhost` | Minecraft server hostname |
| `MINECRAFT_RCON_PORT` | `25575` | RCON port (optional fallback) |
| `MINECRAFT_RCON_PASSWORD` | `` | RCON password |
| `MINECRAFT_PLAYER_NAME` | `AIBot` | Player name for RCON |
| `MC_API_HOST` | `localhost` | MCPQ plugin hostname |
| `MC_API_PORT` | `1789` | MCPQ plugin gRPC port |
| `MC_API_PLAYER_NAME` | `AIBot` | Player name the agent controls |

### LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `openai` | Provider: openai, anthropic, ollama, openrouter, opencode_server |
| `LLM_MODEL` | `gpt-4o` | Model ID for the chosen provider |
| `LLM_TEMPERATURE` | `0.7` | LLM temperature (0.0-2.0) |
| `LLM_MAX_TOKENS` | `2048` | Max tokens per response |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `LLM_OPENAI_API_KEY` | — | Alternative OpenAI API key env var |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `LLM_ANTHROPIC_API_KEY` | — | Alternative Anthropic API key env var |
| `LLM_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `LLM_OPENROUTER_API_KEY` | — | Alternative OpenRouter API key |
| `LLM_OPencode_SERVER_URL` | `http://localhost:4096` | OpenCode Server URL |
| `LLM_OPencode_SERVER_API_KEY` | `` | OpenCode Server API key |
| `LLM_OPencode_SERVER_MODEL` | `big-pickle` | OpenCode Server model ID |

### Bridge Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `BRIDGE_MAX_ITERATIONS` | `100` | Max think-act-observe cycles |
| `BRIDGE_CYCLE_DELAY` | `1.0` | Seconds to wait between actions |
| `BRIDGE_MEMORY_WINDOW` | `20` | Recent actions to include in prompts |
| `BRIDGE_VERBOSE` | `true` | Show detailed LLM reasoning |

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Goal completed successfully |
| `1` | Fatal error during execution |
| `2` | Invalid command-line arguments |
| `3` | Shut down after losing controller authority (Authz session expired, revoked, or fenced), after an authorized `!stop` or the `authz.suspended_shutdown_seconds` grace period |
| `130` | Interrupted by user (Ctrl+C) |

## MCP Server (`minecraft-ai-bridge-mcp`)

Serves one Steve to MCP clients (Claude Code, Codex, OpenCode, and other
harnesses) over stdio or Streamable HTTP. Install the extra first:

```bash
pip install "minecraft-ai-bridge[mcp]"
```

```bash
# stdio: the harness launches the process and supplies the controller token
MAB_CONTROLLER_TOKEN=mab_... minecraft-ai-bridge-mcp --transport stdio

# Streamable HTTP on http://127.0.0.1:8765/mcp; every request sends
# Authorization: Bearer <controller token>
minecraft-ai-bridge-mcp --transport http --port 8765
```

| Option | Default | Description |
|--------|---------|-------------|
| `--config` | `config.yaml` | Reads `mc_api.*`, `authz.database_path`, and `authz.steve_id` |
| `--transport` | `stdio` | `stdio` or `http` |
| `--host` / `--port` | `127.0.0.1` / `8765` | HTTP bind address |
| `--allow-remote` | off | Required to bind HTTP to a non-loopback address; terminate TLS in front of it |
| `--allowed-host` | loopback hosts | Host header value to accept (repeatable; `name:*` allows any port). Required with `--allow-remote` |
| `--allowed-origin` | loopback origins | Browser Origin to accept (repeatable). For remote binds, omit it unless a browser client needs it |
| `--outcome-store` | `data/action-outcomes.sqlite3` | Durable request-ID outcomes |
| `--event-journal` | `bridge.event_journal_path` | SQLite event journal polled by `minecraft_get_events` |
| `--task-store` | `data/tasks.sqlite3` | Durable task plans and checkpoints; one server process per file |
| `--max-edit-distance` | `6` | Farthest `break_block`/`place_block` target (with coordinates) from the bot |
| `--max-move-distance` | `32` | Farthest `teleport`/`move_to`/`walk_to` destination from the bot (`walk_to` is also capped at its 50-block walking range); must be finite |
| `--session-lease-seconds` | `300` | Controller session lease |

| Variable | Used for |
|----------|----------|
| `MAB_CONTROLLER_TOKEN` | stdio only: the controller credential for this process |
| `AUTHZ_WORLD_ADMIN_TOKEN` | Operator's `admin:world` token; needed for movement, block, item, and combat actions together with the credential's `steve:world:*` delegation |

Tools: `minecraft_get_capabilities`, `minecraft_get_state`,
`minecraft_open_session`, `minecraft_renew_session`, `minecraft_close_session`,
and `minecraft_submit_action` (`session_id`, `generation`, `request_id`,
`action`, `params`). Except for `stop` (see below), reuse a `request_id` only
to retry the same action; the
retry never runs it twice. A retry returns the original result only within
the session that sent it; from a new session the request ID is refused
(`POLICY_REJECTED`), still without running it again.

Action results include a JSON `data` object with the backend's structured output,
such as `raw_inventory` for `check_inventory` or the sampling fields for `scan`.
Replays preserve the observation; per-call correlation metadata may change.
Use `block_type` (not `block`) for `place_block` and nested `params` objects in
task steps. MCP discovery documents the supported parameter shapes. A successful
placement response is not independent block-state verification, and MCPQ crafting
does not prove survival recipe consumption.

If the server restarts, keep using your session: while its lease lasts, the
restarted server resumes it for the credential that opened it, with the same
`session_id` and `generation`.

`minecraft_get_events` (`limit`, `reset_expired`) returns this bot's next
unacknowledged events with their cursors, and `minecraft_ack_events`
(`cursor`) acknowledges everything up to that cursor; unacknowledged events
are returned again on the next poll. Both need a credential with
`events:read` for this Steve, but no session. Session opens and releases are
recorded as `controller.changed` events. If retention dropped events you had
not acknowledged, polling fails until you pass `reset_expired: true`, which
resumes at the oldest retained event and returns `reset: true`.

Safety reflexes run continuously beside the MCP transport, including with
no controller session. `safety.intervened` events carry this Steve's ID.

The same polling records the bot's condition, also attributed to this Steve:

| Event | When | Payload |
|-------|------|---------|
| `steve.damaged` | Health dropped by at least 1.5 HP. During sustained damage, at most one event every 2 s; damage taken in between is added to the next event. | `health`, `previous_health`, `amount` (HP lost), `hits`, `source` |
| `steve.health_low` | Health fell below 4 (HIGH). | `health`, `threshold` |
| `steve.hunger_low` | Hunger fell below 6 (MEDIUM). | `hunger`, `threshold` |
| `steve.died` | The bot died (CRITICAL). | `message`: the server's death message, such as `AIBot was slain by Zombie` |
| `steve.respawned` | The bot is alive again after dying (INFO). The bot plugin respawns it one tick after death. Every `steve.died` is followed by exactly one `steve.respawned` before the next `steve.died`. | `health`, `position` (`[x, y, z]`); both null if the bot died again before a respawn was observed |

The two `_low` events fire once when the value crosses below the threshold and
again only after it has recovered by 2 points.

`source` on `steve.damaged` is `{"kind": "entity", "entity_type":
"minecraft:zombie"}` when an entity hurt the bot within the last 3 seconds
(`entity_type` is null if the attacker has despawned), `{"kind":
"environment"}` otherwise (falls, fire, lava, drowning), or `{"kind":
"unknown"}` if the server could not be asked. It is an inference from the
server's last-attacker record, so a fall shortly after a mob hit is credited
to the mob. These events are informational: the reflexes do not wait for
anyone to read them.
`SAFETY_PREEMPTED` means the action never reached the backend: inspect fresh
state and use a new request ID to try again. `SAFETY_INTERRUPTED` means a
multi-step action stopped after partial progress; inspect state before
planning the next action. Reflexes stop when the server exits.

`stop` (through `minecraft_submit_action`, with any `request_id`) ends the
actions running at that moment without waiting for them: multi-step actions
(`walk_to`, `move_forward`, `move_back`, `sprint`) stop at their next step and
return `interrupted` with `STOP_REQUESTED`. Single-command actions finish
normally, `wait` runs out its time, and safety reflexes are never stopped. It
needs the current controller session and never affects an action started
after it. It is accepted while a task runs, but it does not pause the task:
none of today's task steps is a multi-step move, so the step finishes and the
task goes on. Use `minecraft_pause_task` or `minecraft_cancel_task` to stop a
task. Unlike other actions, `stop` is not deduplicated by `request_id`: each
call stops whatever is running at that moment, so do not retry it blindly.

Tasks run a plan of up to 100 steps without holding the MCP request open:
`minecraft_create_task` (`session_id`, `generation`, `request_id`, `title`,
`steps` as `[{"action": ..., "params": {...}}]`), then `minecraft_get_task`
(`task_id`) to follow `status`, `next_step`, and `reason_code`. Steps may be
`chat`, `done`, `wait` (up to 30 seconds), the `check_*`/`list_players`
observations, or `teleport`/`move_to`/`break_block`/`place_block` with explicit
`x`, `y`, `z`; each step passes the same checks as `minecraft_submit_action`.
`minecraft_pause_task`, `minecraft_resume_task`, and `minecraft_cancel_task`
take the session and `task_id`; a running step finishes first, and cancelling
does not undo completed steps. While a task runs, other actions are refused.
Cancel your own paused task before creating another; other controllers' paused
tasks do not block you. Finished tasks can be read for 7 days.
Tasks pause on a safety intervention or a server restart; resume them with a
current session. A task paused with `OUTCOME_UNKNOWN` cannot resume: check the
world, cancel it, and create a new plan. Task progress is also published as
`task.*` events.

### Webhooks

`minecraft-ai-bridge-mcp` can also push every journaled event (safety,
condition, controller, and task events) to HTTPS endpoints you configure. There
is no tool or endpoint to add subscriptions; they come only from the
`webhooks` section of `config.yaml`, and each signing secret comes from the
environment variable its `secret_env` names, never from the file:

```yaml
webhooks:
  database_path: data/webhooks.sqlite3   # delivery state, retries, dead letters
  poll_interval: 1.0                     # seconds between checks when idle
  subscriptions:
    - id: ops
      url: https://hooks.example.com/minecraft
      secret_env: OPS_WEBHOOK_SECRET     # at least 32 bytes
      event_types: [safety.intervened, steve.health_low]   # omit for all events
      # allowed_hosts: [hooks.example.com]   # default: the URL's host
      # events_per_minute: 60
      # max_attempts: 8
      # initial_retry_seconds: 1.0
      # max_retry_seconds: 300.0
```

The whole list can also be set as JSON in `WEBHOOKS_SUBSCRIPTIONS`. A missing
secret or an invalid URL stops the server at startup, before it connects.

Each subscription receives events in journal order, one at a time, retried
with doubling backoff until it answers 2xx or runs out of attempts (then the
event is dead-lettered and the next one is sent). Delivery is at least once,
and delivery state survives restarts: events journaled while the server was
down are sent when it starts again, as long as the journal still retains them
(events pruned before delivery are dead-lettered). URLs must be HTTPS on port 443 and resolve
to public addresses. Requests are signed: verify `X-Webhook-Signature`
(`sha256=` + hex HMAC-SHA256 of `X-Webhook-Timestamp` + `.` + the raw body) and
deduplicate on `X-Webhook-Delivery-Id`. See
[ARCHITECTURE.md](ARCHITECTURE.md#outbound-webhook-delivery) for the details.

Example stdio client configuration:

```json
{
  "mcpServers": {
    "minecraft": {
      "command": "minecraft-ai-bridge-mcp",
      "args": ["--transport", "stdio", "--config", "/path/to/config.yaml"],
      "env": {"MAB_CONTROLLER_TOKEN": "mab_..."}
    }
  }
}
```

Remote deployment behind a TLS-terminating reverse proxy:

```bash
minecraft-ai-bridge-mcp --transport http --host 0.0.0.0 --port 8765 \
  --allow-remote --allowed-host mcp.example.com
```

The server checks every request's Host header (and Origin, when present)
against these lists to block DNS-rebinding requests: a request with any other
Host is answered `421`, and one with any other Origin `403`. Behind a proxy,
list the Host value the server actually receives: the public hostname if the
proxy preserves the Host header, or the upstream address it rewrites to
otherwise. Loopback binds accept only loopback hosts and origins by default.

The bot entity must already exist in the world (run the bridge once, or
`/botsummon <name>`). Credentials and delegation scopes are created with
`AuthzService` by an `admin:steves` administrator.

## Docker CLI

When using Docker, the command format is:

```bash
GOAL="your goal" docker compose run [OPTIONS] [--rm] bridge [ARGS...]
```

The bridge container's entrypoint is `minecraft-ai-bridge`, so any arguments after the service name are passed to the CLI:

```bash
# These are equivalent:
docker compose run --rm bridge --verbose "Build a house"
docker compose run --rm bridge minecraft-ai-bridge --verbose "Build a house"

# Use a different config file mounted in the container
docker compose run --rm bridge -c /app/config.custom.yaml "Mine diamonds"
```

For persistent mode:

```bash
docker compose up -d bridge
bridge  # The default command reads GOAL from the env or docker-compose.yml
```
