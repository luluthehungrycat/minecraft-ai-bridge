"""Reach limits for external controllers' targeted world actions (#25)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionStatus,
    AuthzService,
    ControllerService,
)
from minecraft_ai_bridge.runtime.controller_service import ReachLimits
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient

WORLD_SCOPES = {"steve:world:move", "steve:world:edit", "steve:world:items", "steve:world:combat"}


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    token = auth.create_credential(
        "AIBot", "owner", {"steve:control", *WORLD_SCOPES}, admin_token=admin.token
    ).token
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token
    world = MockMcpqClient()
    world.set_position(100, 64, 100)
    yield auth, world, token, world_admin
    auth.close()


async def _submit(setup, action: ActionType, params: dict, limits: ReachLimits | None = None):
    auth, world, token, world_admin = setup
    service = ControllerService(
        McpqWorldAdapter(world), auth, "AIBot", world_admin_token=world_admin, reach_limits=limits
    )
    session = await service.open_session(token)
    try:
        request = ActionRequestSchema(
            steve_id="AIBot",
            controller_session_id=session.session_id,
            generation=session.generation,
            request_id=f"req-{uuid4()}",
            action=action,
            params=params,
        )
        return await service.submit_action(token, request)
    finally:
        await service.close_session(token, session.session_id)


@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.PLACE_BLOCK, {"block_type": "stone", "x": 103, "y": 64, "z": 100}),
        (ActionType.BREAK_BLOCK, {"x": 100, "y": 60, "z": 100}),
        (ActionType.PLACE_BLOCK, {"block_type": "stone"}),  # next to the bot
        (ActionType.TELEPORT, {"x": 120, "y": 70, "z": 110}),
        (ActionType.MOVE_TO, {"x": 90, "y": 64, "z": 90}),
        (ActionType.WALK_TO, {"x": 125, "z": 100}),
    ],
)
async def test_targets_within_reach_run(setup, action, params):
    result = await _submit(setup, action, params)
    assert result.reason_code != "OUT_OF_REACH", result.message


@pytest.mark.parametrize(
    ("action", "params", "message"),
    [
        (ActionType.PLACE_BLOCK, {"block_type": "stone", "x": 107, "y": 64, "z": 100}, "edits"),
        (ActionType.BREAK_BLOCK, {"x": 0, "y": 64, "z": 0}, "edits"),
        (ActionType.BREAK_BLOCK, {"x": 100, "y": 50, "z": 100}, "edits"),
        (ActionType.TELEPORT, {"x": 140, "y": 64, "z": 100}, "movement"),
        (ActionType.MOVE_TO, {"x": 0, "y": 0, "z": 0}, "movement"),  # missing axes are 0
        (ActionType.MOVE_TO, {"x": 100, "z": 100}, "movement"),  # y defaults to 0: 64 down
        (ActionType.WALK_TO, {"x": 100, "z": 140}, "movement"),
        (ActionType.TELEPORT, {"x": 100, "y": 330, "z": 100}, "outside"),
        (ActionType.TELEPORT, {"x": 100, "y": -70, "z": 100}, "outside"),
    ],
)
async def test_out_of_reach_targets_are_rejected_without_dispatch(setup, action, params, message):
    _, world, _, _ = setup
    result = await _submit(setup, action, params)

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "OUT_OF_REACH"
    assert message in result.message
    assert world.commands_ran == []
    assert await world.get_player_pos() == (100, 64, 100)


async def test_unknown_position_fails_closed(setup, monkeypatch):
    _, world, _, _ = setup

    async def no_position():
        return None

    monkeypatch.setattr(world, "get_player_pos", no_position)
    result = await _submit(setup, ActionType.TELEPORT, {"x": 101, "y": 64, "z": 100})

    assert result.reason_code == "OUT_OF_REACH"
    assert "position is unknown" in result.message
    assert world.commands_ran == []


async def test_operator_can_change_the_limits(setup):
    wide = ReachLimits(edit_distance=200, move_distance=200)
    tight = ReachLimits(edit_distance=1, move_distance=1)

    far = await _submit(setup, ActionType.TELEPORT, {"x": 0, "y": 64, "z": 0}, wide)
    near = await _submit(setup, ActionType.TELEPORT, {"x": 102, "y": 64, "z": 100}, tight)

    assert far.status is ActionStatus.SUCCEEDED
    assert near.reason_code == "OUT_OF_REACH"


async def test_unchecked_actions_are_unaffected(setup):
    result = await _submit(setup, ActionType.MOVE_FORWARD, {"steps": 2})
    assert result.reason_code != "OUT_OF_REACH"


async def test_malformed_coordinates_are_left_to_the_handler(setup):
    _, world, _, _ = setup
    result = await _submit(setup, ActionType.TELEPORT, {"x": "far", "y": 64, "z": 0})

    assert result.reason_code != "OUT_OF_REACH"
    assert result.status is ActionStatus.FAILED
    assert world.commands_ran == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"edit_distance": 0},
        {"move_distance": -1},
        {"min_y": 10, "max_y": 10},
    ],
)
def test_invalid_limits_are_refused(kwargs):
    with pytest.raises(ValueError):
        ReachLimits(**kwargs)
