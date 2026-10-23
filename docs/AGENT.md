# Agent Internals

This document explains how the AI agent works under the hood — the loop, action system, goal management, memory, and state formatting.

## The Think-Act-Observe Loop

The agent runs a continuous loop in `Orchestrator._step()`:

```
┌──────────────┐
│   Observe    │  ← Gather world state via MCPQ (position, inventory, etc.)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│    Think     │  ← Send context to LLM, get action decision
│  (LLM call)  │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│     Act      │  ← Execute chosen action via MCPQ
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Record     │  ← Store in memory, check completion
└──────┬───────┘
       │
       └──→ Repeat until done or max_iterations
```

### Context Sent to the LLM

On each turn, the LLM receives:

1. **System prompt** — role definition, action catalog, output format rules
2. **Goal context** — the overall goal and current sub-goal
3. **World state** — formatted summary of position, health, inventory, time, nearby blocks
4. **Recent actions** — last N actions and their results (short-term memory)
5. **Notable facts** — persistent discoveries from long-term memory
6. **Last action result** — what happened the previous turn

Example world state format:

```
=== World State ===
Position: (0.0, 65.0, 0.0)
Health: 20.0 / 20.0
Time: 1000 (Day)
Players: [AIBot]
Inventory: (empty or item count)
Nearby blocks:
  north: dirt   south: dirt
  east: stone   west: dirt
  front_eye: air
  d+2z+2: grass_block
```

### LLM Response

The LLM returns a structured response:

```json
{
  "action": "place_block",
  "action_params": {
    "x": 0,
    "y": 65,
    "z": 3,
    "block_type": "oak_planks"
  },
  "reasoning": "I need to build the north wall. The floor is at (0,64,0). I'll place planks at the north edge."
}
```

The `action` field must match an `ActionType` enum value. The `action_params` must match the handler's expected parameters.

## Action System

### ActionType Enum

Defined in `minecraft/actions.py`:

```python
class ActionType(str, Enum):
    MOVE_TO = "move_to"
    MOVE_FORWARD = "move_forward"
    MOVE_BACK = "move_back"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    JUMP = "jump"
    TELEPORT = "teleport"
    BREAK_BLOCK = "break_block"
    PLACE_BLOCK = "place_block"
    INTERACT = "interact"
    CHECK_INVENTORY = "check_inventory"
    EQUIP_ITEM = "equip_item"
    CRAFT_ITEM = "craft_item"
    DROP_ITEM = "drop_item"
    ATTACK = "attack"
    SCAN = "scan"
    CHECK_TIME = "check_time"
    CHECK_WEATHER = "check_weather"
    CHECK_HEALTH = "check_health"
    CHECK_POSITION = "check_position"
    LIST_PLAYERS = "list_players"
    CHAT = "chat"
    WAIT = "wait"
    DONE = "done"
```

### Action Parameters

Each action expects specific parameters:

| Action | Parameters | Description |
|--------|-----------|-------------|
| `move_to` | `x`, `y`, `z` (float) | Teleport to coordinates |
| `move_forward` | `steps` (int, default 2) | Move forward N blocks |
| `move_back` | `steps` (int, default 2) | Move back N blocks |
| `turn_left` | — | Rotate 15 degrees left (via `/tp` facing adjustment) |
| `turn_right` | — | Rotate 15 degrees right (via `/tp` facing adjustment) |
| `jump` | — | Move up 1 block |
| `teleport` | `x`, `y`, `z` (float) | Same as move_to |
| `break_block` | `x`, `y`, `z` (int, optional) | Break block at coords or in front |
| `place_block` | `x`, `y`, `z` (int, optional), `block_type` (string) | Place block |
| `interact` | — | Right-click interaction |
| `check_inventory` | — | List inventory contents |
| `equip_item` | `slot` (int) | Equip item from hotbar slot |
| `craft_item` | `item_type` (string), `amount` (int) | Give item to player |
| `drop_item` | `item_type` (string), `amount` (int) | Remove items from inventory |
| `attack` | — | Attack target entity |
| `scan` | `radius` (int, max 16) | Scan nearby blocks |
| `check_time` | — | Get game time |
| `check_weather` | — | Get weather state |
| `check_health` | — | Get player health |
| `check_position` | — | Get player coordinates |
| `list_players` | — | List online players |
| `chat` | `message` (string) | Send chat message |
| `wait` | `seconds` (float) | Do nothing for N seconds |
| `done` | `message` (string) | Mark task complete |

