"""Structured inventory management for the Minecraft agent.

Builds on the ``InventorySlot`` dataclass from the observer layer to
provide item tracking, counting, slot management, and human-readable
summaries that the LLM can use to make informed decisions.

Usage::

    mgr = InventoryManager(mc)
    await mgr.refresh()
    if mgr.has_item("oak_planks", 10):
        print("Enough planks!")
    print(mgr.get_summary())
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..minecraft.observer import InventorySlot
    from ..runtime import WorldAdapter

logger = logging.getLogger(__name__)


class InventoryManager:
    """Manages a cached view of the player's inventory.

    Call ``refresh()`` before each use to sync with the server.
    """

    def __init__(self, mc: WorldAdapter) -> None:
        self._mc = mc
        self._items: list[InventorySlot] = []
        self._raw: str = ""

    # ── Sync ─────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Fetch and parse the current inventory from the server."""
        from ..minecraft.actions import ActionType, execute_action

        result = await execute_action(self._mc, ActionType.CHECK_INVENTORY)
        if result.success:
            self._raw = result.data.get("raw_inventory", "")
            self._items = self._parse(self._raw)
        else:
            logger.warning("Inventory refresh failed: %s", result.message)
            self._items = []

    @staticmethod
    def _parse(raw: str) -> list:
        """Parse raw NBT inventory string into InventorySlot objects.

        Reuses the same parser from the observer module.
        """
        from ..minecraft.observer import _parse_inventory_nbt

        return _parse_inventory_nbt(raw)

    # ── Query helpers ────────────────────────────────────────────────

    def has_item(self, item_id: str, min_count: int = 1) -> bool:
        """Check whether the player has at least *min_count* of *item_id*.

        The *item_id* can be with or without the ``minecraft:`` prefix.
        """
        return self.count_item(item_id) >= min_count

    def count_item(self, item_id: str) -> int:
        """Return the total count of a specific item across all slots."""
        clean_id = item_id.removeprefix("minecraft:")
        total = 0
        for slot in self._items:
            slot_id = slot.item_id.removeprefix("minecraft:")
            if slot_id == clean_id:
                total += slot.count
        return total

    def get_item_slots(self, item_id: str) -> list:
        """Return all inventory slots containing *item_id*."""
        clean_id = item_id.removeprefix("minecraft:")
        return [slot for slot in self._items if slot.item_id.removeprefix("minecraft:") == clean_id]

    def get_hotbar(self) -> list:
        """Return slots in the hotbar (slots 0-8)."""
        return [s for s in self._items if s.slot < 9]

    def get_armor(self) -> list:
        """Return armor slots (slots 100-103)."""
        return [s for s in self._items if 100 <= s.slot <= 103]

    def get_offhand(self) -> list:
        """Return offhand slot (slot -106 or similar)."""
        return [s for s in self._items if s.slot < 0]

    def total_slots_used(self) -> int:
        """Return count of non-empty inventory slots."""
        return len(self._items)

    def total_item_types(self) -> int:
        """Return count of distinct item types."""
        return len(set(s.item_id for s in self._items))

    @property
    def summary(self) -> str:
        """Return a compact, human-readable summary for the LLM prompt."""
        if not self._items:
            return "Inventory: (unavailable or empty)"

        counts: Counter = Counter()
        for slot in self._items:
            name = getattr(
                slot, "display_name", slot.item_id.replace("minecraft:", "").replace("_", " ")
            )
            counts[name] += slot.count

        items_str = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))
        return f"Inventory ({len(self._items)} slots used, {len(counts)} types): {items_str}"

    @property
    def item_count(self) -> int:
        """Total number of items across all stacks."""
        return sum(s.count for s in self._items)

    def get_all_items(self) -> list:
        """Return the raw list of parsed InventorySlot objects."""
        return list(self._items)
