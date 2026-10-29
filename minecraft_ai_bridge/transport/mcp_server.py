"""MCP adapter over ControllerService (RFC-0001 Phase B).

Every tool translates one MCP call into one ControllerService call, so
authentication, session fencing, delegation scopes, the exposed-action
allowlist, parameter validation, and idempotency are all enforced there,
not here. The adapter only decides which bearer token a call carries:

- Streamable HTTP: the ``Authorization: Bearer <token>`` header of the HTTP
  request that carried the call. A missing or malformed header is refused;
  there is no fallback to any other token.
- stdio: the single controller token configured for the server process
  (the harness that launches the process supplies it in the environment).

The adapter does not run the safety supervisor (``minecraft-ai-bridge-mcp``
starts it beside the adapter) or manage tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..minecraft.actions import ActionType
from ..runtime.contracts import ActionRequestSchema
from ..runtime.controller_service import _EXPOSED_ACTIONS, ControllerService
from ..runtime.event_journal import EventCursorExpiredError

logger = logging.getLogger(__name__)

SERVER_NAME = "minecraft-ai-bridge"
_ACTION_NAMES = ", ".join(sorted(action.value for action in _EXPOSED_ACTIONS))
INSTRUCTIONS = (
    "Controls one Minecraft bot (a 'Steve'). Call minecraft_open_session first, then "
    "send each action with minecraft_submit_action, passing the session_id and "
    "generation it returned and a request_id that is unique per logical action. "
    "Retrying with the same request_id never repeats the action. Renew the session "
    "before it expires and close it when done. Observation, chat, and wait need only "
    "a session; movement, block edits, items, and combat need world delegation granted "
    "by the server's operator. Poll minecraft_get_events for what happened to the bot "
    "(needs events:read) and acknowledge processed events with minecraft_ack_events; "
    "unacknowledged events are returned again. The bot's safety reflexes can preempt "
    "an action (reason_code SAFETY_PREEMPTED); retry it with a new request_id."
)


class TokenSource:
    """Resolves the controller bearer token for the current MCP call."""

    def __init__(self, stdio_token: str | None = None) -> None:
        self._stdio_token = (stdio_token or "").strip() or None

    def __call__(self, ctx: Context) -> str:
        request = ctx.request_context.request
        if request is not None:
            # HTTP: only the header of this request counts.
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not value.strip():
                raise ToolError("unauthorized: an Authorization: Bearer token is required")
            return value.strip()
        if self._stdio_token is None:
            raise ToolError("unauthorized: no controller token is configured for this server")
        return self._stdio_token


def _refusal(exc: Exception) -> ToolError:
    if isinstance(exc, PermissionError):
        return ToolError(f"unauthorized: {exc}")
    if isinstance(exc, EventCursorExpiredError):
        return ToolError(
            "cursor expired: events you had not acknowledged were dropped by retention; "
            "call minecraft_get_events with reset_expired=true to resume"
        )
    return ToolError(f"invalid request: {exc}")


def build_mcp_server(controller: ControllerService, *, stdio_token: str | None = None) -> MCPServer:
    """Return an MCP server exposing ``controller`` as tools."""
    token_for = TokenSource(stdio_token)
    server: MCPServer = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.tool(name="minecraft_get_capabilities")
    async def get_capabilities(ctx: Context) -> dict[str, Any]:
        """Describe what this bot's backend can do (movement, crafting, world edits)."""
        try:
            capabilities = await controller.get_capabilities(token_for(ctx))
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None
        return capabilities.model_dump(mode="json")

    @server.tool(name="minecraft_get_state")
    async def get_state(ctx: Context) -> dict[str, Any]:
        """Observe the bot: position, health, hunger, inventory, nearby entities, time."""
        try:
            return await controller.get_state(token_for(ctx))
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None

    @server.tool(name="minecraft_open_session")
    async def open_session(ctx: Context) -> dict[str, Any]:
        """Take exclusive control of the bot. Fails while another controller holds it."""
        try:
            session = await controller.open_session(token_for(ctx))
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None
        return session.model_dump(mode="json")

    @server.tool(name="minecraft_renew_session")
    async def renew_session(session_id: str, ctx: Context) -> dict[str, Any]:
        """Extend the session lease. An expired session cannot be renewed."""
        try:
            session = await controller.renew_session(token_for(ctx), session_id)
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None
        return session.model_dump(mode="json")

    @server.tool(name="minecraft_close_session")
    async def close_session(session_id: str, ctx: Context) -> dict[str, str]:
        """Release control so another controller can take over."""
        try:
            await controller.close_session(token_for(ctx), session_id)
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None
        return {"status": "released", "session_id": session_id}

    @server.tool(
        name="minecraft_submit_action",
        description=(
            "Run one action under an open session. action is one of: "
            f"{_ACTION_NAMES}. The result status is succeeded, failed, or rejected "
            "(with a reason_code)."
        ),
    )
    async def submit_action(
        session_id: str,
        generation: int,
        request_id: str,
        action: str,
        ctx: Context,
        params: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        token = token_for(ctx)
        try:
            request = ActionRequestSchema.model_validate(
                {
                    "steve_id": controller.steve_id,
                    "controller_session_id": session_id,
                    "generation": generation,
                    "request_id": request_id,
                    "action": ActionType(action),
                    "params": params or {},
                    "task_id": task_id,
                }
            )
            result = await controller.submit_action(token, request)
        except (PermissionError, ValueError) as exc:
            raise _refusal(exc) from None
        return result.model_dump(mode="json")

    @server.tool(name="minecraft_get_events")
    async def get_events(
        ctx: Context, limit: int = 100, reset_expired: bool = False
    ) -> dict[str, Any]:
        """Return the next unacknowledged events for this bot, oldest first.

        Events repeat until acknowledged with minecraft_ack_events. reset is
        true when retention dropped unacknowledged events before this poll.
        """
        try:
            page = await controller.get_events(
                token_for(ctx), limit=limit, reset_expired=reset_expired
            )
        except (PermissionError, ValueError, EventCursorExpiredError) as exc:
            raise _refusal(exc) from None
        return {
            "events": [
                {"cursor": record.cursor, "event": record.event.model_dump(mode="json")}
                for record in page.events
            ],
            "reset": page.reset,
        }

    @server.tool(name="minecraft_ack_events")
    async def ack_events(cursor: int, ctx: Context) -> dict[str, Any]:
        """Acknowledge every event up to and including cursor."""
        try:
            await controller.ack_events(token_for(ctx), cursor)
        except (PermissionError, ValueError, EventCursorExpiredError) as exc:
            raise _refusal(exc) from None
        return {"status": "acknowledged", "cursor": cursor}

    return server


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "TokenSource", "build_mcp_server"]
