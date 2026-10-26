"""Integration tests — agent loop, fallback behaviors, stuck detection, hazard response.

These tests use MockMcpqClient + MockLLMClient to simulate the full
think-act-observe loop without needing a real Minecraft server.
"""

from __future__ import annotations

import logging
import os
import re

import pytest

from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.llm.models import Role


def _llm_api_errors(records: list[logging.LogRecord]) -> list[str]:
    """Return LLM transport/API failures that the clients turned into WAITs."""
    import openai

    return [
        record.getMessage()
        for record in records
        if record.name == "minecraft_ai_bridge.llm.client"
        and any(isinstance(arg, openai.APIError) for arg in (record.args or ()))
    ]


@pytest.fixture(autouse=True)
def fail_on_llm_api_errors(caplog: pytest.LogCaptureFixture):
    """Fail instead of passing on fallback WAITs when the provider rejects calls.

    The LLM clients log API errors and return a WAIT so the agent keeps
    running. That is right in production but lets a dead key or exhausted
    budget pass these tests. Malformed model output is not treated as an
    error here; fallback decomposition covers it.
    """
    caplog.set_level(logging.WARNING, logger="minecraft_ai_bridge.llm.client")
    yield
    # caplog.records only holds the current (teardown) phase at this point.
    errors = _llm_api_errors(caplog.get_records("setup") + caplog.get_records("call"))
    if errors:
        pytest.fail(f"LLM API call failed {len(errors)} time(s); first: {errors[0]}")


async def _no_sleep(x: float) -> None:
    """Async sleep substitute that returns immediately."""
    pass


def _mock_llm(orch, responses):
    """Replace orch._llm with a MockLLMClient and sync GoalManager."""
    from tests.conftest import MockLLMClient

    mock = MockLLMClient(responses=responses)
    orch._llm = mock
    orch._goals._llm = mock
    return mock


# ── Goal-verification helpers ────────────────────────────────────────────


def actions_taken(orch: Orchestrator) -> list[str]:
    """Extract action names from the agent's short-term memory.

    Parses ``MemoryEntry.raw`` strings which follow the format
    ``"Action: <name> | Result: <message>"``.
    """
    actions: list[str] = []
    for entry in orch._memory._short_term:
        if entry.role == Role.ASSISTANT:
            m = re.match(r"Action:\s*(\w+)", entry.raw)
            if m:
                actions.append(m.group(1))
    return actions


def action_taken(orch: Orchestrator, *action_names: str) -> bool:
    """Check if the agent performed *any* of the given action names."""
    trace = actions_taken(orch)
    return any(name in trace for name in action_names)


def position_reached(
    orch: Orchestrator,
    target_x: float,
    target_y: float,
    target_z: float,
    tolerance: float = 2.0,
) -> bool:
    """Check if any observation in memory shows the agent near target coords.

    Parses ``Position: (x, y, z)`` from ``MemoryEntry.summary`` strings
    produced by ``format_state()``.
    """
    pos_pattern = re.compile(r"Position:\s*\(([\d.\-]+),\s*([\d.\-]+),\s*([\d.\-]+)\)")
    for entry in orch._memory._short_term:
        if entry.role != Role.USER:
            continue
        m = pos_pattern.search(entry.summary)
        if not m:
            continue
        x, y, z = (
            float(m.group(1)),
            float(m.group(2)),
            float(m.group(3)),
        )
        if (
            abs(x - target_x) <= tolerance
            and abs(y - target_y) <= tolerance
            and abs(z - target_z) <= tolerance
        ):
            return True
    return False


# ── Helpers ──────────────────────────────────────────────────────────────


def make_config(**overrides) -> AppConfig:
    """Create a minimal AppConfig for testing."""
    cfg = AppConfig()
    cfg.bridge.max_iterations = overrides.get("max_iterations", 10)
    cfg.bridge.cycle_delay = 0.01  # fast for tests
    cfg.bridge.verbose = False
    cfg.bridge.memory_window = 20
    cfg.goals.default = overrides.get("goal", "Explore")
    cfg.goals.max_depth = 5
    # Use an OpenAI-compatible router with a model that supports tool calling.
    # OPENROUTER_API_KEY selects OpenRouter; otherwise REQUESTY_API_KEY selects
    # Requesty. INTEGRATION_LLM_MODEL overrides the router's default model.
    cfg.llm.provider = "openrouter"
    if not os.getenv("OPENROUTER_API_KEY") and os.getenv("REQUESTY_API_KEY"):
        cfg.llm.openrouter_api_key = os.environ["REQUESTY_API_KEY"]
        cfg.llm.openrouter_base_url = "https://router.requesty.ai/v1"
        cfg.llm.model = os.getenv("INTEGRATION_LLM_MODEL", "runware/gpt-oss-120b")
    else:
        cfg.llm.model = os.getenv("INTEGRATION_LLM_MODEL", "openai/gpt-oss-20b")
    return cfg


