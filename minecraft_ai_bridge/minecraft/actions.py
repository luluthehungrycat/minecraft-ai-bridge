"""Action primitives the LLM can invoke inside Minecraft.

Each action maps to one or more MCPQ plugin operations (or RCON commands
as a fallback), allowing the agent to observe and interact with the world.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .observer import InventorySlot

from .world_types import WorldAdapter

logger = logging.getLogger(__name__)


class ActionType(StrEnum):
    """Every action the LLM can take.  Keep this list concise so the
    LLM's action space stays manageable."""

    # ── Movement ────────────────────────────────────────────────────
    MOVE_TO = "move_to"
    MOVE_FORWARD = "move_forward"
    MOVE_BACK = "move_back"
    WALK_TO = "walk_to"  # human-like step-by-step walking
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    JUMP = "jump"
    SPRINT = "sprint"  # faster forward movement (1.0-block steps)
    TELEPORT = "teleport"
    # Internal-only: dispatched by the orchestrator for `!follow` mode.
    # Deliberately omitted from the LLM action space (ACTION_TOOL enum).
    FOLLOW_TELEPORT = "follow_teleport"
    # Internal-only: dispatched by the orchestrator for trusted startup
    # provisioning (bot summon and safe-spawn platform). Deliberately
    # omitted from the LLM action space (ACTION_TOOL enum).
    PROVISION_BOT = "provision_bot"
    PREPARE_SAFE_SPAWN = "prepare_safe_spawn"
    # Internal-only: dispatched by the built-in chat command handler for
    # !goto (bot teleports to a named player) and !come (sender teleports
    # to the bot). Deliberately omitted from the LLM action space
    # (ACTION_TOOL enum); the orchestrator rejects them if a model names
    # them, so chat-issued teleports keep distinct policy/audit identities
    # from LLM strategic teleports.
    GOTO_TELEPORT = "goto_teleport"
    COME_TELEPORT = "come_teleport"

    # ── Interaction ─────────────────────────────────────────────────
    BREAK_BLOCK = "break_block"
    PLACE_BLOCK = "place_block"
    INTERACT = "interact"

    # ── Inventory / Items ───────────────────────────────────────────
    CHECK_INVENTORY = "check_inventory"
    EQUIP_ITEM = "equip_item"
    EQUIP_BEST_WEAPON = "equip_best_weapon"
    CRAFT_ITEM = "craft_item"
    DROP_ITEM = "drop_item"
    EAT = "eat"
    HEAL = "heal"

    # ── Combat ──────────────────────────────────────────────────────
    ATTACK = "attack"
    SCAN_ENTITIES = "scan_entities"

    # ── Information ─────────────────────────────────────────────────
    SCAN = "scan"
    CHECK_TIME = "check_time"
    CHECK_WEATHER = "check_weather"
    CHECK_HEALTH = "check_health"
    CHECK_HUNGER = "check_hunger"
    CHECK_POSITION = "check_position"
    LIST_PLAYERS = "list_players"

    # ── Communication ───────────────────────────────────────────────
    CHAT = "chat"

    # ── Meta ────────────────────────────────────────────────────────
    WAIT = "wait"
    DONE = "done"  # signal sub-goal / goal completion


@dataclass
class ActionResult:
    """Result of executing a single action."""

    success: bool
    action: ActionType
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


_WEAPON_SCORES: dict[str, int] = {
    "wooden_sword": 1,
    "golden_sword": 1,
    "stone_sword": 2,
    "iron_sword": 3,
    "diamond_sword": 4,
    "netherite_sword": 5,
    "wooden_axe": 1,
    "golden_axe": 1,
    "stone_axe": 2,
    "iron_axe": 3,
    "diamond_axe": 4,
    "netherite_axe": 5,
    "trident": 4,
    "mace": 5,
}


def select_best_weapon(items: Iterable[InventorySlot]) -> InventorySlot | None:
    """Select the strongest supported melee weapon in the hotbar.

    Main-inventory, armor, and offhand slots are excluded because the
    current equip action only copies between hotbar slots. Damage is not
    ranked because ``InventorySlot`` does not include max durability, so a
    nonzero value does not identify a broken weapon.
    """
    candidates: list[tuple[int, InventorySlot]] = []
    for item in items:
        try:
            slot = item.slot
            count = item.count
            item_id = item.item_id
        except AttributeError:
            continue
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or not 0 <= slot <= 8
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(item_id, str)
        ):
            continue
        score = _WEAPON_SCORES.get(item_id.removeprefix("minecraft:").lower())
        if score is not None:
            candidates.append((score, item))

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (candidate[0], -candidate[1].slot))[1]


# ── Block classification ─────────────────────────────────────────────────
# These sets define which blocks the agent can walk through (passable),
# which are hazards (cause damage), and which indicate player-made
# structures (should not be built over).

# ── Edible food items ────────────────────────────────────────────────────
# Used by the EAT action and the auto-consume path in the
# SelfPreservationLayer.  Each entry is (item_id, hunger_restored,
# saturation_points).  Higher saturation is better — we prefer
# golden_carrot over bread over raw_potato.  Values are from the
# Minecraft 1.21.x food table.
#
# The auto-consume layer picks the highest-saturation food in the
# player's inventory; if multiple stacks have the same item, the
# one with the highest count is preferred.
_FOOD_ITEMS: tuple[tuple[str, float, float], ...] = (
    # Tier 1 — best foods
    ("enchanted_golden_apple", 4.0, 9.6),
    ("golden_apple", 4.0, 9.6),
    ("golden_carrot", 6.0, 14.4),
    ("cooked_salmon", 6.0, 9.6),
    ("cooked_cod", 5.0, 6.0),
    # Tier 2 — solid foods
    ("cooked_beef", 8.0, 12.8),
    ("cooked_porkchop", 8.0, 12.8),
    ("cooked_mutton", 6.0, 9.6),
    ("cooked_chicken", 6.0, 7.2),
    ("baked_potato", 5.0, 6.0),
    ("bread", 5.0, 6.0),
    ("melon_slice", 2.0, 1.2),
    ("sweet_berries", 2.0, 0.4),
    ("dried_kelp", 1.0, 0.6),
    # Tier 3 — acceptable but lower quality
    ("carrot", 3.0, 3.6),
    ("potato", 1.0, 0.6),
    ("beetroot", 1.0, 1.2),
    ("apple", 4.0, 2.4),
    ("cookie", 2.0, 0.4),
    ("honey_bottle", 6.0, 1.2),
    # Tier 4 — raw / less effective
    ("raw_beef", 3.0, 1.8),
    ("raw_porkchop", 3.0, 1.8),
    ("raw_chicken", 2.0, 1.2),
    ("raw_mutton", 2.0, 1.2),
    ("raw_cod", 2.0, 0.4),
    ("raw_salmon", 2.0, 0.4),
    # Tier 5 — soups and special
    ("mushroom_stew", 6.0, 7.2),
    ("beetroot_soup", 6.0, 7.2),
    ("rabbit_stew", 10.0, 12.0),
    ("pumpkin_pie", 8.0, 4.8),
    ("suspicious_stew", 6.0, 7.2),  # may have status effects
    # Tier 6 — emergency (poisonous but fills hunger)
    ("spider_eye", 2.0, 3.2),
    ("poisonous_potato", 2.0, 1.2),
    ("pufferfish", 1.0, 0.2),
    ("chorus_fruit", 4.0, 2.4),
    # Tier 7 — cake (only fills 1 hunger per slice, so low priority)
    ("cake", 1.0, 0.1),
)

# Map from item_id to (hunger, saturation) for fast lookup
_FOOD_LOOKUP: dict[str, tuple[float, float]] = {
    item_id: (hunger, sat) for item_id, hunger, sat in _FOOD_ITEMS
}


def _is_food(item_id: str) -> bool:
    """Check whether an item ID is edible.

    Strips the ``minecraft:`` namespace before checking, so both
    ``"minecraft:bread"`` and ``"bread"`` are recognised.
    """
    bid = item_id.lower().replace("minecraft:", "")
    return bid in _FOOD_LOOKUP


def _food_value(item_id: str) -> tuple[float, float]:
    """Return (hunger_restored, saturation) for an edible item.

    Caller should check ``_is_food(item_id)`` first; this returns
    ``(0.0, 0.0)`` for non-food items.
    """
    return _FOOD_LOOKUP.get(item_id.lower().replace("minecraft:", ""), (0.0, 0.0))


_PASSABLE_BLOCKS: set[str] = {
    "air",
    "cave_air",
    "void_air",
    "grass",
    "tall_grass",
    "fern",
    "large_fern",
    "dead_bush",
    "water",
    "flowing_water",
    "lava",
    "flowing_lava",  # passable but hazardous!
    "snow",
    "vine",
    "torch",
    "wall_torch",
    "soul_torch",
    "redstone_torch",
    "redstone_wire",
    "repeater",
    "comparator",
    "lever",
    "button",
    "stone_button",
    "oak_button",
    "pressure_plate",
    "stone_pressure_plate",
    "oak_pressure_plate",
    "rail",
    "powered_rail",
    "detector_rail",
    "activator_rail",
    "red_mushroom",
    "brown_mushroom",
    "dandelion",
    "poppy",
    "blue_orchid",
    "oxeye_daisy",
    "cornflower",
    "lily_of_the_valley",
    "wheat",
    "carrots",
    "potatoes",
    "beetroots",
    "nether_wart",
    "cobweb",
    "ladder",
    "scaffolding",
}

_HAZARD_BLOCKS: set[str] = {
    "lava",
    "flowing_lava",
    "fire",
    "soul_fire",
    "cactus",
    "magma_block",
    "campfire",
    "soul_campfire",
    "wither_rose",
    "sweet_berry_bush",
    "powder_snow",
}

