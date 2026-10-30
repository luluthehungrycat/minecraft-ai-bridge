"""End-to-end: the minecraft-ai-bridge-mcp server against a live Paper/MCPQ server.

Launches the real server command as a subprocess (stdio and Streamable HTTP)
and drives it with the MCP client. Skips when no MCPQ server is reachable.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from minecraft_ai_bridge.minecraft.mc_api import McpqClient  # noqa: E402
from minecraft_ai_bridge.runtime import AuthzService  # noqa: E402

MC_HOST = os.environ.get("MC_API_HOST", "localhost")
MC_PORT = int(os.environ.get("MC_API_PORT", "1789"))
PLAYER = os.environ.get("MC_API_PLAYER_NAME", "AIBot")
WORLD_SCOPES = {"steve:world:move", "steve:world:edit", "steve:world:items", "steve:world:combat"}


def _mcpq_reachable() -> bool:
    try:
        with socket.create_connection((MC_HOST, MC_PORT), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _mcpq_reachable(), reason="no MCPQ server reachable")


@pytest.fixture
async def stack(tmp_path):
    """Authz database, config file, tokens, and a summoned bot."""
    mc = McpqClient(host=MC_HOST, port=MC_PORT, player_name=PLAYER)
    await mc.connect(retries=3)
    if await mc.get_player_pos() is None:
        await mc.run_command_blocking(f"botsummon {PLAYER}")
        for _ in range(20):
            if await mc.get_player_pos() is not None:
                break
            await asyncio.sleep(0.5)
    await mc.disconnect()

    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve(PLAYER, ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    controller = auth.create_credential(
        PLAYER,
        "owner",
        {"steve:control", "steve:observe:self", "events:read", *WORLD_SCOPES},
        admin_token=admin.token,
    ).token
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token
    auth.close()

    config = tmp_path / "config.yaml"
    config.write_text(
        f"mc_api:\n  host: {MC_HOST}\n  port: {MC_PORT}\n  player_name: {PLAYER}\n"
        f"authz:\n  database_path: {tmp_path / 'authz.sqlite3'}\n  steve_id: {PLAYER}\n"
    )
    env = {
        **os.environ,
        "MAB_CONTROLLER_TOKEN": controller,
        "AUTHZ_WORLD_ADMIN_TOKEN": world_admin,
    }
    args = [
        "--config",
        str(config),
        "--outcome-store",
        str(tmp_path / "outcomes.sqlite3"),
        "--event-journal",
        str(tmp_path / "events.sqlite3"),
        "--task-store",
        str(tmp_path / "tasks.sqlite3"),
    ]
    return {"env": env, "args": args, "token": controller}


async def _exercise(client: Client) -> None:
    opened = await client.call_tool("minecraft_open_session", {})
    assert not opened.is_error, opened.content
    session = opened.structured_content

    state = await client.call_tool("minecraft_get_state", {})
    assert not state.is_error, state.content

    async def submit(request_id: str, action: str, params: dict) -> dict:
        result = await client.call_tool(
            "minecraft_submit_action",
            {
                "session_id": session["session_id"],
                "generation": session["generation"],
                "request_id": request_id,
                "action": action,
                "params": params,
            },
        )
        assert not result.is_error, result.content
        return result.structured_content

    chat = await submit("chat-1", "chat", {"message": "minecraft-ai-bridge MCP integration test"})
    replay = await submit("chat-1", "chat", {"message": "minecraft-ai-bridge MCP integration test"})
    position = state.structured_content["position"]
    assert position, "the live bot has no position"
    target = {"x": position[0] + 3, "y": position[1], "z": position[2] + 3}
    moved = await submit("tp-1", "teleport", target)
    far = await submit("tp-far", "teleport", {**target, "x": position[0] + 500})
    internal = await submit("internal-1", "goto_teleport", {"target": "Alex"})

    assert chat["status"] == "succeeded"
    assert replay["action_id"] == chat["action_id"]
    assert moved["status"] == "succeeded", moved
    assert far["reason_code"] == "OUT_OF_REACH", far
    assert internal["reason_code"] == "ACTION_NOT_EXPOSED"

    created = await client.call_tool(
        "minecraft_create_task",
        {
            "session_id": session["session_id"],
            "generation": session["generation"],
            "request_id": "task-1",
            "title": "live task",
            "steps": [
                {"action": "check_position", "params": {}},
                {"action": "chat", "params": {"message": "minecraft-ai-bridge task step"}},
            ],
        },
    )
    assert not created.is_error, created.content
    task_id = created.structured_content["task_id"]
    async with asyncio.timeout(30):
        while True:
            task = await client.call_tool("minecraft_get_task", {"task_id": task_id})
            assert not task.is_error, task.content
            if task.structured_content["status"] in {"succeeded", "failed", "paused"}:
                break
            await asyncio.sleep(0.2)
    assert task.structured_content["status"] == "succeeded", task.structured_content

    closed = await client.call_tool(
        "minecraft_close_session", {"session_id": session["session_id"]}
    )
    assert not closed.is_error

    # Task events are published from a durable outbox a moment after the
    # task finishes.
    async with asyncio.timeout(10):
        while True:
            polled = await client.call_tool("minecraft_get_events", {})
            assert not polled.is_error, polled.content
            events = polled.structured_content["events"]
            if any(record["event"]["type"] == "task.completed" for record in events):
                break
            await asyncio.sleep(0.2)
    changes = [
        record["event"]["payload"]["change"]
        for record in events
        if record["event"]["type"] == "controller.changed"
    ]
    assert changes[-2:] == ["opened", "released"], events
    acked = await client.call_tool("minecraft_ack_events", {"cursor": events[-1]["cursor"]})
    assert not acked.is_error
    after = await client.call_tool("minecraft_get_events", {})
    assert after.structured_content["events"] == []


async def test_stdio_server_against_live_mcpq(stack):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "minecraft_ai_bridge.transport.mcp_main",
            "--transport",
            "stdio",
            *stack["args"],
        ],
        env=stack["env"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with Client(params, read_timeout_seconds=60) as client:
        await _exercise(client)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_http_server_against_live_mcpq(stack):
    port = _free_port()
    env = {key: value for key, value in stack["env"].items() if key != "MAB_CONTROLLER_TOKEN"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "minecraft_ai_bridge.transport.mcp_main",
            "--transport",
            "http",
            "--port",
            str(port),
            *stack["args"],
        ],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                await asyncio.sleep(0.2)
        else:
            pytest.fail("MCP HTTP server did not start")

        unauthenticated = httpx2.AsyncClient()
        async with Client(streamable_http_client(url, http_client=unauthenticated)) as client:
            refused = await client.call_tool("minecraft_open_session", {})
        assert refused.is_error

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {stack['token']}"})
        async with Client(streamable_http_client(url, http_client=http)) as client:
            await _exercise(client)
    finally:
        process.terminate()
        process.wait(timeout=20)
