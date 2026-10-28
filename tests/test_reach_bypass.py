"""Malformed targets must not bypass reach checks (round-1 review of #32)."""

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
    service = ControllerService(
        McpqWorldAdapter(world), auth, "AIBot", world_admin_token=world_admin
    )
    yield service, world, token
    auth.close()


async def _submit(setup, action: ActionType, params: dict):
    service, _, token = setup
    session = await service.open_session(token)
    try:
        return await service.submit_action(
            token,
            ActionRequestSchema(
                steve_id="AIBot",
                controller_session_id=session.session_id,
                generation=session.generation,
                request_id=f"req-{uuid4()}",
                action=action,
                params=params,
            ),
        )
    finally:
        await service.close_session(token, session.session_id)


@pytest.mark.parametrize(
    ("action", "params", "reason_code"),
    [
        # walk_to treats y: null as "keep height", so it is a far walk.
        (ActionType.WALK_TO, {"x": 1000, "z": 100, "y": None}, "OUT_OF_REACH"),
        # Handlers would coerce these (int(True) == 1); the guard refuses them.
        (
            ActionType.PLACE_BLOCK,
            {"block_type": "stone", "x": 1000, "y": True, "z": 100},
            "INVALID_TARGET",
        ),
        (ActionType.BREAK_BLOCK, {"x": 1000, "y": True, "z": 100}, "INVALID_TARGET"),
        (ActionType.BREAK_BLOCK, {"x": 1000, "y": None, "z": 100}, "INVALID_TARGET"),
        (ActionType.TELEPORT, {"x": 1000, "y": None, "z": 100}, "INVALID_TARGET"),
        (ActionType.WALK_TO, {"x": [1000], "z": 100}, "INVALID_TARGET"),
        # Numeric strings parse and are then measured.
        (
            ActionType.PLACE_BLOCK,
            {"block_type": "stone", "x": "1000", "y": 64, "z": 100.5e1},
            "OUT_OF_REACH",
        ),
    ],
)
async def test_malformed_targets_are_refused_without_backend_effects(
    setup, action, params, reason_code
):
    _, world, _ = setup
    result = await _submit(setup, action, params)

    assert result.status is ActionStatus.REJECTED, result
    assert result.reason_code == reason_code
    assert world.commands_ran == []
    assert await world.get_player_pos() == (100, 64, 100)
    assert await world.get_block(1000, 1, 100) == "air"
    assert await world.get_block(1000, 64, 100) == "air"


@pytest.mark.parametrize("action", [ActionType.PLACE_BLOCK, ActionType.BREAK_BLOCK])
@pytest.mark.parametrize(
    ("bot", "target", "allowed"),
    [
        # Handlers edit int(coordinate); the guard must measure that block.
        ((100.9, 64, 100), {"x": 94.99, "y": 64, "z": 100}, False),  # int -> 94: 6.9 away
        ((100.9, 64, 100), {"x": 95.5, "y": 64, "z": 100}, True),  # int -> 95: 5.9 away
        ((-100.9, 64, 0), {"x": -106.5, "y": 64, "z": 0}, True),  # int -> -106: 5.1 away
        ((-100.9, 64, 0), {"x": -107.5, "y": 64, "z": 0}, False),  # int -> -107: 6.1 away
    ],
)
async def test_edits_are_measured_to_the_block_the_handler_edits(
    setup, action, bot, target, allowed
):
    _, world, _ = setup
    world.set_position(*bot)
    params = {"block_type": "stone", **target} if action is ActionType.PLACE_BLOCK else target
    result = await _submit(setup, action, params)

    if allowed:
        assert result.reason_code != "OUT_OF_REACH", result.message
    else:
        assert result.reason_code == "OUT_OF_REACH", result
        assert world.commands_ran == []
