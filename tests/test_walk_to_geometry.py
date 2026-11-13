"""World-state regressions for MCPQ walking, found with the Hermes player profile."""

from __future__ import annotations

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action


@pytest.mark.parametrize("target", [(3.5, 0.5), (-3.5, 0.5), (8.5, 0.5), (-8.5, 0.5)])
async def test_walk_to_reaches_target_without_depending_on_entity_yaw(mock_mc, target):
    mock_mc.set_position(0.5, 65, 0.5)
    for x in range(-12, 13):
        for z in range(-12, 13):
            await mock_mc.set_block("stone", x, 64, z)
    result = await execute_action(
        mock_mc, ActionType.WALK_TO, {"x": target[0], "y": 65, "z": target[1]}
    )
    assert result.success, result
    pos = await mock_mc.get_player_pos()
    assert pos[0] == pytest.approx(target[0], abs=0.5)
    assert pos[2] == pytest.approx(target[1], abs=0.5)


async def test_walk_to_checks_collision_before_moving(mock_mc):
    mock_mc.set_position(0.5, 65, 0.5)
    for x in range(-2, 6):
        await mock_mc.set_block("stone", x, 64, 0)
    await mock_mc.set_block("bedrock", 1, 65, 0)
    await mock_mc.set_block("bedrock", 1, 66, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 4.5, "y": 65, "z": 0.5})
    assert not result.success
    assert await mock_mc.get_player_pos() == (0.5, 65, 0.5)


async def test_walk_to_does_not_step_into_unsupported_air(mock_mc):
    mock_mc.set_position(0.5, 65, 0.5)
    await mock_mc.set_block("stone", 0, 64, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 4.5, "y": 65, "z": 0.5})
    assert not result.success
    assert await mock_mc.get_player_pos() == (0.5, 65, 0.5)


async def test_walk_to_routes_around_obstacle_without_corner_clipping(mock_mc):
    mock_mc.set_position(0.5, 65, 0.5)
    for x in range(-12, 13):
        for z in range(-12, 13):
            await mock_mc.set_block("stone", x, 64, z)
    await mock_mc.set_block("oak_log", 3, 65, 0)
    await mock_mc.set_block("oak_log", 3, 66, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 8.5, "y": 65, "z": 0.5})
    assert result.success, result
    assert await mock_mc.get_player_pos() == pytest.approx((8.5, 65, 0.5))


async def test_walk_to_checks_body_swept_between_fractional_endpoints(mock_mc):
    start = (0.69, 65, 0.95)
    mock_mc.set_position(*start)
    for x in range(-2, 6):
        for z in range(-2, 6):
            await mock_mc.set_block("stone", x, 64, z)
    await mock_mc.set_block("bedrock", 1, 65, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 2.69, "y": 65, "z": 2.95})
    assert not result.success
    assert await mock_mc.get_player_pos() == start


async def test_walk_to_checks_full_head_height_at_fractional_y(mock_mc):
    start = (0.5, 65.5, 0.5)
    mock_mc.set_position(*start)
    for x in range(-2, 4):
        await mock_mc.set_block("stone", x, 64, 0)
    await mock_mc.set_block("bedrock", 1, 67, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 1.5, "y": 65.5, "z": 0.5})
    assert not result.success
    assert await mock_mc.get_player_pos() == start


@pytest.mark.parametrize(
    ("start_x", "target_x", "obstacle_x", "stopped_x"),
    [(0.5, 1.9, 2, 1.5), (-0.5, -1.9, -3, -1.5)],
)
async def test_walk_to_collision_is_failure_even_within_arrival_tolerance(
    mock_mc, start_x, target_x, obstacle_x, stopped_x
):
    mock_mc.set_position(start_x, 65, 0.5)
    for x in range(-6, 7):
        await mock_mc.set_block("stone", x, 64, 0)
    await mock_mc.set_block("bedrock", obstacle_x, 65, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": target_x, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert result.data["remaining"] == pytest.approx(0.4)
    assert await mock_mc.get_player_pos() == (stopped_x, 65, 0.5)
