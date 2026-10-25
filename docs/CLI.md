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
