# RFC-0001 Phase D — Mineflayer backend design spike

- **Status:** Design spike (2026-11-10). Not implemented.
- **Parent:** [RFC-0001](RFC-0001-harness-agnostic-agent-runtime.md), Phase D.
- **Gate (from the RFC):** simple observe/move/dig/place/stop tasks pass through the **same** Python `ActionService`; policies remain enforced during network or sidecar faults.

## Summary

1. **Mineflayer cannot connect to Paper 26.2 natively today.** The newest supported version is 26.1. 26.2 support has been open upstream since July 2026 and is unreleased; 26.3 work has already started. A live check of a 26.1 bot through **ViaBackwards** ([section 1.1](#11-live-compatibility-check-2026-11-10)) found:
   - spawn, observe, dig, place, attack and stop work;
   - movement is **unreliable**: at some spots the server keeps pulling the walking bot back, for new and returning players alike (see [section 1.2](#12-follow-up-the-snap-back-2026-11-11)).
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
| B. Run a separate Paper 26.1 test server for the Mineflayer track | A second server, and a world that diverges from MCPQ's | MCPQ and the bot plugin are built against 26.2. Tested in section 1.2: movement corrections happen there too, so it is not a movement workaround |
| C. Carry the unmerged upstream 26.2 patches | Maintaining a fork | Breaks on every upstream change |
| D. Wait for upstream 26.2 support | None | Unbounded delay; upstream already lags by one version |

Recommendation: design so that switching to native 26.2 later only changes the sidecar's `version` setting.
- **A** is usable for development once the movement snap-back below is understood or worked around.
- **B** was tested (section 1.2) and also shows the snap-back, so it is not a demonstrated movement workaround.

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
| **Movement** | **Unreliable**: the first session's pathfinder `goto` reached its goal and a never-seen username walked 31 blocks, but in other sessions the bot was snapped back (41 `forcedMove` packets in about 8 s; `goto` timed out 4 of 4 times). The server logged no "moved wrongly" or "moved too quickly". |

### 1.2 Follow-up: the snap-back (2026-11-11)

Four more bounded windows narrowed the movement failure down. The initial saved-player-data hypothesis did not explain the follow-up results:

| Test | Result |
|------|--------|
| Same returning player, three logins (the second with `tick_end`, see below) | 0.00, 0.00 and 6.18 blocks net, with 40, 48 and 34 forced moves |
| Returning player vs a new one in the same window | The returning player walked with **0** resets; the new one got **24**, all putting it back at the same spot |
| Sending `tick_end` after every physics tick | No change (48 forced moves). mineflayer 4.39.0 never sends it; master does since [mineflayer#4137](https://github.com/PrismarineJS/mineflayer/pull/4137) (2026-10-03). It is what 26.3 servers kick for ([ViaBackwards#1362](https://github.com/ViaVersion/ViaBackwards/issues/1362), [mineflayer#4189](https://github.com/PrismarineJS/mineflayer/issues/4189)), but not this. |
| Bot's view vs the server's (MCPQ `getBlock`), 100 blocks around each reset spot | Block names match exactly. Block states such as stair or slab orientation were not compared. |
| mineflayer master ([5a252e6](https://github.com/PrismarineJS/mineflayer/commit/5a252e6d258882b042e60e21c37d552cdf3b3380), sends `tick_end`), same three players and spots | Still reset: 21, 37 and 24 forced moves |
| Native Paper 26.1.2 build 74 server, no ViaVersion/ViaBackwards (throwaway container), three new players, mineflayer 4.39.0 | **Also reset**, less often: 11, 6 and 6 forced moves for the same walk |

So the snap-back **reproduces on a native 26.1 server**: ViaBackwards is not needed to cause it, though it does not isolate the faulty component. A mismatch between Mineflayer's client-side physics (prismarine-physics) and the 26.x server is suspected, not established, while full block states are still uncompared. In these probes the native runs showed fewer corrections; the runs were not matched closely enough to put a number on the difference. A walking client should normally get no forced moves at all. Neither Mineflayer master nor `tick_end` fixes it. **Remaining checks:**
- find or file the upstream prismarine-physics/mineflayer issue for 26.x movement corrections, with this reproduction;
- compare full block states, not just names, at a reset spot;
- check whether pathfinder's `goto` still reaches its goal despite resets (it recomputes when stuck) or whether the backend needs its own retry and progress check.

Until movement is reliable, the Mineflayer gate test cannot rely on `walk_to`; option B (a native 26.1 server) does not help either.

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
- **Authorization stays conservative at first.** `classify_action` depends only on the `ActionType`. Physical Mineflayer moves, digs and places therefore keep their current class and still need the service's `admin:world` token plus the matching `steve:world:*` delegation scope. Relaxing that for physical actions (for example, a survival-only scope that does not need `admin:world`) is a separate, later decision that would make the classification backend-aware.
- **Capabilities needs a schema extension.** Today it has no per-action field. Add `actions: dict[ActionType, SupportLevel]` (`supported` | `physical` | `unsupported`), which ControllerService checks before dispatch.

## 3. Capability parity matrix (controller-exposed actions)

| Action | MCPQ today | Mineflayer plan | Parity notes |
|--------|-----------|-----------------|--------------|
| check_position / health / hunger / inventory | NBT via commands | `bot.entity.position`, `bot.health`, `bot.food`, `bot.inventory` | Same shape, different source |
| check_time / weather / list_players | Commands | `bot.time`, `bot.isRaining`, `bot.players` | Equivalent |
| scan / scan_entities | Block reads and selector tests | `bot.blockAt` in loaded chunks; `bot.entities` | Mineflayer only sees loaded, visible chunks |
| chat | `post_to_chat` | `bot.chat` | Equivalent |
| wait / done | Local | Local | Equivalent |
| **walk_to** | A* plus stepwise teleports | `mineflayer-pathfinder` `goto`, with `Movements.canDig = false`, `allow1by1towers = false` and no scaffolding blocks | **Physical**: can fail, fall or take damage; longer. Pathfinder digs and builds by default, which would turn a movement grant into world edits; movement must never edit. |
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

Every action gets an explicit status per backend (supported, physical, unsupported) through the new `Capabilities.actions` field. An unsupported action is rejected with `ACTION_NOT_SUPPORTED` before dispatch, never silently mapped to a privileged path.

## 4. Sidecar protocol

- **Transport:** the Python adapter spawns `node sidecar/index.js`, and the two exchange newline-delimited JSON-RPC 2.0 messages over stdin/stdout, with stderr used for logs. There is no port to secure, the OS process boundary does the authentication, and closing stdin ends the bot.
- **Requests** (Python → sidecar): `connect{host, port, username, auth, version}`, `observe{}`, `block_at{x,y,z}`, `goto{x,y,z,range,timeout_ms}`, `dig{x,y,z,timeout_ms}`, `place{x,y,z,item,timeout_ms}`, `equip`, `consume`, `attack{entity_id}`, `chat{message}`, `stop{}`. Each request carries an `id` and a deadline. The sidecar answers with a result `{ok, data}` or a typed error (`TIMEOUT`, `NOT_REACHABLE`, `NO_ITEM`, `INTERRUPTED`, `NOT_CONNECTED`).
- **Notifications** (sidecar → Python): `spawn`, `health{health, food}`, `hurt{attacker_type?}`, `death{message}`, `respawn`, `chat`, `kicked{reason}`, `end{reason}`, plus a `heartbeat` every second. These feed the existing condition detector and event journal (`steve.damaged` with a real attacker, `steve.died`, `steve.respawned`), replacing MCPQ's NBT polling with push events.
- **One mutation at a time:** Python's ActionService already serializes dispatches. The sidecar also rejects a second mutating request while one is running.
- **Stop does not wait for the dispatch lock, but still goes through ActionService.** `ActionService.execute` holds `_dispatch_lock` for the whole handler, so a `stop` dispatched as an ordinary action would wait for the `goto` it is meant to end. Instead:
  - ActionService gets a second entry point, `interrupt(request)`, that never takes the dispatch lock. It runs the same steps as `execute` except dispatch serialization: the identity scope and policy hook (a `stop` action class that a session holder may always use), the audit trace, and the journal. It then calls `adapter.interrupt(op_id)` for the operation that is currently dispatched. If nothing is dispatched, it records a no-op.
  - ControllerService's `stop` validates the session, generation and credential like any action, then delegates to `ActionService.interrupt`. It never calls the adapter directly.
  - The adapter sends `stop{op_id}` to the sidecar. The in-flight handler then returns `INTERRUPTED`, which releases the lock.
  - **Operation targeting:** the adapter publishes the active `(epoch, op_id)` before sending an operation and clears it on completion only if it still matches. `interrupt` targets that pair. The sidecar treats a `stop` whose `op_id` is not running as a no-op, so a stop that races with A finishing can never stop B. ActionService's existing `_inflight` and `_dispatch_started` can't serve this purpose: the first includes queued requests and the second tracks only explicit request IDs.
  - `stop` is not a mutation and has no request-ID idempotency; a repeated stop is harmless.
  - The gate tests stop against a deliberately stalled `goto`.
- **Versioning:** the first message is `hello{protocol: 1, mineflayer, minecraft_version}`, and Python refuses a mismatched protocol version.

## 5. Faults and safety

- **Policies cannot be bypassed by faults:** authorization, reach limits, idempotency and audit all run in Python before a request is sent. A sidecar that crashes, hangs or behaves badly can only fail requests; it cannot widen what is allowed.
- **Outcomes when the sidecar or network fails:** a mutating request that times out, or whose sidecar dies, finishes as `OUTCOME_UNKNOWN` and is never retried automatically. Today's dispatcher cannot express that: `execute_action` turns handler exceptions into failed results, and ActionService records unsuccessful results as `failed`. So:
  - the Mineflayer adapter raises a dedicated `BackendOutcomeUnknown`;
  - `execute_action` re-raises it instead of converting it;
  - ActionService records the existing `unknown` outcome status (as it already does for a dispatch cancelled midway) in memory and in the durable outcome store, so a replay returns `unknown` and never re-dispatches;
  - ControllerService returns `INTERRUPTED` with reason code `OUTCOME_UNKNOWN`;
  - the task runner checkpoints the step as `OUTCOME_UNKNOWN`, as it does for an in-flight step after a restart.
- **Deadlines fence the outstanding work.** A sidecar can stay alive and answer pings while one operation runs past its deadline, so neither watchdog below would catch it. Therefore:
  - every mutating request carries an `op_id` and `deadline_ms`, and the sidecar aborts the operation itself at the deadline;
  - when Python's own deadline (sidecar deadline plus a margin) passes, the adapter records `OUTCOME_UNKNOWN`, sends `stop{op_id}`, and enters a **fenced** state that rejects every new mutation;
  - it leaves the fenced state only when the sidecar acknowledges `stopped{op_id}` with no operation running. Without that acknowledgement within a few seconds, it kills the sidecar process;
  - each sidecar process and connection gets a new `epoch`. Responses and notifications from an older epoch are dropped, so a late result from before a restart or reconnect cannot be mistaken for current state.
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
- **Offline mode trusts the username as identity.** That is acceptable for this isolated loopback test server and the gate experiment. It does not enable protected chat commands: those still require an explicitly asserted online-mode server, so they stay disabled with an offline Mineflayer login.

## 7. What the gate test looks like

The test mirrors `tests/test_phase_c_integration.py`: a live server and a Mineflayer-backed Steve. A controller drives observe, walk_to, dig, place and stop through `ControllerService`/`ActionService`, and the test asserts:

- each action is audited and policy-checked;
- an out-of-reach edit is still refused;
- killing the sidecar while `goto` is running leaves an `OUTCOME_UNKNOWN` result, no further mutation, and a refused next request until reconnect;
- a sidecar that stays alive but stalls an operation past its deadline is fenced: the result is `OUTCOME_UNKNOWN`, new mutations are refused until it acknowledges `stopped`, and it is killed if it never does;
- `stop` ends a deliberately stalled `goto` without waiting for it;
- a controller with `steve:world:move` but not `steve:world:edit` cannot cause a block change by walking into an obstacle;
- a replayed request ID does not dig or place twice.

## Next steps

1. **Done:** the live compatibility check ([section 1.1](#11-live-compatibility-check-2026-11-10)). **Open:** why the server pulls the walking bot back through ViaBackwards; the remaining checks are in [section 1.2](#12-follow-up-the-snap-back-2026-11-11). Until it is resolved, movement through ViaBackwards cannot pass the gate, and option B does not avoid it (section 1.2).
2. Refactor first, with no behaviour change: split `WorldAdapter` into the shared observation protocol and an MCPQ command protocol; select the handler table by backend; add `Capabilities.actions` and return `ACTION_NOT_SUPPORTED` per action; add the `BackendOutcomeUnknown` path to ActionService; add the lock-free `stop` controller call.
3. Sidecar and `MineflayerWorldAdapter` behind an optional extra (`[mineflayer]`), with unit tests that use a fake sidecar speaking the JSON-RPC protocol.
4. Live gate test as described in section 7.