### ActionResult

Every action returns an `ActionResult`:

```python
@dataclass
class ActionResult:
    success: bool
    action: ActionType
    message: str
    data: dict     # Action-specific data (position, inventory, etc.)
```

## Goal System

### GoalNode Tree

```python
class GoalNode:
    description: str       # "Gather wood"
    completed: bool        # True when done action succeeds for this goal
    sub_goals: list[GoalNode]  # Children for decomposition
    parent: GoalNode | None    # Back-reference for traversal
```

### Decomposition Flow

1. User provides goal (e.g., "Build a house")
2. `GoalManager.set_goal()` sends to LLM:
   > "Break down 'Build a house' into 3-8 concrete sub-goals. Return as JSON array of strings."
3. If LLM returns valid JSON array → those become sub-goals
4. If LLM fails or returns nothing → fallback plans match by keyword

### Fallback Plans

Built-in fallback decomposition plans in `goal_manager.py`:

| Pattern | Steps |
|---------|-------|
| `build.*house\|build.*home\|construct` | 11-step building plan (foundation → floor → walls → door → roof → decorate) |
| `diamond\|\\bore\\b\|mine\|dig\|tunnel\|excavate` | 12-step mining plan (craft pickaxe → dig → find ore → smelt). Uses `\\bore\\b` (word boundaries) so "Explore" doesn't match via the "ore" substring. |
| `farm\|wheat\|plant\|grow\|crop` | 10-step farming plan (hoe → till → seeds → plant → water → harvest) |
| `enchant\|workshop\|alchemy\|potion\|table` | 9-step workshop plan (craft table → bookshelf → enchant → experiment) |
| `explore\|scout\|biome\|village\|find\|locate\|map` | 8-step exploration plan (scan → pick direction → scout → map) |
| (any) | 6-step generic plan (scan → gather → craft → build → expand → done) |

### Progress Tracking

- `mark_current_complete()` — marks the current goal as done, moves to next uncompleted sibling
- `current_goal` — returns the first uncompleted goal (depth-first traversal)
- `is_complete` — True when all goals in the tree are completed
- `progress` — formatted string showing the goal tree with checkmarks

### Chat Commands

The `chat_commands.py` parser intercepts incoming Minecraft chat messages for live agent control without restarting:

| Command | Purpose |
|---------|---------|
| `!stop` | Gracefully shuts down the agent after the current action |
| `!status` | Agent broadcasts goal progress and last action summary to chat |
| `!follow` | Sets follow-player mode for coordinated building; each turn teleports within ~4 blocks of the target via the policy-gated `follow_teleport` action |
| `!goto <player>` | Teleports the agent to the named player via the policy-gated internal `goto_teleport` action |
| `!come` | Teleports the speaker to the agent via the policy-gated internal `come_teleport` action |

Startup provisioning (the `/botsummon` bot summon and the safe-spawn
platform preparation) is also routed through the action gate as the internal
`provision_bot` and `prepare_safe_spawn` actions, which are hidden from the
LLM action space; the bot player name is validated as a strict Minecraft
username before command interpolation.

