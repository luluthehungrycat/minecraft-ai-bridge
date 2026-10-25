"""Deterministic tests for durable safety intervention event publication.

Scope note: this slice covers the `safety.intervened` producer only.
Other RFC-0001 event producers (attacked, starvation, death, backend
disconnect, task lifecycle, ...) remain unimplemented.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.llm.models import LLMResponse
from minecraft_ai_bridge.minecraft.actions import ActionResult, ActionType
from minecraft_ai_bridge.minecraft.observer import InventorySlot, WorldState
from minecraft_ai_bridge.runtime import Event, EventJournal, EventSeverity, SafetySupervisor
from tests.conftest import MockMcpqClient


def make_event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        type="safety.intervened",
        severity=EventSeverity.HIGH,
        source="test",
    )


async def _run_one_intervention(
    supervisor: SafetySupervisor, stop_event: asyncio.Event
) -> ActionResult:
    task = asyncio.create_task(supervisor.run(stop_event))
    result = await asyncio.wait_for(supervisor.wait_for_intervention(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    return result


@pytest.mark.asyncio
async def test_supervisor_appends_validated_safety_event_to_journal(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    intervention = ActionResult(success=True, action=ActionType.HEAL)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return intervention

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=journal.append)
    stop_event = asyncio.Event()
    result = await _run_one_intervention(supervisor, stop_event)

    assert result is intervention
    await supervisor.close()
    records = journal.get_events("consumer-a")
    assert len(records) == 1
    event = records[0].event
    assert event.type == "safety.intervened"
    assert event.severity is EventSeverity.HIGH
    assert event.payload == {"action": "heal", "success": True}
    assert event.steve_id is None
    assert event.task_id is None
    assert event.causal_action_id is None
    journal.close()


@pytest.mark.asyncio
async def test_failed_intervention_event_uses_critical_severity(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    intervention = ActionResult(success=False, action=ActionType.ATTACK)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return intervention

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=journal.append)
    stop_event = asyncio.Event()
    result = await _run_one_intervention(supervisor, stop_event)

    assert result is intervention
    await supervisor.close()
    records = journal.get_events("consumer-a")
    assert len(records) == 1
    event = records[0].event
    assert event.severity is EventSeverity.CRITICAL
    assert event.payload == {"action": "attack", "success": False}
    journal.close()


@pytest.mark.asyncio
async def test_supervisor_stops_promptly_with_unconsumed_intervention() -> None:
    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return ActionResult(success=True, action=ActionType.HEAL)

    supervisor = SafetySupervisor(observe, evaluate, interval=60)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(supervisor._intervention_ready.wait(), timeout=1)

    stop_event.set()
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_completed_reflex_is_recorded_when_shutdown_starts_during_evaluation() -> None:
    evaluation_started = asyncio.Event()
    release_evaluation = asyncio.Event()
    events: list[Event] = []

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        evaluation_started.set()
        await release_evaluation.wait()
        return ActionResult(success=True, action=ActionType.HEAL)

    async def event_sink(event: Event) -> None:
        events.append(event)

    supervisor = SafetySupervisor(observe, evaluate, event_sink=event_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(evaluation_started.wait(), timeout=1)
    stop_event.set()
    release_evaluation.set()
    await asyncio.wait_for(task, timeout=1)
    await supervisor.close()

    assert len(events) == 1
    assert events[0].payload == {"action": "heal", "success": True}
    assert events[0].severity is EventSeverity.HIGH


@pytest.mark.asyncio
async def test_failing_event_sink_does_not_block_or_suppress_intervention() -> None:
    sink_calls = 0

    async def failing_sink(_event: Event) -> None:
        nonlocal sink_calls
        sink_calls += 1
        raise RuntimeError("journal unavailable")

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return ActionResult(success=True, action=ActionType.EAT)

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=failing_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    result = await asyncio.wait_for(supervisor.wait_for_intervention(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert result.success
    assert result.action is ActionType.EAT
    await supervisor.close()
    assert sink_calls == 1


@pytest.mark.asyncio
async def test_no_event_is_published_when_evaluation_returns_none(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    evaluations = 0

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        nonlocal evaluations
        evaluations += 1
        return None

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=journal.append)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert evaluations >= 1
    assert journal.retained_event_count == 0
    await supervisor.close()
    journal.close()


@pytest.mark.asyncio
async def test_slow_event_write_does_not_delay_intervention_consumption(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    release_write = asyncio.Event()

    async def slow_sink(_event: Event) -> None:
        await release_write.wait()
        journal.append(_event)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return ActionResult(success=True, action=ActionType.HEAL)

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=slow_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))

    result = await asyncio.wait_for(supervisor.wait_for_intervention(), timeout=0.2)
    assert result.action is ActionType.HEAL

    release_write.set()
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    for _ in range(100):
        if journal.retained_event_count == 1:
            break
        await asyncio.sleep(0.01)
    assert journal.retained_event_count == 1
    await supervisor.close()
    journal.close()


@pytest.mark.asyncio
async def test_close_joins_outstanding_event_writes(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def slow_sink(_event: Event) -> None:
        write_started.set()
        await release_write.wait()
        journal.append(_event)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return ActionResult(success=True, action=ActionType.HEAL)

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=slow_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(supervisor.wait_for_intervention(), timeout=1)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    close_task = asyncio.create_task(supervisor.close())
    await asyncio.wait_for(write_started.wait(), timeout=1)
    assert journal.retained_event_count == 0

    release_write.set()
    await asyncio.wait_for(close_task, timeout=1)
    assert journal.retained_event_count == 1
    journal.close()


@pytest.mark.asyncio
async def test_slow_event_sink_coalesces_to_one_bounded_pending_write() -> None:
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    five_evaluations = asyncio.Event()
    events: list[Event] = []
    evaluations = 0

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult:
        nonlocal evaluations
        evaluations += 1
        if evaluations >= 5:
            five_evaluations.set()
        action = ActionType.HEAL if evaluations % 2 else ActionType.EAT
        return ActionResult(success=True, action=action)

    async def slow_sink(event: Event) -> None:
        events.append(event)
        if len(events) == 1:
            write_started.set()
            await release_write.wait()

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=slow_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(write_started.wait(), timeout=1)
    await asyncio.wait_for(five_evaluations.wait(), timeout=1)

    assert len(supervisor._event_writes) == 1
    assert supervisor._coalesced_event is not None
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    release_write.set()
    await supervisor.close()

    assert len(events) == 2
    assert events[-1].payload["action"] == ("heal" if evaluations % 2 else "eat")


@pytest.mark.asyncio
async def test_coalescing_preserves_failed_safety_interventions() -> None:
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    three_evaluations = asyncio.Event()
    events: list[Event] = []
    evaluations = 0

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            result = ActionResult(success=True, action=ActionType.HEAL)
        elif evaluations == 2:
            result = ActionResult(success=False, action=ActionType.ATTACK)
        else:
            result = ActionResult(success=True, action=ActionType.EAT)
            three_evaluations.set()
        return result

    async def slow_sink(event: Event) -> None:
        events.append(event)
        if len(events) == 1:
            write_started.set()
            await release_write.wait()

    supervisor = SafetySupervisor(observe, evaluate, interval=0.01, event_sink=slow_sink)
    stop_event = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(write_started.wait(), timeout=1)
    await asyncio.wait_for(three_evaluations.wait(), timeout=1)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    release_write.set()
    await supervisor.close()

    assert len(events) == 2
    assert events[1].severity is EventSeverity.CRITICAL
    assert events[1].payload == {
        "action": "attack",
        "success": False,
        "coalesced_count": 2,
        "outcome_counts": {
            "attack": {"failed": 1, "succeeded": 0},
            "eat": {"failed": 0, "succeeded": 1},
        },
    }


@pytest.mark.asyncio
async def test_sync_sink_runs_off_loop_and_each_intervention_has_unique_id() -> None:
    events: list[Event] = []
    sink_thread_ids: list[int] = []

    def sync_sink(event: Event) -> None:
        sink_thread_ids.append(threading.get_ident())
        events.append(event)

    async def observe() -> WorldState:
        return WorldState()

    async def evaluate(_world: WorldState) -> ActionResult | None:
        return None

    supervisor = SafetySupervisor(observe, evaluate, event_sink=sync_sink)
    result = ActionResult(success=True, action=ActionType.HEAL)
    supervisor._record_intervention_event(result)
    supervisor._record_intervention_event(result)
    await supervisor.close()

    assert len(events) == 2
    assert events[0].event_id != events[1].event_id
    assert all(thread_id != threading.get_ident() for thread_id in sink_thread_ids)


class BlockingThenWaitLLM:
    """LLM double: blocks the first decision, then returns a WAIT action."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def decide(
        self, system_prompt: str, messages: list, tool_choice: str = "auto"
    ) -> LLMResponse:
        del system_prompt, messages, tool_choice
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return LLMResponse(action="wait")

    async def decompose_goal(self, goal: str) -> list:
        del goal
        return []


