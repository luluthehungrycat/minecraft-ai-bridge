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
    # Standing on bottom slabs (top at 65.5), so the body reaches cell 67.
    start = (0.5, 65.5, 0.5)
    mock_mc.set_position(*start)
    for x in range(-2, 4):
        await mock_mc.set_block("stone", x, 64, 0)
        await mock_mc.set_block("stone_slab", x, 65, 0)
    await mock_mc.set_block("bedrock", 1, 67, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 1.5, "y": 65.5, "z": 0.5})
    assert not result.success
    assert "Head would be inside" in result.message
    assert await mock_mc.get_player_pos() == start


async def test_walk_to_walks_along_slab_tops(mock_mc):
    mock_mc.set_position(0.5, 65.5, 0.5)
    for x in range(-2, 6):
        await mock_mc.set_block("air", x, 64, 0)
        await mock_mc.set_block("oak_slab", x, 65, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 3.5, "y": 65.5, "z": 0.5})
    assert result.success, result
    assert await mock_mc.get_player_pos() == pytest.approx((3.5, 65.5, 0.5))


async def test_walk_to_refuses_mid_air_fractional_height(mock_mc):
    # Floating above full blocks: the feet cell holds nothing to stand on.
    start = (0.5, 65.5, 0.5)
    mock_mc.set_position(*start)
    for x in range(-2, 4):
        await mock_mc.set_block("stone", x, 64, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert "no ground support" in result.message
    assert await mock_mc.get_player_pos() == start


async def test_walk_to_refuses_hazard_sharing_the_fractional_feet_cell(mock_mc):
    start = (0.5, 65.5, 0.5)
    mock_mc.set_position(*start)
    for x in range(-2, 4):
        await mock_mc.set_block("stone", x, 64, 0)
        await mock_mc.set_block("stone_slab", x, 65, 0)
    await mock_mc.set_block("fire", 1, 65, 0)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 2.5, "z": 0.5})
    assert not result.success
    assert result.data["blocked"] is True
    assert await mock_mc.get_player_pos() == start


async def _flat_floor(mock_mc, half: int) -> None:
    mock_mc.set_block_map(
        {(x, 64, z): "stone" for x in range(-half, half + 1) for z in range(-half, half + 1)}
    )


async def test_walk_to_walks_the_full_advertised_range(mock_mc):
    # 40 blocks is inside WALK_TO_TELEPORT_DISTANCE; a fixed 50-step budget
    # used to stop at 25 blocks.
    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 45)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 40.5, "y": 65, "z": 0.5})
    assert result.success, result
    assert "teleported" not in result.data
    assert result.data["budget_exhausted"] is False
    assert result.data["steps_taken"] == 80
    assert await mock_mc.get_player_pos() == pytest.approx((40.5, 65, 0.5))


async def test_walk_to_budget_covers_a_detour(mock_mc):
    # A wall forces A* off the straight line; the budget follows the route.
    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 30)
    for z in range(-6, 7):
        await mock_mc.set_block("bedrock", 10, 65, z)
        await mock_mc.set_block("bedrock", 10, 66, z)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 20.5, "z": 0.5})
    assert result.success, result
    assert result.data["steps_taken"] > 40
    assert await mock_mc.get_player_pos() == pytest.approx((20.5, 65, 0.5))


async def test_walk_budget_exhaustion_is_reported(mock_mc):
    from minecraft_ai_bridge.minecraft.actions import _walk_toward

    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 12)
    outcome = await _walk_toward(mock_mc, 10.5, 0.5, max_steps=4)
    assert outcome.budget_exhausted is True
    assert outcome.blocked is False
    assert outcome.steps_taken == 4
    assert await mock_mc.get_player_pos() == pytest.approx((2.5, 65, 0.5))


async def test_walk_refuses_route_longer_than_limit_before_moving(mock_mc, monkeypatch):
    from minecraft_ai_bridge.minecraft import actions

    monkeypatch.setattr(actions, "WALK_MAX_ROUTE_LENGTH", 5.0)
    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 12)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 8.5, "z": 0.5})
    assert not result.success
    assert result.data["budget_exhausted"] is True
    assert result.data["steps_taken"] == 0
    assert await mock_mc.get_player_pos() == (0.5, 65, 0.5)


@pytest.mark.parametrize("target_y", [66, 64, 63.4])
async def test_walk_to_refuses_vertical_target_before_moving(mock_mc, target_y):
    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 6)
    result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 4.5, "y": target_y, "z": 0.5})
    assert not result.success
    assert result.data["vertical_target_unsupported"] is True
    assert result.data["steps_taken"] == 0
    assert await mock_mc.get_player_pos() == (0.5, 65, 0.5)


@pytest.mark.parametrize("target_y", [65, 65.5, 64.5, None])
async def test_walk_to_accepts_y_within_tolerance_or_omitted(mock_mc, target_y):
    mock_mc.set_position(0.5, 65, 0.5)
    await _flat_floor(mock_mc, 6)
    params = {"x": 4.5, "z": 0.5}
    if target_y is not None:
        params["y"] = target_y
    result = await execute_action(mock_mc, ActionType.WALK_TO, params)
    assert result.success, result
    assert await mock_mc.get_player_pos() == pytest.approx((4.5, 65, 0.5))


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