The built-in chat handler routes `!goto` and `!come` through the
orchestrator's shared ActionService as the internal `goto_teleport` and
`come_teleport` actions, which are also hidden from the LLM action space
(the orchestrator rejects them if a model names them). The `!goto` target
and the `!come` sender are untrusted chat-derived strings: both are
validated with the same strict Minecraft username validator used by
follow-mode teleports and provisioning before they are interpolated into
teleport commands. ActionService and the dispatcher report failures as
failed results rather than raising, so the handler sends the failure
message to the chat explicitly. A handler constructed directly without an
orchestrator ActionService (test/direct harness construction) defaults to
a local ActionService bound to its MCPQ client, so the mutation still
crosses the policy/audit gate; there is no raw command bypass. A harness
exposing only an ActionService (with no adapter and no raw client)
resolves the world from that service, so polling, status, and chat replies
still work. This routing and validation do not add authenticated chat sender
authorization; RFC-0001 chat ownership and authorization remain open.

The self-preservation layer (see below) routes its mutating reflexes —
reflex ATTACK, auto EAT, and auto HEAL — through the same shared
ActionService instance as LLM-selected actions, so policy and audit see
them. Its SCAN_ENTITIES and damage-source checks are observations and stay
direct. The boundary is routing only: a directly constructed layer without
an ActionService still dispatches directly, so this is not complete
mutation mediation or independent safety enforcement.

The parser regex `_COMMAND_RE = r"<([^>]+)>\s+((?:/[!\u00a7]?|!)\w+)(.*)"` handles both `/!status` (with leading slash) and `!status` formats.

### Inventory Manager

The `inventory_manager.py` module provides structured tracking of the player's 41-slot inventory:

- `update_from_nbt(nbt_data)` — parses raw NBT from `/data get entity @p Inventory`
- `slot_summary()` — human-readable summary (hotbar + armor + offhand) for LLM prompts
- `get_slot(slot)` / `find_item(item_type)` — structured lookups

The inventory is presented to the LLM as structured text rather than raw NBT, making it easier for the model to understand what items are available.

## Memory System

### Short-Term Memory

A rolling deque of `ActionRecord` objects:

```python
@dataclass
class ActionRecord:
    action: str
    success: bool
    message: str
    timestamp: float
    data: dict
```

- Configurable window size (default: 20)
- Injected into every LLM prompt as structured text
- Oldest entries drop off automatically

### Long-Term Memory

A set of persistent fact strings:

- Position data from `check_position` actions
- Notable discoveries from `scan` data
- Extracted by `memory.remember_fact()` during action recording

### Summary Formatting

Short-term memory summary format:

```
[1] check_position ✓ Position: (0.0, 65.0, 0.0)
[2] scan ✓ Scanned surroundings (r=5)
[3] craft_item ✓ Gave 1 crafting_table
[4] place_block ✓ Placed crafting_table at (1, 65, 0)
[5] craft_item ✓ Gave 4 oak_planks
```

Long-term facts format:

```
Notable facts:
- Position at spawn: (0.0, 65.0, 0.0)
- Found stone below dirt at spawn
```

## State Observation

The `Observer` class gathers world state every turn:

```python
async def observe(self) -> WorldState:
    # Concurrent queries via asyncio.gather:
    pos = await self._mc.get_player_pos()
    health_raw = await self._mc.run_command_blocking("data get entity @p Health")
    inv_raw = await self._mc.run_command_blocking("data get entity @p Inventory")
    time_raw = await self._mc.get_time()
    players = await self._mc.get_players_online()
    
    # Parse health from NBT response
    health = self._parse_health(health_raw)
    
    # Multi-direction scan
    scan_data = self._scan_surroundings(pos)
    
    return WorldState(...)
```

The `format_state()` function in `prompts.py` converts this to a human-readable string for the LLM.

## Config File Priority

Configuration is resolved in this order (higher wins):

1. Environment variables (highest priority)
2. YAML config file values
3. Pydantic field defaults (lowest priority)

This means you can set `MC_API_HOST=minecraft` in Docker env and it overrides whatever is in `config.yaml`.
