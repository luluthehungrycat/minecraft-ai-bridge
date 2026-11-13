"""Async wrapper around the MCPQ plugin connection.

MCPQ (Minecraft Protobuf Queries) replaces both pyCraft (bot) and most
RCON usage — it connects to a Paper server's mcpq plugin via gRPC and
provides direct world interaction, player control, and command execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcpq import Minecraft, Vec3

logger = logging.getLogger(__name__)


# An entity hit within this many ticks (3 s) counts as the damage source.
_RECENT_ATTACK_TICKS = 60


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
                await self._discard_client()
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
        if await self._discard_client():
            logger.info("Disconnected from MCPQ plugin")

    async def _discard_client(self) -> bool:
        """Clean up and forget the current mcpq client; False if there was none."""
        mc, self._mc = self._mc, None
        if mc is None:
            return False
        try:
            # mcpq exposes no public close(); _cleanup is idempotent, so the
            # later __del__ call is a no-op.
            await asyncio.to_thread(mc._cleanup)
        except Exception:
            logger.exception("MCPQ cleanup failed")
        return True

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
        """Read fresh coordinates, bypassing MCPQ's 200 ms Player.pos cache.

        Movement/reach checks cannot use a cached position after command-based
        teleports. Read one authoritative command result off the event loop.
        """
        if self._mc is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", self._player_name):
            return None
        raw = await self.run_command_blocking(f"data get entity {self._player_name} Pos")
        match = re.search(r"has the following entity data:\s*\[([^\]]*)\]\s*$", raw)
        if match is None:
            return None
        try:
            values = tuple(float(part.strip().rstrip("dDfF")) for part in match[1].split(","))
        except ValueError:
            return None
        if len(values) != 3 or not all(math.isfinite(value) for value in values):
            return None
        return values[0], values[1], values[2]

    async def get_last_attacker(
        self, max_age_ticks: int = _RECENT_ATTACK_TICKS
    ) -> LastAttacker | None:
        """Return the entity that hurt the player in the last ``max_age_ticks``.

        Paper 26.x (Minecraft 1.21.5+) stores the attacker's UUID as
        ``last_hurt_by_mob`` next to ``ticks_since_last_hurt_by_mob`` and
        drops both about 100 ticks after the hit; older servers store a
        ``LastHurtByEntity`` field instead, which only says that an entity
        was involved. Returns None when no entity hurt the player recently
        (environmental damage: falls, fire, lava, drowning, ...). The
        attacker's type is resolved while it is still loaded; a despawned
        attacker leaves ``entity_type`` None.
        """
        recent = await self._recent_attack(max_age_ticks)
        if recent is None:
            return None
        ticks = recent
        if ticks < 0:
            # Legacy server: an entity was involved, nothing more is known.
            return LastAttacker(entity_type=None, uuid=None, ticks_since=None)
        name = self._player_name
        attacker_uuid = _nbt_uuid(
            await self.run_command_blocking(f"data get entity {name} last_hurt_by_mob")
        )
        entity_type = None
        if attacker_uuid is not None:
            entity_type = await self._entity_type(attacker_uuid)
        return LastAttacker(entity_type=entity_type, uuid=attacker_uuid, ticks_since=ticks)

    async def _recent_attack(self, max_age_ticks: int) -> int | None:
        """Ticks since an entity hurt the player, if within ``max_age_ticks``.

        Returns None when no entity hurt the player recently, and -1 on
        legacy servers that only record that some entity was involved.
        """
        name = self._player_name
        ticks = _nbt_int(
            await self.run_command_blocking(f"data get entity {name} ticks_since_last_hurt_by_mob")
        )
        if ticks is None:
            legacy = await self.run_command_blocking(f"data get entity {name} LastHurtByEntity")
            return -1 if legacy and _NBT_FOUND in legacy else None
        return ticks if ticks <= max_age_ticks else None

    async def _entity_type(self, entity_id: str) -> str | None:
        """Return ``minecraft:<type>`` of a loaded entity, or None if it is gone."""
        mc = self._mc
        if mc is None:
            return None

        def lookup() -> str:
            # Reading ``type`` can itself query the server, so it runs in the
            # worker thread with the lookup, never on the event loop.
            return str(mc.getEntityById(entity_id).type)

        try:
            entity_type = await self._run(lookup)
        except Exception:
            # Despawned or unloaded attackers cannot be looked up.
            return None
        if not entity_type or entity_type.upper() == "UNKNOWN":
            # MCPQ reports an entity it could not find as type UNKNOWN.
            return None
        return entity_type if ":" in entity_type else f"minecraft:{entity_type}"

    async def get_hurt_by_entity(self) -> bool:
        """Return True if an entity hurt the player in the last 3 seconds.

        The reflex-attack path in the self-preservation layer uses this to
        skip reflex behaviour on environmental damage, so the agent does
        not charge a zombie because it tripped on a cactus. Any failure
        reads as False.
        """
        try:
            # Only the cheap NBT check: resolving the attacker's type is not
            # needed here and must not slow the reflex path.
            return await self._recent_attack(_RECENT_ATTACK_TICKS) is not None
        except Exception:
            return False

    async def get_death_messages(self) -> list[str]:
        """Return the death messages of this player since the last call.

        Uses MCPQ's player-death event stream, which starts recording at
        the first call; deaths of other players are discarded.
        """
        if self._mc is None:
            return []
        events = await self._run(self._mc.events.player_death.poll)
        name = self._player_name
        return [str(event.deathMessage) for event in events if event.player.name == name]

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


_NBT_FOUND = "has the following entity data:"
_NBT_INT = re.compile(r"has the following entity data: (-?\d+)[bsl]?\s*$")
_NBT_UUID = re.compile(r"\[I;\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\s*\]")


@dataclass(frozen=True)
class LastAttacker:
    """The entity that most recently hurt the player.

    Every field may be None: ``entity_type`` (for example
    ``"minecraft:zombie"``) when the attacker is no longer loaded, and all
    of them on servers that only record that some entity was involved.
    """

    entity_type: str | None
    uuid: str | None
    ticks_since: int | None


def _nbt_int(response: str | None) -> int | None:
    match = _NBT_INT.search(response or "")
    return int(match.group(1)) if match else None


def _nbt_uuid(response: str | None) -> str | None:
    """Convert an NBT int-array UUID (``[I; a, b, c, d]``) to its string form."""
    match = _NBT_UUID.search(response or "")
    if match is None:
        return None
    value = 0
    for part in match.groups():
        value = (value << 32) | (int(part) & 0xFFFFFFFF)
    return str(uuid.UUID(int=value))