_STRUCTURE_BLOCKS: set[str] = {
    # Building materials (player-made)
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "jungle_planks",
    "acacia_planks",
    "dark_oak_planks",
    "crimson_planks",
    "warped_planks",
    "glass",
    "glass_pane",
    "white_stained_glass",
    "bricks",
    "stone_bricks",
    "cobblestone",
    # Infrastructure
    "rail",
    "powered_rail",
    "detector_rail",
    "activator_rail",
    "oak_door",
    "spruce_door",
    "birch_door",
    "iron_door",
    "oak_fence",
    "oak_fence_gate",
    # Functional
    "crafting_table",
    "furnace",
    "chest",
    "barrel",
    "shulker_box",
    "bed",
    "white_bed",
    "red_bed",
    "enchanting_table",
    "anvil",
    "grindstone",
    "campfire",
    "lantern",
    "soul_lantern",
}


def _is_passable(block_id: str) -> bool:
    """Check whether a block can be walked through."""
    bid = block_id.lower().replace("minecraft:", "")
    if bid in _PASSABLE_BLOCKS:
        return True
    # Any "air" variant is passable
    if bid.endswith("air"):
        return True
    # Transparent blocks that don't block movement
    passable_suffixes = (
        "_slab",
        "_stairs",
        "_door",
        "_trapdoor",
        "_fence",
        "_fence_gate",
        "_wall",
        "_sign",
        "sign",
        "_button",
        "_plate",
        "_carpet",
        "_glass",
        "_pane",
        "_sapling",
        "_seed",
        "seed",
        "_coral",
        "_kelp",
        "_seagrass",
    )
    return bid.endswith(passable_suffixes)


def _is_hazard(block_id: str) -> bool:
    """Check whether a block causes damage to the player."""
    bid = block_id.lower().replace("minecraft:", "")
    return bid in _HAZARD_BLOCKS or any(h in bid for h in ("lava", "fire"))


def _is_artificial(block_id: str) -> bool:
    """Check whether a block indicates player-made construction."""
    bid = block_id.lower().replace("minecraft:", "")
    return bid in _STRUCTURE_BLOCKS or any(
        s in bid
        for s in (
            "_planks",
            "_door",
            "_fence",
            "_glass",
            "_bed",
            "chest",
            "furnace",
            "anvil",
            "crafting_table",
            "enchanting_table",
        )
    )


async def _can_move_to(
    mc: WorldAdapter,
    x: int,
    y: int,
    z: int,
) -> tuple[bool, str]:
    """Check whether the player can safely occupy the target position.

    Returns (can_occupy, reason) where reason describes any blockage.
    """
    try:
        head = await mc.get_block(x, y + 1, z)
        feet = await mc.get_block(x, y, z)
        below = await mc.get_block(x, y - 1, z)

        if not _is_passable(head):
            return False, f"Head would be inside {head}"

        if not _is_passable(feet):
            return False, f"Feet would be inside {feet}"

        if _is_hazard(below):
            return False, f"Ground below is hazardous ({below})"

        if _is_hazard(head) or _is_hazard(feet):
            return False, f"Player would be inside hazard ({head or feet})"

        return True, "passable"
    except Exception as exc:
        return False, f"Collision check failed: {exc}"


async def _walk_toward(
    mc: WorldAdapter,
    target_x: float,
    target_z: float,
    step_size: float = 0.5,
    max_steps: int = 50,
) -> str:
    """Walk the player toward a target coordinate, using A* pathfinding
    to navigate around obstacles when available.

    For the first invocation the pathfinder is called; subsequent steps
    follow the pre-computed waypoint list.  If pathfinding isn't available
    or the path is short, falls back to straight-line movement with
    collision detection.

    Each step uses ``/execute as @p at @s run tp @s ^ ^ ^{step}`` for
    proper entity-context movement.

    Returns a summary string describing what happened.
    """
    pos = await mc.get_player_pos()
    if pos is None:
        return "Cannot walk — player position unknown"

    px, py, pz = pos[0], pos[1], pos[2]
    dx = target_x - px
    dz = target_z - pz
    distance = (dx * dx + dz * dz) ** 0.5

    if distance < step_size:
        # Already close enough — just face the target
        await mc.run_as_player(
            f"execute as @p at @s facing {target_x} {py} {target_z} run tp @s ~ ~ ~"
        )
        return f"Already at target ({distance:.1f}m)"

    # Try A* pathfinding for medium-to-long distances
    waypoints: list[tuple[float, float]] | None = None
    if distance > 5:
        try:
            from .pathfinding import find_walk_path

            found = await find_walk_path(mc, px, pz, target_x, target_z, int(py))
            if found and len(found) > 1:
                waypoints = found
                logger.info(
                    "Pathfinder returned %d waypoints for %.0fm route",
                    len(found),
                    distance,
                )
        except Exception as exc:
            logger.debug("Pathfinding failed — falling back to straight-line: %s", exc)

    if waypoints:
        # Pathfinding waypoint follow
        steps_taken = 0
        failed_at = ""
        for wx, wz in waypoints:
            if steps_taken >= max_steps:
                break
            if _safety_interrupted():
                failed_at = _INTERRUPTED_MESSAGE
                break
            # Face the waypoint
            await mc.run_as_player(f"execute as @p at @s facing {wx} {py} {wz} run tp @s ~ ~ ~")
            # Step forward
            await mc.run_as_player(f"execute as @p at @s run tp @s ^ ^ ^{step_size}")
            steps_taken += 1

            # Quick collision check every other step
            if steps_taken % 2 == 0:
                new_pos = await mc.get_player_pos()
                if new_pos:
                    ok, reason = await _can_move_to(
                        mc,
                        int(new_pos[0]),
                        int(new_pos[1]),
                        int(new_pos[2]),
                    )
                    if not ok:
                        failed_at = reason
                        break
                    # Hazard check
                    try:
                        below = await mc.get_block(
                            int(new_pos[0]),
                            int(new_pos[1]) - 1,
                            int(new_pos[2]),
                        )
                        if _is_hazard(below):
                            failed_at = f"hazard below ({below})"
                            break
                    except Exception:
                        pass

        if failed_at:
            return f"Pathfinding walked {steps_taken} waypoint steps before stopping: {failed_at}"
        return f"Pathfinding completed {steps_taken} waypoint steps ({distance:.1f}m)"

    # Fallback: straight-line walking with collision detection (original logic)
    steps_taken = 0
    failed_at = ""
    for step_n in range(min(max_steps, int(distance / step_size) + 1)):
        if _safety_interrupted():
            failed_at = _INTERRUPTED_MESSAGE
            break
        # Face the target
        await mc.run_as_player(
            f"execute as @p at @s facing {target_x} {py} {target_z} run tp @s ~ ~ ~"
        )
        # Take a small step forward (execute-based for entity context)
        await mc.run_as_player(f"execute as @p at @s run tp @s ^ ^ ^{step_size}")

        # Check if we've reached the target
        new_pos = await mc.get_player_pos()
        if new_pos is None:
            break
        new_dx = target_x - new_pos[0]
        new_dz = target_z - new_pos[2]
        new_dist = (new_dx * new_dx + new_dz * new_dz) ** 0.5

        steps_taken += 1

        if new_dist < step_size:
            break

        # Collision check every 2 steps
        if step_n % 2 == 0:
            ok, reason = await _can_move_to(
                mc,
                int(new_pos[0]),
                int(new_pos[1]),
                int(new_pos[2]),
            )
            if not ok:
                failed_at = reason
                break

        # Hazard check every 2 steps
        if step_n % 2 == 0:
            try:
                block_below = await mc.get_block(
                    int(new_pos[0]), int(new_pos[1]) - 1, int(new_pos[2])
                )
                if _is_hazard(block_below):
                    failed_at = f"hazard below ({block_below})"
                    break
            except Exception:
                pass

    if failed_at:
        return f"Walked {steps_taken} steps toward target before stopping: {failed_at}"
    return f"Walked {steps_taken} steps toward target ({distance:.1f}m)"


# ── Action execution ────────────────────────────────────────────────────

# Handlers receive a transport-independent WorldAdapter instead of the
# legacy RCONClient. MCPQ gives direct world-manipulation and
# player-control APIs — much richer than RCON commands alone.


async def execute_action(
    mc: WorldAdapter,
    action_type: ActionType,
    params: dict[str, Any] | None = None,
) -> ActionResult:
    """Dispatch an action to its handler and return the result.

    Parameters
    ----------
    mc : connected MCPQ client
    action_type : which action to perform
    params : action-specific parameters (see each handler)
    """
    params = params or {}
    handler = _HANDLERS.get(action_type)
    if handler is None:
        return ActionResult(
            success=False,
            action=action_type,
            message=f"No handler registered for {action_type.value}",
        )
    try:
        return await handler(mc, params)
    except Exception as exc:
        logger.exception("Action %s failed", action_type.value)
        return ActionResult(
            success=False,
            action=action_type,
            message=f"Error: {exc}",
        )


# ── Individual action handlers ─────────────────────────────────────────

Handler = Callable[[WorldAdapter, dict[str, Any]], Awaitable[ActionResult]]


async def _cmd(mc: WorldAdapter, cmd: str) -> str:
    """Run a command as the configured player and return stripped response.

    Substitutes ``@p`` with the actual player name so commands always
    target the bot, not a human player who happens to be nearest.
    """
    raw = await mc.run_as_player(cmd)
    return raw.strip()


_COMMAND_FAILURE_MARKERS = (
    "no entity was found",
    "source stack is empty",
    "unknown or incomplete command",
    "incorrect argument",
    "failed to",
    "couldn't",
    "could not",
)


def _command_failed(response: str) -> bool:
    normalized = response.casefold().strip()
    return any(marker in normalized for marker in _COMMAND_FAILURE_MARKERS)


