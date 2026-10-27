"""``minecraft-ai-bridge-mcp``: serve one Steve to MCP clients.

Usage::

    # stdio: the harness launches this process and sets the controller token
    MAB_CONTROLLER_TOKEN=... minecraft-ai-bridge-mcp --transport stdio

    # Streamable HTTP on loopback; each request sends Authorization: Bearer <token>
    minecraft-ai-bridge-mcp --transport http --port 8765

The Steve, MCPQ connection, and Authz database come from ``config.yaml``
(``authz.steve_id``, ``mc_api.*``, ``authz.database_path``). The operator's
world-admin token is read only from ``AUTHZ_WORLD_ADMIN_TOKEN``. The bot
entity must already exist in the world; this server does not summon it.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import sys
from pathlib import Path

from ..config import AppConfig

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
        "--outcome-store",
        default="data/action-outcomes.sqlite3",
        help="SQLite file for durable request-ID outcomes",
    )
    parser.add_argument(
        "--session-lease-seconds",
        type=float,
        default=300,
        help="Controller session lease length",
    )
    return parser


def check_bind_address(host: str, *, allow_remote: bool) -> None:
    """Refuse non-loopback HTTP binds unless explicitly allowed."""
    if allow_remote or host == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError(
            f"refusing to serve HTTP on non-loopback address {host!r}; pass "
            "--allow-remote and terminate TLS in front of this server"
        )


async def serve(args: argparse.Namespace, config: AppConfig) -> None:
    """Connect the runtime services and serve until the transport stops."""
    from ..minecraft.mc_api import McpqClient
    from ..runtime import AuthzService, ControllerService, SqliteActionOutcomeStore
    from ..runtime.world_adapter import McpqWorldAdapter
    from .mcp_server import build_mcp_server

    steve_id = config.authz.steve_id.strip()
    if not steve_id:
        raise ValueError("config authz.steve_id must name the Steve to serve")
    stdio_token = os.environ.get(STDIO_TOKEN_ENV, "").strip() or None
    if args.transport == "stdio" and stdio_token is None:
        raise ValueError(f"stdio transport requires the {STDIO_TOKEN_ENV} environment variable")

    api = config.mc_api
    mc = McpqClient(host=api.host, port=api.port, player_name=api.player_name)
    await mc.connect()
    authz = AuthzService(config.authz.database_path)
    store = SqliteActionOutcomeStore(Path(args.outcome_store))
    controller = ControllerService(
        McpqWorldAdapter(mc),
        authz,
        steve_id,
        world_admin_token=config.authz.world_admin_token.get_secret_value(),
        session_lease_seconds=args.session_lease_seconds,
        outcome_store=store,
    )
    # Over HTTP each request authenticates with its own header; the process
    # token is only for stdio.
    server = build_mcp_server(
        controller, stdio_token=stdio_token if args.transport == "stdio" else None
    )
    try:
        if args.transport == "stdio":
            await server.run_stdio_async()
        else:
            logger.info("Serving Steve %s over HTTP on %s:%d", steve_id, args.host, args.port)
            await server.run_streamable_http_async(
                host=args.host, port=args.port, stateless_http=True, json_response=True
            )
    finally:
        await controller.aclose()
        store.close()
        authz.close()
        await mc.disconnect()


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
        config = AppConfig.from_yaml(args.config)
        asyncio.run(serve(args, config))
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
