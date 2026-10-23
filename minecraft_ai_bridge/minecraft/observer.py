"""World observation — translates MCPQ plugin data into structured state.

The observer provides the LLM with a high-level view of the world:
- Player position, health, inventory
- Time of day, weather
- Notable events (e.g., damage taken, mobs nearby)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .actions import ActionResult, ActionType, execute_action
from .world_types import WorldAdapter

logger = logging.getLogger(__name__)


@dataclass
class InventorySlot:
    """A single item stack in the player's inventory."""

    item_id: str
    count: int
    slot: int
    damage: int = 0

    @property
    def display_name(self) -> str:
        """Human-readable item name (strip Minecraft namespace)."""
        return self.item_id.replace("minecraft:", "").replace("_", " ")


@dataclass
class WorldState:
    """Snapshot of the player's current world state."""

    position: tuple[float, float, float] | None = None
    health: float | None = None
    hunger: int | None = None
    inventory_raw: str = ""
    inventory: list[InventorySlot] = field(default_factory=list)
    time_raw: str = ""
    weather_raw: str = ""
    players: list[str] = field(default_factory=list)
    biome: str = ""
    scan_data: dict[str, Any] = field(default_factory=dict)
    last_action_result: str = ""


class Observer:
    """Collects structured observations from the MCPQ plugin.

    Uses the action system internally so all observation paths go through
    the same action-execution layer.
    """

    def __init__(self, mc: WorldAdapter) -> None:
        self._mc = mc
        self._biome_cache: dict[tuple[str, int, int, int], str] = {}

    async def observe(self) -> WorldState:
        """Gather a full state snapshot.  Returns a ``WorldState``."""
        state = WorldState()

        # Run several observation actions concurrently-ish
        results = await asyncio.gather(
            self._exec(ActionType.CHECK_POSITION),
            self._exec(ActionType.CHECK_HEALTH),
            self._exec(ActionType.CHECK_HUNGER),
            self._exec(ActionType.CHECK_INVENTORY),
            self._exec(ActionType.CHECK_TIME),
            self._exec(ActionType.LIST_PLAYERS),
            return_exceptions=True,
        )

        pos_res, health_res, hunger_res, inv_res, time_res, players_res = results

        if isinstance(pos_res, ActionResult) and pos_res.success:
            raw = pos_res.data.get("position_raw", "")
            parsed = _parse_nbt_list(raw)
            if parsed and len(parsed) == 3:
                state.position = (parsed[0], parsed[1], parsed[2])

        if isinstance(health_res, ActionResult) and health_res.success:
            raw = health_res.data.get("health_raw", "")
            state.health = _parse_health_value(raw)

        if isinstance(hunger_res, ActionResult) and hunger_res.success:
            raw = hunger_res.data.get("hunger_raw", "")
            # hunger is an integer 0-20; fall through to None if unparseable
            try:
                h = int(float(raw)) if raw not in (None, "") else None
                if h is not None and 0 <= h <= 20:
                    state.hunger = h
            except (ValueError, TypeError):
                pass

        if isinstance(inv_res, ActionResult) and inv_res.success:
            state.inventory_raw = inv_res.data.get("raw_inventory", "")
            state.inventory = _parse_inventory_nbt(state.inventory_raw)

        if isinstance(time_res, ActionResult) and time_res.success:
            state.time_raw = time_res.data.get("time_raw", "")

        if isinstance(players_res, ActionResult) and players_res.success:
            state.players = players_res.data.get("players", [])

        # Also do a quick scan
        scan_res = await self._exec(ActionType.SCAN, {"radius": 5})
        if isinstance(scan_res, ActionResult) and scan_res.success:
            state.scan_data = scan_res.data

        # Best-effort authoritative biome detection, cached per chunk.
        if state.position:
            player_info = await self._mc.get_player_info()
            dimension = str(player_info.get("world") or "unknown")
            chunk_key = (
                dimension,
                math.floor(state.position[0] / 16),
                math.floor(state.position[1] / 16),
                math.floor(state.position[2] / 16),
            )
            if chunk_key in self._biome_cache:
                state.biome = self._biome_cache[chunk_key]
            else:
                try:
                    biome = await self._mc.get_biome(
                        int(state.position[0]),
                        int(state.position[1]),
                        int(state.position[2]),
                    )
                except Exception:
                    biome = "unknown"
                state.biome = biome or "unknown"
                self._biome_cache[chunk_key] = state.biome
        return state

    async def observe_position(self) -> tuple[float, float, float] | None:
        """Quick position-only check."""
        return await self._mc.get_player_pos()

    async def _exec(self, action: ActionType, params: dict | None = None) -> ActionResult:
        return await execute_action(self._mc, action, params)


# ── Simple NBT-value parsers (for command output) ──────────────────────