# Set by ActionService while a preemptible strategic action runs; returns
# True once a safety reflex is waiting for the dispatch lock.
_SAFETY_INTERRUPT: ContextVar[Callable[[], bool] | None] = ContextVar(
    "safety_interrupt", default=None
)
_INTERRUPTED_MESSAGE = "interrupted by safety intervention"


@contextlib.contextmanager
def safety_interrupt_scope(check: Callable[[], bool]) -> Iterator[None]:
    """Let multi-step handlers stop between steps when ``check()`` is True."""
    token = _SAFETY_INTERRUPT.set(check)
    try:
        yield
    finally:
        _SAFETY_INTERRUPT.reset(token)


def _safety_interrupted() -> bool:
    check = _SAFETY_INTERRUPT.get()
    return check is not None and check()


def _interrupted_result(action: ActionType, steps_taken: int) -> ActionResult:
    return ActionResult(
        success=False,
        action=action,
        message=f"Stopped after {steps_taken} steps: {_INTERRUPTED_MESSAGE}",
        data={"steps_taken": steps_taken, "interrupted": True},
    )


_PLAYER_TARGET_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def _sanitize_player_target(target: object) -> str:
    """Return a command-safe single-player target, or raise `ValueError`.

    Accepts Minecraft-style usernames only: letters, digits, and
    underscores, at most 16 characters. Selectors (`@a`, `@e[...]`),
    quotes, whitespace, and command metacharacters are rejected so a
    target can never alter command structure when embedded in
    `execute as <target>` or `data get entity <target>` commands.
    """
    if not isinstance(target, str):
        raise ValueError("target must be a string")
    if not _PLAYER_TARGET_RE.fullmatch(target):
        raise ValueError(f"target {target!r} is not a valid Minecraft username")
    return target


_RESOURCE_ID_RE = re.compile(r"^(?:minecraft:)?[a-z0-9_]{1,64}$")


def _sanitize_resource_id(value: object, label: str) -> str:
    """Return a plain block/item ID, or raise `ValueError`.

    Only lowercase IDs with an optional `minecraft:` namespace are accepted.
    Block states, NBT data, whitespace, and any other syntax are rejected, so
    the value cannot change what a `setblock`, `give`, `clear`, or `summon`
    command does when embedded in it.
    """
    if not isinstance(value, str) or not _RESOURCE_ID_RE.fullmatch(value):
        raise ValueError(f"{label} {value!r} is not a plain Minecraft ID")
    return value.removeprefix("minecraft:")


