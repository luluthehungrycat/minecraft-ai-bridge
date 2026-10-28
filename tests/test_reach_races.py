"""Reach checks under concurrency and replay (F3, F5 from the #32 review)."""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import (
    ActionRequestSchema,
    ActionStatus,
    AuthzService,
    ControllerService,
    SqliteActionOutcomeStore,
)
from minecraft_ai_bridge.runtime import action_service as action_service_module
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient

WORLD_SCOPES = {"steve:world:move", "steve:world:edit", "steve:world:items", "steve:world:combat"}


@pytest.fixture
def env(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    token = auth.create_credential(
        "AIBot", "owner", {"steve:control", *WORLD_SCOPES}, admin_token=admin.token
    ).token
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token
    world = MockMcpqClient()
    world.set_position(100, 64, 100)
    store = SqliteActionOutcomeStore(tmp_path / "outcomes.sqlite3")
    service = ControllerService(
        McpqWorldAdapter(world), auth, "AIBot", world_admin_token=world_admin, outcome_store=store
    )
    service.test_store = store  # type: ignore[attr-defined]
    yield service, world, token
    store.close()
    auth.close()


def _request(session, action: ActionType, params: dict, request_id: str):
    return ActionRequestSchema(
        steve_id="AIBot",
        controller_session_id=session.session_id,
        generation=session.generation,
        request_id=request_id,
        action=action,
        params=params,
    )


async def test_reach_is_checked_against_the_position_at_dispatch(env, monkeypatch):
    """An edit queued behind a teleport is measured from where the teleport left the bot."""
    service, world, token = env
    session = await service.open_session(token)
    teleport_running = asyncio.Event()
    release_teleport = asyncio.Event()
    original = action_service_module.execute_action

    async def gated(adapter, action, params):
        if action is ActionType.TELEPORT:
            teleport_running.set()
            await release_teleport.wait()
        return await original(adapter, action, params)

    monkeypatch.setattr(action_service_module, "execute_action", gated)
    # The durable claim happens after any pre-dispatch check and before the
    # dispatch lock, so once the edit has claimed it is queued on the lock.
    edit_queued = asyncio.Event()
    store = service.test_store
    original_claim = store.claim

    def claim(request_id, *args):
        result = original_claim(request_id, *args)
        if request_id == "edit":
            loop.call_soon_threadsafe(edit_queued.set)
        return result

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(store, "claim", claim)

    teleport = asyncio.create_task(
        service.submit_action(
            token, _request(session, ActionType.TELEPORT, {"x": 130, "y": 64, "z": 100}, "tp")
        )
    )
    await teleport_running.wait()
    # 3 blocks from where the bot is now, 27 from where it is about to be.
    edit = asyncio.create_task(
        service.submit_action(
            token,
            _request(
                session,
                ActionType.PLACE_BLOCK,
                {"block_type": "stone", "x": 103, "y": 64, "z": 100},
                "edit",
            ),
        )
    )
    await edit_queued.wait()
    release_teleport.set()

    assert (await teleport).status is ActionStatus.SUCCEEDED
    placed = await edit
    assert placed.status is ActionStatus.REJECTED
    assert placed.reason_code == "OUT_OF_REACH"
    assert await world.get_block(103, 64, 100) == "air"


async def test_completed_request_replays_after_the_bot_moves(env):
    service, world, token = env
    session = await service.open_session(token)
    edit = _request(
        session, ActionType.PLACE_BLOCK, {"block_type": "stone", "x": 103, "y": 64, "z": 100}, "e1"
    )

    first = await service.submit_action(token, edit)
    moved = await service.submit_action(
        token, _request(session, ActionType.TELEPORT, {"x": 125, "y": 64, "z": 100}, "tp")
    )
    replay = await service.submit_action(token, edit)

    assert first.status is ActionStatus.SUCCEEDED
    assert moved.status is ActionStatus.SUCCEEDED
    assert replay.status is ActionStatus.SUCCEEDED, replay.message
    assert replay.action_id == first.action_id
    assert sum("place" in c.lower() or "setblock" in c for c in world.commands_ran) <= 1


async def test_reused_request_id_with_new_params_still_conflicts(env):
    service, world, token = env
    session = await service.open_session(token)
    await service.submit_action(
        token,
        _request(
            session,
            ActionType.PLACE_BLOCK,
            {"block_type": "stone", "x": 103, "y": 64, "z": 100},
            "e1",
        ),
    )
    reused = await service.submit_action(
        token,
        _request(
            session,
            ActionType.PLACE_BLOCK,
            {"block_type": "dirt", "x": 104, "y": 64, "z": 100},
            "e1",
        ),
    )

    assert reused.status is ActionStatus.REJECTED
    assert await world.get_block(104, 64, 100) == "air"


async def test_fresh_out_of_reach_request_is_still_rejected_and_not_retried(env):
    service, world, token = env
    session = await service.open_session(token)
    far = _request(
        session, ActionType.PLACE_BLOCK, {"block_type": "stone", "x": 140, "y": 64, "z": 100}, "far"
    )

    first = await service.submit_action(token, far)
    await service.submit_action(
        token, _request(session, ActionType.TELEPORT, {"x": 125, "y": 64, "z": 100}, "tp")
    )
    retry = await service.submit_action(token, far)  # now within reach, same request_id

    assert first.reason_code == "OUT_OF_REACH"
    assert retry.status is ActionStatus.REJECTED  # the request_id's outcome is final
    assert await world.get_block(140, 64, 100) == "air"
