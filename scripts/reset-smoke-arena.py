#!/usr/bin/env python3
"""Reset and verify the disposable Hermes player smoke arena over MCPQ.

Operator-only: runs privileged server commands on the world named by
--host/--port. Use it only on a disposable test world, never a shared one.

The arena matches the prompt in scripts/smoke-hermes-player.py: a stone
floor at y=100, a bedrock perimeter at x/z +/-12 (y=101..103), oak logs at
(3, 101..103, 0), a gold block obstacle at (-3, 101, 0), the bot at
(0.5, 101, 0.5) in survival mode with 32 cobblestone, 16 oak planks and
8 bread, and its spawn point at the start.

Difficulty is left unchanged unless --difficulty is given: the profile
trials that hostile mobs would disrupt need peaceful (the smoke runner
checks), while the Phase C gate needs a hostile mob and so easy or harder.

After the commands run, the arena is read back block by block, and the
position and inventory are checked. Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Protocol

DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
HALF = 12
FLOOR_Y = 100
WALL_YS = (101, 102, 103)
CLEAR_TOP = 108
START = (0.5, 101.0, 0.5)
FIXTURES = {
    (3, 101, 0): "oak_log",
    (3, 102, 0): "oak_log",
    (3, 103, 0): "oak_log",
    (-3, 101, 0): "gold_block",
}
INVENTORY = {"cobblestone": 32, "oak_planks": 16, "bread": 8}


class World(Protocol):
    async def run_command_blocking(self, command: str) -> str: ...
    async def get_block(self, x: int, y: int, z: int) -> str: ...
    async def get_player_pos(self) -> tuple[float, float, float] | None: ...


def reset_commands(player: str, difficulty: str | None) -> list[str]:
    """Return the server commands that rebuild the arena, in order."""
    commands = []
    if difficulty is not None:
        commands.append(f"difficulty {difficulty}")
    commands += [
        "time set day",
        f"fill -{HALF} {FLOOR_Y} -{HALF} {HALF} {FLOOR_Y} {HALF} minecraft:stone",
        f"fill -{HALF} {FLOOR_Y + 1} -{HALF} {HALF} {CLEAR_TOP} {HALF} minecraft:air",
        f"fill -{HALF} 101 -{HALF} {HALF} 103 -{HALF} minecraft:bedrock",
        f"fill -{HALF} 101 {HALF} {HALF} 103 {HALF} minecraft:bedrock",
        f"fill -{HALF} 101 -{HALF} -{HALF} 103 {HALF} minecraft:bedrock",
        f"fill {HALF} 101 -{HALF} {HALF} 103 {HALF} minecraft:bedrock",
        f"tp {player} {START[0]} {START[1]} {START[2]}",
        f"gamemode survival {player}",
        f"clear {player}",
        *(f"give {player} minecraft:{item} {count}" for item, count in INVENTORY.items()),
        *(f"setblock {x} {y} {z} minecraft:{kind}" for (x, y, z), kind in FIXTURES.items()),
        f"spawnpoint {player} 0 101 0",
    ]
    return commands


def parse_difficulty(response: str) -> str | None:
    """Read the difficulty from Minecraft's ``difficulty`` query response."""
    match = re.search(r"difficulty is (\w+)", response, re.IGNORECASE)
    return match[1].lower() if match else None


def parse_item_count(response: str) -> int | None:
    """Read the count from a ``clear <player> <item> 0`` response.

    Only the two known forms are accepted, anchored to their wording so digits
    in a player name are never taken for a count. Anything else (player not
    found, command error) is None, and verification fails.
    """
    match = re.match(r"\s*Found (\d+) matching item", response)
    if match:
        return int(match[1])
    if re.match(r"\s*No items were found", response):
        return 0
    return None


def expected_block(x: int, y: int, z: int) -> str | None:
    """The block the arena must hold at (x, y, z), or None if unchecked."""
    if not (-HALF <= x <= HALF and -HALF <= z <= HALF):
        return None
    if y == FLOOR_Y:
        return "stone"
    if (x, y, z) in FIXTURES:
        return FIXTURES[x, y, z]
    if y in WALL_YS:
        return "bedrock" if abs(x) == HALF or abs(z) == HALF else "air"
    # Above the walls the fill clears to air too, but no trial depends on it.
    return None


async def verify(world: World, player: str, difficulty: str | None) -> list[str]:
    """Return every mismatch between the world and the arena; empty means PASS."""
    problems = []
    for x in range(-HALF, HALF + 1):
        for z in range(-HALF, HALF + 1):
            for y in (FLOOR_Y, *WALL_YS):
                want = expected_block(x, y, z)
                if want is None:
                    continue
                got = (await world.get_block(x, y, z)).removeprefix("minecraft:")
                if got != want:
                    problems.append(f"block ({x}, {y}, {z}) is {got}, expected {want}")
    position = await world.get_player_pos()
    if position is None or not all(
        math.isclose(a, b, abs_tol=0.01) for a, b in zip(position, START, strict=True)
    ):
        problems.append(f"{player} is at {position}, expected {START}")
    for item, count in INVENTORY.items():
        # `clear <player> <item> 0` counts matching items without removing them.
        response = await world.run_command_blocking(f"clear {player} minecraft:{item} 0")
        found = parse_item_count(response)
        if found is None:
            problems.append(f"unrecognized inventory response for {item}: {response!r}")
        elif found != count:
            problems.append(f"{player} has {found} {item}, expected {count}")
    actual = parse_difficulty(await world.run_command_blocking("difficulty"))
    if difficulty is not None and actual != difficulty:
        problems.append(f"difficulty is {actual}, expected {difficulty}")
    return problems


async def reset(world: World, player: str, difficulty: str | None) -> dict[str, Any]:
    log = []
    for command in reset_commands(player, difficulty):
        log.append({"command": command, "response": await world.run_command_blocking(command)})
    # Let the teleport and fills settle before reading back.
    await asyncio.sleep(1)
    problems = await verify(world, player, difficulty)
    return {
        "verdict": "FAIL" if problems else "PASS",
        "difficulty": parse_difficulty(await world.run_command_blocking("difficulty")),
        "problems": problems,
        "commands": log,
    }


async def _main(args: argparse.Namespace) -> int:
    from minecraft_ai_bridge.minecraft.mc_api import McpqClient

    world = McpqClient(host=args.host, port=args.port, player_name=args.player)
    await world.connect(retries=3)
    try:
        report = await reset(world, args.player, args.difficulty)
    finally:
        await world.disconnect()
    if args.log is not None:
        args.log.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "commands"}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=os.environ.get("MC_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MC_API_PORT", "1789")))
    parser.add_argument("--player", default=os.environ.get("MC_API_PLAYER_NAME", "SmokeSteve"))
    parser.add_argument(
        "--difficulty",
        choices=DIFFICULTIES,
        help="set this difficulty (default: leave it unchanged)",
    )
    parser.add_argument("--log", type=Path, help="write the full command log and report as JSON")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