def _finite_coordinate(value: object, label: str) -> float:
    """Return a finite coordinate, or raise `ValueError`.

    Accepts numbers and numeric strings. Anything else, including booleans,
    NaN, and infinity, is rejected so it cannot alter a `tp` command.
    """
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{label} {value!r} must be a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _sanitize_amount(value: object) -> int:
    """Return an item count from 1 to 64, or raise `ValueError`."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 64:
        raise ValueError(f"amount {value!r} must be an integer from 1 to 64")
    return value


# ── Movement ────────────────────────────────────────────────────────────


async def _move_to(mc: WorldAdapter, params: dict) -> ActionResult:
    try:
        x, y, z = (_finite_coordinate(params.get(axis, 0), axis) for axis in ("x", "y", "z"))
    except ValueError as exc:
        return ActionResult(success=False, action=ActionType.MOVE_TO, message=str(exc))
    await mc.teleport_player(x, y, z)
    return ActionResult(
        success=True,
        action=ActionType.MOVE_TO,
        message=f"Teleported to ({x}, {y}, {z})",
        data={"x": x, "y": y, "z": z},
    )


async def _move_forward(mc: WorldAdapter, params: dict) -> ActionResult:
    """Move forward in small steps with collision detection.

    Uses caret-relative teleport for small (0.5 block) steps, checking
    collision before each step.  Falls back to a direct ``/tp`` for
    larger distances.
    """
    steps = params.get("steps", 2)
    step_size = 0.5
    actual_steps = 0

    for _ in range(min(steps, 20)):
        if _safety_interrupted():
            return _interrupted_result(ActionType.MOVE_FORWARD, actual_steps)
        # Check where we'd be moving
        pos = await mc.get_player_pos()
        if pos is None:
            break

        target_x = int(pos[0])
        target_y = int(pos[1] + 0.5)
        target_z = int(pos[2])

        # Use caret-relative (^ ^ ^ moves in facing direction)
        # Check collision at the target position
        try:
            front_x = int(pos[0] + pos[2] * step_size)  # rough forward
            front_z = int(pos[2] + pos[0] * step_size)
        except Exception:
            front_x, front_z = target_x, target_z

        ok, reason = await _can_move_to(mc, front_x, target_y, front_z)
        if not ok:
            # Don't auto-step over hazards
            if "hazard" in reason.lower():
                return ActionResult(
                    success=False,
                    action=ActionType.MOVE_FORWARD,
                    message=f"Hazard detected after {actual_steps} steps: {reason}",
                    data={"steps_taken": actual_steps, "hazard": reason},
                )
            # Try one block up (auto-step over obstacles)
            ok_up, _ = await _can_move_to(mc, front_x, target_y + 1, front_z)
            if ok_up:
                await _cmd(mc, f"execute as @p at @s run tp @s ^ ^ ^{step_size}")
                await _cmd(mc, "tp @p ^ ^1 ^")  # step up
                actual_steps += 1
                continue
            return ActionResult(
                success=False,
                action=ActionType.MOVE_FORWARD,
                message=f"Blocked after {actual_steps} steps: {reason}",
                data={"steps_taken": actual_steps, "blocked_by": reason},
            )

        # Check hazard below
        try:
            below = await mc.get_block(front_x, target_y - 1, front_z)
            if _is_hazard(below):
                return ActionResult(
                    success=False,
                    action=ActionType.MOVE_FORWARD,
                    message=f"Hazard below ({below}) at {actual_steps} steps — not moving",
                    data={"steps_taken": actual_steps, "hazard": below},
                )
        except Exception:
            pass

        await _cmd(mc, f"execute as @p at @s run tp @s ^ ^ ^{step_size}")
        actual_steps += 1

    return ActionResult(
        success=(actual_steps > 0),
        action=ActionType.MOVE_FORWARD,
        message=f"Moved forward {actual_steps} step(s)",
        data={"steps": steps, "steps_taken": actual_steps},
    )


async def _move_back(mc: WorldAdapter, params: dict) -> ActionResult:
    """Move backward in small steps with collision detection."""
    steps = params.get("steps", 2)
    step_size = 0.5
    actual_steps = 0

    for _ in range(min(steps, 20)):
        if _safety_interrupted():
            return _interrupted_result(ActionType.MOVE_BACK, actual_steps)
        pos = await mc.get_player_pos()
        if pos is None:
            break

        target_x = int(pos[0])
        target_y = int(pos[1] + 0.5)
        target_z = int(pos[2])

        try:
            back_x = int(pos[0] - pos[2] * step_size)
            back_z = int(pos[2] - pos[0] * step_size)
        except Exception:
            back_x, back_z = target_x, target_z

        ok, reason = await _can_move_to(mc, back_x, target_y, back_z)
        if not ok:
            return ActionResult(
                success=False,
                action=ActionType.MOVE_BACK,
                message=f"Blocked after {actual_steps} steps: {reason}",
                data={"steps_taken": actual_steps, "blocked_by": reason},
            )

        try:
            below = await mc.get_block(back_x, target_y - 1, back_z)
            if _is_hazard(below):
                return ActionResult(
                    success=False,
                    action=ActionType.MOVE_BACK,
                    message=f"Hazard below ({below}) at {actual_steps} steps",
                    data={"steps_taken": actual_steps, "hazard": below},
                )
        except Exception:
            pass

        await _cmd(mc, f"execute as @p at @s run tp @s ^ ^ ^-{step_size}")
        actual_steps += 1

    return ActionResult(
        success=(actual_steps > 0),
        action=ActionType.MOVE_BACK,
        message=f"Moved back {actual_steps} step(s)",
        data={"steps": steps, "steps_taken": actual_steps},
    )


async def _sprint(mc: WorldAdapter, params: dict) -> ActionResult:
    """Sprint forward — faster forward movement with 1.0-block steps.

    Uses larger steps than ``move_forward``, ideal for open terrain.
    Collision detection runs every 3 steps to keep it fast.
    """
    steps = params.get("steps", 4)
    step_size = 1.0
    actual_steps = 0

    for _ in range(min(steps, 30)):
        if _safety_interrupted():
            return _interrupted_result(ActionType.SPRINT, actual_steps)
        pos = await mc.get_player_pos()
        if pos is None:
            break

        # Rough forward position
        try:
            front_x = int(pos[0] + pos[2] * step_size)
            front_z = int(pos[2] + pos[0] * step_size)
        except Exception:
            break

        target_y = int(pos[1] + 0.5)

        # Collision check every 3 steps
        if actual_steps % 3 == 0:
            ok, reason = await _can_move_to(mc, front_x, target_y, front_z)
            if not ok:
                return ActionResult(
                    success=(actual_steps > 0),
                    action=ActionType.SPRINT,
                    message=f"Sprinted {actual_steps} step(s) before stopping: {reason}",
                    data={"steps_taken": actual_steps, "blocked_by": reason},
                )

            # Quick hazard check
            try:
                below = await mc.get_block(front_x, target_y - 1, front_z)
                if _is_hazard(below):
                    return ActionResult(
                        success=(actual_steps > 0),
                        action=ActionType.SPRINT,
                        message=f"Sprinted {actual_steps} steps — hazard below ({below})",
                        data={"steps_taken": actual_steps, "hazard": below},
                    )
            except Exception:
                pass

        await _cmd(mc, f"execute as @p at @s run tp @s ^ ^ ^{step_size}")
        actual_steps += 1

    return ActionResult(
        success=(actual_steps > 0),
        action=ActionType.SPRINT,
        message=f"Sprinted {actual_steps} step(s)",
        data={"steps": steps, "steps_taken": actual_steps},
    )


async def _walk_to(mc: WorldAdapter, params: dict) -> ActionResult:
    """Walk to a target coordinate step-by-step with collision detection.

    For short-to-medium distances (up to ~50 blocks), this uses
    ``_walk_toward()`` with small steps and collision checks.
    For longer distances, it falls back to teleport.
    """
    x = params.get("x")
    z = params.get("z")
    if x is None or z is None:
        return ActionResult(
            success=False,
            action=ActionType.WALK_TO,
            message="walk_to requires 'x' and 'z' parameters",
        )
    y = params.get("y")

    pos = await mc.get_player_pos()
    if pos is None:
        return ActionResult(
            success=False,
            action=ActionType.WALK_TO,
            message="Cannot walk — player position unknown",
        )

    dx = float(x) - pos[0]
    dz = float(z) - pos[2]
    distance = (dx * dx + dz * dz) ** 0.5

    if distance > 50:
        # Long distance — teleport instead
        if y is not None:
            await mc.teleport_player(float(x), float(y), float(z))
        else:
            await mc.teleport_player(float(x), pos[1], float(z))
        return ActionResult(
            success=True,
            action=ActionType.WALK_TO,
            message=f"Distance {distance:.0f}m > 50, teleported to ({x}, {z})",
            data={"x": x, "z": z, "teleported": True},
        )

    result = await _walk_toward(mc, float(x), float(z))
    interrupted = _INTERRUPTED_MESSAGE in result
    new_pos = await mc.get_player_pos()
    new_dx = float(x) - (new_pos[0] if new_pos else pos[0])
    new_dz = float(z) - (new_pos[2] if new_pos else pos[2])
    remaining = (new_dx * new_dx + new_dz * new_dz) ** 0.5

    return ActionResult(
        success=(remaining < 3 and not interrupted),  # within 3 blocks = close enough
        action=ActionType.WALK_TO,
        message=result,
        data={
            "x": x,
            "z": z,
            "distance": distance,
            "remaining": remaining,
            "interrupted": interrupted,
        },
    )


async def _turn_left(mc: WorldAdapter, params: dict) -> ActionResult:
    """Turn left by 15 degrees (gradual rotation).

    The space after the third ``~`` is critical — without it Minecraft
    parses ``~-90`` as ``z = current_z - 90`` (teleport) instead of
    ``yaw = current_yaw - 15`` (rotate).
    """
    resp = await _cmd(mc, "tp @p ~ ~ ~ ~-15 ~")
    return ActionResult(
        success=True,
        action=ActionType.TURN_LEFT,
        message="Turned left 15°",
        data={"response": resp, "degrees": -15},
    )


async def _turn_right(mc: WorldAdapter, params: dict) -> ActionResult:
    """Turn right by 15 degrees (gradual rotation)."""
    resp = await _cmd(mc, "tp @p ~ ~ ~ ~15 ~")
    return ActionResult(
        success=True,
        action=ActionType.TURN_RIGHT,
        message="Turned right 15°",
        data={"response": resp, "degrees": 15},
    )


async def _jump(mc: WorldAdapter, params: dict) -> ActionResult:
    """Jump up one block (teleport-based)."""
    resp = await _cmd(mc, "tp @p ~ ~1 ~")
    return ActionResult(
        success=True,
        action=ActionType.JUMP,
        message="Jumped",
        data={"response": resp},
    )


async def _teleport(mc: WorldAdapter, params: dict) -> ActionResult:
    """Instant teleport to coordinates (bypasses collision)."""
    return await _move_to(mc, params)


async def _follow_teleport(mc: WorldAdapter, params: dict) -> ActionResult:
    """Teleport the bot alongside its `!follow` target.

    The orchestrator performs the distance check and passes only a
    target name; this handler validates it before embedding it in the
    command, keeping the follow-mode mutation inside the registered
    action space so policy can identify it.
    """
    try:
        target = _sanitize_player_target(params.get("target"))
    except ValueError as exc:
        return ActionResult(
            success=False,
            action=ActionType.FOLLOW_TELEPORT,
            message=f"Invalid follow target: {exc}",
            data={"target": params.get("target")},
        )
    resp = await _cmd(mc, f"execute as {target} at @s run tp @p ^-3 ^ ^-3")
    if _command_failed(resp):
        return ActionResult(
            success=False,
            action=ActionType.FOLLOW_TELEPORT,
            message=f"Could not follow {target}: {resp[:80]}",
            data={"target": target, "raw": resp},
        )
    return ActionResult(
        success=True,
        action=ActionType.FOLLOW_TELEPORT,
        message=f"Moved alongside {target}",
        data={"target": target, "raw": resp},
    )


async def _provision_bot(mc: WorldAdapter, params: dict) -> ActionResult:
    """Summon the bot entity via the MCPQ-Bot plugin's `/botsummon` command.

    The orchestrator is the only caller; this internal action exists so the
    trusted startup provisioning path is visible to the action policy and
    audit trace as a distinct action. The player name is validated as a
    strict Minecraft username before it is embedded in the command.
    """
    try:
        player_name = _sanitize_player_target(params.get("player_name"))
    except ValueError as exc:
        return ActionResult(
            success=False,
            action=ActionType.PROVISION_BOT,
            message=f"Invalid bot player name: {exc}",
            data={"player_name": params.get("player_name")},
        )
    resp = await mc.run_command_blocking(f"botsummon {player_name}")
    if _command_failed(resp):
        return ActionResult(
            success=False,
            action=ActionType.PROVISION_BOT,
            message=f"Could not summon bot {player_name}: {resp[:80]}",
            data={"player_name": player_name, "raw": resp},
        )
    return ActionResult(
        success=True,
        action=ActionType.PROVISION_BOT,
        message=f"Summoned bot {player_name}",
        data={"player_name": player_name, "raw": resp},
    )


async def _prepare_safe_spawn(mc: WorldAdapter, params: dict) -> ActionResult:
    """Teleport the bot to its startup spawn point and place a 3x3 platform.

    Internal startup-only action: see the ActionService docstring for why
    this provisioning mutation is exposed as a distinct typed action. The
    spawn point is fixed (0, 65, 0) — one block above the platform layer.
    """
    x = int(params.get("x", 0))
    y = int(params.get("y", 65))
    z = int(params.get("z", 0))
    await mc.teleport_player(float(x), float(y), float(z))
    placed = 0
    failures: list[dict[str, str]] = []
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            # Each write is attempted independently so one failed corner
            # cannot skip the rest of the platform (including the block
            # beneath the bot). Failures are recorded instead of swallowed
            # so the result reflects whether the support platform exists.
            bx, by, bz = x + dx, y - 1, z + dz
            try:
                await mc.set_block("dirt", bx, by, bz)
                placed += 1
            except Exception as exc:
                failures.append({"x": str(bx), "y": str(by), "z": str(bz), "error": str(exc)})
    if failures:
        return ActionResult(
            success=False,
            action=ActionType.PREPARE_SAFE_SPAWN,
            message=(
                f"Platform incomplete at ({x}, {y - 1}, {z}): "
                f"{len(failures)} of 9 support blocks failed to place"
            ),
            data={
                "x": x,
                "y": y,
                "z": z,
                "blocks_placed": placed,
                "blocks_failed": len(failures),
                "failed_blocks": failures,
            },
        )
    return ActionResult(
        success=True,
        action=ActionType.PREPARE_SAFE_SPAWN,
        message=f"Placed 3x3 platform at ({x}, {y - 1}, {z})",
        data={"x": x, "y": y, "z": z, "blocks_placed": placed, "blocks_failed": 0},
    )


# ── Interaction ─────────────────────────────────────────────────────────


async def _goto_teleport(mc: WorldAdapter, params: dict) -> ActionResult:
    """Teleport the bot to a named player (!goto chat command).

    Internal chat-command action: the target comes from untrusted chat
    text, so it is validated as a strict Minecraft username before it is
    interpolated into the teleport command. The built-in chat handler is
    the only caller and reports the returned failure result to the chat.
    """
    try:
        target = _sanitize_player_target(params.get("target"))
    except ValueError as exc:
        return ActionResult(
            success=False,
            action=ActionType.GOTO_TELEPORT,
            message=f"Invalid goto target: {exc}",
            data={"target": params.get("target")},
        )
    resp = await _cmd(mc, f"tp @p {target}")
    if _command_failed(resp):
        return ActionResult(
            success=False,
            action=ActionType.GOTO_TELEPORT,
            message=f"Could not teleport to {target}: {resp[:80]}",
            data={"target": target, "raw": resp},
        )
    return ActionResult(
        success=True,
        action=ActionType.GOTO_TELEPORT,
        message=f"Teleported to {target}",
        data={"target": target, "raw": resp},
    )


async def _come_teleport(mc: WorldAdapter, params: dict) -> ActionResult:
    """Teleport the chat sender to the bot (!come chat command).

    Internal chat-command action: the sender name is parsed from an
    untrusted chat line, so it is validated as a strict Minecraft
    username before it is interpolated into the teleport command. The
    built-in chat handler is the only caller and reports the returned
    failure result to the chat.
    """
    try:
        sender = _sanitize_player_target(params.get("sender"))
    except ValueError as exc:
        return ActionResult(
            success=False,
            action=ActionType.COME_TELEPORT,
            message=f"Invalid come sender: {exc}",
            data={"sender": params.get("sender")},
        )
    resp = await _cmd(mc, f"tp {sender} @p")
    if _command_failed(resp):
        return ActionResult(
            success=False,
            action=ActionType.COME_TELEPORT,
            message=f"Could not teleport {sender}: {resp[:80]}",
            data={"sender": sender, "raw": resp},
        )
    return ActionResult(
        success=True,
        action=ActionType.COME_TELEPORT,
        message=f"Teleported {sender} to the bot",
        data={"sender": sender, "raw": resp},
    )


async def _break_block(mc: WorldAdapter, params: dict) -> ActionResult:
    x = params.get("x")
    y = params.get("y")
    z = params.get("z")
    if x is not None and y is not None and z is not None:
        await mc.set_block("air", int(x), int(y), int(z))
        msg = f"Broke block at ({x}, {y}, {z})"
    else:
        # Break block player is looking at (relative ~ ~ ~1)
        resp = await _cmd(mc, "setblock ~ ~ ~1 air destroy")
        msg = "Attempted to break targeted block"
        if "Changed the block" not in resp:
            msg = f"Could not break targeted block: {resp[:80]}"
    return ActionResult(
        success=True,
        action=ActionType.BREAK_BLOCK,
        message=msg,
    )


async def _place_block(mc: WorldAdapter, params: dict) -> ActionResult:
    x = params.get("x")
    y = params.get("y")
    z = params.get("z")
    try:
        block_type = _sanitize_resource_id(params.get("block_type", "stone"), "block_type")
    except ValueError as exc:
        return ActionResult(success=False, action=ActionType.PLACE_BLOCK, message=str(exc))
    try:
        if x is not None and y is not None and z is not None:
            await mc.set_block(block_type, int(x), int(y), int(z))
            msg = f"Placed {block_type} at ({x}, {y}, {z})"
        else:
            # Place in front of player
            resp = await _cmd(mc, f"setblock ~ ~ ~1 {block_type}")
            msg = f"Placed {block_type} in front"
            if "Changed the block" not in resp and "block changed" not in resp:
                msg = f"Place result: {resp[:80]}"
        return ActionResult(success=True, action=ActionType.PLACE_BLOCK, message=msg)
    except Exception as exc:
        return ActionResult(success=False, action=ActionType.PLACE_BLOCK, message=str(exc))


async def _interact(mc: WorldAdapter, params: dict) -> ActionResult:
    # Use /execute as @p run interact — works on Paper
    resp = await _cmd(mc, "execute as @p at @p run interact")
    return ActionResult(
        success=True,
        action=ActionType.INTERACT,
        message="Interacted",
        data={"response": resp},
    )


# ── Inventory / Items ───────────────────────────────────────────────────


async def _check_inventory(mc: WorldAdapter, params: dict) -> ActionResult:
    resp = await _cmd(mc, "data get entity @p Inventory")
    success = not _command_failed(resp)
    return ActionResult(
        success=success,
        action=ActionType.CHECK_INVENTORY,
        message="Retrieved inventory" if success else f"Inventory read failed: {resp}",
        data={"raw_inventory": resp},
    )


async def _equip_item(mc: WorldAdapter, params: dict) -> ActionResult:
    slot = params.get("slot", 0)
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= 8:
        return ActionResult(
            success=False,
            action=ActionType.EQUIP_ITEM,
            message=f"Invalid hotbar slot {slot!r}; expected an integer from 0 to 8",
            data={"slot": slot},
        )
    resp = await _cmd(
        mc,
        f"item replace entity @p weapon.mainhand from entity @p hotbar.{slot}",
    )
    success = not _command_failed(resp)
    return ActionResult(
        success=success,
        action=ActionType.EQUIP_ITEM,
        message=(
            f"Equipped item from hotbar slot {slot}"
            if success
            else f"Could not equip item from hotbar slot {slot}: {resp}"
        ),
        data={"slot": slot, "response": resp},
    )


async def _equip_best_weapon(mc: WorldAdapter, params: dict) -> ActionResult:
    """Equip the strongest supported weapon already in the hotbar."""
    from .observer import _parse_inventory_nbt

    inventory_result = await _check_inventory(mc, {})
    if not inventory_result.success:
        return ActionResult(
            success=False,
            action=ActionType.EQUIP_BEST_WEAPON,
            message=f"Could not read inventory: {inventory_result.message}",
            data={"available_weapon": False},
        )
    items = _parse_inventory_nbt(inventory_result.data.get("raw_inventory", ""))
    weapon = select_best_weapon(items)
    if weapon is None:
        return ActionResult(
            success=False,
            action=ActionType.EQUIP_BEST_WEAPON,
            message="No supported weapon found in the hotbar.",
            data={"available_weapon": False},
        )

    selected_slot = (await mc.get_player_info()).get("selected_item_slot", 0)
    selected_slot = int(selected_slot) if isinstance(selected_slot, int) else 0
    if not 0 <= selected_slot <= 8:
        selected_slot = 0

    if weapon.slot == selected_slot:
        return ActionResult(
            success=True,
            action=ActionType.EQUIP_BEST_WEAPON,
            message=f"Best weapon already equipped: {weapon.item_id}",
            data={
                "available_weapon": True,
                "already_equipped": True,
                "item_id": weapon.item_id,
                "slot": weapon.slot,
            },
        )

    equip_result = await _equip_item(mc, {"slot": weapon.slot})
    if not equip_result.success:
        return ActionResult(
            success=False,
            action=ActionType.EQUIP_BEST_WEAPON,
            message=f"Could not equip best weapon: {equip_result.message}",
            data={
                "available_weapon": True,
                "already_equipped": False,
                "item_id": weapon.item_id,
                "slot": weapon.slot,
                "response": equip_result.data.get("response", ""),
            },
        )

    return ActionResult(
        success=True,
        action=ActionType.EQUIP_BEST_WEAPON,
        message=f"Equipped best weapon: {weapon.item_id} from hotbar slot {weapon.slot}",
        data={
            "available_weapon": True,
            "already_equipped": False,
            "item_id": weapon.item_id,
            "slot": weapon.slot,
            "response": equip_result.data.get("response", ""),
        },
    )


async def _craft_item(mc: WorldAdapter, params: dict) -> ActionResult:
    """Give items to the player (creative-mode /give).

    NOTE: This uses /give under the hood, which requires OP permissions or
    creative mode.  In survival this will fail — a proper survival crafting
    system (recipe matching + crafting table interaction) is tracked as a
    planned feature (see docs/features/).
    """
    try:
        item = _sanitize_resource_id(params.get("item_type", "crafting_table"), "item_type")
        amount = _sanitize_amount(params.get("amount", 1))
    except ValueError as exc:
        return ActionResult(success=False, action=ActionType.CRAFT_ITEM, message=str(exc))
    try:
        resp = await _cmd(mc, f"give @p {item} {amount}")
        return ActionResult(
            success=True,
            action=ActionType.CRAFT_ITEM,
            message=f"Gave {amount}x {item}",
            data={"item": item, "amount": amount, "response": resp},
        )
    except Exception as exc:
        return ActionResult(
            success=False,
            action=ActionType.CRAFT_ITEM,
            message=f"Could not give {item}: {exc}. May need OP/creative mode.",
        )


async def _drop_item(mc: WorldAdapter, params: dict) -> ActionResult:
    """Drop items from inventory as entities in the world.

    Uses /replaceitem to clear the slot and /summon to create the item
    entity at the player's position.  This is a best-effort simulation;
    a proper drop system should use MCPQ's direct player-inventory API.
    """
    try:
        item = _sanitize_resource_id(params.get("item_type", "stone"), "item_type")
        amount = _sanitize_amount(params.get("amount", 1))
    except ValueError as exc:
        return ActionResult(success=False, action=ActionType.DROP_ITEM, message=str(exc))
    try:
        # Get player position to spawn item drop there
        pos = await mc.get_player_pos()
        if pos:
            # Find the item in inventory and remove it, then spawn as entity
            await _cmd(mc, f"clear @p {item} {amount}")
            spawn_cmd = (
                f"summon item {pos[0]:.1f} {pos[1]:.1f} {pos[2]:.1f} "
                f'{{Item:{{id:"minecraft:{item}",Count:{amount}b}}}}'
            )
            await _cmd(mc, spawn_cmd)
            return ActionResult(
                success=True,
                action=ActionType.DROP_ITEM,
                message=f"Dropped {amount}x {item} at current position",
                data={"item": item, "amount": amount},
            )
        else:
            # Fallback: just clear from inventory
            resp = await _cmd(mc, f"clear @p {item} {amount}")
            return ActionResult(
                success=False,
                action=ActionType.DROP_ITEM,
                message=(
                    f"Position unknown — removed {amount}x {item} from inventory "
                    f"but could not spawn drop entity"
                ),
                data={"item": item, "amount": amount, "response": resp},
            )
    except Exception as exc:
        return ActionResult(
            success=False,
            action=ActionType.DROP_ITEM,
            message=f"Drop failed: {exc}",
        )


# ── Eating ─────────────────────────────────────────────────────────────


async def _eat(mc: WorldAdapter, params: dict) -> ActionResult:
    """Eat a food item from the player's inventory.

    Parameters (from ``params``):
      - ``food_item`` (required): the item ID to eat (e.g. ``"bread"``).
        The ``minecraft:`` namespace is optional.
      - ``slot`` (optional): the inventory slot number to eat from. If
        omitted, scans inventory for the item.

    Mechanics:
      1. Equip the food item to the main hand.
      2. Apply a short saturation effect via ``/effect give @p
         saturation <duration> <level>`` so the player immediately
         gains hunger back even if no client is consuming the held
         item.  In a real client this would happen via right-click;
         for the headless bridge we just restore the hunger.
      3. Decrement the item count from the inventory.

    Returns ActionResult with structured ``data``:
      - ``food_item``: the item that was eaten
      - ``hunger_restored``: integer hunger gained
      - ``saturation_points``: saturation effect duration × level
    """
    food_item = str(params.get("food_item", "")).strip()
    if not food_item:
        return ActionResult(
            success=False,
            action=ActionType.EAT,
            message="EAT requires a 'food_item' parameter (e.g. food_item='bread')",
        )

    # Normalise the item ID (strip the namespace for lookups)
    bare = food_item.lower().replace("minecraft:", "")
    if not _is_food(bare):
        return ActionResult(
            success=False,
            action=ActionType.EAT,
            message=f"'{food_item}' is not a known edible item",
        )

    hunger_restored, saturation = _food_value(bare)

    if not await _consume_one_item(mc, bare):
        return ActionResult(
            success=False,
            action=ActionType.EAT,
            message=f"Could not verify consuming one {bare}; no hunger effect was applied.",
            data={"food_item": bare, "hunger_restored": 0, "consumed": False},
        )

    # Apply saturation only after the server confirms item consumption. The
    # duration is 1 second
    # (configurable); the level is rounded up to match the food's
    # saturation value.  This is a headless-friendly approximation:
    # in a real client the player would consume the held item via
    # right-click and the vanilla hunger/saturation system would
    # take over.
    sat_level = max(1, int(saturation / 2 + 0.5))  # 1 level = 2 saturation
    try:
        effect_result = await _cmd(mc, f"effect give @p saturation 1 {sat_level}")
    except Exception as exc:
        return ActionResult(
            success=False,
            action=ActionType.EAT,
            message=f"Food was consumed, but the saturation effect failed: {exc}",
            data={"food_item": bare, "hunger_restored": 0, "consumed": True},
        )
    if _command_failed(effect_result):
        return ActionResult(
            success=False,
            action=ActionType.EAT,
            message="Food was consumed, but the saturation effect failed.",
            data={"food_item": bare, "hunger_restored": 0, "consumed": True},
        )

    return ActionResult(
        success=True,
        action=ActionType.EAT,
        message=f"Ate 1x {bare} (restored {int(hunger_restored)} hunger)",
        data={
            "food_item": bare,
            "hunger_restored": int(hunger_restored),
            "saturation_points": saturation,
            "consumed": True,
        },
    )


async def _consume_one_item(mc: WorldAdapter, item: str) -> bool:
    """Consume one item only when the server confirms the inventory change."""
    response = await _cmd(mc, f"clear @p minecraft:{item} 1")
    return bool(
        response
        and not _command_failed(response)
        and re.search(r"\b(?:removed|cleared)\s+1\b", response, flags=re.IGNORECASE)
    )


# ── Healing ─────────────────────────────────────────────────────────────


async def _heal(mc: WorldAdapter, params: dict) -> ActionResult:
    """Apply healing effects to restore health.

    Uses ``/effect give @p regeneration`` and optionally
    ``/effect give @p instant_health`` to restore health without
    requiring a real client to consume healing items.  The action
    always applies regeneration for 30s at level 2 (regenerates
    ~10 HP).  If ``heal_item`` is ``"golden_apple"`` or
    ``"enchanted_golden_apple"``, it also applies absorption
    and instant health effects.

    Parameters (from ``params``):
      - ``heal_item`` (optional): the item ID to 'simulate'
        consuming (e.g. ``"golden_apple"``).  If provided and
        is a healing item, the corresponding effects are applied
        and the item is cleared from inventory.

    Returns ActionResult with structured ``data``:
      - ``heal_item``: the item that was used (or ``"generic"``)
      - ``heal_effects``: list of effect names applied
    """
    heal_item = str(params.get("heal_item", "")).strip().lower().replace("minecraft:", "")
    if heal_item and heal_item not in {"golden_apple", "enchanted_golden_apple"}:
        return ActionResult(
            success=False,
            action=ActionType.HEAL,
            message="HEAL accepts only golden_apple or enchanted_golden_apple.",
            data={"heal_item": heal_item, "heal_effects": [], "consumed": False},
        )
    if heal_item and not await _consume_one_item(mc, heal_item):
        return ActionResult(
            success=False,
            action=ActionType.HEAL,
            message=f"Could not verify consuming one {heal_item}; no healing effect was applied.",
            data={"heal_item": heal_item, "heal_effects": [], "consumed": False},
        )
    effects_applied: list[str] = []
    is_gapple = heal_item in ("golden_apple", "enchanted_golden_apple")

    # Always apply regeneration
    try:
        regeneration_result = await _cmd(mc, "effect give @p regeneration 30 2")
    except Exception as exc:
        return ActionResult(
            success=False,
            action=ActionType.HEAL,
            message=f"Healing item was consumed, but regeneration failed: {exc}",
            data={
                "heal_item": heal_item or "generic",
                "heal_effects": [],
                "consumed": bool(heal_item),
            },
        )
    if _command_failed(regeneration_result):
        return ActionResult(
            success=False,
            action=ActionType.HEAL,
            message="Healing item was consumed, but regeneration failed.",
            data={
                "heal_item": heal_item or "generic",
                "heal_effects": [],
                "consumed": bool(heal_item),
            },
        )
    effects_applied.append("regeneration II (30s)")
    logger.debug("Applied regeneration effect")

    # Golden apple bonus effects
    if is_gapple:
        with contextlib.suppress(Exception):
            await _cmd(mc, "effect give @p absorption 120 1")
            effects_applied.append("absorption I (120s)")
        with contextlib.suppress(Exception):
            await _cmd(mc, "effect give @p instant_health 1 2")
            effects_applied.append("instant_health II")
        if heal_item == "enchanted_golden_apple":
            with contextlib.suppress(Exception):
                await _cmd(mc, "effect give @p fire_resistance 600 0")
                effects_applied.append("fire_resistance (600s)")
            with contextlib.suppress(Exception):
                await _cmd(mc, "effect give @p resistance 600 0")
                effects_applied.append("resistance (600s)")

    return ActionResult(
        success=True,
        action=ActionType.HEAL,
        message=(f"Healed with {heal_item or 'generic effects'}: {', '.join(effects_applied)}"),
        data={
            "heal_item": heal_item or "generic",
            "heal_effects": effects_applied,
            "consumed": bool(heal_item),
        },
    )


# ── Combat ──────────────────────────────────────────────────────────────


def _damage_hit_anything(resp: str | None) -> bool:
    """Check whether a /damage command response indicates an entity was hit.

    /damage returns a message like 'Damaged 1 entity' or 'No entity was
    found'.  We also check for '0 entities' as a safety net.
    """
    if not resp:
        return False
    lower = resp.lower().strip()
    if "no entity" in lower:
        return False
    if "0 entit" in lower:
        return False
    if "damaged" in lower or "hurt" in lower or "damage" in lower:
        return True
    # If none of the above patterns match, assume it worked (the damage
    # succeeded but the output format might differ).
    return True


# ── Hostile mob types the scan_entities action can detect ────────────────
# Curated subset of common hostile mobs. Extending this list is safe —
# each entry is one extra /execute command. The cost is linear in the
# number of types times radius, so keep this under ~30 entries.
_HOSTILE_MOBS: tuple[str, ...] = (
    "zombie",
    "skeleton",
    "creeper",
    "spider",
    "cave_spider",
    "enderman",
    "witch",
    "slime",
    "magma_cube",
    "phantom",
    "drowned",
    "husk",
    "stray",
    "bogged",
    "breeze",
    "piglin",
    "piglin_brute",
    "hoglin",
    "zoglin",
    "zombified_piglin",
    "guardian",
    "elder_guardian",
    "shulker",
    "evoker",
    "vindicator",
    "pillager",
    "ravager",
    "vex",
)


# ── Mob threat levels ────────────────────────────────────────────────────
# Used by scan_entities to communicate danger to the LLM and by the
# self-preservation layer to decide which mobs to engage.  Higher
# threat = more dangerous, more reason to fight back or flee.
#
# Levels:
#   "low"      — slow, weak mobs. Safe to engage solo.
#   "medium"   — moderate threat. Common hostile mobs.
#   "high"     — dangerous. Ranged, explosive, or strong melee.
#   "critical" — run, don't fight (e.g. warden, wither).
#
# Mobs not in this table default to "medium" if they're in
# _HOSTILE_MOBS, or "neutral" if they are not.
_MOB_THREAT_LEVELS: dict[str, str] = {
    # Low
    "zombie": "low",
    "husk": "low",
    "drowned": "low",
    "stray": "low",
    "bogged": "low",
    "slime": "low",
    "magma_cube": "low",
    "zombified_piglin": "low",
    # Medium
    "skeleton": "medium",
    "spider": "medium",
    "cave_spider": "medium",
    "phantom": "medium",
    "breeze": "medium",
    "hoglin": "medium",
    # High
    "creeper": "high",
    "witch": "high",
    "enderman": "high",
    "piglin": "high",
    "piglin_brute": "high",
    "zoglin": "high",
    "vindicator": "high",
    "evoker": "high",
    "ravager": "high",
    "shulker": "high",
    "guardian": "high",
    "elder_guardian": "high",
    "vex": "high",
    "wither_skeleton": "high",
    "blaze": "high",
    # Critical — flee
    "warden": "critical",
    "wither": "critical",
}


# ── Mob blacklist ────────────────────────────────────────────────────────
# Mobs that the agent should never attack, even if they appear to be
# hostile.  Most of these are village protectors, tamed animals, or
# peaceful creatures.  Attacking them in a village would either
# turn the entire village hostile (iron golems) or just be wrong
# (villagers, tamed wolves, etc.).
#
# The mob_type is the lowercase bare name (no "minecraft:" prefix).
_MOB_BLACKLIST: frozenset[str] = frozenset(
    {
        # Village protectors
        "iron_golem",
        "villager",
        "wandering_trader",
        # Tamed/friendly animals (wild versions of some are hostile, but
        # we can't tell from the entity name alone)
        "wolf",  # tamed wolves are friendly
        "cat",
        "parrot",
        "horse",
        "donkey",
        "mule",
        "llama",
        "trader_llama",
        "ocelot",
        "axolotl",
        "fox",
        "bee",  # only hostile if provoked
        "rabbit",
        "squid",
        "glow_squid",
        "dolphin",
        "turtle",
        "frog",
        "goat",
        "strider",
        "armadillo",
        # Passive farm animals — would be wrong to attack
        "pig",
        "cow",
        "sheep",
        "chicken",
        "mooshroom",
    }
)


# Keep the scan bounded: one selector per curated hostile type plus the two
# critical threats that are intentionally excluded from the hostile list.
# Threat and blacklist helpers still classify any type that is detected.
_SCAN_ENTITY_TYPES: tuple[str, ...] = tuple(dict.fromkeys((*_HOSTILE_MOBS, "warden", "wither")))


def _get_threat_level(mob_type: str) -> str:
    """Return the threat level for a mob type.

    Strips the ``minecraft:`` namespace.  Falls back to ``"medium"``
    for unknown hostile mobs and ``"neutral"`` for unknown
    non-hostile mobs.
    """
    bare = mob_type.lower().replace("minecraft:", "")
    return _MOB_THREAT_LEVELS.get(bare, "medium" if bare in _HOSTILE_MOBS else "neutral")


def _is_blacklisted(mob_type: str) -> bool:
    """Return True if the agent should never attack this mob type."""
    bare = mob_type.lower().replace("minecraft:", "")
    return bare in _MOB_BLACKLIST


def _should_attack(mob_type: str) -> bool:
    """Decide whether the agent should attack a given mob type.

    Combines threat level (don't attack critical — too dangerous
    to even engage) with blacklist (don't attack iron golems,
    villagers, tamed animals, etc.).  Returns False for blacklisted
    or critical mobs, True otherwise.
    """
    if _is_blacklisted(mob_type):
        return False
    return _get_threat_level(mob_type) != "critical"


async def _scan_entities(mc: WorldAdapter, params: dict) -> ActionResult:
    """Detect hostile mobs near the player.

    Iterates over a curated list of common hostile mob types and runs
    ``/execute if entity @e[type=...minecraft:X,r=R,limit=1]`` for each
    to check whether at least one of that type is within radius.

    Parameters (from ``params``):
      - ``radius`` (optional, default 16, cap 16): search radius in blocks

    Returns ActionResult with structured ``data`` for the LLM:
      - ``radius``: the effective search radius
      - ``mobs``: dict mapping mob type name -> bool (True if present)
      - ``mobs_nearby``: list of mob type names that were detected
        (convenient for the LLM prompt)
      - ``detailed``: list of dicts with ``{type, threat, should_attack}``
        for each detected mob.  ``threat`` is one of ``low``,
        ``medium``, ``high``, ``critical`` (or ``neutral`` for
        unknown mobs).  ``should_attack`` is False for blacklisted
        scanned types and critical mobs (warden, wither) — the agent
        should never engage them.
      - ``too_dangerous``: list of mob types in range that are too
        dangerous to engage (warden, wither).  Even though the
        ``should_attack`` flag is False, the agent needs to know
        these are present so it can decide to flee.
    """
    try:
        radius = int(params.get("radius", 16))
    except (TypeError, ValueError):
        radius = 16
    radius = max(1, min(radius, 16))  # cap to avoid spamming commands

    detected: list[str] = []
    presence: dict[str, bool] = {}
    # Rich per-mob info: type, threat level, should_attack boolean.
    # Backward compatible: existing code reads `mobs_nearby` (a list
    # of strings).  New code can read `detailed` for the richer view.
    detailed: list[dict[str, str | bool]] = []
    # Mob types that are too dangerous to engage (e.g. warden).
    too_dangerous: list[str] = []

    for mob in _SCAN_ENTITY_TYPES:
        try:
            marker = f"__mob_{mob}__"
            cmd = (
                f"execute if entity @e[type=minecraft:{mob},"
                f"distance=..{radius},limit=1] run say {marker}"
            )
            resp = await _cmd(mc, cmd)
            present = bool(resp) and marker in resp
        except Exception:
            present = False
        presence[mob] = present
        if present:
            detected.append(mob)
            threat = _get_threat_level(mob)
            should_attack = _should_attack(mob)
            detailed.append(
                {
                    "type": mob,
                    "threat": threat,
                    "should_attack": should_attack,
                }
            )

            if not should_attack and not _is_blacklisted(mob):
                too_dangerous.append(mob)
    msg = (
        f"No hostile mobs within {radius} blocks"
        if not detected
        else f"Detected hostile mobs within {radius} blocks: {', '.join(detected)}"
    )
    return ActionResult(
        success=True,
        action=ActionType.SCAN_ENTITIES,
        message=msg,
        data={
            "radius": radius,
            "mobs": presence,
            "mobs_nearby": detected,
            "detailed": detailed,
            "too_dangerous": too_dangerous,
        },
    )


async def _attack(mc: WorldAdapter, params: dict) -> ActionResult:
    """Attack a target entity using ``/damage``.

    Paper 26.x broke ``execute as @p at @p run attack`` (throws
    CommandException), so this action uses the ``/damage`` command
    instead, which has been available since MC 1.20.5.

    Parameters (from ``params``):
      - ``entity_type`` (optional): curated Minecraft mob type to target
        specifically. Player names and protected/unknown entity types fail
        closed. If omitted, tries nearby curated attackable mob types.
      - ``damage_amount`` (optional): how much damage to deal, integer
        1-20. Default 4 (= 2 hearts). 20 kills most mobs in one hit.

    Returns ActionResult with structured ``data`` for the LLM:
      - ``target_type``: "player" | "mob" | "unknown"
      - ``target_name``: the entity_type that was attacked (if any)
      - ``damage_dealt``: the integer damage amount
      - ``target_hit``: bool — whether the /damage actually hit
    """
    # Clamp damage to a sane range. /damage accepts any positive float,
    # but Minecraft's max health caps at 20 (10 hearts) for players and
    # varies for mobs. 64+ is enough to one-shot everything that matters.
    raw_target = params.get("entity_type", "")
    target = raw_target.strip().lower() if isinstance(raw_target, str) else ""
    # Target only curated bare Minecraft mob types; never place user text in
    # selector grammar or a raw command argument.
    target_type = target.removeprefix("minecraft:")
    valid_target = (
        (target == target_type or target == f"minecraft:{target_type}")
        and target_type in _SCAN_ENTITY_TYPES
        and _should_attack(target_type)
    )
    try:
        damage = int(params.get("damage_amount", 4))
    except (TypeError, ValueError):
        damage = 4
    damage = max(1, min(damage, 64))

    if target and not valid_target:
        return ActionResult(
            success=False,
            action=ActionType.ATTACK,
            message="Attack failed — target must be an attackable mob type.",
            data={"target_name": target, "damage_dealt": damage, "target_hit": False},
        )

    if valid_target:
        # Target a specific entity with generic damage
        try:
            cmd = (
                "execute as @p at @s run damage "
                f"@e[type=minecraft:{target_type},distance=..6,limit=1,sort=nearest] {damage}"
            )
            resp = await _cmd(mc, cmd)
            if _damage_hit_anything(resp):
                return ActionResult(
                    success=True,
                    action=ActionType.ATTACK,
                    message=f"Attacked {target} for {damage} damage",
                    data={
                        "target_type": "mob",
                        "target_name": target,
                        "damage_dealt": damage,
                        "target_hit": True,
                        "response": resp,
                    },
                )
        except Exception:
            pass

    # A broad non-player selector could still hit villagers, pets, or
    # protectors, so generic targeting is limited to curated attackable mobs.
    candidates = (
        (target_type,)
        if valid_target
        else tuple(mob for mob in _SCAN_ENTITY_TYPES if _should_attack(mob))
    )
    for mob in candidates:
        try:
            cmd = (
                "execute as @p at @s run damage "
                f"@e[type=minecraft:{mob},distance=..6,limit=1,sort=nearest] {damage}"
            )
            resp = await _cmd(mc, cmd)
            if _damage_hit_anything(resp):
                return ActionResult(
                    success=True,
                    action=ActionType.ATTACK,
                    message=f"Attacked nearest {mob} for {damage} damage",
                    data={
                        "target_type": "mob",
                        "target_name": mob,
                        "damage_dealt": damage,
                        "target_hit": True,
                        "response": resp,
                    },
                )
        except Exception:
            continue

    # Nothing in range hit
    return ActionResult(
        success=False,
        action=ActionType.ATTACK,
        message=(
            "Attack failed — no entity in range. "
            "Try scan_entities to find targets, then attack with entity_type."
        ),
        data={
            "target_name": target or None,
            "damage_dealt": damage,
            "target_hit": False,
        },
    )


# ── Information ─────────────────────────────────────────────────────────


async def _scan(mc: WorldAdapter, params: dict) -> ActionResult:
    radius = min(params.get("radius", 5), 16)  # cap at 16 to avoid spam

    # Get player position first
    pos = await mc.get_player_pos()
    data: dict[str, Any] = {"radius": radius}

    if pos:
        px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
        data["position"] = [px, py, pz]

        # Scan blocks around the player (3×3 at feet level, plus above/below)
        nearby: dict[str, str] = {}
        try:
            # Key reference blocks
            nearby["block_feet"] = await mc.get_block(px, py - 1, pz)
            nearby["block_head"] = await mc.get_block(px, py + 1, pz)

            # Front/back/left/right at feet level
            nearby["north"] = await mc.get_block(px, py - 1, pz - 1)
            nearby["south"] = await mc.get_block(px, py - 1, pz + 1)
            nearby["east"] = await mc.get_block(px + 1, py - 1, pz)
            nearby["west"] = await mc.get_block(px - 1, py - 1, pz)

            # What's directly in front at eye level
            nearby["front_eye"] = await mc.get_block(px, py, pz + 1)
        except Exception:
            pass

        data["nearby"] = nearby

        # Scan visible blocks in the radius (sample at cardinal points)
        if radius >= 3:
            sample: dict[str, str] = {}
            try:
                for dx, dz in [
                    (0, 2),
                    (2, 0),
                    (0, -2),
                    (-2, 0),
                    (2, 2),
                    (2, -2),
                    (-2, 2),
                    (-2, -2),
                ]:
                    key = f"d{dx:+}z{dz:+}"
                    sample[key] = await mc.get_block(px + dx, py - 1, pz + dz)
            except Exception:
                pass
            data["sample"] = sample

        # Note: biome detection omitted — the /locate biome command
        # produces noisy error output that clutters the LLM prompt.
    else:
        data["note"] = "Player position unavailable"

    # Gather time / weather / players
    try:
        data["time"] = await mc.get_time()
        data["players"] = await mc.get_players_online()
    except Exception:
        pass

    return ActionResult(
        success=True,
        action=ActionType.SCAN,
        message=f"Scanned surroundings (r={radius})",
        data=data,
    )


async def _check_time(mc: WorldAdapter, params: dict) -> ActionResult:
    resp = await mc.get_time()
    return ActionResult(
        success=True,
        action=ActionType.CHECK_TIME,
        message="Time checked",
        data={"time_raw": resp},
    )


async def _check_weather(mc: WorldAdapter, params: dict) -> ActionResult:
    resp = await _cmd(mc, "weather query")
    return ActionResult(
        success=True,
        action=ActionType.CHECK_WEATHER,
        message="Weather checked",
        data={"weather_raw": resp},
    )


_HEALTH_RESULT_RE = re.compile(
    r"\bhealth\s*:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:[eE][+-]?\d+)?[bBsSlLfFdD]?)",
    re.IGNORECASE,
)
_NUMERIC_RESULT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[bBsSlLfFdD]?$")


def _parse_health_response(raw: str | None) -> float | None:
    """Parse current health, never interpreting max-health output as current."""
    text = str(raw or "").strip()
    match = _HEALTH_RESULT_RE.search(text)
    token = match.group(1) if match else text
    if not _NUMERIC_RESULT_RE.fullmatch(token):
        return None
    if token[-1].isalpha():
        token = token[:-1]
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


async def _check_health(mc: WorldAdapter, params: dict) -> ActionResult:
    """Check the player's health.

    Uses ``/data get entity @p Health`` first, which works for real players.
    For fake players that may not have a Health attribute, falls back to
    ``get_player_info()`` which reads NBT directly via MCPQ's gRPC API,
    then to ``/attribute`` queries.
    """
    resp = await _cmd(mc, "data get entity @p Health")
    result_data: dict[str, Any] = {"health_raw": resp}

    parsed = _parse_health_response(resp)
    if parsed is not None:
        return ActionResult(
            success=True,
            action=ActionType.CHECK_HEALTH,
            message=f"Health: {parsed}",
            data=result_data,
        )

    # Fallback 1: try MCPQ get_player_info() which reads NBT attributes
    try:
        info = await mc.get_player_info()
        nbt_health = info.get("health")
        nbt_value = _parse_health_response(str(nbt_health))
        if nbt_value is not None:
            result_data["health_raw"] = str(nbt_health)
            result_data["health_source"] = "mcpq_nbt"
            return ActionResult(
                success=True,
                action=ActionType.CHECK_HEALTH,
                message=f"Health: {nbt_value}",
                data=result_data,
            )
    except Exception:
        pass

    # Fallback 2: report max health separately; it is not current health.
    try:
        attr_resp = await _cmd(mc, "attribute @p minecraft:generic.max_health get")
        if attr_resp and "Has no attribute" not in attr_resp:
            result_data["health_raw"] = ""
            result_data["max_health_raw"] = attr_resp
            result_data["health_source"] = "attribute_max"
            return ActionResult(
                success=True,
                action=ActionType.CHECK_HEALTH,
                message=f"Current health unavailable; max health: {attr_resp}",
                data=result_data,
            )
    except Exception:
        pass

    # No current-health source succeeded; do not guess a value.
    result_data["health_raw"] = ""
    result_data["health_source"] = "unknown"
    return ActionResult(
        success=True,
        action=ActionType.CHECK_HEALTH,
        message="Current health unavailable",
        data=result_data,
    )


async def _check_hunger(mc: WorldAdapter, params: dict) -> ActionResult:
    """Check the player's hunger (foodLevel, 0-20).

    Uses ``/data get entity @p foodLevel`` first. Falls back to
    ``get_player_info()`` which reads NBT directly via MCPQ's gRPC API.
    The food level is an integer 0-20 (same scale as health) where 20
    is fully satiated and 0 is starving.
    """
    resp = await _cmd(mc, "data get entity @p foodLevel")
    result_data: dict[str, Any] = {"hunger_raw": resp}

    parsed: int | None = None
    try:
        import re as _re

        m = _re.search(r"(-?\d+)", resp or "")
        if m:
            # foodLevel is stored as an integer (no float suffix in the data)
            parsed = int(m.group(1))
            # Clamp to valid range — values outside [0, 20] are parse errors
            if not 0 <= parsed <= 20:
                parsed = None
    except (ValueError, TypeError):
        pass

    if parsed is not None:
        return ActionResult(
            success=True,
            action=ActionType.CHECK_HUNGER,
            message=f"Hunger: {parsed}/20",
            data=result_data,
        )

    # Fallback: try MCPQ get_player_info() which reads NBT foodLevel
    try:
        info = await mc.get_player_info()
        food = info.get("food")
        if food is not None:
            food_int = int(food)
            if 0 <= food_int <= 20:
                result_data["hunger_raw"] = str(food_int)
                result_data["hunger_source"] = "mcpq_nbt"
                return ActionResult(
                    success=True,
                    action=ActionType.CHECK_HUNGER,
                    message=f"Hunger: {food_int}/20",
                    data=result_data,
                )
    except Exception:
        pass

    # Ultimate fallback: assume full hunger (20) if nothing worked
    result_data["hunger_raw"] = "20"
    result_data["hunger_source"] = "default"
    return ActionResult(
        success=True,
        action=ActionType.CHECK_HUNGER,
        message="Hunger: 20/20 (assumed — NBT unavailable)",
        data=result_data,
    )


async def _check_position(mc: WorldAdapter, params: dict) -> ActionResult:
    pos = await mc.get_player_pos()
    if pos:
        msg = f"Position: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})"
    else:
        msg = "Could not get player position"
        pos = (0, 0, 0)
    return ActionResult(
        success=True,
        action=ActionType.CHECK_POSITION,
        message=msg,
        data={"position_raw": f"[{pos[0]}d, {pos[1]}d, {pos[2]}d]"},
    )


async def _list_players(mc: WorldAdapter, params: dict) -> ActionResult:
    players = await mc.get_players_online()
    msg = f"Online players ({len(players)}): {', '.join(players)}"
    return ActionResult(
        success=True,
        action=ActionType.LIST_PLAYERS,
        message=msg,
        data={"players": players},
    )


async def _chat(mc: WorldAdapter, params: dict) -> ActionResult:
    message = params.get("message", "")
    if message:
        await mc.post_to_chat(message)
    return ActionResult(
        success=True,
        action=ActionType.CHAT,
        message=f'Said: "{message}"',
        data={"message": message},
    )


async def _wait(mc: WorldAdapter, params: dict) -> ActionResult:
    import asyncio as _asyncio

    seconds = params.get("seconds", 2.0)
    await _asyncio.sleep(seconds)
    return ActionResult(
        success=True,
        action=ActionType.WAIT,
        message=f"Waited {seconds}s",
        data={"seconds": seconds},
    )


async def _done(mc: WorldAdapter, params: dict) -> ActionResult:
    message = params.get("message", "Task complete")
    return ActionResult(
        success=True,
        action=ActionType.DONE,
        message=message,
        data={"completion_message": message},
    )


# ── Handler registry ────────────────────────────────────────────────────

_HANDLERS: dict[ActionType, Handler] = {
    ActionType.MOVE_TO: _move_to,
    ActionType.MOVE_FORWARD: _move_forward,
    ActionType.MOVE_BACK: _move_back,
    ActionType.WALK_TO: _walk_to,
    ActionType.TURN_LEFT: _turn_left,
    ActionType.TURN_RIGHT: _turn_right,
    ActionType.JUMP: _jump,
    ActionType.SPRINT: _sprint,
    ActionType.TELEPORT: _teleport,
    ActionType.FOLLOW_TELEPORT: _follow_teleport,
    ActionType.PROVISION_BOT: _provision_bot,
    ActionType.PREPARE_SAFE_SPAWN: _prepare_safe_spawn,
    ActionType.GOTO_TELEPORT: _goto_teleport,
    ActionType.COME_TELEPORT: _come_teleport,
    ActionType.BREAK_BLOCK: _break_block,
    ActionType.PLACE_BLOCK: _place_block,
    ActionType.INTERACT: _interact,
    ActionType.CHECK_INVENTORY: _check_inventory,
    ActionType.EQUIP_ITEM: _equip_item,
    ActionType.EQUIP_BEST_WEAPON: _equip_best_weapon,
    ActionType.CRAFT_ITEM: _craft_item,
    ActionType.DROP_ITEM: _drop_item,
    ActionType.EAT: _eat,
    ActionType.HEAL: _heal,
    ActionType.ATTACK: _attack,
    ActionType.SCAN_ENTITIES: _scan_entities,
    ActionType.SCAN: _scan,
    ActionType.CHECK_TIME: _check_time,
    ActionType.CHECK_WEATHER: _check_weather,
    ActionType.CHECK_HEALTH: _check_health,
    ActionType.CHECK_HUNGER: _check_hunger,
    ActionType.CHECK_POSITION: _check_position,
    ActionType.LIST_PLAYERS: _list_players,
    ActionType.CHAT: _chat,
    ActionType.WAIT: _wait,
    ActionType.DONE: _done,
}
