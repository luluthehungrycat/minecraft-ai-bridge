"""CLI exit codes."""

from __future__ import annotations

import argparse

import pytest

from minecraft_ai_bridge import main as cli


def _args(tmp_path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(tmp_path / "missing.yaml"),
        verbose=False,
        max_iterations=None,
        model=None,
        goal="test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("lost", "expected"), [(False, 0), (True, cli.EXIT_AUTHORITY_LOST)])
async def test_exit_code_reports_authority_loss(tmp_path, monkeypatch, lost, expected):
    class FakeOrchestrator:
        def __init__(self, _config) -> None:
            self.strategic_authority_lost = False

        async def run(self, _goal) -> None:
            self.strategic_authority_lost = lost

    monkeypatch.setattr(cli, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli, "_setup_logging", lambda _verbose: None)

    assert await cli._async_main(_args(tmp_path)) == expected
