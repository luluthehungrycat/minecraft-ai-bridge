"""Tests for bounded orchestrator prompt context and guarded retries."""

from __future__ import annotations

from typing import Any

from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.minecraft import ActionResult, ActionType
from minecraft_ai_bridge.minecraft.observer import WorldState
from minecraft_ai_bridge.runtime import ActionService
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockLLMClient


class RecordingLLM(MockLLMClient):
    """Mock LLM that keeps each message batch for prompt assertions."""

    def __init__(self, responses: list[tuple[str, dict[str, Any]]]) -> None:
        super().__init__(responses=responses)
        self.message_batches: list[list[Any]] = []

    async def decide(self, system_prompt: str, messages: list, tool_choice: str = "auto") -> Any:
        self.message_batches.append(messages)
        return await super().decide(system_prompt, messages, tool_choice)


async def _run_steps(
    orch: Orchestrator,
    steps: int,
) -> None:
    for _ in range(steps):
        orch._turn += 1
        if await orch._step():
            break


def _make_orchestrator(mock_mc, monkeypatch, responses: list[tuple[str, dict[str, Any]]]):
    config = AppConfig()
    config.bridge.max_iterations = 10
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orch = Orchestrator(config)
    llm = RecordingLLM(responses)
    orch._llm = llm
    orch._goals._llm = llm
    orch._goals.set_goal_from_subgoals("Test goal", [{"description": "Test task"}])
    orch._mc = mock_mc
    # Mirror the production _connect wiring: one adapter around the mock
    # client plus one shared ActionService, so _act routes through the gate.
    orch._world = McpqWorldAdapter(mock_mc)
    orch._action_service = ActionService(
        orch._world,
        policy=orch._action_policy,
    )
    orch._preservation = None

    async def observe() -> WorldState:
        state = WorldState(position=(0.0, 65.0, 0.0))
        orch._memory.record_observation(state)
        return state

    monkeypatch.setattr(orch, "_observe", observe)
    return orch, llm


async def test_failed_action_exposes_bounded_context_and_retry_hint(mock_mc, monkeypatch):
    orch, llm = _make_orchestrator(
        mock_mc,
        monkeypatch,
        [("not_real", {}), ("wait", {})],
    )
    orch._memory.remember_fact("x" * 5000)

    await _run_steps(orch, 2)

    retry_prompt = "\n".join(message.content for message in llm.message_batches[1])
    assert "Recovery Required" in retry_prompt
    assert "different action" in retry_prompt or "different parameters" in retry_prompt
    assert len(retry_prompt) < 12000


async def test_failed_action_retries_once_on_next_turn(mock_mc, monkeypatch):
    orch, _llm = _make_orchestrator(
        mock_mc,
        monkeypatch,
        [("not_real", {}), ("wait", {})],
    )

    await _run_steps(orch, 2)

    actions = [entry.raw for entry in orch._memory._short_term if entry.raw.startswith("Action:")]
    assert actions[0].startswith("Action: not_real")
    assert actions[1].startswith("Action: wait")
    assert orch._retry_pending is False


async def test_identical_retry_is_rejected_without_second_execution(mock_mc, monkeypatch):
    orch, _llm = _make_orchestrator(
        mock_mc,
        monkeypatch,
        [("not_real", {}), ("not_real", {})],
    )
    calls = 0
    original_act = orch._act

    async def counted_act(response):
        nonlocal calls
        calls += 1
        return await original_act(response)

    monkeypatch.setattr(orch, "_act", counted_act)
    await _run_steps(orch, 2)

    assert calls == 1
    assert any("identical" in entry.raw.lower() for entry in orch._memory._short_term)
    assert orch._retry_pending is False


async def test_retry_budget_is_one(mock_mc, monkeypatch):
    orch, _llm = _make_orchestrator(
        mock_mc,
        monkeypatch,
        [("not_real", {}), ("also_not_real", {}), ("wait", {})],
    )

    await _run_steps(orch, 2)

    assert orch._retry_pending is False
    assert orch._consecutive_failures == 2


async def test_wait_failure_does_not_schedule_retry(mock_mc, monkeypatch):
    orch, _llm = _make_orchestrator(mock_mc, monkeypatch, [("wait", {})])

    async def fail_action(response):

        return ActionResult(
            success=False,
            action=ActionType(response.action),
            message="wait failed",
        )

    monkeypatch.setattr(orch, "_act", fail_action)
    await _run_steps(orch, 1)

    assert orch._retry_pending is False


async def test_done_failure_does_not_schedule_retry(mock_mc, monkeypatch):
    orch, _llm = _make_orchestrator(mock_mc, monkeypatch, [("done", {})])

    async def fail_action(response):

        return ActionResult(
            success=False,
            action=ActionType(response.action),
            message="done failed",
        )

    monkeypatch.setattr(orch, "_act", fail_action)
    await _run_steps(orch, 1)

    assert orch._retry_pending is False
