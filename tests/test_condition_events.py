"""Steve condition events: damage, low health, and low hunger (#18)."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from minecraft_ai_bridge.bridge.self_preservation import PreservationConfig
from minecraft_ai_bridge.minecraft.observer import WorldState
from minecraft_ai_bridge.runtime import (
    ConditionEventConfig,
    ConditionEventDetector,
    Event,
    EventJournal,
    EventSeverity,
    SafetySupervisor,
)
from minecraft_ai_bridge.runtime import safety_supervisor as supervisor_module


def types(events: list[Event]) -> list[str]:
    return [event.type for event in events]


def feed(detector: ConditionEventDetector, values: list[tuple[float | None, float]]) -> list[Event]:
    """Observe ``(health, now)`` pairs with healthy hunger; return all events."""
    events: list[Event] = []
    for health, now in values:
        events += detector.observe(WorldState(health=health, hunger=20), now)
    return events


# ── Low health and hunger edge triggers ────────────────────────────────


def test_low_health_fires_once_and_rearms_only_after_recovery() -> None:
    detector = ConditionEventDetector(steve_id="AIBot")

    first = detector.observe(WorldState(health=3, hunger=20), 0.0)
    assert types(first) == ["steve.health_low"]
    assert first[0].severity is EventSeverity.HIGH
    assert first[0].payload == {"health": 3.0, "threshold": 4.0}
    assert first[0].steve_id == "AIBot" and first[0].source == "condition_monitor"

    def low(health: float, now: float) -> list[str]:
        events = detector.observe(WorldState(health=health, hunger=20), now)
        return [t for t in types(events) if t == "steve.health_low"]

    # Still low, then back above the threshold but within the hysteresis band.
    assert low(3, 10.0) == []
    assert low(5, 20.0) == []
    assert low(3.5, 30.0) == []

    # Recovered to threshold + hysteresis: the next crossing fires again.
    assert low(6, 40.0) == []
    assert low(3.9, 50.0) == ["steve.health_low"]


def test_low_hunger_fires_on_first_observation_and_rearms() -> None:
    detector = ConditionEventDetector()

    first = detector.observe(WorldState(health=20, hunger=5), 0.0)
    assert types(first) == ["steve.hunger_low"]
    assert first[0].severity is EventSeverity.MEDIUM
    assert first[0].payload == {"hunger": 5.0, "threshold": 6.0}

    assert detector.observe(WorldState(health=20, hunger=4), 1.0) == []
    assert detector.observe(WorldState(health=20, hunger=8), 2.0) == []
    assert types(detector.observe(WorldState(health=20, hunger=1), 3.0)) == ["steve.hunger_low"]


def test_values_at_the_threshold_are_not_low() -> None:
    detector = ConditionEventDetector()
    assert detector.observe(WorldState(health=4, hunger=6), 0.0) == []


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, True])
def test_missing_or_non_finite_values_leave_state_unchanged(bad: object) -> None:
    detector = ConditionEventDetector()
    assert detector.observe(WorldState(health=bad, hunger=bad), 0.0) == []  # type: ignore[arg-type]
    # The trigger is still armed, and no damage is measured from the bad value.
    events = detector.observe(WorldState(health=3, hunger=20), 1.0)
    assert types(events) == ["steve.health_low"]


# ── Damage ──────────────────────────────────────────────────────────────


def test_damage_at_least_min_damage_is_reported() -> None:
    detector = ConditionEventDetector()
    events = feed(detector, [(20, 0.0), (19, 1.0), (17, 5.0)])
    # The 1 HP drop is below min_damage (1.5); the 2 HP drop is reported.
    assert types(events) == ["steve.damaged"]
    assert events[0].payload == {
        "health": 17.0,
        "previous_health": 19.0,
        "amount": 2.0,
        "hits": 1,
    }
    assert events[0].severity is EventSeverity.MEDIUM


def test_healing_is_not_damage() -> None:
    detector = ConditionEventDetector()
    assert feed(detector, [(10, 0.0), (18, 1.0)]) == []


def test_sustained_damage_is_rate_limited_and_accumulated() -> None:
    detector = ConditionEventDetector()  # cooldown 2 s
    events = feed(
        detector,
        [
            (20, 0.0),
            (18, 0.25),  # reported immediately
            (16, 0.5),  # within cooldown: held
            (14, 0.75),  # within cooldown: held
            (14, 2.25),  # cooldown elapsed: held damage reported, no new hit
            (14, 3.0),
        ],
    )
    assert types(events) == ["steve.damaged", "steve.damaged"]
    assert events[0].payload["amount"] == 2.0 and events[0].payload["hits"] == 1
    assert events[1].payload == {
        "health": 14.0,
        "previous_health": 18.0,
        "amount": 4.0,
        "hits": 2,
    }


def test_held_damage_is_reported_when_health_is_unreadable() -> None:
    detector = ConditionEventDetector()
    events = feed(detector, [(20, 0.0), (18, 0.1), (15, 0.2), (None, 1.0), (None, 2.5)])
    assert types(events) == ["steve.damaged", "steve.damaged"]
    assert events[1].payload == {
        "health": 15.0,
        "previous_health": 18.0,
        "amount": 3.0,
        "hits": 1,
    }


def test_damage_into_low_health_is_high_severity_and_both_events_fire() -> None:
    detector = ConditionEventDetector()
    events = feed(detector, [(8, 0.0), (3, 1.0)])
    assert types(events) == ["steve.damaged", "steve.health_low"]
    assert events[0].severity is EventSeverity.HIGH


def test_event_ids_are_unique() -> None:
    detector = ConditionEventDetector()
    events = feed(detector, [(20, 0.0), (17, 10.0), (14, 20.0), (11, 30.0)])
    assert len({event.event_id for event in events}) == len(events) == 3


# ── Configuration ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_damage": 0},
        {"min_damage": -1},
        {"damage_cooldown": math.nan},
        {"health_low_threshold": math.inf},
        {"hysteresis": -0.5},
    ],
)
def test_invalid_config_is_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ConditionEventConfig(**kwargs)


def test_preservation_config_maps_its_thresholds() -> None:
    config = PreservationConfig(
        sudden_health_drop=3.0, health_critical_threshold=5.0, hunger_critical_threshold=8
    ).condition_event_config()
    assert (config.min_damage, config.health_low_threshold, config.hunger_low_threshold) == (
        3.0,
        5.0,
        8.0,
    )


# ── Supervisor wiring ───────────────────────────────────────────────────


async def _no_intervention(world: WorldState) -> None:
    return None


async def _run_until(
    supervisor: SafetySupervisor, condition: asyncio.Event, *, timeout: float = 2
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop))
    try:
        await asyncio.wait_for(condition.wait(), timeout)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout)


@pytest.mark.asyncio
async def test_supervisor_journals_condition_events(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    observed = asyncio.Event()
    states = iter([WorldState(health=20, hunger=20), WorldState(health=3, hunger=4)])

    async def observe() -> WorldState:
        state = next(states, None)
        if state is None:
            observed.set()
            return WorldState()
        return state

    supervisor = SafetySupervisor(
        observe,
        _no_intervention,
        interval=0.001,
        event_sink=journal.append,
        steve_id="AIBot",
        condition_events=ConditionEventDetector(steve_id="AIBot"),
    )
    try:
        await _run_until(supervisor, observed)
        await supervisor.close()
        records = journal.get_events("reader")
        assert sorted(r.event.type for r in records) == [
            "steve.damaged",
            "steve.health_low",
            "steve.hunger_low",
        ]
        assert {r.event.steve_id for r in records} == {"AIBot"}
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_no_condition_events_without_an_event_sink() -> None:
    detector = ConditionEventDetector()
    observed = asyncio.Event()

    async def observe() -> WorldState:
        observed.set()
        return WorldState(health=1, hunger=1)

    supervisor = SafetySupervisor(observe, _no_intervention, condition_events=detector)
    await _run_until(supervisor, observed)
    await supervisor.close()
    # Without a sink the detector is not consulted at all, so it stays armed.
    assert types(detector.observe(WorldState(health=1, hunger=20), 0.0)) == ["steve.health_low"]


@pytest.mark.asyncio
async def test_stalled_journal_neither_blocks_polling_nor_grows_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "_MAX_PENDING_CONDITION_EVENTS", 3)
    release = asyncio.Event()
    written: list[Event] = []

    async def sink(event: Event) -> int:
        await release.wait()
        written.append(event)
        return len(written)

    # Every observation alternates hunger low/recovered, so each low one
    # fires a new event.
    ticks = 0
    enough = asyncio.Event()

    async def observe() -> WorldState:
        nonlocal ticks
        ticks += 1
        if ticks >= 20:
            enough.set()
        return WorldState(health=20, hunger=1 if ticks % 2 else 20)

    supervisor = SafetySupervisor(
        observe,
        _no_intervention,
        interval=0.001,
        event_sink=sink,
        condition_events=ConditionEventDetector(),
    )
    await _run_until(supervisor, enough)
    # Polling kept going while the first write was stuck; the backlog is capped.
    assert ticks >= 20
    assert len(supervisor._condition_queue) <= 3
    assert supervisor._condition_dropped > 0

    release.set()
    await asyncio.wait_for(supervisor.close(), 1)
    # The stuck event plus the capped backlog, in order of occurrence.
    assert 1 < len(written) <= 4
    assert not supervisor._condition_queue


@pytest.mark.asyncio
async def test_detector_failure_does_not_stop_reflexes(monkeypatch: pytest.MonkeyPatch) -> None:
    detector = ConditionEventDetector()

    def broken(world: WorldState, now: float) -> list[Event]:
        raise RuntimeError("boom")

    monkeypatch.setattr(detector, "observe", broken)
    evaluated = asyncio.Event()

    async def evaluate(world: WorldState) -> None:
        evaluated.set()

    async def observe() -> WorldState:
        return WorldState(health=1)

    async def sink(event: Event) -> int:
        return 1

    supervisor = SafetySupervisor(
        observe, evaluate, interval=0.001, event_sink=sink, condition_events=detector
    )
    await _run_until(supervisor, evaluated)
    await supervisor.close()
