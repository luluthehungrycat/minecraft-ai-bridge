"""Async wrapper around the MCPQ plugin connection.

MCPQ (Minecraft Protobuf Queries) replaces both pyCraft (bot) and most
RCON usage — it connects to a Paper server's mcpq plugin via gRPC and
provides direct world interaction, player control, and command execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from mcpq import Minecraft, Vec3

logger = logging.getLogger(__name__)


class McpqClient:
    """Async wrapper around a gRPC connection to the MCPQ plugin.

    All potentially-blocking gRPC calls are dispatched to a thread-pool
    so the rest of the bridge can stay async.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1789,
        player_name: str = "AIBot",
    ) -> None:
        self._host = host
        self._port = port
        self._player_name = player_name
        self._mc: Minecraft | None = None

    # ── Connection ────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._mc is not None

    async def connect(
        self,
        retries: int = 20,
        delay: float = 5.0,
    ) -> None:
        """Open a gRPC channel to the MCPQ plugin.

        Retries with backoff so the bridge can wait for the Paper server
        to finish starting up (downloads + builds on first run).

        Parameters
        ----------
        retries : max number of attempts (default 20 → ~2 min at 5 s intervals)
        delay : seconds between attempts (doubles each retry, caps at 30 s)
        """
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                self._mc = await asyncio.to_thread(
                    Minecraft,
                    host=self._host,
                    port=self._port,
                )
                # Verify the connection by fetching the Minecraft version
                assert self._mc is not None
                version = await self._run(self._mc.getMinecraftVersion)
                server = await self._run(self._mc.getServerVersion)
                logger.info(
                    "Connected to MCPQ plugin — %s (MC %s)",
                    server,
                    version,
                )
                return
            except Exception as exc:
                last_exc = exc
                self._mc = None
                if attempt < retries:
                    wait = min(delay * (1.5 ** (attempt - 1)), 30.0)
                    logger.warning(
                        "MCPQ not ready yet (attempt %d/%d): %s  Retrying in %.0fs …",
                        attempt,
                        retries,
                        exc.__class__.__name__,
                        wait,
                    )
                    await asyncio.sleep(wait)

        # All retries exhausted
        raise ConnectionError(
            f"Failed to connect to MCPQ plugin at {self._host}:{self._port} "
            f"after {retries} attempts.  Is the server running and the "
            f"plugin installed?  Last error: {last_exc}"
        ) from last_exc

    async def disconnect(self) -> None:
        """Stop MCPQ's event thread and close the gRPC channel.

        Dropping the reference alone left cleanup to ``Minecraft.__del__``,
        which garbage collection can run inside another thread's gRPC call;
        that cleanup then deadlocks waiting on gRPC's own lock. Cleaning up
        here, in a worker thread, keeps it deterministic and off the loop.
        """
        mc, self._mc = self._mc, None
        if mc is None:
            return
        try:
            # mcpq exposes no public close(); _cleanup is idempotent, so the
            # later __del__ call is a no-op.
            await asyncio.to_thread(mc._cleanup)
        except Exception:
            logger.exception("MCPQ cleanup failed during disconnect")
        logger.info("Disconnected from MCPQ plugin")

    # ── Thread-pool helper ────────────────────────────────────────────

    async def _run(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous function in a thread-pool."""
        return await asyncio.to_thread(func, *args, **kwargs)

    # ── Player methods ────────────────────────────────────────────────

    async def get_player(self):
        """Return the configured player's Player object, or *None*."""
        assert self._mc is not None
        try:
            return await self._run(self._mc.getPlayer, self._player_name)
        except Exception:
            return None

    async def get_player_pos(self) -> tuple[float, float, float] | None:
        """Get (x, y, z) of the configured player, or *None*."""
        player = await self.get_player()
        if player is not None:
            return (player.pos.x, player.pos.y, player.pos.z)
        return None

    async def get_hurt_by_entity(self) -> bool:
        """Return True if the player was last hurt by an entity.

        Reads the ``LastHurtByEntity`` NBT field via
        ``/data get entity @p LastHurtByEntity``.  Returns True when
        the field is present (damage came from another entity — a mob,
        a player, a projectile, etc.), False when the field is absent
        (damage came from the environment: fall, cactus, fire, lava,
        suffocation, drowning, etc.).

        The reflex-attack path in the self-preservation layer uses
        this to skip reflex behaviour on environmental damage — we
        don't want the agent to charge a zombie because it tripped
        on a cactus.
        """
        try:
            resp = await self.run_command_blocking("data get entity @p LastHurtByEntity")
            if not resp:
                return False
            # The response includes the UUID array only when an entity
            # is recorded. Paper 1.21 emits the response as
            # "<name> has the following entity data: [I;...]" when set
            # and "No entity was found" (or similar) when not.
            return "has the following entity data" in resp
        except Exception:
            return False

    async def get_player_info(self) -> dict[str, Any]:
        """Return structured info about the configured player."""
        player = await self.get_player()
        if player is None:
            return {"online": False}

        nbt = None
        with contextlib.suppress(Exception):
            nbt = await self._run(player.getNbt)

        info: dict[str, Any] = {
            "online": True,
            "name": player.name,
            "pos": {"x": player.pos.x, "y": player.pos.y, "z": player.pos.z},
            "world": player.world.name if player.world else None,
        }

        if nbt is not None:
            info["health"] = nbt.get("Health")
            info["food"] = nbt.get("foodLevel")
            info["gamemode"] = nbt.get("playerGameType")

            info["selected_item_slot"] = nbt.get("SelectedItemSlot", 0)
        return info

    @property
    def player_name(self) -> str:
        """The configured player name."""
        return self._player_name

    async def run_as_player(self, command: str) -> str:
        """Run a Minecraft command **as** the configured player.

        Replaces ``@p`` with the actual player name so the command
        always targets the bot, never a human player who happens to
        be nearest.
        """
        substituted = command.replace("@p", self._player_name)
        return await self.run_command_blocking(substituted)

    async def get_chat_events(self):
        """Poll for recent chat events via MCPQ's event system.

        Returns a list of ``ChatEvent`` objects, each with ``.player.name``
        and ``.message`` attributes.  Call this periodically from the
        chat command handler to detect ``!commands``.
        """
        if self._mc is None:
            return []
        return await self._run(self._mc.events.chat.poll)

    async def teleport_player(self, x: float, y: float, z: float) -> None:
        """Teleport the configured player to an absolute position.

        Uses ``/tp`` command because MCPQ's ``player.teleport(Vec3)`` gRPC
        call silently fails on Paper 26.x (reports success but doesn't
        actually move the player).
        """
        # float() keeps a non-numeric value from changing the command.
        await self.run_as_player(f"tp @p {float(x)} {float(y)} {float(z)}")

    # ── World methods ─────────────────────────────────────────────────

    async def get_block(self, x: int, y: int, z: int) -> str:
        """Return the block type name at (x, y, z), e.g. ``"stone"``."""
        assert self._mc is not None
        block = await self._run(self._mc.getBlock, Vec3(x, y, z))
        return str(block.name) if block else "unknown"

    async def set_block(
        self,
        block_type: str,
        x: int,
        y: int,
        z: int,
    ) -> None:
        """Place ``block_type`` at (x, y, z), overwriting anything there."""
        assert self._mc is not None
        await self._run(self._mc.setBlock, block_type, Vec3(x, y, z))

    async def set_sign(
        self,
        x: int,
        y: int,
        z: int,
        lines: list[str],
        color: str = "black",
        glowing: bool = False,
        direction: str = "south",
        sign_block: str = "oak_sign",
    ) -> None:
        """Place a sign with text at (x, y, z).

        Parameters
        ----------
        x, y, z : block coordinates
        lines : up to 4 strings for each sign line
        """
        assert self._mc is not None
        await self._run(
            self._mc.setSign,
            Vec3(x, y, z),
            lines,
            color=color,
            glowing=glowing,
            direction=direction,
            sign_block=sign_block,
        )

    # ── Commands ──────────────────────────────────────────────────────

    async def run_command(self, command: str) -> None:
        """Run a server command (fire-and-forget)."""
        assert self._mc is not None
        await self._run(self._mc.runCommand, command)

    async def run_command_blocking(self, command: str) -> str:
        """Run a server command and return its output."""
        assert self._mc is not None
        return await self._run(self._mc.runCommandBlocking, command)

    # ── Chat ──────────────────────────────────────────────────────────

    async def post_to_chat(self, message: str) -> None:
        """Broadcast a chat message to all players."""
        assert self._mc is not None
        await self._run(self._mc.postToChat, message)

    # ── Utility ───────────────────────────────────────────────────────

    async def get_server_version(self) -> str:
        """Return the Paper server version string."""
        assert self._mc is not None
        return await self._run(self._mc.getServerVersion)

    async def get_biome(self, x: int, y: int, z: int) -> str:
        """Detect the biome at a position using authoritative server checks.

        Paper 1.21.4 removed the ``/locatebiome`` command (it's now
        ``/locate biome <type>`` and requires a biome argument), so probe a
        bounded list of common biomes with ``/execute if biome``.  Each probe
        executes as the configured player so the coordinates are evaluated in
        that player's dimension.  Block contents cannot identify a biome
        reliably and are intentionally not used as a fallback.

        Returns the biome name (e.g. ``"plains"``) or ``"unknown"``.
        """
        common_biomes = [
            "plains",
            "desert",
            "forest",
            "taiga",
            "snowy_plains",
            "badlands",
            "ocean",
            "river",
            "swamp",
            "jungle",
            "nether_wastes",
            "soul_sand_valley",
            "crimson_forest",
            "warped_forest",
            "basalt_deltas",
            "the_end",
            "end_highlands",
            "end_midlands",
            "small_end_islands",
            "end_barrens",
        ]
        for biome in common_biomes:
            try:
                check_cmd = (
                    f"execute as {self._player_name} at @s if biome "
                    f"{x} {y} {z} minecraft:{biome} run say __biome_{biome}__"
                )
                resp = await self.run_command_blocking(check_cmd)
                if f"__biome_{biome}__" in resp:
                    return biome
            except Exception:
                continue

        return "unknown"

    async def get_time(self) -> str:
        """Query the current in-game time.

        Uses ``time query day`` instead of the legacy ``time query daytime``
        which is broken on Paper 26.x (throws CommandException).
        The result gives total in-game ticks (days × 24000 + daytime).
        """
        return await self.run_command_blocking("time query day")

    async def get_players_online(self) -> list[str]:
        """Return list of online player names."""
        assert self._mc is not None
        players = await self._run(self._mc.getPlayerList)
        return [p.name for p in players]
