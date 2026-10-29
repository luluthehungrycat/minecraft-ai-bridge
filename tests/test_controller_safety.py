"""External-controller safety, shared dispatch, attribution and cleanup."""

from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.bridge.controller_safety import ControllerSafety
from minecraft_ai_bridge.minecraft.actions import ActionResult, ActionType
from minecraft_ai_bridge.minecraft.observer import InventorySlot, WorldState
from minecraft_ai_bridge.runtime import ActionStatus, ControllerService, EventJournal
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.test_controller_service import _request
from tests.test_controller_service import setup as controller_setup


@pytest.fixture
def setup(tmp_path):
    yield from controller_setup.__wrapped__(tmp_path)


def service_for(setup, journal=None):
    authority = object()
    service = ControllerService(
        McpqWorldAdapter(setup.world),
        setup.auth,
        "AIBot",
        safety_authority=authority,
        event_journal=journal,
    )
    return service, ControllerSafety(
        service, authority, event_sink=journal.append if journal else None, interval=0.001
    )


async def test_reflex_without_controller_or_world_delegation(setup):
    service, safety = service_for(setup)
    result = await safety._evaluate(
        WorldState(
            health=20,
            hunger=2,
            inventory=[InventorySlot("minecraft:bread", 1, 0)],
        )
    )
    assert result is not None and result.success
    assert result.action is ActionType.EAT
    assert service.action_service.audit_records[-1].allowed
    assert not service.action_service._safety_intervention_pending()
    assert setup.world.commands_ran


async def test_reflex_preempts_queued_request_even_after_ack(setup, monkeypatch):
    service, safety = service_for(setup)
    session = await service.open_session(setup.controller)
    actions = service.action_service
    actions.enable_safety_preemption()
    entered = asyncio.Event()
    original_policy = actions._policy

    async def policy(request):
        result = await original_policy(request)
        if request.action is ActionType.CHAT:
            entered.set()
        return result

    monkeypatch.setattr(actions, "_policy", policy)
    await actions._dispatch_lock.acquire()
    queued = asyncio.create_task(
        service.submit_action(
            setup.controller, _request(session, ActionType.CHAT, {"message": "stale"})
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # A reflex arrived and completed before the old request got the lock.
        actions._preemption_generation += 1
        actions.acknowledge_safety_intervention()
    finally:
        actions._dispatch_lock.release()
    result = await queued
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "SAFETY_PREEMPTED"
    assert setup.world.chat_messages_sent == []
    fresh = await service.submit_action(
        setup.controller, _request(session, ActionType.CHAT, {"message": "fresh"}, "fresh")
    )
    assert fresh.status is ActionStatus.SUCCEEDED


async def test_partial_interruption_is_not_predispatch_rejection(setup, monkeypatch):
    service, _ = service_for(setup)
    session = await service.open_session(setup.controller)

    async def interrupted(*args, **kwargs):
        return ActionResult(
            False, ActionType.MOVE_FORWARD, data={"interrupted": True, "steps_taken": 1}
        )

    monkeypatch.setattr(service.action_service, "execute", interrupted)
    result = await service.submit_action(
        setup.controller, _request(session, ActionType.MOVE_FORWARD)
    )
    assert result.status is ActionStatus.INTERRUPTED
    assert result.reason_code == "SAFETY_INTERRUPTED"


async def test_polling_journals_attributed_intervention_and_stops(setup, tmp_path, monkeypatch):
    journal = EventJournal(tmp_path / "events.sqlite3")
    service, safety = service_for(setup, journal)
    evaluated = asyncio.Event()

    async def observe():
        return WorldState(health=20, hunger=2, inventory=[InventorySlot("minecraft:bread", 1, 0)])

    original = safety._preservation.evaluate

    async def evaluate(world):
        result = await original(world)
        evaluated.set()
        return result

    monkeypatch.setattr(safety._supervisor, "_observe", observe)
    monkeypatch.setattr(safety._preservation, "evaluate", evaluate)
    try:
        safety.start()
        safety.start()  # start is idempotent
        await asyncio.wait_for(evaluated.wait(), 2)
        await safety.aclose()
        await safety.aclose()
        token = setup.auth.create_credential(
            "AIBot", "owner", {"events:read"}, admin_token=setup.admin
        ).token
        events = await service.get_events(token)
        assert events.events
        event = events.events[0].event
        assert event.type == "safety.intervened" and event.steve_id == "AIBot"
        assert event.payload["action"] == "eat"
        assert not service.action_service._safety_preemption_enabled
        assert safety._task is None
    finally:
        await safety.aclose()
        journal.close()


async def test_evaluation_failure_releases_preemption(setup, monkeypatch):
    service, safety = service_for(setup)
    service.action_service.enable_safety_preemption()
    service.action_service._preemption_generation += 1

    async def fail(world):
        raise ValueError("evaluation failed")

    monkeypatch.setattr(safety._preservation, "evaluate", fail)
    with pytest.raises(ValueError, match="evaluation failed"):
        await safety._evaluate(WorldState())
    assert not service.action_service._safety_intervention_pending()


async def test_reflex_starting_during_reach_read_preempts_dispatch(setup, monkeypatch):
    authority = object()
    service = ControllerService(
        McpqWorldAdapter(setup.world),
        setup.auth,
        "AIBot",
        world_admin_token=setup.world_admin,
        safety_authority=authority,
    )
    safety = ControllerSafety(service, authority)
    service.action_service.enable_safety_preemption()
    session = await service.open_session(setup.delegated)
    entered, release = asyncio.Event(), asyncio.Event()
    original = setup.world.get_player_pos

    async def position():
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(setup.world, "get_player_pos", position)
    request = asyncio.create_task(
        service.submit_action(
            setup.delegated, _request(session, ActionType.MOVE_TO, {"x": 3, "y": 64, "z": 0})
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    reflex = asyncio.create_task(
        safety._evaluate(
            WorldState(health=20, hunger=2, inventory=[InventorySlot("minecraft:bread", 1, 0)])
        )
    )
    try:
        async with asyncio.timeout(2):
            while not service.action_service._safety_intervention_pending():
                await asyncio.sleep(0)
    finally:
        release.set()
    result, intervention = await asyncio.gather(request, reflex)
    assert intervention.success
    assert result.status is ActionStatus.REJECTED
    assert result.reason_code == "SAFETY_PREEMPTED"
    assert await original() == (0, 64, 0)
