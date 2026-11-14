"""Issue #58: MCPQ state strings must prove clearance and slab support."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest
from mcpq import Block
from mcpq.exception import UnknownError

from minecraft_ai_bridge.minecraft.actions import (
    ActionType,
    BackendOutcomeUnknownError,
    execute_action,
)
from minecraft_ai_bridge.minecraft.mc_api import McpqClient
from minecraft_ai_bridge.minecraft.pathfinding import Pathfinder
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


class StatefulWorld(MockMcpqClient):
    async def get_block(self, x, y, z):
        return (await super().get_block(x, y, z)).split("[")[0]

    async def get_block_state(self, x, y, z):
        return await super().get_block(x, y, z)


def world_at(y=65, x=0.5):
    mc = StatefulWorld()
    mc.set_position(x, y, 0.5)
    mc.set_block_map({(bx, 64, bz): "stone" for bx in range(-12, 13) for bz in range(-12, 13)})
    return mc


@pytest.mark.parametrize(("slab_type", "feet_y"), [("bottom", 65.5), ("top", 65)])
async def test_unknown_slab_cannot_prove_walking_or_astar_support(slab_type, feet_y):
    mc = world_at(feet_y)
    support_y = math.floor(feet_y) if feet_y % 1 else int(feet_y) - 1
    mc.set_block_map(
        {
            (x, support_y, z): f"future_slab[type={slab_type},waterlogged=false]"
            for x in range(-12, 13)
            for z in range(-12, 13)
        }
    )
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["steps_taken"] == 0
    assert await mc.get_player_pos() == (0.5, feet_y, 0.5)
    assert await Pathfinder(mc).find_path(0.5, 0.5, 7.5, 0.5, feet_y) is None


async def test_sdk_read_failure_preserves_partial_walk_progress():
    mc = world_at()
    read = mc.get_block_state
    teleport = mc.teleport_player
    moved = False

    async def move(x, y, z):
        nonlocal moved
        await teleport(x, y, z)
        moved = True

    async def read_after_move(x, y, z):
        if moved:
            raise UnknownError("state query failed")
        return await read(x, y, z)

    mc.teleport_player = move
    mc.get_block_state = read_after_move
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["steps_taken"] == 1
    assert result.data["remaining"] == 1.5
    assert await mc.get_player_pos() == (1.0, 65, 0.5)


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, BackendOutcomeUnknownError])
async def test_geometry_does_not_swallow_cancellation_or_unknown_outcome(error_type):
    mc = world_at()

    async def broken(x, y, z):
        raise error_type("not an ordinary read failure")

    mc.get_block_state = broken
    with pytest.raises(error_type):
        await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})


@pytest.mark.parametrize(
    "block",
    [
        "oak_fence[east=false,north=false,south=false,waterlogged=false,west=false]",
        "cobblestone_wall[east=none,north=none,south=none,up=true,waterlogged=false,west=none]",
        "glass_pane[east=false,north=false,south=false,waterlogged=false,west=false]",
        "red_stained_glass",
        "glass",
        "oak_door[facing=north,half=lower,hinge=left,open=false,powered=false]",
        "oak_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]",
        "oak_fence_gate[facing=north,in_wall=false,open=false,powered=false]",
        "oak_door[facing=east,half=lower,hinge=right,open=true,powered=false]",
        "oak_trapdoor[facing=west,half=top,open=true,powered=false,waterlogged=false]",
        "oak_fence_gate[facing=south,in_wall=false,open=true,powered=false]",
        "stone_slab[type=bottom,waterlogged=false]",
        "stone_stairs[facing=north,half=bottom,shape=straight,waterlogged=false]",
        "snow[layers=1]",
        "repeater[delay=1,facing=north,locked=false,powered=false]",
        "white_carpet",
        "scaffolding[bottom=false,distance=0,waterlogged=false]",
        "brain_coral[waterlogged=false]",
        "oak_pressure_plate[powered=false]",
        "future_air",
        "future_sign",
        "future_sapling",
        "unknown",
    ],
)
async def test_body_collision_refused_before_first_step(block):
    mc = world_at()
    await mc.set_block(block, 1, 65, 0)
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["steps_taken"] == 0
    assert await mc.get_player_pos() == (0.5, 65, 0.5)


@pytest.mark.parametrize(
    ("state", "y", "allowed"),
    [
        ("stone_slab[type=bottom,waterlogged=false]", 65.5, True),
        ("stone_slab[type=top,waterlogged=false]", 65, True),
        ("stone_slab[type=double,waterlogged=false]", 65, True),
        ("stone_slab[type=top,waterlogged=false]", 65.5, False),
        ("stone_slab[type=bottom,waterlogged=false]", 65, False),
        ("stone_slab", 65.5, False),
        ("stone_slab[type=sideways]", 65.5, False),
        ("stone_slab[type=bottom,type=top]", 65.5, False),
        ("stone_slab[type=bottom", 65.5, False),
        ("stone_slab[type=bottom,waterlogged=true]", 65.5, False),
        ("unknown", 65, False),
        ("future_block", 65, False),
        ("oak_fence[east=false,north=false,south=false,waterlogged=false,west=false]", 65, False),
        ("cobblestone_wall[up=true]", 65, False),
    ],
)
@pytest.mark.parametrize("start_x", [0.5, -3.5])
async def test_support_requires_known_surface(state, y, allowed, start_x):
    mc = world_at(y, start_x)
    support_y = math.floor(y) if y % 1 else int(y) - 1
    for x in range(-6, 6):
        await mc.set_block(state, x, support_y, 0)
    result = await execute_action(mc, ActionType.WALK_TO, {"x": start_x + 2, "z": 0.5})
    assert result.success is allowed, result
    if allowed:
        assert await mc.get_player_pos() == (start_x + 2, y, 0.5)
    else:
        assert result.data["steps_taken"] == 0


@pytest.mark.parametrize("y", [65, 65.5, -0.5])
async def test_head_slab_intersects_full_body(y):
    mc = world_at(y)
    for x in range(-3, 5):
        await mc.set_block(
            "stone_slab[type=bottom,waterlogged=false]" if y % 1 else "stone",
            x,
            math.floor(y) if y % 1 else int(y) - 1,
            0,
        )
    await mc.set_block("stone_slab[type=bottom,waterlogged=false]", 1, math.ceil(y + 1.8) - 1, 0)
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["steps_taken"] == 0


@pytest.mark.parametrize(
    "block",
    [
        "oak_fence",
        "glass_pane",
        "oak_door[open=false]",
        "stone_slab[type=bottom,waterlogged=false]",
    ],
)
async def test_astar_detours_around_collision(block):
    mc = world_at()
    await mc.set_block(block, 3, 65, 0)
    path = await Pathfinder(mc).find_path(0.5, 0.5, 7.5, 0.5, 65)
    assert path is not None
    assert (3.5, 0.5) not in path
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 7.5, "z": 0.5})
    assert result.success, result


async def test_astar_preserves_fractional_body_and_support():
    mc = world_at(65.5)
    for x in range(-12, 13):
        for z in range(-12, 13):
            await mc.set_block("stone_slab[type=bottom,waterlogged=false]", x, 65, z)
    await mc.set_block("stone_slab[type=bottom,waterlogged=false]", 3, 67, 0)
    path = await Pathfinder(mc).find_path(0.5, 0.5, 7.5, 0.5)
    assert path is not None
    assert (3.5, 0.5) not in path
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 7.5, "z": 0.5})
    assert result.success, result


async def test_failed_state_read_never_falls_back_to_clear_id():
    mc = world_at()

    async def broken(x, y, z):
        raise RuntimeError("state RPC failed")

    mc.get_block_state = broken
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert await mc.get_player_pos() == (0.5, 65, 0.5)


async def test_geometry_is_read_again_after_each_step():
    mc = world_at()
    teleport = mc.teleport_player

    async def move_and_close(x, y, z):
        await teleport(x, y, z)
        await mc.set_block(
            "oak_door[facing=north,half=lower,hinge=left,open=false,powered=false]", 1, 65, 0
        )

    mc.teleport_player = move_and_close
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["steps_taken"] == 1
    assert result.data["blocked"] is True
    assert await mc.get_player_pos() == (1.0, 65, 0.5)


async def test_mcpq_state_wrapper_preserves_block_data_and_bare_id():
    calls = []

    def state(pos):
        calls.append((pos.x, pos.y, pos.z))
        return Block("minecraft:stone_slab[type=bottom,waterlogged=false]")

    client = McpqClient(host="localhost", port=1789, player_name="AIBot")
    client._mc = SimpleNamespace(getBlockWithData=state, getBlock=lambda pos: Block("stone_slab"))
    adapter = McpqWorldAdapter(client)
    assert await adapter.get_block_state(-2, 65, 1) == "stone_slab[type=bottom,waterlogged=false]"
    assert await adapter.get_block(-2, 65, 1) == "stone_slab"
    assert calls == [(-2, 65, 1)]


@pytest.mark.parametrize(
    "value", [None, "", "air[", "air[broken]", "minecraft:unknown", "other:air"]
)
async def test_invalid_state_value_refuses_movement(value):
    mc = world_at()

    async def invalid(x, y, z):
        return value

    mc.get_block_state = invalid
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["steps_taken"] == 0


async def test_rpc_failure_is_a_blocked_walk():
    from grpc import RpcError

    mc = world_at()

    async def broken(x, y, z):
        raise RpcError("unavailable")

    mc.get_block_state = broken
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["steps_taken"] == 0


async def test_id_only_adapter_refuses_unproven_slab_but_walks_stone():
    mc = world_at()
    mc.get_block_state = None
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert result.success
    mc.set_position(0.5, 65.5, 0.5)
    for x in range(-3, 5):
        await mc.set_block("stone_slab[type=bottom,waterlogged=false]", x, 65, 0)
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["steps_taken"] == 0


async def test_astar_refuses_missing_support():
    mc = StatefulWorld()
    path = await Pathfinder(mc).find_path(0.5, 0.5, 7.5, 0.5, 65)
    assert path is None


async def test_unknown_geometry_below_swept_body_is_refused():
    mc = world_at()
    mc.set_position(0.5, 65, 0.8)
    # The body overlaps z=1 even though the center's support remains at z=0.
    await mc.set_block("future_partial", 1, 64, 1)
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.8})
    assert not result.success
    assert result.data["steps_taken"] == 0
    assert await mc.get_player_pos() == (0.5, 65, 0.8)
