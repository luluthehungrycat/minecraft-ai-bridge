"""Shared world-access type aliases for the Minecraft layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from ..runtime.world_adapter import McpqCommandAdapter as _McpqCommandAdapter
    from ..runtime.world_adapter import WorldAdapter as _WorldAdapter

    WorldAdapter: TypeAlias = _WorldAdapter
    McpqCommandAdapter: TypeAlias = _McpqCommandAdapter
else:
    # The runtime import would cycle through actions -> runtime.action_service.
    # This alias is used only in annotations; runtime dispatch accepts adapters.
    WorldAdapter: TypeAlias = Any
    McpqCommandAdapter: TypeAlias = Any
"""Type aliases: the real Protocols under type checking, ``Any`` at runtime."""

__all__ = ["McpqCommandAdapter", "WorldAdapter"]
