"""MCP adapter over ControllerService: tools, bearer handling, and startup checks."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from minecraft_ai_bridge.config import AppConfig  # noqa: E402
from minecraft_ai_bridge.runtime import AuthzService, ControllerService  # noqa: E402
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter  # noqa: E402
from minecraft_ai_bridge.transport.mcp_main import (  # noqa: E402
    build_parser,
    check_bind_address,
    serve,
    transport_security_for,
)
from minecraft_ai_bridge.transport.mcp_server import build_mcp_server  # noqa: E402
from tests.conftest import MockMcpqClient  # noqa: E402

TOOLS = {
    "minecraft_get_capabilities",
    "minecraft_get_state",
    "minecraft_open_session",
    "minecraft_renew_session",
    "minecraft_close_session",
    "minecraft_submit_action",
}
BASE_URL = "http://127.0.0.1:8765"


@dataclass
class Env:
    auth: AuthzService
    world: MockMcpqClient
    controller: str
    second: str
    observer: str
    world_admin: str

    def service(self) -> ControllerService:
        return ControllerService(
            McpqWorldAdapter(self.world), self.auth, "AIBot", world_admin_token=self.world_admin
        )


@pytest.fixture
def env(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")

    def credential(*scopes: str) -> str:
        return auth.create_credential("AIBot", "owner", set(scopes), admin_token=admin.token).token

    world = MockMcpqClient()
    world.set_position(0, 64, 0)
    yield Env(
        auth=auth,
        world=world,
        controller=credential("steve:control", "steve:observe:self"),
        second=credential("steve:control"),
        observer=credential("steve:observe:self"),
        world_admin=auth.grant_admin_credential(admin.token, "world-op", {"admin:world"}).token,
    )
    auth.close()


@asynccontextmanager
async def http_client(server, headers: dict[str, str]):
    """An MCP client talking Streamable HTTP to the server's ASGI app in-process."""
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        )
        async with Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client:
            yield client


async def _open(client) -> dict:
    result = await client.call_tool("minecraft_open_session", {})
    assert not result.is_error, result.content
    return result.structured_content


def _submit_args(session: dict, action: str, params=None, request_id: str = "req-1") -> dict:
    return {
        "session_id": session["session_id"],
        "generation": session["generation"],
        "request_id": request_id,
        "action": action,
        "params": params or {},
    }


def _error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


# ── Tool surface and the stdio path ──────────────────────────────────


async def test_exposes_exactly_the_controller_tools(env):
    async with Client(build_mcp_server(env.service(), stdio_token=env.controller)) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == TOOLS


async def test_stdio_flow_submits_through_the_controller(env):
    server = build_mcp_server(env.service(), stdio_token=env.controller)
    async with Client(server) as client:
        session = await _open(client)
        args = _submit_args(session, "chat", {"message": "hi"})
        first = (await client.call_tool("minecraft_submit_action", args)).structured_content
        replay = (await client.call_tool("minecraft_submit_action", args)).structured_content
        state = await client.call_tool("minecraft_get_state", {})
        closed = await client.call_tool(
            "minecraft_close_session", {"session_id": session["session_id"]}
        )

    assert first["status"] == "succeeded"
    assert replay["action_id"] == first["action_id"]
    assert env.world.chat_messages_sent == ["hi"]
    assert not state.is_error and "health" in state.structured_content
    assert not closed.is_error


async def test_stdio_without_a_configured_token_is_refused(env):
    async with Client(build_mcp_server(env.service())) as client:
        result = await client.call_tool("minecraft_open_session", {})
    assert "unauthorized" in _error_text(result)


# ── Streamable HTTP: the request's bearer token, and only that ──────


async def test_http_uses_the_requests_bearer_token(env):
    server = build_mcp_server(env.service())
    async with http_client(server, {"Authorization": f"Bearer {env.controller}"}) as client:
        session = await _open(client)
        result = await client.call_tool(
            "minecraft_submit_action", _submit_args(session, "chat", {"message": "over http"})
        )
    assert result.structured_content["status"] == "succeeded"
    assert env.world.chat_messages_sent == ["over http"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer mab-not-a-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer "},
    ],
)
async def test_http_without_a_valid_bearer_never_falls_back(env, headers):
    # A valid stdio token is configured; HTTP must still ignore it.
    server = build_mcp_server(env.service(), stdio_token=env.controller)
    async with http_client(server, headers) as client:
        result = await client.call_tool("minecraft_open_session", {})
    assert "unauthorized" in _error_text(result)


@pytest.mark.parametrize("scheme", ["Basic", "Token", ""])
async def test_http_accepts_only_the_bearer_scheme(env, scheme):
    server = build_mcp_server(env.service())
    header = f"{scheme} {env.controller}".strip()
    async with http_client(server, {"Authorization": header}) as client:
        result = await client.call_tool("minecraft_open_session", {})
    assert "unauthorized" in _error_text(result)


