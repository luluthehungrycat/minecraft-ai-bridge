# RFC-0001 Phase D — Mineflayer backend design spike

- **Status:** Design spike (2026-11-10). Not implemented.
- **Parent:** [RFC-0001](RFC-0001-harness-agnostic-agent-runtime.md), Phase D.
- **Gate (from the RFC):** simple observe/move/dig/place/stop tasks pass through the **same** Python `ActionService`; policies remain enforced during network or sidecar faults.

## Summary

1. **Mineflayer cannot connect to Paper 26.2 natively today.** The newest supported version is 26.1. 26.2 support has been open upstream since July 2026 and is unreleased; 26.3 work has already started. A live check of a 26.1 bot through **ViaBackwards** ([section 1.1](#11-live-compatibility-check-2026-11-10)) found:
   - spawn, observe, dig, place, attack and stop work;
   - movement works for a new player, but a **returning player is pulled back by the server**, so movement through ViaBackwards is not reliable yet.
2. **Our `WorldAdapter` seam is shaped around server commands**, not a player's actions. Action handlers build commands (`setblock`, `tp`, `execute as … run …`) and send them through `run_as_player`/`run_command_blocking`/`set_block`/`teleport_player`. A Mineflayer bot is an ordinary, non-operator player and must stay one (RFC-0001: no OP commands on controller keys). So Phase D needs **backend-specific action handlers** behind the same `ActionService`, not just a second `WorldAdapter` implementation.
3. **Sidecar protocol:** a Node.js child process speaking newline-delimited JSON-RPC 2.0 over stdio. No network port; its lifetime is tied to the Python process. Policy, authorization, idempotency and the event journal all stay in Python.

## 1. Version and ecosystem findings

Checked 2026-11-10 against the npm registry and the PrismarineJS repositories.

| Item | Finding |
|------|---------|
| mineflayer | 4.39.0 (2026-09-06). `testedVersions` ends at **26.1**. |
| minecraft-protocol | 1.68.0 (2026-08-27). `defaultVersion` is `26.1`. |
| minecraft-data | 3.119.0 (2026-10-29). 26.2 data PRs are still open. |
| 26.2 tracking | [mineflayer#3952](https://github.com/PrismarineJS/mineflayer/issues/3952); PRs mineflayer#3926/#4192/#3958, node-minecraft-protocol#1496/#1550, minecraft-data#1219/#1344. One report has a patched bot working on 26.2 (the key fix is the new `fluidCount` u16 in 26.x chunk sections). |
| ViaVersion / ViaBackwards | 5.12.2 (2026-10-27). Its release notes include 26.2 → 26.1 translation fixes. Tested live, see [section 1.1](#11-live-compatibility-check-2026-11-10). |
| Node.js | mineflayer 4.39 requires Node ≥ 22. The VPS has Node 22.23. |
| mineflayer-pathfinder | npm 2.4.5 (2023), with repository commits through 2026-10. It works with mineflayer, but behaviour on 26.x is unverified. |
| mineflayer-pvp | Effectively unmaintained (npm 2022). Not used; attacks use the built-in `bot.attack`. |
| Combat and digging on 26.x | Open PRs change the attack/`use_entity` packet shape and the digging action IDs, so they are not final upstream. |

Options for the version gap:

| Option | Cost | Risk |
|--------|------|------|
| **A. ViaVersion + ViaBackwards on the server, bot speaks 26.1** (recommended to try first) | Two server plugins | A translation layer; chunk, entity and inventory quirks are possible |
| B. Run a separate Paper 26.1 test server for the Mineflayer track | A second server, and a world that diverges from MCPQ's | MCPQ and the bot plugin are built against 26.2 |
| C. Carry the unmerged upstream 26.2 patches | Maintaining a fork | Breaks on every upstream change |
| D. Wait for upstream 26.2 support | None | Unbounded delay; upstream already lags by one version |

Recommendation: design so that switching to native 26.2 later only changes the sidecar's `version` setting.
- **A** is usable for development once the returning-player movement problem below is understood or worked around.
- **B** is the fallback if movement cannot be made reliable through ViaBackwards.

### 1.1 Live compatibility check (2026-11-10)

Setup:
- ViaVersion and ViaBackwards 5.12.2 were added temporarily to the Paper 26.2 build 123 test server and removed afterwards. ViaVersion detected "server version 26.2 (776)".
- Client: mineflayer 4.39.0 and mineflayer-pathfinder 2.4.5 on Node 22.23, offline auth, `version: '26.1'`.
- Five bounded server windows, each a separate login.

| Step | Result |
|------|--------|
| Login, spawn | Works. Survival mode; ViaVersion detected the server as 26.2. |
| Observe | Works: position, health, food, time, players, inventory, `blockAt`. |
| Dig | Works: `grass_block -> air` (by hand), and the server agreed. |
| Place | Works: dirt placed on a reference block 2 away. One attempt directly beside the bot's own hitbox was refused by the server. |
| Attack | Works: one punch took a NoAI zombie (at night) from 20.0 to 19.06 health. |
| Stop | Works: `pathfinder.setGoal(null)` + `clearControlStates()` gave a drift of 0.00 over 1.5 s. `pathfinder.stop()` alone keeps moving until a safe node. |
| Movement, new player | Works: the first session's pathfinder `goto` reached its goal, and a never-seen username walked 31 blocks with 3 forced moves after spawn. |
| **Movement, returning player** | **Fails**: the same username in later sessions was snapped back (41 `forcedMove` packets in about 8 s; `goto` timed out 4 of 4 times). The server logged no "moved wrongly" or "moved too quickly". |

The returning-player failure matches the server never accepting the client's teleport confirmation and so repeatedly resending its position. The difference is saved player data, which suggests something in the restored player state is translated wrongly between 26.2 and 26.1. **Not confirmed:**
- deleting the saved data was not tried;
- the same client was not tried against a native 26.1 server;
- the upstream issues were not checked for this symptom.

## 2. Architecture

```
ControllerService ─▶ ActionService (policy, reach guard, idempotency, audit)  ← unchanged
                         │
                         ▼
               execute_action(adapter, action, params)
                 ├─ MCPQ handlers  ──▶ McpqWorldAdapter ──▶ MCPQ gRPC (server commands)
                 └─ Mineflayer handlers ─▶ MineflayerWorldAdapter ─▶ stdio JSON-RPC ─▶ Node sidecar ─▶ Minecraft protocol
```

- `execute_action` chooses the handler table by `adapter.capabilities.backend`. MCPQ handlers stay as they are. Mineflayer handlers call adapter methods that express what a player does, not commands.
- The shared `WorldAdapter` Protocol is narrowed to what both backends can honestly provide (position, health/food, inventory, block reads, time, players, chat, death/respawn events). Command methods (`run_as_player`, `run_command_blocking`, `set_block`, `teleport_player`) move to an MCPQ-only protocol. Code that needs them (observer NBT parsing, safety reflexes, chat commands) gets a per-backend path or reports the capability as missing.
- `Capabilities` already has `backend=mineflayer`, `movement=physical`, `crafting=vanilla`. The Mineflayer adapter reports `world_edit=False`, `max_interaction_distance≈4.5` (survival reach), and `supported_versions` taken from the connected session.
- Reach limits and authorization are unchanged and still enforced in Python before dispatch. Mineflayer adds the game's own limits on top (survival reach, needing the item in inventory, line of sight).

## 3. Capability parity matrix (controller-exposed actions)

| Action | MCPQ today | Mineflayer plan | Parity notes |
|--------|-----------|-----------------|--------------|
| check_position / health / hunger / inventory | NBT via commands | `bot.entity.position`, `bot.health`, `bot.food`, `bot.inventory` | Same shape, different source |
| check_time / weather / list_players | Commands | `bot.time`, `bot.isRaining`, `bot.players` | Equivalent |
| scan / scan_entities | Block reads and selector tests | `bot.blockAt` in loaded chunks; `bot.entities` | Mineflayer only sees loaded, visible chunks |
| chat | `post_to_chat` | `bot.chat` | Equivalent |
| wait / done | Local | Local | Equivalent |
| **walk_to** | A* plus stepwise teleports | `mineflayer-pathfinder` `goto` | **Physical**: can fail, fall or take damage; longer |
| move_to / teleport | `/tp` (privileged) | **Unsupported**, or mapped to `walk_to` within reach limits | Teleporting needs OP; do not grant it |
| move_forward/back, turn, jump, sprint | Small teleports | Control states for a bounded time; `bot.look` | Physical |
| **break_block** | `setblock air` (instant, any block) | `bot.dig(block)` | Takes time, needs a suitable tool, drops items, needs reach |
| **place_block** | `setblock` (any block, no item needed) | `equip` + `bot.placeBlock(reference, face)` | Needs the item in inventory and an adjacent reference block |
| equip_item / equip_best_weapon / drop_item | Inventory commands | `bot.equip`, `bot.toss` | Equivalent |
| craft_item | Privileged-simulated | `bot.craft` with a recipe (crafting table nearby for 3×3) | Vanilla rules |
| eat / heal | Effects and commands (heal is privileged) | `bot.consume`; heal **unsupported** | No cheat healing |
| attack | `damage` command | `bot.attack(entity)` | 26.x attack packet is still changing upstream |
| interact | `execute … run interact` | `bot.activateBlock` / `activateEntity` | Equivalent intent |
| **stop** (gate) | No dedicated action; tasks cancel between steps | `pathfinder.setGoal(null)` (not `stop()`, which runs on to a safe node), `clearControlStates()`, `stopDigging()` | Needed for both backends: add a `stop` action to the controller API |

Every action gets an explicit status per backend (supported, physical, unsupported) through `Capabilities`. An unsupported action is rejected with `ACTION_NOT_SUPPORTED` before dispatch, never silently mapped to a privileged path.

## 4. Sidecar protocol

- **Transport:** the Python adapter spawns `node sidecar/index.js`, and the two exchange newline-delimited JSON-RPC 2.0 messages over stdin/stdout, with stderr used for logs. There is no port to secure, the OS process boundary does the authentication, and closing stdin ends the bot.
- **Requests** (Python → sidecar): `connect{host, port, username, auth, version}`, `observe{}`, `block_at{x,y,z}`, `goto{x,y,z,range,timeout_ms}`, `dig{x,y,z,timeout_ms}`, `place{x,y,z,item,timeout_ms}`, `equip`, `consume`, `attack{entity_id}`, `chat{message}`, `stop{}`. Each request carries an `id` and a deadline. The sidecar answers with a result `{ok, data}` or a typed error (`TIMEOUT`, `NOT_REACHABLE`, `NO_ITEM`, `INTERRUPTED`, `NOT_CONNECTED`).
- **Notifications** (sidecar → Python): `spawn`, `health{health, food}`, `hurt{attacker_type?}`, `death{message}`, `respawn`, `chat`, `kicked{reason}`, `end{reason}`, plus a `heartbeat` every second. These feed the existing condition detector and event journal (`steve.damaged` with a real attacker, `steve.died`, `steve.respawned`), replacing MCPQ's NBT polling with push events.
- **One mutation at a time:** Python's ActionService already serializes dispatches. The sidecar rejects a second mutating request while one is running, except `stop`, which always preempts.
- **Versioning:** the first message is `hello{protocol: 1, mineflayer, minecraft_version}`, and Python refuses a mismatched protocol version.

## 5. Faults and safety

- **Policies cannot be bypassed by faults:** authorization, reach limits, idempotency and audit all run in Python before a request is sent. A sidecar that crashes, hangs or behaves badly can only fail requests; it cannot widen what is allowed.
- **Outcomes when the sidecar or network fails:** a mutating request that times out, or whose sidecar dies, finishes as `OUTCOME_UNKNOWN` and is never retried automatically. This is the same rule as the durable outcome store and task runner today.
- **Watchdogs:**
  - Python marks the adapter disconnected after 3 missed heartbeats and reports degraded capabilities.
  - The sidecar runs `stop` (clearing control states, stopping pathfinding and digging) when stdin closes or no Python request or ping arrives within 5 s. It quits the server if the parent is gone.
- **Reconnects:** after `kicked`/`end`, Python reconnects with backoff. Pending requests resolve as `NOT_CONNECTED` (reads) or `OUTCOME_UNKNOWN` (mutations).
- **Fast paths in the sidecar:** safety logic stays in Python's `SafetySupervisor`. The sidecar reacts to push events within one tick, which is faster than MCPQ polling. A sidecar-local reflex (for example stopping at a lava edge) is a later option, not part of the gate.

## 6. Identity mapping

- `steve_id` stays the stable logical ID. Its backend config names the backend (`mcpq` | `mineflayer`) and, for Mineflayer, `username`, `auth: offline | microsoft`, and `version`.
- **One live backend per Steve.** Migrating between backends is an explicit admin operation, never automatic.
- **Offline-mode collision:** our bot plugin and an offline-mode Mineflayer login both derive the UUID from `OfflinePlayer:<name>`. A Mineflayer bot with the same name as a summoned fake player therefore has the same UUID, and the server refuses or kicks one of them ("already online"). Use a distinct username per backend, or make the adapter refuse to connect while a fake player with that name is online.
- **Microsoft auth** (online-mode servers) needs a device-code login and token cache. That is a credential the operator provides, kept out of controller-visible config, and is out of scope for the gate.
- **Offline mode trusts the username as identity.** That is acceptable for this loopback test server; it is the same caveat that applies to chat commands today.

## 7. What the gate test looks like

The test mirrors `tests/test_phase_c_integration.py`: a live server and a Mineflayer-backed Steve. A controller drives observe, walk_to, dig, place and stop through `ControllerService`/`ActionService`, and the test asserts:

- each action is audited and policy-checked;
- an out-of-reach edit is still refused;
- killing the sidecar while `goto` is running leaves an `OUTCOME_UNKNOWN` result, no further mutation, and a refused next request until reconnect;
- a replayed request ID does not dig or place twice.

## Next steps

1. **Done:** the live compatibility check ([section 1.1](#11-live-compatibility-check-2026-11-10)). **Open:** why a returning player is pulled back through ViaBackwards. Ways to narrow it down:
   - run the same client against a native Paper 26.1 server;
   - delete the bot's saved player data before each login;
   - look for upstream ViaBackwards and mineflayer reports.
   
   Until it is resolved, the live gate test must use a fresh player for each run, or option B.
2. Refactor first, with no behaviour change: split `WorldAdapter` into the shared observation protocol and an MCPQ command protocol; select the handler table by backend; add a `stop` controller action; return `ACTION_NOT_SUPPORTED` per capability.
3. Sidecar and `MineflayerWorldAdapter` behind an optional extra (`[mineflayer]`), with unit tests that use a fake sidecar speaking the JSON-RPC protocol.
4. Live gate test as described in section 7.
