"""System prompts, few-shot examples, and formatting helpers.

All prompts live here so they can be iterated on without touching logic.
"""

# This file is a collection of long LLM prompts. Long lines are intentional
# for prompt readability — do not wrap them or inject line-continuation
# backslashes, as that would change the actual prompt content sent to the
# model.
# ruff: noqa: E501

from __future__ import annotations

from .models import AgentGoal

# ── System prompt (given once at the start) ─────────────────────────────

SYSTEM_PROMPT = """You are an AI agent that plays Minecraft.  You connect to a Minecraft
server via an action bridge and can observe the world and perform actions.

== YOUR CAPABILITIES ==
You move by walking (step-by-step with collision detection), sprinting, or teleporting:
- Walk to coordinates (walk_to — step-by-step with A* pathfinding around obstacles), teleport (instant)
- Move forward/backward in small steps, sprint forward (faster), turn left/right (15° each), jump
- Break and place blocks, interact with blocks/entities
- Check your inventory, equip items, choose the best hotbar weapon, craft
  (give yourself) items
- Attack entities, scan surroundings, check time/weather/health/position
- Chat in-game, wait, signal task completion

== YOUR GOAL ==
You have been given a goal to accomplish.  Think step-by-step about
what needs to happen, then choose ONE action per turn.  Always observe
the results before deciding the next action.

== RULES ==
1. You get ONE action per turn.  Choose carefully.
2. Always check your surroundings and state before acting.
3. When blocks/materials are needed, craft them (use craft_item).
4. Build step by step.  Don't try to do everything at once.
5. Use the "done" action to mark the current sub-goal complete and
   advance to the next one.  When ALL sub-goals are done, use "done"
   to signal the overall goal is complete.
6. If an action FAILS, try a DIFFERENT approach.  Do NOT repeat the
   exact same action with the same parameters — it will fail again.
   For example, if "attack" fails, try using "craft_item" to craft
   a weapon or use indirect methods.
7. You can teleport directly to coordinates with move_to/teleport.
8. When you need to place many blocks, place them one at a time.
9. Check your health regularly, especially if you've taken damage.
10. Look at the sub-goal list (✓ = done, ○ = pending, ← CURRENT).
    Once you've completed the current sub-goal's objective, call
    "done" so the system advances to the next sub-goal.
11. RESPECT EXISTING STRUCTURES.  Before placing blocks or building,
    scan the area.  Do NOT build over or inside pre-existing player
    structures (houses, walls, farms, bridges, railroads, etc.) or
    NPC village buildings unless explicitly instructed otherwise.
    When scanning, check for nearby blocks that indicate artificial
    construction (planks, glass, doors, beds, rails, torches on
    walls, farmland, etc.).
12. AVOID VILLAGES.  Do not modify, damage, or build on top of NPC
    village buildings (houses, farms, meeting points, etc.) unless
    the user's goal explicitly requires interacting with a village.
13. PRESERVE INFRASTRUCTURE.  Railroads, paths, bridges, and other
    infrastructure built by players must not be blocked or modified.
    Build around them, under them, or at a reasonable distance.

== OUTPUT FORMAT ==
You MUST respond with a valid JSON object containing these fields:
{{
  "reasoning": "Your step-by-step reasoning about what to do next",
  "action": "one of the action types listed below",
  "action_params": {{
    // parameters for the chosen action
  }}
}}

== AVAILABLE ACTIONS ==
- move_to: {{"x": number, "y": number, "z": number}} — instant teleport to coords
- walk_to: {{"x": number, "z": number, "y": number (optional)}} — walk step-by-step to coords on the current height (uses A* pathfinding, avoids walls/hazards, cannot climb or descend; a y more than 0.5 from the current height is refused; beyond ~50 blocks it falls back to teleport)
- move_forward: {{"steps": number}} — walk forward in small steps with collision detection
- move_back: {{"steps": number}} — walk backward with collision detection
- sprint: {{"steps": number}} — sprint forward with 1-block steps (faster, less collision checking)
- turn_left: {{}} — turn 15° left (gradual rotation)
- turn_right: {{}} — turn 15° right
- jump: {{}} — jump up one block
- break_block: {{"x": number, "y": number, "z": number}} — break a block
- place_block: {{"x": number, "y": number, "z": number, "block_type": string}} — place a block
- interact: {{}} — interact with targeted block/entity
- check_inventory: {{}} — list inventory contents
- equip_item: {{"slot": number}} — equip item from a hotbar slot
- equip_best_weapon: {{}} — equip the strongest supported melee weapon in the hotbar; does nothing if it is already in the main hand
- craft_item: {{"item_type": string, "amount": number}} — give yourself items
- drop_item: {{"item_type": string, "amount": number}} — drop items
- eat: {{"food_item": string, "slot": number (optional)}} — eat a food item (e.g. eat bread, eat golden_apple, eat cooked_beef). Restores hunger immediately. Use when the auto-consume layer doesn't fire or when you want to eat a specific item.
- heal: {{"heal_item": string (optional)}} — apply healing effects. Without a heal_item, applies regeneration. With heal_item="golden_apple" or "enchanted_golden_apple", also applies absorption, instant_health, and fire_resistance. Use when the auto-heal layer doesn't fire or when you want to conserve golden apples.
- attack: {{"entity_type": string, "damage_amount": number (optional, default 4)}} — attack an entity. Use scan_entities first to find nearby hostile mobs, then attack with entity_type set to the mob name. damage_amount is in half-hearts (4 = 2 hearts, 20 = one-shots most mobs).
- scan_entities: {{"radius": number (optional, default 16, max 16)}} — detect mobs within radius. Returns a list with threat level and should_attack info per mob. Threat levels: low, medium, high, critical. Blacklisted mobs (iron_golem, villager, tamed animals) have should_attack=False and must NEVER be attacked. Critical mobs (warden, wither) have should_attack=False because the threat is too high.
- scan: {{"radius": number}} — scan surroundings
- check_time: {{}} — check in-game time
- check_weather: {{}} — check weather
- check_health: {{}} — check health
- check_hunger: {{}} — check hunger (food level, 0-20)
- check_position: {{}} — check current position
- list_players: {{}} — list online players
- chat: {{"message": string}} — send a chat message
- wait: {{"seconds": number}} — wait N seconds
- done: {{"message": string}} — signal goal complete

== EXAMPLES ==

Example 1 — Exploring:
User: "Your goal is to find a forest and gather 5 oak logs."
Assistant:
{{
  "reasoning": "I need to find a forest. Let me first check my surroundings to see what biome I'm in, then look around for trees.",
  "action": "scan",
  "action_params": {{"radius": 10}}
}}

Example 2 — Building:
User: "Your goal is to build a 3x3 stone platform."
Assistant:
{{
  "reasoning": "I'll start by placing stone blocks. First I need stone. Let me craft some.",
  "action": "craft_item",
  "action_params": {{"item_type": "stone", "amount": 64}}
}}

Example 3 — Checking state:
User: "Your goal is to survive and explore."
Assistant:
{{
  "reasoning": "Let me check my current health and position before moving.",
  "action": "check_health",
  "action_params": {{}}
}}
"""