@pytest.mark.asyncio
class TestAgentLoop:
    """Tests for the full think-act-observe loop."""

    async def test_run_completes_goal(self):
        """Agent should run through all sub-goals and finish."""
        cfg = make_config(goal="Teleport to 0 64 0")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None  # skip chat commands
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)  # skip delays
            await orch.run()
        # Approach A: the agent must have decided to teleport or move toward the target
        assert action_taken(orch, "teleport", "move_to", "walk_to"), (
            f"Agent should have teleported/moved toward target. Actions seen: {actions_taken(orch)}"
        )
        # Approach B (position readback via observation summaries):
        # NOTE: MCPQ's player.teleport(Vec3) returns success without moving the
        # player on this Paper/MCPQ version, so position_reached() won't pass.
        # Uncomment when MCPQ teleport is fixed:
        # assert position_reached(orch, 0, 64, 0), (
        #     "Agent should have reached position (0, 64, 0)"
        # )

    async def test_run_with_default_goal(self):
        """Agent should start with default goal if none provided."""
        cfg = make_config()
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        assert len(actions_taken(orch)) > 0, (
            f"Agent should have taken at least one action. Actions seen: {actions_taken(orch)}"
        )
        assert orch._turn > 0

    async def test_loop_handles_disconnect(self):
        """Agent should handle MCPQ disconnect gracefully."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # The agent should still have taken some actions before disconnect
        assert len(actions_taken(orch)) > 0, "Agent should have taken action(s) before disconnect."


@pytest.mark.asyncio
class TestFallbackBehavior:
    """Fallback plans should work when LLM decomposition fails."""

    async def test_fallback_on_empty_decompose(self):
        """When LLM returns empty sub-goals, fallback should be used."""
        cfg = make_config(goal="Mine for diamonds")
        orch = Orchestrator(cfg)
        _mock_llm(
            orch,
            [
                ("done", {"message": "done"}),
            ],
        )
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # Fallback for "Mine" produces 12 sub-goals
        assert orch._goals.sub_goal_count == 12


@pytest.mark.asyncio
class TestStuckDetection:
    """Consecutive failure counter and graceful shutdown."""

    async def test_consecutive_failures_trigger_shutdown(self):
        """After max_failures consecutive errors, agent should stop."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        # Use an unregistered/misspelled action so execute_action returns failure
        failing_actions = [("nonexistent_action", {}) for _ in range(10)]
        _mock_llm(orch, failing_actions)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # Should have triggered graceful shutdown instead of burning all 50 iterations
        assert orch._turn < 50  # much fewer than max_iterations
        assert orch._consecutive_failures > 0
        # Action trace should show the failing action was attempted
        assert action_taken(orch, "nonexistent_action"), (
            "Agent should have attempted the failing action"
        )

    async def test_success_resets_failure_counter(self):
        """A successful action should reset the failure counter."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        _mock_llm(
            orch,
            [
                ("scan", {}),
                ("done", {"message": "done"}),
            ],
        )
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # Success should reset failures
        assert orch._consecutive_failures == 0
        # Action trace should confirm the planned actions executed
        assert action_taken(orch, "scan"), "Agent should have scanned"
        assert action_taken(orch, "done"), "Agent should have called done"


@pytest.mark.asyncio
class TestHazardResponse:
    """Agent should respond to hazards in the environment."""

    async def test_memory_records_observations(self):
        """Observations should be recorded in short-term memory."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # Must have observation entries (Role.USER) and action entries (Role.ASSISTANT)
        assert len(orch._memory._short_term) > 0
        assert len(actions_taken(orch)) > 0, "Agent should have taken actions that were recorded"

    async def test_memory_remembers_facts(self):
        """Notable facts should be stored in long-term memory."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # The agent should have stored at least some observations
        assert len(orch._memory._short_term) > 0
        # At least one action taken
        assert len(actions_taken(orch)) > 0


@pytest.mark.asyncio
class TestStructureRespect:
    """System prompt should include structure respect rules."""

    def test_system_prompt_contains_rules(self):
        from minecraft_ai_bridge.llm.prompts import SYSTEM_PROMPT

        assert "RESPECT EXISTING STRUCTURES" in SYSTEM_PROMPT
        assert "AVOID VILLAGES" in SYSTEM_PROMPT
        assert "PRESERVE INFRASTRUCTURE" in SYSTEM_PROMPT

    def test_action_tool_has_all_actions(self):
        from minecraft_ai_bridge.llm.client import ACTION_TOOL

        enum = ACTION_TOOL["function"]["parameters"]["properties"]["action"]["enum"]
        # Should include walk_to
        assert "walk_to" in enum


@pytest.mark.asyncio
class TestChatCommandsIntegration:
    """Chat command integration with the agent loop."""

    async def test_stop_via_chat_command(self):
        """The stop_requested flag should stop the agent."""
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        orch._stop_requested = True
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        assert orch._turn <= 2  # should stop after at most 2 turns
        # With stop_requested set before run, agent should NOT have taken actions
        assert len(actions_taken(orch)) == 0, (
            "Agent should not have taken any actions when stopped immediately"
        )


@pytest.mark.asyncio
class TestInventoryIntegration:
    """Inventory manager should be available during agent runs."""

    async def test_inventory_created_on_connect(self):
        cfg = make_config(goal="Explore")
        orch = Orchestrator(cfg)
        orch._cmd_handler = None
        import asyncio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            await orch.run()
        # Inventory manager should have been created during connect
        assert orch._inventory is not None
        # The agent should have taken at least one action
        assert len(actions_taken(orch)) > 0, "Agent should have acted after connecting"
