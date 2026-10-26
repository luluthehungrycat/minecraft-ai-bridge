"""Transport-independent world access for runtime consumers.

Phase A of RFC-0001 introduces a typed ``WorldAdapter`` Protocol so runtime
consumers (Observer, InventoryManager, ChatCommandHandler, SelfPreservationLayer)
can be expressed against a normalized world surface instead of the concrete
``McpqClient``. ``McpqWorldAdapter`` is a thin composition wrapper that
delegates to an existing connected ``McpqClient`` — it holds no connection
state of its own and adds no new backend or dependency.

The adapter covers only the methods the runtime currently consumes
(position/info reads, hurt-source check, chat event polling, run-as-player and
blocking commands, teleport, block get/set, chat post, time, online players,
biome, and connection state). The orchestrator keeps the raw ``McpqClient``
solely for connection lifecycle (connect/disconnect).

The protocol is intentionally *not* a full backend contract (Mineflayer or
otherwise); extending it for other backends is later RFC-0001 phase work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .contracts import BackendKind, Capabilities, CraftingMode, MovementMode

if TYPE_CHECKING:
    from ..minecraft.mc_api import McpqClient

__all__ = ["McpqWorldAdapter", "WorldAdapter"]


@runtime_checkable
class WorldAdapter(Protocol):
    """Normalized, transport-independent view of the Minecraft world.

    All methods are async; the concrete wrapper delegates to the underlying
    client. ``connected`` exposes connection liveness without exposing the
    underlying connection object itself.
    """

    @property
    def connected(self) -> bool: ...

    @property
    def capabilities(self) -> Capabilities: ...

    # ── Player reads ───────────────────────────────────────────────

    async def get_player_pos(self) -> tuple[float, float, float] | None: ...

    async def get_player_info(self) -> dict[str, Any]: ...

    async def get_hurt_by_entity(self) -> bool: ...

    # ── Chat events ────────────────────────────────────────────────

    async def get_chat_events(self) -> list[Any]: ...

    # ── Commands ───────────────────────────────────────────────────

    async def run_as_player(self, command: str) -> str: ...

    async def run_command_blocking(self, command: str) -> str: ...

    # ── Movement ───────────────────────────────────────────────────

    async def teleport_player(self, x: float, y: float, z: float) -> None: ...

    # ── World ──────────────────────────────────────────────────────

    async def get_block(self, x: int, y: int, z: int) -> str: ...

    async def set_block(self, block_type: str, x: int, y: int, z: int) -> None: ...

    # ── Chat post ──────────────────────────────────────────────────

    async def post_to_chat(self, message: str) -> None: ...

    # ── Utility ────────────────────────────────────────────────────

    async def get_time(self) -> str: ...

    async def get_players_online(self) -> list[str]: ...

    async def get_biome(self, x: int, y: int, z: int) -> str: ...


class McpqWorldAdapter:
    """Composition wrapper adapting a connected ``McpqClient`` to ``WorldAdapter``.

    The wrapper holds a reference to the client and delegates every call
    verbatim — no buffering, no retries, no semantic changes. It does not
    manage the connection: creating, connecting, and disconnecting the
    underlying ``McpqClient`` remains the orchestrator's responsibility
    ("keep raw client only for connection lifecycle").
    """

    __slots__ = ("_client",)

    def __init__(self, client: McpqClient) -> None:
        self._client = client

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def capabilities(self) -> Capabilities:
        """Return a fresh, conservative profile of the MCPQ adapter surface.

        This reports implementation abilities, not controller authorization,
        live world metadata, or an independent safety guarantee.
        """

        return Capabilities(
            backend=BackendKind.MCPQ,
            movement=MovementMode.TELEPORT_SIMULATED,
            crafting=CraftingMode.PRIVILEGED_SIMULATED,
            world_edit=True,
            max_interaction_distance=None,
            supported_versions=[],
            dimensions=[],
            hazard_sensors=False,
        )

    async def get_player_pos(self) -> tuple[float, float, float] | None:
        return await self._client.get_player_pos()

    async def get_player_info(self) -> dict[str, Any]:
        return await self._client.get_player_info()

    async def get_hurt_by_entity(self) -> bool:
        return await self._client.get_hurt_by_entity()

    async def get_chat_events(self) -> list[Any]:
        return await self._client.get_chat_events()

    async def run_as_player(self, command: str) -> str:
        return await self._client.run_as_player(command)

    async def run_command_blocking(self, command: str) -> str:
        return await self._client.run_command_blocking(command)

    async def teleport_player(self, x: float, y: float, z: float) -> None:
        await self._client.teleport_player(x, y, z)

    async def get_block(self, x: int, y: int, z: int) -> str:
        return await self._client.get_block(x, y, z)

    async def set_block(self, block_type: str, x: int, y: int, z: int) -> None:
        """Place ``block_type`` at (x, y, z), overwriting anything there."""
        await self._client.set_block(block_type, x, y, z)

    async def post_to_chat(self, message: str) -> None:
        await self._client.post_to_chat(message)

    async def get_time(self) -> str:
        return await self._client.get_time()

    async def get_players_online(self) -> list[str]:
        return await self._client.get_players_online()

    async def get_biome(self, x: int, y: int, z: int) -> str:
        return await self._client.get_biome(x, y, z)
