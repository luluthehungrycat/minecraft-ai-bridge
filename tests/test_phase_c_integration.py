"""RFC-0001 Phase C gate, end to end against a live Paper/MCPQ server.

The gate: an emergency response is triggered without any LLM answer and
continues across an external-client disconnect, and replayed commands do
not cause duplicate mutations.

``minecraft-ai-bridge-mcp`` has no LLM at all, so every reflex here is
LLM-free. The test:

1. connects a controller over Streamable HTTP, places a block with a fixed
   request ID, and drops the connection without closing its session;
2. with no client connected, makes a zombie hit the bot and checks the
   attack reflex fights back (the zombie is hurt);
3. restarts the server process, reconnects, and checks the reflex's events
   were journaled durably;
4. removes the placed block out of band and replays the original request ID
   on the restarted server. Within the original (still leased) session the
   original result comes back; from a new session the request is refused,
   because idempotency records are bound to the session and fencing
   generation. Either way the block is not placed again.

Skips when no MCPQ server is reachable; CI excludes it with
``-k "not integration"``.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from minecraft_ai_bridge.minecraft.mc_api import McpqClient  # noqa: E402
from tests.test_mcp_integration import (  # noqa: E402
    MC_HOST,
    MC_PORT,
    PLAYER,
    _free_port,
    _mcpq_reachable,
    stack,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.skipif(not _mcpq_reachable(), reason="no MCPQ server reachable")

# Long enough to outlive the server restart, so the original session resumes.
LEASE_SECONDS = 120
ZOMBIE_TAG = "phase_c_gate"
_HEALTH = re.compile(r"has the following entity data: (-?[\d.]+)f")


@asynccontextmanager
async def _server(setup: dict, port: int) -> AsyncIterator[str]:
    """Run minecraft-ai-bridge-mcp over HTTP until the block exits."""
    env = {key: value for key, value in setup["env"].items() if key != "MAB_CONTROLLER_TOKEN"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "minecraft_ai_bridge.transport.mcp_main",
            "--transport",
            "http",
            "--port",
            str(port),
            "--session-lease-seconds",
            str(LEASE_SECONDS),
            *setup["args"],
        ],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        for _ in range(150):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if process.poll() is not None:
                    pytest.fail(f"MCP server exited with {process.returncode}")
                await asyncio.sleep(0.2)
        else:
            pytest.fail("MCP HTTP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@asynccontextmanager
async def _client(url: str, token: str) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with Client(streamable_http_client(url, http_client=http)) as client:
        yield client


async def _call(client: Client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


async def _open_session(client: Client) -> dict:
    """Open a session for the controller."""
    return await _call(client, "minecraft_open_session", {})


async def _submit(client: Client, session: dict, request_id: str, action: str, params: dict):
    return await _call(
        client,
        "minecraft_submit_action",
        {
            "session_id": session["session_id"],
            "generation": session["generation"],
            "request_id": request_id,
            "action": action,
            "params": params,
        },
    )


async def _zombie_health(mc: McpqClient) -> float | None:
    match = _HEALTH.search(
        await mc.run_command_blocking(f"data get entity @e[tag={ZOMBIE_TAG},limit=1] Health")
    )
    return float(match.group(1)) if match else None


@pytest.fixture
async def world():
    """The harness's own MCPQ connection: puts the bot somewhere safe, cleans up."""
    mc = McpqClient(host=MC_HOST, port=MC_PORT, player_name=PLAYER)
    await mc.connect(retries=3)
    raw = mc._mc
    assert raw is not None
    # A freshly summoned bot can start inside the ground; stand it on the
    # surface near spawn, at night so the test zombie does not burn.
    surface = await asyncio.to_thread(raw.getHeight, 0, 0)
    await mc.run_command_blocking(f"tp {PLAYER} 0.5 {surface + 1} 0.5")
    await mc.run_command_blocking(f"effect clear {PLAYER}")
    await mc.run_command_blocking("time set night")
    # Let spawn/teleport invulnerability wear off before anything hits the bot.
    await asyncio.sleep(6)
    try:
        yield mc
    finally:
        await mc.run_command_blocking(f"kill @e[tag={ZOMBIE_TAG}]")
        await mc.run_command_blocking("time set day")
        await mc.disconnect()


