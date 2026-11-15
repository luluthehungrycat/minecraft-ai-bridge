"""Bounded #59 reads: instrument the explicit #58 stateful world."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter

import pytest

from minecraft_ai_bridge.minecraft.actions import (
    ActionType,
    BackendOutcomeUnknownError,
    execute_action,
)
from minecraft_ai_bridge.minecraft.pathfinding import Pathfinder
from tests.test_state_aware_walking import world_at


@pytest.mark.parametrize("feet_y", [65, 65.5])
async def test_each_step_reads_each_coordinate_once_and_next_step_reads_fresh(feet_y):
    mc = world_at(feet_y)
    if feet_y == 65.5:
        mc.set_block_map(
            {
                (x, 65, z): "stone_slab[type=bottom,waterlogged=false]"
                for x in range(-2, 5)
                for z in range(-2, 3)
            }
        )
    read, teleport, position = mc.get_block_state, mc.teleport_player, mc.get_player_pos
    steps = [Counter()]
    positions = []

    async def counted(x, y, z):
        steps[-1][x, y, z] += 1
        return await read(x, y, z)

    async def move(x, y, z):
        await teleport(x, y, z)
        steps.append(Counter())

    async def observed():
        value = await position()
        positions.append(value)
        return value

    mc.get_block_state, mc.teleport_player, mc.get_player_pos = counted, move, observed
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert result.success and result.data["steps_taken"] == 4
    assert all(count == 1 for step in steps for count in step.values())
    assert (1, 65, 0) in steps[0] and (1, 65, 0) in steps[1]
    assert len(positions) == 8  # entry, walker entry, four steps, arrival, final readback
    assert positions[-1] == (2.5, feet_y, 0.5)


async def test_movement_reads_are_concurrent_and_bounded_without_multiplying_astar():
    mc = world_at()
    read = mc.get_block_state
    active = peak = 0

    async def delayed(x, y, z):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return await read(x, y, z)
        finally:
            active -= 1

    mc.get_block_state = delayed
    # Diagonal swept envelope covers four columns (12 independent cells).
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 2.5})
    assert result.success
    assert 2 <= peak <= 8
    assert active == 0
    peak = 0
    assert await Pathfinder(mc).find_path(0.5, 0.5, 7.5, 0.5, 65)
    assert peak <= 25
    assert active == 0


@pytest.mark.parametrize(("y", "block"), [(65, "glass_pane"), (64, "air"), (64, "lava")])
async def test_geometry_changed_after_first_step_refuses_with_partial_progress(y, block):
    mc = world_at()
    teleport = mc.teleport_player

    async def move(x, feet_y, z):
        await teleport(x, feet_y, z)
        await mc.set_block(block, 1, y, 0)

    mc.teleport_player = move
    result = await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success and result.data["blocked"]
    assert result.data["steps_taken"] == 1
    assert result.data["remaining"] == 1.5
    assert await mc.get_player_pos() == (1, 65, 0.5)


@pytest.mark.parametrize("error_type", [BackendOutcomeUnknownError, asyncio.CancelledError])
async def test_read_failure_joins_all_sibling_reads_before_propagating(error_type):
    mc = world_at()
    active = 0
    started = asyncio.Event()

    async def broken(x, y, z):
        nonlocal active
        active += 1
        started.set()
        try:
            if (x, y, z) == (0, 64, 0):
                await asyncio.sleep(0.01)
                raise error_type("uncertain or cancelled read")
            await asyncio.sleep(0.05)
            return "stone" if y == 64 else "air"
        finally:
            # Cleanup itself suspends: cancellation must be joined.
            await asyncio.sleep(0)
            active -= 1

    mc.get_block_state = broken
    with pytest.raises(error_type):
        await execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert started.is_set() and active == 0
    assert await mc.get_player_pos() == (0.5, 65, 0.5)


async def test_external_cancellation_joins_inflight_reads():
    mc = world_at()
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def blocked(x, y, z):
        nonlocal active
        active += 1
        started.set()
        try:
            await release.wait()
        finally:
            await asyncio.sleep(0)
            active -= 1

    mc.get_block_state = blocked
    task = asyncio.create_task(execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5}))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert active == 0
    assert await mc.get_player_pos() == (0.5, 65, 0.5)


async def test_cancellation_waits_for_dispatched_thread_reads_even_if_cancelled_again():
    mc = world_at()
    started = asyncio.Event()
    release = threading.Event()
    active = 0
    loop = asyncio.get_running_loop()

    def sdk_read(x, y, z):
        nonlocal active
        active += 1
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(3), "test did not release SDK read"
            return "stone" if y == 64 else "air"
        finally:
            active -= 1

    async def threaded(x, y, z):
        return await asyncio.to_thread(sdk_read, x, y, z)

    mc.get_block_state = threaded
    task = asyncio.create_task(execute_action(mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "action ended with SDK reads still running"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert active == 0
    assert await mc.get_player_pos() == (0.5, 65, 0.5)