async def test_http_requests_authenticate_independently(env):
    server = build_mcp_server(env.service())
    async with http_client(server, {"Authorization": f"Bearer {env.controller}"}) as first:
        await _open(first)
        async with http_client(server, {"Authorization": f"Bearer {env.second}"}) as second:
            taken = await second.call_tool("minecraft_open_session", {})
        async with http_client(server, {"Authorization": f"Bearer {env.observer}"}) as observer:
            observed = await observer.call_tool("minecraft_get_state", {})
            denied = await observer.call_tool("minecraft_open_session", {})

    assert "already active" in _error_text(taken)
    assert not observed.is_error
    assert "unauthorized" in _error_text(denied)


# ── The controller's guarantees hold through MCP ─────────────────────


async def test_privileged_actions_still_need_delegation(env):
    async with Client(build_mcp_server(env.service(), stdio_token=env.controller)) as client:
        session = await _open(client)
        result = await client.call_tool(
            "minecraft_submit_action", _submit_args(session, "teleport", {"x": 5, "y": 64, "z": 5})
        )
    assert result.structured_content["status"] == "rejected"
    assert result.structured_content["reason_code"] == "POLICY_REJECTED"
    assert env.world.commands_ran == []


async def test_internal_actions_are_not_exposed(env):
    async with Client(build_mcp_server(env.service(), stdio_token=env.controller)) as client:
        session = await _open(client)
        result = await client.call_tool(
            "minecraft_submit_action", _submit_args(session, "goto_teleport", {"target": "Alex"})
        )
    assert result.structured_content["reason_code"] == "ACTION_NOT_EXPOSED"
    assert env.world.commands_ran == []


@pytest.mark.parametrize(
    ("action", "params", "message"),
    [
        ("not_an_action", {}, "invalid request"),
        ("chat", ["not", "an", "object"], "params"),
    ],
)
async def test_invalid_requests_become_tool_errors(env, action, params, message):
    async with Client(build_mcp_server(env.service(), stdio_token=env.controller)) as client:
        session = await _open(client)
        args = _submit_args(session, action)
        args["params"] = params
        result = await client.call_tool("minecraft_submit_action", args)
    assert message in _error_text(result)
    assert env.world.chat_messages_sent == []


