"""``minecraft-ai-bridge-mcp``: serve one Steve to MCP clients.

Usage::

    # stdio: the harness launches this process and sets the controller token
    MAB_CONTROLLER_TOKEN=... minecraft-ai-bridge-mcp --transport stdio

    # Streamable HTTP on loopback; each request sends Authorization: Bearer <token>
    minecraft-ai-bridge-mcp --transport http --port 8765

The Steve, MCPQ connection, and Authz database come from ``config.yaml``
(``authz.steve_id``, ``mc_api.*``, ``authz.database_path``). The operator's
world-admin token is read only from ``AUTHZ_WORLD_ADMIN_TOKEN``. The bot
entity must already exist in the world; this server does not summon it. The
bot's safety reflexes (attack, eat, heal) run for as long as the server does
and preempt controller actions while they act.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import AppConfig

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

STDIO_TOKEN_ENV = "MAB_CONTROLLER_TOKEN"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minecraft-ai-bridge-mcp",
        description="Serve one Minecraft bot to MCP clients over stdio or Streamable HTTP.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding HTTP to a non-loopback address (put TLS in front of it)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help=(
            "Host header value clients use (repeatable; 'name:*' allows any port). "
            "Required with --allow-remote; behind a reverse proxy, the public hostname"
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="Browser Origin allowed to call the server (repeatable)",
    )
    parser.add_argument(
        "--outcome-store",
        default="data/action-outcomes.sqlite3",
        help="SQLite file for durable request-ID outcomes",
    )
    parser.add_argument("--task-store", default="data/tasks.sqlite3", help="Durable task database")
    parser.add_argument(
        "--event-journal",
        default=None,
        metavar="PATH",
        help="SQLite event journal (default: bridge.event_journal_path from the config)",
    )
    parser.add_argument(
        "--max-edit-distance",
        type=float,
        default=6.0,
        help="Farthest block edit (break/place with coordinates) from the bot",
    )
    parser.add_argument(
        "--max-move-distance",
        type=float,
        default=32.0,
        help="Farthest teleport, move_to, or walk_to destination from the bot",
    )
    parser.add_argument(
        "--session-lease-seconds",
        type=float,
        default=300,
        help="Controller session lease length",
    )
    return parser


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def transport_security_for(
    host: str, allowed_hosts: list[str], allowed_origins: list[str]
) -> TransportSecuritySettings:
    """Return explicit Host/Origin validation for an HTTP bind address.

    The SDK only auto-enables DNS-rebinding protection for the exact hosts
    127.0.0.1, localhost, and ::1, and disables it otherwise, so this is
    always passed explicitly. Loopback binds accept loopback Host and Origin
    values plus any configured extras. Other binds accept only the configured
    hosts and origins; with no origins, only requests without an Origin
    header (non-browser clients) pass.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = list(allowed_hosts)
    origins = list(allowed_origins)
    if _is_loopback(host):
        bracketed = f"[{host}]" if ":" in host else host
        hosts += ["127.0.0.1:*", "localhost:*", "[::1]:*", f"{bracketed}:*"]
        origins += [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            f"http://{bracketed}:*",
        ]
    elif not hosts:
        raise ValueError(
            f"serving HTTP on {host!r} requires --allowed-host with the Host header "
            "clients send (the public hostname behind a reverse proxy)"
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )


def check_bind_address(host: str, *, allow_remote: bool) -> None:
    """Refuse non-loopback HTTP binds unless explicitly allowed."""
    if not allow_remote and not _is_loopback(host):
        raise ValueError(
            f"refusing to serve HTTP on non-loopback address {host!r}; pass "
            "--allow-remote and terminate TLS in front of this server"
        )


async def serve(args: argparse.Namespace, config: AppConfig) -> None:
    """Connect the runtime services and serve until the transport stops."""
    from ..bridge.controller_safety import ControllerSafety
    from ..minecraft.mc_api import McpqClient
    from ..runtime import (
        AuthzService,
        ControllerService,
        EventJournal,
        SqliteActionOutcomeStore,
        SqliteTaskStore,
    )
    from ..runtime.controller_service import ReachLimits
    from ..runtime.world_adapter import McpqWorldAdapter
    from .mcp_server import build_mcp_server

    steve_id = config.authz.steve_id.strip()
    if not steve_id:
        raise ValueError("config authz.steve_id must name the Steve to serve")
    reach = ReachLimits(edit_distance=args.max_edit_distance, move_distance=args.max_move_distance)
    stdio_token = os.environ.get(STDIO_TOKEN_ENV, "").strip() or None
    if args.transport == "stdio" and stdio_token is None:
        raise ValueError(f"stdio transport requires the {STDIO_TOKEN_ENV} environment variable")

    # Each resource registers its cleanup as soon as it exists, so a failure
    # at any later step (or in another cleanup) still closes everything
    # opened so far, in reverse order.
    async with AsyncExitStack() as stack:
        api = config.mc_api
        mc = McpqClient(host=api.host, port=api.port, player_name=api.player_name)
        # Registered before connect(): connect() cleans up after an ordinary
        # failure itself, but not after cancellation mid-connect, and
        # disconnect() is a no-op on a client that never connected.
        stack.push_async_callback(mc.disconnect)
        await mc.connect()
        authz = AuthzService(config.authz.database_path)
        stack.callback(authz.close)
        store = SqliteActionOutcomeStore(Path(args.outcome_store))
        stack.callback(store.close)
        journal = EventJournal(args.event_journal or config.bridge.event_journal_path)
        stack.callback(journal.close)
        tasks = SqliteTaskStore(args.task_store)
        stack.callback(tasks.close)
        safety_authority = object()
        controller = ControllerService(
            McpqWorldAdapter(mc),
            authz,
            steve_id,
            world_admin_token=config.authz.world_admin_token.get_secret_value(),
            session_lease_seconds=args.session_lease_seconds,
            outcome_store=store,
            reach_limits=reach,
            event_journal=journal,
            safety_authority=safety_authority,
            task_store=tasks,
        )
        stack.push_async_callback(controller.aclose)
        safety = ControllerSafety(controller, safety_authority, event_sink=journal.append)
        # Stops (and flushes its events) before the controller and journal
        # close.
        stack.push_async_callback(safety.aclose)
        safety.start()
        stack.push_async_callback(controller.stop_tasks)
        controller.start_tasks()
        # Over HTTP each request authenticates with its own header; the
        # process token is only for stdio.
        server = build_mcp_server(
            controller, stdio_token=stdio_token if args.transport == "stdio" else None
        )
        if args.transport == "stdio":
            await server.run_stdio_async()
        else:
            logger.info("Serving Steve %s over HTTP on %s:%d", steve_id, args.host, args.port)
            await server.run_streamable_http_async(
                host=args.host,
                port=args.port,
                stateless_http=True,
                json_response=True,
                transport_security=transport_security_for(
                    args.host, args.allowed_host, args.allowed_origin
                ),
            )


def main(argv: list[str] | None = None) -> None:
    """Console entry point."""
    args = build_parser().parse_args(argv)
    # stdout carries the MCP protocol over stdio, so logs always go to stderr.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        import mcp  # noqa: F401
    except ImportError:
        logger.error("The MCP server needs the 'mcp' extra: pip install minecraft-ai-bridge[mcp]")
        raise SystemExit(2) from None
    try:
        if args.transport == "http":
            check_bind_address(args.host, allow_remote=args.allow_remote)
            transport_security_for(args.host, args.allowed_host, args.allowed_origin)
        config = AppConfig.from_yaml(args.config)
        asyncio.run(serve(args, config))
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