async def test_phase_c_gate(stack, world, tmp_path):  # noqa: F811 - pytest fixture
    port = _free_port()
    token = stack["token"]

    # 1. A controller acts, then disconnects without closing its session.
    async with _server(stack, port) as url:
        async with _client(url, token) as client:
            session = await _open_session(client)
            state = await _call(client, "minecraft_get_state", {})
            x, y, z = (int(v) for v in state["position"])
            target = {"x": x + 2, "y": y, "z": z, "block_type": "stone"}
            await world.run_command_blocking(f"setblock {x + 2} {y} {z} air")
            placed = await _submit(client, session, "gate-place-1", "place_block", target)
            assert placed["status"] == "succeeded", placed
            # MCPQ applies block changes on a later server tick.
            async with asyncio.timeout(10):
                while await _block(world, x + 2, y, z) != "stone":
                    await asyncio.sleep(0.25)
        # The client is gone; its session is still leased.

        # 2. Emergency with no client connected and no LLM anywhere.
        await world.run_command_blocking(
            f"execute at {PLAYER} run summon minecraft:zombie ~3 ~ ~ "
            f'{{NoAI:1b,PersistenceRequired:1b,Tags:["{ZOMBIE_TAG}"]}}'
        )
        await asyncio.sleep(1)
        before = await _zombie_health(world)
        assert before is not None, "the test zombie did not spawn"
        hit = await world.run_command_blocking(
            f"damage {PLAYER} 4 minecraft:mob_attack by @e[tag={ZOMBIE_TAG},limit=1]"
        )
        assert hit.startswith("Applied"), hit
        async with asyncio.timeout(30):
            while True:
                after = await _zombie_health(world)
                if after is None or after < before:
                    break  # the reflex hit (or killed) the zombie
                await asyncio.sleep(0.5)

    # 3. A restarted server still has the reflex's events.
    async with _server(stack, port) as url, _client(url, token) as client:
        async with asyncio.timeout(15):
            while True:
                polled = await _call(client, "minecraft_get_events", {})
                types = [record["event"]["type"] for record in polled["events"]]
                if "safety.intervened" in types:
                    break
                await asyncio.sleep(0.5)
        events = [record["event"] for record in polled["events"]]
        attacks = [
            e
            for e in events
            if e["type"] == "safety.intervened" and e["payload"]["action"] == "attack"
        ]
        assert attacks and attacks[0]["payload"]["success"], events
        assert attacks[0]["steve_id"] == PLAYER
        damaged = [e for e in events if e["type"] == "steve.damaged"]
        assert damaged and damaged[0]["payload"]["source"] == {
            "kind": "entity",
            "entity_type": "minecraft:zombie",
        }, damaged

        # 4. Replays after the restart do not place the block again.
        await world.run_command_blocking(f"setblock {x + 2} {y} {z} air")
        async with asyncio.timeout(10):
            while await _block(world, x + 2, y, z) != "air":
                await asyncio.sleep(0.25)
        # The client resumes its original session: the original result.
        replay = await _submit(client, session, "gate-place-1", "place_block", target)
        assert replay["action_id"] == placed["action_id"], (placed, replay)
        assert replay["status"] == placed["status"], (replay["reason_code"], replay.get("message"))
        # A new session gets a refusal, never a second dispatch.
        await _call(client, "minecraft_close_session", {"session_id": session["session_id"]})
        fresh = await _open_session(client)
        refused = await _submit(client, fresh, "gate-place-1", "place_block", target)
        assert refused["status"] == "rejected", refused
        assert "another authorized identity" in (refused.get("message") or ""), refused
        # Give a (wrong) re-dispatch ample server ticks to land.
        await asyncio.sleep(5)
        assert await _block(world, x + 2, y, z) == "air", (
            "a replayed request mutated the world again"
        )
        await _call(client, "minecraft_close_session", {"session_id": fresh["session_id"]})


async def _block(mc: McpqClient, x: int, y: int, z: int) -> str:
    return str(await asyncio.to_thread(mc._mc.getBlock, _vec(x, y, z)))  # type: ignore[union-attr]


def _vec(x: int, y: int, z: int):
    from mcpq import Vec3

    return Vec3(x, y, z)


@pytest.fixture(autouse=True)
def _no_leftover_servers():
    """Fail loudly if a test left an MCP server process behind."""
    yield
    leftovers = subprocess.run(
        ["pgrep", "-f", "minecraft_ai_bridge.transport.mcp_main"],
        capture_output=True,
        text=True,
    ).stdout.split()
    leftovers = [pid for pid in leftovers if int(pid) != os.getpid()]
    assert not leftovers, f"MCP server processes left running: {leftovers}"