async def _noop_async() -> None:
    return None


@pytest.mark.asyncio
async def test_orchestrator_uses_configured_journal_path_and_closes_it(
    mock_mc: MockMcpqClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    journal_path = tmp_path / "durable" / "safety-events.sqlite3"
    config = AppConfig()
    config.bridge.event_journal_path = str(journal_path)
    config.bridge.max_iterations = 1
    config.bridge.cycle_delay = 0
    orch = Orchestrator(config)
    llm = BlockingThenWaitLLM()
    orch._llm = llm
    orch._goals._llm = llm

    async def no_goal(_goal: str) -> None:
        return None

    monkeypatch.setattr(orch._goals, "set_goal", no_goal)
    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.McpqClient", lambda **_: mock_mc)
    monkeypatch.setattr(orch, "_prepare_safe_spawn", _noop_async)

    observation_state = {"critical": False}
    healthy_state = WorldState(position=(0.0, 65.0, 0.0))
    critical_state = WorldState(
        health=2.0,
        hunger=20,
        inventory=[InventorySlot(item_id="golden_apple", count=1, slot=0)],
    )

    async def observe() -> WorldState:
        state = critical_state if observation_state["critical"] else healthy_state
        orch._memory.record_observation(state)
        return state

    monkeypatch.setattr(orch, "_observe", observe)
    monkeypatch.setattr(orch, "_observe_for_safety", observe)

    run_task = asyncio.create_task(orch.run("stay alive"))
    await asyncio.wait_for(llm.started.wait(), timeout=1)

    # The supervisor captured this async observer during startup. Change its
    # backing state so its next poll sees critical health and preempts the LLM.
    observation_state["critical"] = True
    await asyncio.wait_for(llm.cancelled.wait(), timeout=2)
    await asyncio.wait_for(run_task, timeout=5)

    assert journal_path.exists()
    reopened = EventJournal(journal_path)
    records = reopened.get_events("consumer-a")
    assert len(records) == 1
    event = records[0].event
    assert event.type == "safety.intervened"
    assert event.severity is EventSeverity.HIGH
    assert event.payload == {"action": "heal", "success": True}
    reopened.close()
    assert orch._event_journal is None


@pytest.mark.asyncio
async def test_orchestrator_cancellation_during_connect_closes_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    journal_path = tmp_path / "safety-events.sqlite3"
    orch = Orchestrator(AppConfig())

    async def interrupted_connect() -> None:
        orch._event_journal = EventJournal(journal_path)
        await asyncio.Event().wait()

    monkeypatch.setattr(orch, "_connect", interrupted_connect)
    run_task = asyncio.create_task(orch.run("stay alive"))
    while orch._event_journal is None:
        await asyncio.sleep(0)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert orch._event_journal is None
    with EventJournal(journal_path) as reopened:
        assert reopened.retained_event_count == 0


def test_bridge_config_defaults_event_journal_under_data_dir() -> None:
    config = AppConfig()
    assert config.bridge.event_journal_path == "data/safety-events.sqlite3"


def test_event_journal_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_EVENT_JOURNAL_PATH", "/tmp/other-events.sqlite3")
    assert AppConfig().bridge.event_journal_path == "/tmp/other-events.sqlite3"


def test_memory_journal_is_supported() -> None:
    with EventJournal(":memory:") as journal:
        journal.append(make_event("event-mem"))
        assert journal.retained_event_count == 1