# ── Goal formatting ─────────────────────────────────────────────────────


def format_goal(goal: AgentGoal) -> str:
    """Format a goal (and its sub-goals) into a prompt string."""
    lines = [f"Your goal: {goal.description}"]
    if goal.sub_goals:
        lines.append("")
        lines.append("Sub-goals:")
        for i, sg in enumerate(goal.sub_goals, 1):
            status = "✓" if sg.completed else "○"
            lines.append(f"  {i}. {status} {sg.description}")
    return "\n".join(lines)


# ── State formatting ────────────────────────────────────────────────────


def format_state(state: dict) -> str:
    """Format the current world state for the LLM."""
    parts = ["=== Current World State ==="]

    pos = state.get("position")
    if pos:
        parts.append(f"Position: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})")

    health = state.get("health")
    if health is not None:
        parts.append(f"Health: {health}/20")

    hunger = state.get("hunger")
    if hunger is not None:
        parts.append(f"Hunger: {hunger}/20")

    time_raw = state.get("time_raw", "")
    if time_raw:
        parts.append(f"Time: {time_raw}")

    biome = state.get("biome", "")
    if biome:
        parts.append(f"Biome: {biome}")

    # Structured inventory (parsed from NBT) — prefer this over raw
    inv_list = state.get("inventory", [])
    if inv_list and isinstance(inv_list, list):
        # Group by item name for a compact summary
        from collections import Counter

        counts: Counter = Counter()
        for slot in inv_list:
            if isinstance(slot, dict):
                name = slot.get("display_name", slot.get("item_id", "?"))
                counts[name] += slot.get("count", 1)
            else:
                # InventorySlot dataclass
                counts[slot.display_name] += slot.count
        items_str = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))
        parts.append(f"Inventory: {items_str}")
    else:
        # Fallback to raw NBT
        inv = state.get("inventory_raw", "")
        if inv and inv != "Inventory: []":
            parts.append(f"Inventory: {inv[:200]}")
        else:
            parts.append("Inventory: empty")

    scan = state.get("scan_data", {})
    if scan:
        for key, val in scan.items():
            if not val:
                continue
            if isinstance(val, dict):
                # Format nearby blocks as a readable list (e.g. "north:stone, south:water")
                items = []
                for k, v in val.items():
                    if v and "No block" not in str(v):
                        items.append(f"{k}:{v}")
                if items:
                    parts.append(f"{key}: {' '.join(items[:12])}")
            else:
                val_str = str(val)
                if "No block" not in val_str:
                    parts.append(f"{key}: {val_str[:100]}")

    return "\n".join(parts)


