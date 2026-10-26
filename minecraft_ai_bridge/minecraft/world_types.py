"""Shared world-access type aliases for the Minecraft layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from ..runtime.world_adapter import WorldAdapter as _WorldAdapter

    WorldAdapter: TypeAlias = _WorldAdapter
else:
    # The runtime import would cycle through actions -> runtime.action_service.
    # This alias is used only in annotations; runtime dispatch accepts adapters.
    WorldAdapter: TypeAlias = Any
"""Type alias: the real Protocol under type checking, ``Any`` at runtime."""

__all__ = ["WorldAdapter"]