# ── Server command checks ────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_binds_are_allowed(host):
    check_bind_address(host, allow_remote=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_remote_binds_need_explicit_opt_in(host):
    with pytest.raises(ValueError, match="allow-remote"):
        check_bind_address(host, allow_remote=False)
    check_bind_address(host, allow_remote=True)


async def test_serve_requires_a_steve_and_a_stdio_token(monkeypatch):
    monkeypatch.delenv("MAB_CONTROLLER_TOKEN", raising=False)
    args: argparse.Namespace = build_parser().parse_args(["--transport", "stdio"])
    config = AppConfig()

    with pytest.raises(ValueError, match="steve_id"):
        await serve(args, config)
    config.authz.steve_id = "AIBot"
    with pytest.raises(ValueError, match="MAB_CONTROLLER_TOKEN"):
        await serve(args, config)


# ── Host/Origin validation (DNS rebinding) ──────────────────────────

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


async def _post_status(env, security, headers: dict[str, str]) -> int:
    server = build_mcp_server(env.service())
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://placeholder"
        ) as http,
    ):
        response = await http.post(
            "/mcp",
            json=INITIALIZE,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
    return response.status_code


@pytest.mark.parametrize(
    ("bind", "allowed_hosts", "allowed_origins", "headers", "status"),
    [
        # Loopback: loopback hosts and origins only.
        ("127.0.0.1", [], [], {"Host": "127.0.0.1:8765"}, 200),
        ("127.0.0.1", [], [], {"Host": "localhost:8765", "Origin": "http://localhost:3000"}, 200),
        ("127.0.0.1", [], [], {"Host": "evil.example"}, 421),
        ("127.0.0.1", [], [], {"Host": "127.0.0.1:8765", "Origin": "http://evil.example"}, 403),
        # Other loopback addresses, which the SDK does not protect by itself.
        ("127.0.0.2", [], [], {"Host": "127.0.0.2:8765"}, 200),
        ("127.0.0.2", [], [], {"Host": "evil.example"}, 421),
        ("::1", [], [], {"Host": "[::1]:8765"}, 200),
        # Remote / reverse proxy: only the configured public host.
        ("0.0.0.0", ["mcp.example.com"], [], {"Host": "mcp.example.com"}, 200),
        ("0.0.0.0", ["mcp.example.com"], [], {"Host": "mcp.example.com:8443"}, 421),
        ("0.0.0.0", ["mcp.example.com:*"], [], {"Host": "mcp.example.com:8443"}, 200),
        ("0.0.0.0", ["mcp.example.com"], [], {"Host": "127.0.0.1:8765"}, 421),
        ("0.0.0.0", ["mcp.example.com"], [], {"Host": "evil.example"}, 421),
        (
            "0.0.0.0",
            ["mcp.example.com"],
            [],
            {"Host": "mcp.example.com", "Origin": "https://mcp.example.com"},
            403,
        ),
        (
            "0.0.0.0",
            ["mcp.example.com"],
            ["https://app.example.com"],
            {"Host": "mcp.example.com", "Origin": "https://app.example.com"},
            200,
        ),
        (
            "0.0.0.0",
            ["mcp.example.com"],
            ["https://app.example.com"],
            {"Host": "mcp.example.com", "Origin": "https://evil.example"},
            403,
        ),
    ],
)
async def test_host_and_origin_are_validated(
    env, bind, allowed_hosts, allowed_origins, headers, status
):
    security = transport_security_for(bind, allowed_hosts, allowed_origins)
    assert await _post_status(env, security, headers) == status


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.10", "::"])
def test_remote_binds_require_explicit_allowed_hosts(bind):
    with pytest.raises(ValueError, match="allowed-host"):
        transport_security_for(bind, [], [])


def test_main_refuses_a_remote_bind_without_allowed_hosts(monkeypatch):
    from minecraft_ai_bridge.transport import mcp_main

    with pytest.raises(SystemExit) as exit_info:
        mcp_main.main(["--transport", "http", "--host", "0.0.0.0", "--allow-remote"])
    assert exit_info.value.code == 2


# ── Startup and shutdown cleanup ─────────────────────────────────────


class _Stage:
    """Fakes for serve()'s resources that record opens and closes."""

    def __init__(self, fail_at: str | None) -> None:
        self.fail_at = fail_at
        self.events: list[str] = []
        self.http_kwargs: dict = {}

    def check(self, step: str) -> None:
        if step == self.fail_at:
            raise RuntimeError(f"injected failure at {step}")


def _install_fakes(monkeypatch, stage: _Stage) -> None:
    from minecraft_ai_bridge import runtime
    from minecraft_ai_bridge.minecraft import mc_api
    from minecraft_ai_bridge.transport import mcp_server

    class FakeMc:
        def __init__(self, **_kwargs):
            pass

        async def connect(self):
            stage.check("connect")
            stage.events.append("mc.open")

        async def disconnect(self):
            stage.events.append("mc.close")

    class FakeAuthz:
        def __init__(self, _path):
            stage.check("authz")
            stage.events.append("authz.open")

        def close(self):
            stage.events.append("authz.close")

    class FakeStore:
        def __init__(self, _path):
            stage.check("store")
            stage.events.append("store.open")

        def close(self):
            stage.events.append("store.close")

    class FakeController:
        def __init__(self, *_args, **_kwargs):
            stage.check("controller")
            stage.events.append("controller.open")

        async def aclose(self):
            stage.events.append("controller.close")
            stage.check("controller.aclose")

    class FakeServer:
        async def run_streamable_http_async(self, **kwargs):
            stage.http_kwargs = kwargs
            stage.check("run")

    def fake_build(_controller, **_kwargs):
        stage.check("build")
        return FakeServer()

    monkeypatch.setattr(mc_api, "McpqClient", FakeMc)
    monkeypatch.setattr(runtime, "AuthzService", FakeAuthz)
    monkeypatch.setattr(runtime, "SqliteActionOutcomeStore", FakeStore)
    monkeypatch.setattr(runtime, "ControllerService", FakeController)
    monkeypatch.setattr(mcp_server, "build_mcp_server", fake_build)


OPENED = ["mc.open", "authz.open", "store.open", "controller.open"]


@pytest.mark.parametrize(
    ("fail_at", "opened"),
    [
        ("connect", 0),
        ("authz", 1),
        ("store", 2),
        ("controller", 3),
        ("build", 4),
        ("run", 4),
        ("controller.aclose", 4),
        (None, 4),
    ],
)
async def test_serve_closes_everything_it_opened(monkeypatch, tmp_path, fail_at, opened):
    stage = _Stage(fail_at)
    _install_fakes(monkeypatch, stage)
    args = build_parser().parse_args(
        ["--transport", "http", "--outcome-store", str(tmp_path / "o.sqlite3")]
    )
    config = AppConfig()
    config.authz.steve_id = "AIBot"

    if fail_at is None:
        await serve(args, config)
    else:
        with pytest.raises(RuntimeError, match="injected"):
            await serve(args, config)

    closes = [event.replace("open", "close") for event in reversed(OPENED[:opened])]
    assert stage.events == OPENED[:opened] + closes


async def test_serve_passes_explicit_transport_security(monkeypatch, tmp_path):
    stage = _Stage(None)
    _install_fakes(monkeypatch, stage)
    args = build_parser().parse_args(
        ["--transport", "http", "--host", "127.0.0.2", "--outcome-store", str(tmp_path / "o")]
    )
    config = AppConfig()
    config.authz.steve_id = "AIBot"

    await serve(args, config)

    security = stage.http_kwargs["transport_security"]
    assert security.enable_dns_rebinding_protection
    assert "127.0.0.2:*" in security.allowed_hosts
