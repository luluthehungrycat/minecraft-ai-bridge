"""Offline lifecycle checks for the operator's real-profile smoke runner and arena reset."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner(monkeypatch, tmp_path, trials, *, difficulties, returncodes=None):
    module = _load("smoke-hermes-player.py", "smoke_hermes_player")
    calls: list[str] = []
    checks: list[str] = []
    readings = iter(difficulties)

    def run_trial(name, output, profile):
        calls.append(name)
        return {"returncode": (returncodes or {}).get(name, 0), "error": None}

    def current_difficulty(host, port):
        checks.append(calls[-1] if calls else "")
        return next(readings)

    monkeypatch.setattr(module, "run_trial", run_trial)
    monkeypatch.setattr(module, "current_difficulty", current_difficulty)
    monkeypatch.setattr(
        "sys.argv", [str(SCRIPTS / "smoke-hermes-player.py"), "--output", str(tmp_path), *trials]
    )
    return module, calls, checks


@pytest.mark.parametrize("first_returncode", [0, 124])
def test_smoke_batch_stops_after_unresolved_timeout(
    tmp_path, monkeypatch, capsys, first_returncode
):
    module, calls, _ = _runner(
        monkeypatch,
        tmp_path,
        ["wall", "navigation"],
        difficulties=["peaceful", "peaceful"],
        returncodes={"wall": first_returncode},
    )
    if first_returncode == 124:
        with pytest.raises(SystemExit, match="1"):
            module.main()
        assert calls == ["wall"]
        assert "reconcile" in capsys.readouterr().err.lower()
    else:
        module.main()
        assert calls == ["wall", "navigation"]


@pytest.mark.parametrize("difficulty", ["easy", "hard", None])
def test_mob_sensitive_trial_refused_unless_peaceful(tmp_path, monkeypatch, capsys, difficulty):
    module, calls, _ = _runner(monkeypatch, tmp_path, ["navigation"], difficulties=[difficulty])
    with pytest.raises(SystemExit, match="1"):
        module.main()
    assert calls == []
    err = capsys.readouterr().err
    assert "needs peaceful" in err
    assert "reset-smoke-arena.py --difficulty peaceful" in err


def test_difficulty_is_rechecked_before_each_mob_sensitive_trial(tmp_path, monkeypatch):
    # An earlier trial or operator test may change difficulty mid-batch.
    module, calls, checks = _runner(
        monkeypatch,
        tmp_path,
        ["wall", "navigation", "gather_craft"],
        difficulties=["peaceful", "easy", "peaceful"],
    )
    with pytest.raises(SystemExit, match="1"):
        module.main()
    assert calls == ["wall"]
    assert len(checks) == 2


def test_mob_insensitive_trials_skip_the_difficulty_check(tmp_path, monkeypatch):
    module, calls, checks = _runner(
        monkeypatch, tmp_path, ["policy", "containment"], difficulties=[]
    )
    module.main()
    assert calls == ["policy", "containment"]
    assert checks == []


def test_every_trial_is_classified():
    module = _load("smoke-hermes-player.py", "smoke_hermes_player")
    assert set(module.TRIALS) >= module.PEACEFUL_TRIALS
    assert set(module.TRIALS) - module.PEACEFUL_TRIALS == {"policy", "containment"}


# ── Arena reset ────────────────────────────────────────────────────────────


class _ArenaWorld:
    """A world already holding the correct arena; tests then break it."""

    def __init__(self, reset) -> None:
        self.reset = reset
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.position: tuple[float, float, float] | None = reset.START
        self.inventory = dict(reset.INVENTORY)
        self.difficulty = "peaceful"
        self.commands: list[str] = []

    async def get_block(self, x, y, z):
        default = self.reset.expected_block(x, y, z) or "air"
        return "minecraft:" + self.blocks.get((x, y, z), default)

    async def get_player_pos(self):
        return self.position

    async def run_command_blocking(self, command):
        self.commands.append(command)
        if command == "difficulty":
            return f"The difficulty is {self.difficulty.capitalize()}"
        if command.startswith("clear ") and command.endswith(" 0"):
            item = command.split()[2].removeprefix("minecraft:")
            player = command.split()[1]
            count = self.inventory.get(item, 0)
            if count:
                return f"Found {count} matching item(s) on player {player}"
            return f"No items were found on player {player}"
        return ""


def test_reset_commands_leave_difficulty_unchanged_by_default():
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    commands = reset.reset_commands("SmokeSteve", None)
    assert not any(c.startswith("difficulty") for c in commands)
    assert reset.reset_commands("SmokeSteve", "peaceful")[0] == "difficulty peaceful"
    assert reset.reset_commands("SmokeSteve", "easy")[0] == "difficulty easy"
    assert "tp SmokeSteve 0.5 101.0 0.5" in commands
    assert "setblock 3 101 0 minecraft:oak_log" in commands


def test_reset_layout_matches_the_runner_prompt():
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    runner = _load("smoke-hermes-player.py", "smoke_hermes_player")
    assert "stone floor at y=100" in runner.ARENA and reset.FLOOR_Y == 100
    assert "bedrock walls at x/z +/-12" in runner.ARENA and reset.HALF == 12
    assert "starting position (0.5,101,0.5)" in runner.ARENA
    assert reset.START == (0.5, 101.0, 0.5)
    assert {(3, 101, 0), (3, 102, 0), (3, 103, 0)} <= set(reset.FIXTURES)
    assert "(3,101,0), (3,102,0), (3,103,0)" in runner.TRIALS["gather_craft"]


async def test_verify_passes_on_a_correct_arena():
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    world = _ArenaWorld(reset)
    assert await reset.verify(world, "SmokeSteve", "peaceful") == []
    assert await reset.verify(world, "SmokeSteve", None) == []


async def test_verify_reports_every_mismatch():
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    world = _ArenaWorld(reset)
    world.blocks[(4, 100, -2)] = "air"  # blast hole in the floor
    world.blocks[(12, 102, 5)] = "air"  # breach in the wall
    world.blocks[(1, 101, 1)] = "creeper_head"  # debris in the play space
    world.blocks[(3, 102, 0)] = "air"  # missing log
    world.position = (99.5, 144.0, -35.5)  # respawned away from the arena
    world.inventory["bread"] = 0
    world.difficulty = "easy"
    problems = await reset.verify(world, "SmokeSteve", "peaceful")
    text = "\n".join(problems)
    for expected in (
        "block (4, 100, -2) is air, expected stone",
        "block (12, 102, 5) is air, expected bedrock",
        "block (1, 101, 1) is creeper_head, expected air",
        "block (3, 102, 0) is air, expected oak_log",
        "SmokeSteve is at (99.5, 144.0, -35.5)",
        "SmokeSteve has 0 bread, expected 8",
        "difficulty is easy, expected peaceful",
    ):
        assert expected in text, text
    assert len(problems) == 7


async def test_verify_ignores_difficulty_when_not_requested():
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    world = _ArenaWorld(reset)
    world.difficulty = "easy"
    assert await reset.verify(world, "SmokeSteve", None) == []


async def test_reset_runs_commands_then_reports(monkeypatch):
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(reset.asyncio, "sleep", no_sleep)
    world = _ArenaWorld(reset)
    report = await reset.reset(world, "SmokeSteve", "peaceful")
    assert report["verdict"] == "PASS"
    assert report["difficulty"] == "peaceful"
    sent = [entry["command"] for entry in report["commands"]]
    assert sent == reset.reset_commands("SmokeSteve", "peaceful")
    assert world.commands[: len(sent)] == sent


async def test_verify_inventory_ignores_digits_in_the_player_name():
    # "No items were found on player Steve8" must read as 0, not 8.
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    world = _ArenaWorld(reset)
    world.inventory["bread"] = 0
    problems = await reset.verify(world, "Steve8", None)
    assert problems == ["Steve8 has 0 bread, expected 8"]


@pytest.mark.parametrize("response", ["No player was found", "Unknown or incomplete command", ""])
async def test_verify_rejects_unrecognized_inventory_responses(monkeypatch, response):
    reset = _load("reset-smoke-arena.py", "reset_smoke_arena")
    world = _ArenaWorld(reset)
    original = world.run_command_blocking

    async def run(command):
        if command.startswith("clear ") and command.endswith(" 0"):
            return response
        return await original(command)

    monkeypatch.setattr(world, "run_command_blocking", run)
    problems = await reset.verify(world, "SmokeSteve", None)
    assert len(problems) == len(reset.INVENTORY)
    assert all("unrecognized inventory response" in p for p in problems), problems
