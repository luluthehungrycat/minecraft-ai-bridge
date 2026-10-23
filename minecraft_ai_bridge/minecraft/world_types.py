"""Shared world-access type aliases for the Minecraft layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime import WorldAdapter

    WorldAdapterType = WorldAdapter
else:
    WorldAdapterType = object

WorldAdapter = WorldAdapterType
"""Type alias: the real Protocol under type checking, ``object`` at runtime."""

__all__ = ["WorldAdapter"]