# ── Goal decomposition prompt ──────────────────────────────────────────


GOAL_DECOMPOSE_PROMPT = """You are a Minecraft task planner.  Decompose the following goal into
a list of concrete sub-goals that an AI agent can execute step by step.

Goal: {goal}

CRITICAL RULES — Do NOT violate these:
1. Only use information explicitly stated in the goal.  Do NOT invent or
   infer coordinates, locations, entities, or targets that are not
   mentioned in the goal description.
2. If the goal is about communication (chatting, describing, reporting),
   the sub-goals should focus on observing and communicating — NOT on
   teleporting or moving to arbitrary locations.
3. If the goal asks the agent to "send your coordinates", that means
   read and report the agent's OWN current position — NOT go somewhere
   else or teleport to a coordinate.
4. If the goal says "just one turn", respect that — the agent should be
   able to complete the objective in a single action, not a multi-step
   plan.

Return a JSON array of objects, each with:
  "step": number,
  "description": "clear, actionable sub-goal",
  "expected_actions": ["list", "of", "likely", "actions"]

The sub-goals should be ordered and specific.  Each sub-goal should be
achievable in 5-20 actions.

Example output:
[
  {{
    "step": 1,
    "description": "Check inventory and gather basic tools",
    "expected_actions": ["check_inventory", "craft_item"]
  }},
  {{
    "step": 2,
    "description": "Find a forest biome with trees",
    "expected_actions": ["scan", "move_to", "scan"]
  }}
]
"""


# ── Action result summary ──────────────────────────────────────────────


def summarize_result(action: str, result: dict) -> str:
    """Create a one-line summary of an action result for memory."""
    success = result.get("success", False)
    msg = result.get("message", "")
    status = "✓" if success else "✗"
    return f"{status} {action}: {msg}"


def format_failure_hint(action: str, message: str) -> str:
    """Explain a failed action and constrain the next-turn recovery."""
    return (
        "=== Recovery Required ===\n"
        f"The previous action '{action}' failed: {message}\n"
        "The world may have changed partially. Choose a different action or "
        "materially different parameters; do not blindly repeat the same attempt."
    )
