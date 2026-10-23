"""In-game chat command interface.

Players can interact with the agent through prefixed commands sent in
Minecraft chat.  The agent periodically polls for recent chat messages
and dispatches recognised ``!commands``.

Supported commands
------------------
``!status``     — Report current goal, turn count, position, and health.
``!stop``       — Pause / shut down the agent gracefully.
``!goal <...>`` — Re-assign the agent's goal mid-session.
``!goto <p>``   — Teleport to a named player.
``!follow <p>`` — Follow a named player (stay within 5 blocks).
``!come``       — Teleport the speaker to the agent.
``!help``       — List available commands.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING

from ..minecraft.actions import ActionType
from ..runtime import ActionService, McpqWorldAdapter

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^\s*((?:/[!\u00a7]?|!)\w+)(.*)$")
_PUBLIC_COMMANDS = {"!status", "!help"}

# Commands the handler recognises
COMMANDS = {
    "!status",
    "!stop",
    "!goal",
    "!goto",
    "!follow",
    "!come",
    "!help",
}


class ChatCommandHandler:
    """Listens for ``!commands`` in chat and routes them to the orchestrator.

    Usage::

        handler = ChatCommandHandler(orchestrator)
        # In the main loop:
        await handler.poll()
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator
        # Resolve the world reference once: the orchestrator's adapter when
        # present, otherwise a wrapper around a legacy raw MCPQ client so
        # harness/direct construction keeps polling, status, and chat replies
        # working. Use the orchestrator's shared ActionService when present
        # so chat teleports share the same policy hook and audit trace as
        # other mutations; direct/test construction falls back to a local
        # service bound to the resolved world (no direct run_as_player
        # bypass). A harness exposing only an ActionService (with no
        # adapter and no raw client) resolves the world from that service
        # so polling, status, and replies still work.
        service = getattr(orchestrator, "_action_service", None)
        internal_authority = getattr(orchestrator, "_internal_action_authority", None)
        world = getattr(orchestrator, "_world", None)
        if world is None:
            mc = getattr(orchestrator, "_mc", None)
            if mc is not None:
                world = McpqWorldAdapter(mc)
        if world is None and service is not None:
            world = service.world
        if service is None:
            if world is None:
                raise ValueError(
                    "ChatCommandHandler requires an orchestrator with a "
                    "world adapter, an ActionService, or a raw MCPQ client"
                )
            policy = getattr(orchestrator, "_action_policy", None)
            service = ActionService(
                world,
                policy=policy,
                internal_authority=internal_authority,
            )
        self._action_service = service
        self._internal_action_authority = internal_authority
        self._world = world
        self._last_check: str | None = None
        config = getattr(orchestrator, "_config", None)
        chat_control = getattr(config, "chat_control", None)
        self._chat_control = chat_control

    # ── Polling ──────────────────────────────────────────────────────

    async def poll(self) -> None:
        """Check for new chat messages and process any matching commands.

        Uses MCPQ's ``ChatEvent`` polling API to catch in-game chat
        messages.  Should be called once per agent turn.
        """
        world = self._world
        if world is None:
            return

        try:
            events = await world.get_chat_events()
        except Exception:
            return

        if not events:
            return

        for event in events:
            player = getattr(event, "player", None)
            sender_name = getattr(player, "name", "") if player else ""
            message = getattr(event, "message", "")
            await self._process_message(message, sender_name, player)

    async def _process_message(self, message: str, sender_name: str, player: object | None) -> None:
        """Process message text while deriving authority only from event metadata."""
        match = _COMMAND_RE.match(message)
        if not match:
            return
        command = match.group(1).strip().lower().removeprefix("/")
        args = match.group(2).strip()
        if command not in COMMANDS:
            return
        if command not in _PUBLIC_COMMANDS and not self._authorized_player(player):
            logger.warning("Denied chat control command %s from untrusted player metadata", command)
            return
        await self.handle_command(command, args, sender_name, trusted_player=player)

    def _authorized_player(self, player: object | None) -> bool:
        if player is None:
            return False
        player_uuid = getattr(player, "uuid", None) or getattr(player, "unique_id", None)
        allowed_uuids = {
            str(value).strip().casefold()
            for value in getattr(self._chat_control, "allowed_uuids", [])
            if str(value).strip()
        }
        allowed_names = {
            str(value).strip().casefold()
            for value in getattr(self._chat_control, "allowed_names", [])
            if str(value).strip()
        }
        if player_uuid is not None and str(player_uuid).strip():
            # An event that supplies a UUID has a stronger identity than its
            # display name. Never let an unlisted/malformed UUID fall back to
            # a name allowlist entry; that would permit name-based spoofing.
            return str(player_uuid).strip().casefold() in allowed_uuids
        name = getattr(player, "name", None)
        return bool(name and str(name).strip().casefold() in allowed_names)

    async def _process_line(self, line: str) -> None:
        """Compatibility parser; formatted chat lines carry no trusted identity."""
        match = re.match(r"^<([^>]+)>\s+(.*)$", line)
        if match:
            await self._process_message(match.group(2), match.group(1), None)

    # ── Dispatch ─────────────────────────────────────────────────────

    async def handle_command(
        self,
        command: str,
        args: str,
        sender: str,
        *,
        trusted_player: object | None = None,
    ) -> None:
        """Route a parsed command to the appropriate handler."""
        if command not in _PUBLIC_COMMANDS and not self._authorized_player(trusted_player):
            logger.warning("Denied direct chat control dispatch without trusted player identity")
            return
        dispatch = {
            "!status": self._cmd_status,
            "!stop": self._cmd_stop,
            "!goal": self._cmd_goal,
            "!goto": self._cmd_goto,
            "!follow": self._cmd_follow,
            "!come": self._cmd_come,
            "!help": self._cmd_help,
        }
        handler = dispatch.get(command)
        if handler:
            await handler(args, sender)

    # ── Command handlers ─────────────────────────────────────────────

    async def _cmd_status(self, args: str, sender: str) -> None:
        """Report current agent status to the chat."""
        orch = self._orch
        lines = [
            f"@{sender} Status:",
            f'  Goal: "{getattr(orch, "_goals", None) and orch._goals.current_goal or "none"}"',
            f"  Turns: {getattr(orch, '_turn', 0)}",
        ]
        # Add position if available
        world = self._world
        if world and world.connected:
            try:
                pos = await world.get_player_pos()
                if pos:
                    lines.append(f"  Position: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})")
            except Exception:
                pass
        await self._send("\n".join(lines))

    async def _cmd_stop(self, args: str, sender: str) -> None:
        """Signal the agent to stop after the current turn."""
        await self._send(f"@{sender} Shutting down by request …")
        orch = self._orch
        orch._stop_requested = True  # checked in the main loop

    async def _cmd_goal(self, args: str, sender: str) -> None:
        """Re-assign the agent's goal."""
        if not args:
            await self._send(f"@{sender} Usage: !goal <description>")
            return
        orch = self._orch
        if hasattr(orch, "_goals") and hasattr(orch, "_memory"):
            orch._memory.clear_short_term()
            await orch._goals.set_goal(args)
            await self._send(
                f'@{sender} Goal re-assigned to: "{args}" ({orch._goals.sub_goal_count} sub-goals)'
            )

    async def _cmd_goto(self, args: str, sender: str) -> None:
        """Teleport to a named player."""
        if not args:
            await self._send(f"@{sender} Usage: !goto <player>")
            return
        target = args.split()[0]
        result = await self._action_service.execute(
            ActionType.GOTO_TELEPORT,
            {"target": target},
            authority=self._internal_action_authority,
        )
        if result.success:
            await self._send(f"@{sender} Teleported to {target}")
        else:
            # ActionService reports failures as results (and handler
            # exceptions become failed results in execute_action), so the
            # chat response must surface them explicitly.
            await self._send(f"@{sender} Could not teleport to {target}: {result.message}")

    async def _cmd_follow(self, args: str, sender: str) -> None:
        """Toggle follow-mode for a named player."""
        if not args:
            await self._send(f"@{sender} Usage: !follow <player>")
            return
        target = args.split()[0]
        orch = self._orch
        orch._follow_target = target
        orch._memory.remember_fact(f"Following player: {target}")
        await self._send(f"@{sender} Now following {target} (stays ~4 blocks away)")

    async def _cmd_come(self, args: str, sender: str) -> None:
        """Teleport the sender to the agent's position."""
        result = await self._action_service.execute(
            ActionType.COME_TELEPORT,
            {"sender": sender},
            authority=self._internal_action_authority,
        )
        if result.success:
            await self._send(f"@{sender} You have been teleported to me!")
        else:
            await self._send(f"@{sender} Could not teleport you: {result.message}")

    async def _cmd_help(self, args: str, sender: str) -> None:
        """List available commands."""
        help_text = (
            f"@{sender} Available commands: "
            "!status !stop !goal <...> !goto <player> "
            "!follow <player> !come !help"
        )
        await self._send(help_text)

    # ── Helper ───────────────────────────────────────────────────────

    async def _send(self, message: str) -> None:
        """Send a chat message through the world adapter."""
        world = self._world
        if world and world.connected:
            with contextlib.suppress(Exception):
                await world.post_to_chat(message)
