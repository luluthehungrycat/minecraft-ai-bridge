"""Conservative occupancy checks for MCPQ's teleport walker, not client physics.

Only slabs have partial geometry here. Other partial/unknown blocks obstruct
body cells and cannot prove footing. Reads are fresh; there is no world cache.
"""

from __future__ import annotations

import asyncio
import math
import re

from grpc import RpcError
from mcpq.exception import MCPQError

from .world_types import WorldAdapter

# Zero-collision blocks only. Snow, carpet, repeaters, pressure plates,
# scaffolding and coral have collision and deliberately do not appear here.
_CLEAR = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "flowing_water",
    "grass",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "dead_bush",
    "vine",
    "torch",
    "wall_torch",
    "soul_torch",
    "soul_wall_torch",
    "redstone_torch",
    "redstone_wall_torch",
    "redstone_wire",
    "lever",
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
    "seagrass",
    "tall_seagrass",
    "kelp",
    "kelp_plant",
}
# Known full cubes only: an unrecognized ID is never evidence of support.
_FULL = {
    "stone",
    "granite",
    "diorite",
    "andesite",
    "deepslate",
    "cobbled_deepslate",
    "cobblestone",
    "mossy_cobblestone",
    "bedrock",
    "dirt",
    "coarse_dirt",
    "rooted_dirt",
    "grass_block",
    "podzol",
    "mycelium",
    "sand",
    "red_sand",
    "gravel",
    "clay",
    "netherrack",
    "end_stone",
    "obsidian",
    "crying_obsidian",
    "bricks",
    "stone_bricks",
    "glass",
    "tinted_glass",
    "coal_block",
    "iron_block",
    "gold_block",
    "diamond_block",
    "emerald_block",
    "redstone_block",
    "lapis_block",
    "netherite_block",
    "crafting_table",
    "furnace",
    "blast_furnace",
    "smoker",
    "barrel",
}
# Properties alone cannot establish the shape of an unfamiliar block.
# Unsupported materials remain obstacles until explicitly classified.
_SLABS = {
    "stone_slab",
    "smooth_stone_slab",
    "cobblestone_slab",
    "mossy_cobblestone_slab",
    "stone_brick_slab",
    "mossy_stone_brick_slab",
    "brick_slab",
    "sandstone_slab",
    "smooth_sandstone_slab",
    "red_sandstone_slab",
    "smooth_red_sandstone_slab",
    "quartz_slab",
    "smooth_quartz_slab",
    "nether_brick_slab",
    "red_nether_brick_slab",
    "purpur_slab",
}
for _wood in (
    "oak",
    "spruce",
    "birch",
    "jungle",
    "acacia",
    "dark_oak",
    "mangrove",
    "cherry",
    "pale_oak",
):
    _CLEAR.update(
        {
            f"{_wood}_sign",
            f"{_wood}_wall_sign",
            f"{_wood}_hanging_sign",
            f"{_wood}_wall_hanging_sign",
            f"{_wood}_button",
            f"{_wood}_sapling",
        }
    )
    _FULL.update(
        {
            f"{_wood}_planks",
            f"{_wood}_log",
            f"{_wood}_wood",
            f"stripped_{_wood}_log",
            f"stripped_{_wood}_wood",
        }
    )
    _SLABS.add(f"{_wood}_slab")
_STATE = re.compile(r"(?:minecraft:)?([a-z0-9_]+)(?:\[([a-z0-9_=,]+)\])?")


def _parse(raw: str) -> tuple[str, dict[str, str]]:
    match = _STATE.fullmatch(raw)
    if match is None:
        return "unknown", {}
    props: dict[str, str] = {}
    if match[2] is not None:
        for prop in match[2].split(","):
            pair = prop.split("=")
            if len(pair) != 2 or not all(pair) or pair[0] in props:
                return "unknown", {}
            props[pair[0]] = pair[1]
    return match[1], props


async def _read(mc: WorldAdapter, x: int, y: int, z: int) -> str:
    # State is an optional MCPQ extension, not a demand on every WorldAdapter.
    # Legacy adapters can prove air/full cubes by ID, but never partial geometry.
    try:
        reader = getattr(mc, "get_block_state", None)
        value = await reader(x, y, z) if reader is not None else await mc.get_block(x, y, z)
        return value if isinstance(value, str) else "unknown"
    except (RuntimeError, ValueError, TypeError, AttributeError, OSError, RpcError, MCPQError):
        return "unknown"


