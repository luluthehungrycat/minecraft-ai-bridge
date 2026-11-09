"""Damage attribution, death and respawn events (#18)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from minecraft_ai_bridge.bridge.controller_safety import ControllerSafety
from minecraft_ai_bridge.minecraft.mc_api import (
    LastAttacker,
    McpqClient,
    _nbt_int,
    _nbt_uuid,
)
from minecraft_ai_bridge.minecraft.observer import WorldState
from minecraft_ai_bridge.runtime import (
    ConditionEventDetector,
    ControllerService,
    Event,
    EventJournal,
    EventSeverity,
    SafetySupervisor,
)
from minecraft_ai_bridge.runtime import safety_supervisor as supervisor_module
from minecraft_ai_bridge.runtime.condition_events import (
    ATTRIBUTED_EVENT_TYPES,
    RESPAWNED_EVENT_TYPE,
    damage_source,
    death_event,
)
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.test_controller_service import setup as controller_setup

ZOMBIE_UUID = "2a0273df-00a6-4f7e-b92a-5c78ea9ce159"
UUID_RESPONSE = (
    "ProbeBot has the following entity data: [I; 704803807, 10899326, -1188406152, -358817447]"
)


@pytest.fixture
def setup(tmp_path):
    yield from controller_setup.__wrapped__(tmp_path)


# ── NBT parsers ─────────────────────────────────────────────────────────


def test_nbt_int_parses_real_and_suffixed_responses() -> None:
    assert _nbt_int("ProbeBot has the following entity data: 8") == 8
    assert _nbt_int("ProbeBot has the following entity data: -3") == -3
    assert _nbt_int("ProbeBot has the following entity data: 12s\n") == 12


@pytest.mark.parametrize(
    "response",
    [None, "", "Found no elements matching LastHurtByEntity", "No entity was found"],
)
def test_nbt_parsers_return_none_for_missing_keys(response: str | None) -> None:
    assert _nbt_int(response) is None
    assert _nbt_uuid(response) is None


def test_nbt_uuid_converts_negative_ints_to_the_real_uuid() -> None:
    assert _nbt_uuid(UUID_RESPONSE) == ZOMBIE_UUID
    assert _nbt_uuid("x: [I; 0, 0, 0, 1]") == "00000000-0000-0000-0000-000000000001"
    assert _nbt_uuid("x: [I;-1,-1,-1,-1]") == "ffffffff-ffff-ffff-ffff-ffffffffffff"


# ── McpqClient ──────────────────────────────────────────────────────────


def make_client(
    responses: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    entities: dict[str, str] | None = None,
):
    """Client whose commands are answered by substring; returns (client, log).

    ``entities`` maps loaded entity UUIDs to the MCPQ type ``getEntityById``
    reports; any other UUID raises, as MCPQ does for a missing entity.
    """
    client = McpqClient(host="x", port=1, player_name="AIBot")
    log: list[str] = []
    known = entities or {}

    loop_thread = threading.get_ident()

    class FakeEntity:
        def __init__(self, entity_type: str) -> None:
            self._type = entity_type

        @property
        def type(self) -> str:
            # MCPQ may query the server here, so it must not run on the loop.
            assert threading.get_ident() != loop_thread, "entity.type read on the event loop"
            return self._type

    def get_entity_by_id(entity_id: str) -> FakeEntity:
        # Like MCPQ: an unknown ID yields an entity whose type is UNKNOWN.
        return FakeEntity(known.get(entity_id, "UNKNOWN"))

    client._mc = SimpleNamespace(getEntityById=get_entity_by_id)  # type: ignore[assignment]

    async def fake(command: str) -> str:
        log.append(command)
        for key, value in responses.items():
            if key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        return "Found no elements matching x"

    monkeypatch.setattr(client, "run_command_blocking", fake)
    return client, log


async def test_last_attacker_paper_26_resolves_type(monkeypatch: pytest.MonkeyPatch) -> None:
    client, log = make_client(
        {
            "ticks_since_last_hurt_by_mob": "AIBot has the following entity data: 8",
            "last_hurt_by_mob": UUID_RESPONSE,
        },
        monkeypatch,
        entities={ZOMBIE_UUID: "zombie"},
    )
    attacker = await client.get_last_attacker()
    assert attacker == LastAttacker("minecraft:zombie", ZOMBIE_UUID, 8)
    assert len(log) == 2
    assert all(c.startswith("data get entity AIBot ") for c in log)
    assert not any("@p" in c for c in log)


async def test_last_attacker_older_than_max_age_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, log = make_client(
        {"ticks_since_last_hurt_by_mob": "AIBot has the following entity data: 61"}, monkeypatch
    )
    assert await client.get_last_attacker() is None
    assert len(log) == 1
    assert await client.get_last_attacker(max_age_ticks=100) is not None


async def test_last_attacker_despawned_leaves_type_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make_client(
        {
            "ticks_since_last_hurt_by_mob": "AIBot has the following entity data: 2",
            "last_hurt_by_mob": UUID_RESPONSE,
        },
        monkeypatch,
    )
    assert await client.get_last_attacker() == LastAttacker(None, ZOMBIE_UUID, 2)


async def test_last_attacker_legacy_server(monkeypatch: pytest.MonkeyPatch) -> None:
    client, log = make_client(
        {"LastHurtByEntity": "AIBot has the following entity data: [I; 1, 2, 3, 4]"}, monkeypatch
    )
    assert await client.get_last_attacker() == LastAttacker(None, None, None)
    assert log[-1] == "data get entity AIBot LastHurtByEntity"


async def test_last_attacker_nothing_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make_client({}, monkeypatch)
    assert await client.get_last_attacker() is None


async def test_hurt_by_entity_true_false_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    hurt, _ = make_client(
        {"ticks_since_last_hurt_by_mob": "AIBot has the following entity data: 1"}, monkeypatch
    )
    assert await hurt.get_hurt_by_entity() is True
    none, _ = make_client({}, monkeypatch)
    assert await none.get_hurt_by_entity() is False
    broken, _ = make_client({"data get": RuntimeError("boom")}, monkeypatch)
    assert await broken.get_hurt_by_entity() is False


async def test_death_messages_filter_other_players() -> None:
    client = McpqClient(host="x", port=1, player_name="AIBot")

    def poll() -> list[Any]:
        return [
            SimpleNamespace(player=SimpleNamespace(name="AIBot"), deathMessage="AIBot fell"),
            SimpleNamespace(player=SimpleNamespace(name="Other"), deathMessage="Other drowned"),
        ]

    client._mc = SimpleNamespace(events=SimpleNamespace(player_death=SimpleNamespace(poll=poll)))  # type: ignore[assignment]
    assert await client.get_death_messages() == ["AIBot fell"]


async def test_death_messages_empty_when_disconnected() -> None:
    client = McpqClient(host="x", port=1, player_name="AIBot")
    client._mc = None
    assert await client.get_death_messages() == []


# ── Detector respawn, event builders ────────────────────────────────────


def test_respawn_emitted_once_after_death() -> None:
    detector = ConditionEventDetector(steve_id="AIBot")
    emitted: list[Event] = []
    for health, now in [(20, 0.0), (0, 1.0), (0, 2.0)]:
        emitted += detector.observe(WorldState(health=health, hunger=20), now)
    assert RESPAWNED_EVENT_TYPE not in [e.type for e in emitted]

    events = detector.observe(WorldState(health=20, hunger=20, position=(1.0, 64.0, 2.0)), 3.0)
    respawned = [e for e in events if e.type == RESPAWNED_EVENT_TYPE]
    assert len(respawned) == 1
    assert respawned[0].severity is EventSeverity.INFO
    assert respawned[0].payload == {"health": 20.0, "position": [1.0, 64.0, 2.0]}
    assert respawned[0].steve_id == "AIBot"
    again = detector.observe(WorldState(health=20, hunger=20), 4.0)
    assert [e for e in again if e.type == RESPAWNED_EVENT_TYPE] == []


def test_alive_first_observation_is_not_a_respawn() -> None:
    detector = ConditionEventDetector()
    assert detector.observe(WorldState(health=20, hunger=20), 0.0) == []


def test_unreadable_health_while_dead_does_not_respawn_or_rearm() -> None:
    detector = ConditionEventDetector()
    detector.observe(WorldState(health=0, hunger=20), 0.0)
    assert detector.observe(WorldState(health=None, hunger=20), 1.0) == []
    events = detector.observe(WorldState(health=20, hunger=20), 2.0)
    assert [e.type for e in events] == [RESPAWNED_EVENT_TYPE]


def test_dying_again_after_respawn_rearms() -> None:
    detector = ConditionEventDetector()
    count = 0
    for health, now in [(0, 0.0), (20, 1.0), (0, 2.0), (20, 3.0)]:
        events = detector.observe(WorldState(health=health, hunger=20), now)
        count += sum(e.type == RESPAWNED_EVENT_TYPE for e in events)
    assert count == 2


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def test_backend_death_arms_respawn_without_observing_health_zero() -> None:
    detector = ConditionEventDetector(steve_id="AIBot")
    detector.expect_death_messages()
    assert detector.observe(WorldState(health=20, hunger=20, position=(1, 64, 2)), 0.0) == []
    assert _types(detector.record_death("AIBot was slain by Zombie")) == ["steve.died"]
    events = detector.observe(WorldState(health=20, hunger=20, position=(0, 70, 0)), 1.0)
    assert _types(events) == ["steve.respawned"]
    assert events[0].payload == {"health": 20.0, "position": [0, 70, 0]}
    assert detector.observe(WorldState(health=20, hunger=20), 2.0) == []


def test_observation_started_before_the_death_message_is_not_the_respawn() -> None:
    """Seen live: a 2.4 s observation begun before the death finished after the
    death message arrived and reported pre-death health as the respawn."""
    detector = ConditionEventDetector(steve_id="AIBot")
    detector.expect_death_messages()
    detector.observe(WorldState(health=17.5, hunger=20), 0.0)
    assert _types(detector.record_death("AIBot was slain by Zombie", now=10.0)) == ["steve.died"]
    # In flight since t=9.5, before the death was reported: stale.
    stale = detector.observe(WorldState(health=14.5, hunger=20, position=(0, 0, 0)), 9.5)
    assert "steve.respawned" not in _types(stale)
    fresh = detector.observe(WorldState(health=20, hunger=20, position=(8, 70, 2)), 10.25)
    assert [e.payload for e in fresh if e.type == "steve.respawned"] == [
        {"health": 20.0, "position": [8, 70, 2]}
    ]


def test_late_death_message_keeps_one_ordered_lifecycle() -> None:
    """Seen dead, then alive, then the death message arrives (review round 1)."""
    detector = ConditionEventDetector(steve_id="AIBot")
    detector.expect_death_messages()
    events = detector.observe(WorldState(health=0, hunger=20), 0.0)
    events += detector.observe(WorldState(health=20, hunger=20, position=(0, 70, 0)), 0.25)
    assert "steve.respawned" not in _types(events)  # held for the death message
    late = detector.record_death("AIBot was slain by Zombie")
    assert _types(late) == ["steve.died", "steve.respawned"]
    assert late[1].payload == {"health": 20.0, "position": [0, 70, 0]}
    assert "steve.respawned" not in _types(detector.observe(WorldState(health=20, hunger=20), 0.5))


def test_observed_death_then_message_then_alive() -> None:
    detector = ConditionEventDetector(steve_id="AIBot")
    detector.expect_death_messages()
    detector.observe(WorldState(health=0, hunger=20), 0.0)
    assert _types(detector.record_death("AIBot drowned")) == ["steve.died"]
    events = detector.observe(WorldState(health=20, hunger=20), 1.0)
    assert _types(events) == ["steve.respawned"]


def test_held_respawn_is_dropped_if_the_bot_is_dead_again() -> None:
    detector = ConditionEventDetector(steve_id="AIBot")
    detector.expect_death_messages()
    detector.observe(WorldState(health=0, hunger=20), 0.0)
    detector.observe(WorldState(health=20, hunger=20), 0.25)
    detector.observe(WorldState(health=0, hunger=20), 0.5)
    # The message pairs with the death still in progress: no respawn yet.
    assert _types(detector.record_death("AIBot fell from a high place")) == ["steve.died"]
    assert _types(
        [
            e
            for e in detector.observe(WorldState(health=20, hunger=20), 1.0)
            if e.type == "steve.respawned"
        ]
    ) == ["steve.respawned"]


def test_respawn_without_position_has_none_position() -> None:
    detector = ConditionEventDetector()
    detector.observe(WorldState(health=0, hunger=20), 0.0)
    events = detector.observe(WorldState(health=20, hunger=20), 1.0)
    assert events[0].payload["position"] is None


def test_death_event_shape_and_truncation() -> None:
    event = death_event("AIBot was blown up by Creeper", "AIBot")
    assert event.type == "steve.died"
    assert event.severity is EventSeverity.CRITICAL
    assert event.steve_id == "AIBot" and event.source == "condition_monitor"
    assert event.payload == {"message": "AIBot was blown up by Creeper"}
    assert len(death_event("x" * 900, None).payload["message"]) == 500
    assert {"steve.damaged"} == ATTRIBUTED_EVENT_TYPES


def test_damage_source_shapes() -> None:
    assert damage_source(None) == {"kind": "environment"}
    assert damage_source(LastAttacker("minecraft:zombie", None, 3)) == {
        "kind": "entity",
        "entity_type": "minecraft:zombie",
    }
    assert damage_source(LastAttacker(None, None, None)) == {
        "kind": "entity",
        "entity_type": None,
    }


# ── Supervisor ──────────────────────────────────────────────────────────


class FakeWorldEvents:
    def __init__(
        self,
        attacker: LastAttacker | None = None,
        messages: list[str] | None = None,
        attacker_error: Exception | None = None,
        messages_error: Exception | None = None,
    ) -> None:
        self.attacker = attacker
        self.messages = messages or []
        self.attacker_error = attacker_error
        self.messages_error = messages_error
        self.message_polls = 0

    async def get_last_attacker(self, max_age_ticks: int = 60) -> LastAttacker | None:
        if self.attacker_error:
            raise self.attacker_error
        return self.attacker

    async def get_death_messages(self) -> list[str]:
        self.message_polls += 1
        if self.messages_error:
            raise self.messages_error
        messages, self.messages = self.messages, []
        return messages


async def _no_intervention(world: WorldState) -> None:
    return None


async def _drive(
    tmp_path: Path,
    world_events: FakeWorldEvents | None,
    healths: list[float | None],
    *,
    with_sink: bool = True,
    evaluate=_no_intervention,
) -> list[Event]:
    journal = EventJournal(tmp_path / "events.sqlite3")
    queue = list(healths)
    done = asyncio.Event()

    async def observe() -> WorldState:
        if len(queue) > 1:
            health = queue.pop(0)
        else:
            health = queue[0]
            done.set()
        return WorldState(health=health, hunger=20)

    supervisor = SafetySupervisor(
        observe,
        evaluate,
        interval=0.001,
        event_sink=journal.append if with_sink else None,
        steve_id="AIBot",
        condition_events=ConditionEventDetector(steve_id="AIBot"),
        world_events=world_events,
    )
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.wait_for(done.wait(), 2)
        for _ in range(3):  # let a couple more ticks poll death messages
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, 2)
        await supervisor.close()
        return [r.event for r in journal.get_events("reader")]
    finally:
        journal.close()


async def test_death_message_journals_attributed_died_event(tmp_path: Path) -> None:
    world = FakeWorldEvents(
        attacker=LastAttacker("minecraft:creeper", None, 1),
        messages=["AIBot was blown up by Creeper"],
    )
    events = await _drive(tmp_path, world, [20])
    died = [e for e in events if e.type == "steve.died"]
    assert len(died) == 1
    assert died[0].steve_id == "AIBot"
    assert died[0].severity is EventSeverity.CRITICAL
    assert died[0].payload["message"] == "AIBot was blown up by Creeper"
    # The death message names the cause; the respawn clears the attacker record.
    assert "source" not in died[0].payload
    # The plugin respawns the bot before an observation sees health 0, so the
    # death itself arms the respawn event.
    assert [e.type for e in events if e.type == "steve.respawned"] == ["steve.respawned"]


async def test_damaged_event_gets_entity_or_environment_source(tmp_path: Path) -> None:
    hit = await _drive(
        tmp_path, FakeWorldEvents(attacker=LastAttacker("minecraft:zombie", None, 2)), [20, 10]
    )
    damaged = [e for e in hit if e.type == "steve.damaged"]
    assert damaged[0].payload["source"] == {"kind": "entity", "entity_type": "minecraft:zombie"}

    (tmp_path / "events.sqlite3").unlink()
    fall = await _drive(tmp_path, FakeWorldEvents(attacker=None), [20, 10])
    damaged = [e for e in fall if e.type == "steve.damaged"]
    assert damaged[0].payload["source"] == {"kind": "environment"}


async def test_failing_attribution_is_unknown_but_still_journaled(tmp_path: Path) -> None:
    world = FakeWorldEvents(attacker_error=RuntimeError("rcon down"), messages=["AIBot died"])
    events = await _drive(tmp_path, world, [20, 10])
    by_type = {e.type: e for e in events}
    assert by_type["steve.damaged"].payload["source"] == {"kind": "unknown"}
    assert "source" not in by_type["steve.died"].payload


async def test_attribution_timeout_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_WORLD_EVENT_TIMEOUT_SECONDS", 0.01)

    class Slow(FakeWorldEvents):
        async def get_last_attacker(self, max_age_ticks: int = 60) -> LastAttacker | None:
            await asyncio.sleep(10)
            return None

    events = await _drive(tmp_path, Slow(), [20, 10])
    damaged = [e for e in events if e.type == "steve.damaged"]
    assert damaged and damaged[0].payload["source"] == {"kind": "unknown"}


async def test_slow_death_poll_does_not_delay_reflexes(tmp_path: Path) -> None:
    """A death query stuck for a long time must not slow the safety ticks."""
    release = asyncio.Event()

    class StuckDeaths(FakeWorldEvents):
        async def get_death_messages(self) -> list[str]:
            self.message_polls += 1
            await release.wait()
            return []

    world = StuckDeaths()
    times: list[float] = []

    async def evaluate(state: WorldState) -> None:
        times.append(asyncio.get_running_loop().time())

    supervisor = SafetySupervisor(
        lambda: _const_world(),
        evaluate,
        interval=0.001,
        event_sink=lambda event: 1,
        condition_events=ConditionEventDetector(),
        world_events=world,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop))
    try:
        async with asyncio.timeout(2):
            while len(times) < 20:
                await asyncio.sleep(0.001)
        # The death query is still stuck, yet ticks kept their pace.
        assert world.message_polls == 1 and not release.is_set()
        assert max(b - a for a, b in zip(times, times[1:], strict=False)) < 0.2
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
        release.set()
    await supervisor.close()


async def test_death_poller_stops_with_the_supervisor(tmp_path: Path) -> None:
    world = FakeWorldEvents()
    supervisor = SafetySupervisor(
        lambda: _const_world(),
        _no_intervention,
        interval=0.001,
        event_sink=lambda event: 1,
        condition_events=ConditionEventDetector(),
        world_events=world,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop))
    async with asyncio.timeout(2):
        while world.message_polls < 3:
            await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, 2)
    polls = world.message_polls
    await asyncio.sleep(0.02)
    assert world.message_polls == polls
    assert not [t for t in asyncio.all_tasks() if "_poll_death_events" in repr(t.get_coro())]


async def _const_world() -> WorldState:
    return WorldState(health=20, hunger=20)


async def test_failing_death_poll_does_not_stop_reflexes(tmp_path: Path) -> None:
    world = FakeWorldEvents(messages_error=RuntimeError("poll failed"))
    evaluations = 0

    async def evaluate(state: WorldState) -> None:
        nonlocal evaluations
        evaluations += 1

    events = await _drive(tmp_path, world, [20], evaluate=evaluate)
    assert world.message_polls >= 2
    assert evaluations >= 2
    assert [e for e in events if e.type == "steve.died"] == []


async def test_no_world_events_means_no_source_key(tmp_path: Path) -> None:
    events = await _drive(tmp_path, None, [20, 10])
    damaged = [e for e in events if e.type == "steve.damaged"]
    assert damaged and "source" not in damaged[0].payload


async def test_world_events_ignored_without_event_sink(tmp_path: Path) -> None:
    world = FakeWorldEvents(messages=["AIBot died"])
    await _drive(tmp_path, world, [20], with_sink=False)
    assert world.message_polls == 0


# ── ControllerSafety wiring ─────────────────────────────────────────────


async def test_controller_safety_journals_death_for_the_controlled_steve(
    setup, tmp_path: Path
) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    authority = object()
    service = ControllerService(
        McpqWorldAdapter(setup.world),
        setup.auth,
        "AIBot",
        safety_authority=authority,
        event_journal=journal,
    )
    safety = ControllerSafety(service, authority, event_sink=journal.append, interval=0.001)
    setup.world.add_death_message("AIBot was blown up by Creeper")
    seen = asyncio.Event()

    async def observe() -> WorldState:
        seen.set()
        return WorldState(health=20, hunger=20)

    safety._supervisor._observe = observe  # type: ignore[assignment]
    try:
        safety.start()
        await asyncio.wait_for(seen.wait(), 2)
        for _ in range(20):
            await asyncio.sleep(0.005)
            if any(r.event.type == "steve.died" for r in journal.get_events("reader")):
                break
        await safety.aclose()
        died = [r.event for r in journal.get_events("reader") if r.event.type == "steve.died"]
        assert len(died) == 1
        assert died[0].steve_id == "AIBot"
        assert died[0].payload["message"] == "AIBot was blown up by Creeper"
        assert "source" not in died[0].payload
    finally:
        await safety.aclose()
        journal.close()