_NBT_LIST_RE = re.compile(r"\[([^\]]+)\]")
_NBT_VALUE_RE = re.compile(r"(-?\d+(?:\.\d*)?(?:[eE][+-]?\d*)?)[dfbsIL]?")
_NBT_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[bBsSlLfFdD]?$")


def _parse_nbt_value(raw: str) -> Any:
    """Try to extract a single numeric value from an NBT-encoded string."""
    raw = raw.strip()
    if not raw:
        return None
    m = _NBT_VALUE_RE.search(raw)
    if m:
        val = m.group(1)
        if "." in val or "e" in val.lower():
            return float(val)
        return int(float(val))
    return raw


def _parse_health_value(raw: str) -> float | None:
    """Parse a current health value without confusing it with max health."""
    text = str(raw or "").strip()
    match = re.search(
        r"\bhealth\s*:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        r"(?:[eE][+-]?\d+)?[bBsSlLfFdD]?)",
        text,
        re.IGNORECASE,
    )
    token = match.group(1) if match else text
    value = _parse_snbt_number(token)
    if value is None or value < 0:
        return None
    return float(value)


def _parse_snbt_number(raw: str) -> int | float | None:
    """Parse one complete SNBT numeric token, including its suffix."""
    token = raw.strip().strip('"')
    if not _NBT_NUMBER_RE.fullmatch(token):
        return None
    suffix = token[-1].lower() if token[-1].isalpha() else ""
    number = token[:-1] if suffix else token
    try:
        value = float(number)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    if suffix in {"b", "s", "l"} and value.is_integer():
        return int(value)
    return value


def _split_snbt_top_level(text: str) -> list[str]:
    """Split SNBT fields while ignoring nested compounds and quoted commas."""
    parts: list[str] = []
    start = 0
    brace_depth = 0
    bracket_depth = 0
    quote: str | None = None
    escaped = False

    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif char == "," and brace_depth == 0 and bracket_depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1

    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _extract_snbt_compounds(text: str) -> list[str]:
    """Extract top-level ``{...}`` compounds from an inventory list."""
    compounds: list[str] = []
    start: int | None = None
    depth = 0
    quote: str | None = None
    escaped = False

    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                compounds.append(text[start : index + 1])
                start = None
    return compounds


def _parse_snbt_compound(compound: str) -> dict[str, str]:
    """Parse top-level key/value pairs from one SNBT compound."""
    body = compound.strip()
    if not (body.startswith("{") and body.endswith("}")):
        return {}
    fields: dict[str, str] = {}
    for part in _split_snbt_top_level(body[1:-1]):
        key, separator, value = part.partition(":")
        if not separator:
            continue
        normalized_key = key.strip().strip('"').strip("'").lower()
        fields[normalized_key] = value.strip()
    return fields


def _strip_snbt_string(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _inventory_slot_from_fields(fields: dict[str, str]) -> InventorySlot | None:
    item_id = _strip_snbt_string(fields.get("id", ""))
    count = _parse_snbt_number(fields.get("count", ""))
    slot = _parse_snbt_number(fields.get("slot", ""))
    damage = _parse_snbt_number(fields.get("damage", "0"))
    if not item_id or not isinstance(count, (int, float)) or not float(count).is_integer():
        return None
    if not isinstance(slot, (int, float)) or not float(slot).is_integer():
        return None
    if not isinstance(damage, (int, float)) or not float(damage).is_integer():
        return None
    if count < 0 or damage < 0:
        return None
    return InventorySlot(
        item_id=item_id,
        count=int(count),
        slot=int(slot),
        damage=int(damage),
    )


def _parse_inventory_nbt(raw: str) -> list[InventorySlot]:
    """Parse an NBT inventory string while preserving valid partial entries."""
    if not raw or raw.strip() in {"[]", "Inventory: []"}:
        return []

    text = raw.strip()
    if text.startswith("Inventory: "):
        text = text[len("Inventory: ") :].strip()

    items: list[InventorySlot] = []
    for compound in _extract_snbt_compounds(text):
        item = _inventory_slot_from_fields(_parse_snbt_compound(compound))
        if item is not None:
            items.append(item)
    if items:
        return items

    # Keep support for JSON-like simulator fixtures.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        fields = {str(key).lower(): str(value) for key, value in entry.items()}
        item = _inventory_slot_from_fields(fields)
        if item is not None:
            items.append(item)
    return items


def _parse_nbt_list(raw: str) -> list[float] | None:
    """Parse something like ``[1.0d, 64.0d, 3.0d]`` into floats."""
    m = _NBT_LIST_RE.search(raw)
    if not m:
        return None
    parts = m.group(1).split(",")
    out: list[float] = []
    for part in parts:
        value = _parse_nbt_value(part.strip())
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out if out else None
