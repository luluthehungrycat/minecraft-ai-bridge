# Steve Player — Hermes profile template v0.1.0

A Minecraft-only controller for one Steve through `minecraft-ai-bridge` MCP.
This is a **secret-free template**, not an export of a live profile.

## Install

From a checkout with Python/PyYAML and Hermes on PATH:

The installer and runner currently require the standard `~/.hermes/profiles`
layout. A relocated/custom Hermes home is not supported by these helper scripts.

```sh
python profiles/steve-player/install.py
```

It uses `hermes profile create steve-player --no-skills` (wrapper creation is
**on**, not `--no-alias`), applies config through `hermes config set`, copies the
SOUL/manifest, and installs the official `claude-subscription-directsdk` plugin
pinned to `6313fad5ddf2c060e7b16782f4860c0d5cdbfcac` (catalog version 0.3.5).
For an existing profile, applying the template requires explicit
`--configure-existing`; this replaces the authored config sections, not secrets,
memories or sessions. `--skip-plugin` is for an already installed plugin.

The generated PATH wrapper forwards all arguments unchanged:

```sh
steve-player --help
steve-player chat
steve-player acp
```

Hermes authentication must resolve an OpenAI Codex login. Claude Code must be
installed and logged into the intended subscription for the fallback. No login
files are copied into the template. Defaults:

- Primary: `openai-codex` / `gpt-6-luna`.
- Fallback: `claude-subscription-directsdk-experimental` / `claude-haiku-5-5`.
- Compression/title/approval auxiliary calls: the same subscription Haiku route.

Provision **only** these Minecraft variables in the profile's secret scope
(`~/.hermes/profiles/steve-player/.env`, mode 0600), or its configured secret source:

- `MAB_MCP_URL`: operator-managed Streamable HTTP endpoint.
- `MAB_CONTROLLER_TOKEN`: credential bound to the one intended Steve.

Never put an administrator token here. Start the bridge and provision credentials
**before** enabling/discovering MCP. If Hermes' running multiplexed gateway parked
a connection during setup, reconnect it with `/reload-mcp` in a continuing
interactive session (confirm the reload); `hermes mcp test minecraft` alone tests
a new connection and does not prove the gateway's old connection recovered.

## Authority and limitations

The profile enables only `mcp-minecraft`, with an explicit allowlist of all 13
controller/session/state/event/task tools. MCP resource/prompt utilities, sampling
and elicitation are disabled. Native terminal, file, web/browser, code execution,
connections, delegation, cron, memory, skills, and other toolsets are not enabled.
There are no bundled skills, persistent memory, bot channels or scheduled jobs.

**There is no terminal tool**, so no terminal backend/plugin is needed. This is
model-tool minimization, **not an OS sandbox** around Hermes or its inference
clients. The official Claude provider starts its own CLI process with native
tools/settings/skills disabled; it is an inference transport, not a shell tool.
The host operator still runs the trusted bridge and server.

`trust: full` applies only to the operator-controlled bridge: allowed gameplay
mutations need not prompt on each step. The bridge still enforces bearer auth,
Steve binding, controller session/fencing, delegation, reach, safety and idempotency.
Tool hiding and SOUL rules do not replace these checks.

For local smoke tests, use a **separate world and Steve**, private authz/outcome/
event/task stores, loopback-only ports, and a controller with `steve:control`,
`steve:observe:self`, `events:read` and only necessary `steve:world:*` delegations.
The recorded deployment grants move/edit/items, **not combat or admin scopes**.
MCPQ itself remains privileged-simulated: this is not a survival-only backend or
a spatial-claims system. Per-action reach is not an arena fence. Do not attach
this profile to valuable/shared worlds without stronger world protections.

## T3 Code: custom local ACP provider

Add a provider to the intended local T3 backend (do not change other providers):

```json
{
  "driver": "acpRegistry",
  "displayName": "Steve Player (Hermes)",
  "enabled": true,
  "config": {
    "source": "local",
    "commandPath": "steve-player",
    "commandArgs": ["acp"]
  }
}
```

Use a dedicated empty/profile workspace, not this developer repository, to avoid
injecting coding-agent project instructions into a player session. T3 can supply
its own MCP integrations to ACP agents; the SOUL forbids using orchestration or
non-Minecraft tools, but that is an explicit trust boundary, not a technical
sandbox. This provider entry does not authorize the player to delegate work.

## Bounded gameplay trials

Prepare the disposable arena described by the runner (stone y=100, bedrock
perimeter x/z +/-12, logs at x=3/z=0/y=101..103, supplied inventory). Confirm its
initial state independently. Then:

```sh
python scripts/smoke-hermes-player.py --output /path/to/private/evidence \
  wall navigation gather_craft task_lifecycle policy containment
```

The runner gives unique cross-session request-ID prefixes, limits each query to
45 turns, and preserves JSONL, stderr, prompts and summaries. It executes real
Hermes calls; it does not grade the agent's self-report as authoritative success.
On timeout, the runner stops the batch immediately. Investigate/cancel the owning
gateway session and any bridge task before starting another scenario:
killing the CLI client alone does not prove gateway execution stopped. Reset
fixtures between independent batches; preserve earlier evidence before resetting.

Independently read block types, coordinates, inventory and durable task states.
An exit code of zero only means the conversation returned. MCPQ log breaking
removes blocks without harvesting, and `craft_item` grants items without recipe
consumption: classify those as simulation-only, never survival success.

See [the smoke report](../../docs/testing/hermes-steve-player-smoke.md) for actual
results, discovered bugs, validation boundaries, and local operations.