def _slab_height(bid: str, props: dict[str, str]) -> float | None:
    if bid not in _SLABS or props.get("waterlogged") != "false":
        return None
    if set(props) != {"type", "waterlogged"}:
        return None
    return {"bottom": 0.5, "top": 1.0, "double": 1.0}.get(props.get("type", ""))


def body_clear(raw: str, cell_y: int, feet_y: float) -> bool:
    bid, props = _parse(raw)
    if props.get("waterlogged", "false") != "false":
        return False
    if bid in _CLEAR:
        return True
    # Only the empty upper half of a proven bottom slab is enterable.
    return (
        _slab_height(bid, props) == 0.5 and props.get("type") == "bottom" and feet_y >= cell_y + 0.5
    )


def provides_support(raw: str, feet_y: float) -> bool:
    bid, props = _parse(raw)
    fraction = feet_y - math.floor(feet_y)
    if fraction == 0:
        return bid in _FULL or _slab_height(bid, props) == 1.0
    return fraction == 0.5 and _slab_height(bid, props) == 0.5


async def column_clear(
    mc: WorldAdapter,
    x: int,
    feet_y: float,
    z: int,
    *,
    support: bool = False,
    states: dict[tuple[int, int, int], str] | None = None,
) -> tuple[bool, str]:
    """Check a whole 1.8-high body column, including tall geometry below it."""
    if states is None:
        states = {}

    async def read(by: int) -> str:
        key = (x, by, z)
        if key not in states:
            states[key] = await _read(mc, x, by, z)
        return states[key]

    for by in range(math.floor(feet_y), math.ceil(feet_y + 1.8)):
        block = await read(by)
        if not body_clear(block, by, feet_y):
            return (
                False,
                f"Head would be inside {block}" if by > feet_y else f"Feet would be inside {block}",
            )
    below = await read(math.floor(feet_y) - 1)
    bid, props = _parse(below)
    if any(h in bid for h in ("lava", "fire")) or bid in {
        "cactus",
        "magma_block",
        "sweet_berry_bush",
        "powder_snow",
    }:
        return False, f"Ground below is hazardous ({below})"
    # Fences/walls can extend 1.5 blocks above their base. Unknown geometry
    # below *any* swept column is equally unable to prove body clearance.
    if not (
        bid in _FULL
        or body_clear(below, math.floor(feet_y) - 1, feet_y)
        or _slab_height(bid, props) is not None
    ):
        return False, f"Unproven clearance above {below}"
    if support:
        block = await read(support_cell_y(feet_y))
        if not provides_support(block, feet_y):
            return False, f"no ground support ({block})"
    return True, "passable"


async def read_step_states(
    mc: WorldAdapter, columns: list[tuple[int, int]], feet_y: float
) -> dict[tuple[int, int, int], str]:
    """Read one swept envelope, at most eight independent cells at a time.

    Results belong only to the caller's current movement step. A* keeps its
    existing column batching and does not call this helper (no nested fanout).
    Both possible support cells are already in the body/below range.
    """
    coords = list(
        dict.fromkeys(
            (x, y, z)
            for x, z in columns
            for y in range(math.floor(feet_y) - 1, math.ceil(feet_y + 1.8))
        )
    )
    states: dict[tuple[int, int, int], str] = {}
    for offset in range(0, len(coords), 8):
        batch = coords[offset : offset + 8]
        reads = asyncio.gather(*(_read(mc, *coord) for coord in batch), return_exceptions=True)
        try:
            values = await asyncio.shield(reads)
        except asyncio.CancelledError:
            # Cancelling a to_thread await does not stop its dispatched SDK
            # call. Keep the batch alive and join it, even on repeated cancel,
            # before propagating cancellation. No later batch is dispatched.
            while not reads.done():
                try:
                    await asyncio.shield(reads)
                except asyncio.CancelledError:
                    continue
            reads.result()
            raise
        for coord, value in zip(batch, values, strict=True):
            if isinstance(value, BaseException):
                raise value
            states[coord] = value
    return states


def support_cell_y(feet_y: float) -> int:
    return math.floor(feet_y) - 1 if feet_y == math.floor(feet_y) else math.floor(feet_y)
