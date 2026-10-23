"""Grid-based A* pathfinding over the Minecraft world via block queries.

The pathfinder builds a local walkability grid around the player-to-goal
corridor, runs A* (or BFS where movement costs are uniform), and returns a
list of (x, z) waypoints the agent can walk along.

Usage::

    finder = Pathfinder(mc)
    waypoints = await finder.find_path(start_x, start_z, goal_x, goal_z)
    for wx, wz in waypoints:
        await walk_one_step(mc, wx, wz)
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass
from typing import Any

from .world_types import WorldAdapter

logger = logging.getLogger(__name__)

# Default pathfinder limits
_MAX_NODES = 500  # max nodes to explore before giving up
_SCAN_BATCH_SIZE = 25  # concurrent getBlock calls per batch
_STEP_COST = 1.0
_DIAG_COST = 1.414

# Maximum Y difference the pathfinder will try to climb in a single step
_MAX_STEP_UP = 1
_MAX_STEP_DOWN = 3  # max safe drop (no fall damage below 3 blocks)


@dataclass(frozen=True)
class Node:
    """Immutable node for the A* priority queue."""

    x: int
    z: int
    g: float  # cost from start
    h: float  # heuristic to goal
    parent: Any = None  # previous node (kept mutable for path reconstruction)

    def __lt__(self, other: Node) -> bool:
        return (self.g + self.h) < (other.g + other.h)


class Pathfinder:
    """Grid-based pathfinder that queries block types via MCPQ.

    The pathfinder operates on a 2D grid of (x, z) coordinates at a fixed
    Y-level (determined from the start position).  Every cell is checked
    for walkability via three block queries (head, feet, below).

    Once the walkability grid is built, A* finds the shortest route from
    start to goal, respecting obstacles.
    """

    def __init__(
        self,
        mc: WorldAdapter,
        max_nodes: int = _MAX_NODES,
        batch_size: int = _SCAN_BATCH_SIZE,
    ) -> None:
        self._mc = mc
        self._max_nodes = max_nodes
        self._batch_size = batch_size

    # ── Public API ────────────────────────────────────────────────────

    async def find_path(
        self,
        start_x: float,
        start_z: float,
        goal_x: float,
        goal_z: float,
        y_level: int | None = None,
    ) -> list[tuple[float, float]] | None:
        """Find a path from (start_x, start_z) to (goal_x, goal_z).

        Parameters
        ----------
        start_x, start_z : player's current position
        goal_x, goal_z   : target position
        y_level          : the Y-slice to pathfind on (auto-detected from
                           start position if not given)

        Returns
        -------
        list of (x, z) waypoints (including start and goal), or *None* if
        no path exists within ``max_nodes``.
        """
        sx, sz = int(start_x), int(start_z)
        gx, gz = int(goal_x), int(goal_z)

        # Clamp to a reasonable bounding box
        min_x = min(sx, gx) - 8  # margin around the corridor
        max_x = max(sx, gx) + 8
        min_z = min(sz, gz) - 8
        max_z = max(sz, gz) + 8

        width = max_x - min_x + 1
        height = max_z - min_z + 1
        area = width * height

        if area > self._max_nodes * 2:
            # Area too large — shrink the search space
            mid_x = (sx + gx) // 2
            mid_z = (sz + gz) // 2
            half = int((self._max_nodes // 2) ** 0.5)
            min_x = mid_x - half
            max_x = mid_x + half
            min_z = mid_z - half
            max_z = mid_z + half
            width = max_x - min_x + 1
            height = max_z - min_z + 1

        logger.info(
            "Pathfinding corridor: (%d,%d)→(%d,%d)  grid %d×%d = %d cells",
            sx,
            sz,
            gx,
            gz,
            width,
            height,
            width * height,
        )

        # Determine Y-level
        if y_level is None:
            pos = await self._mc.get_player_pos()
            if pos is None:
                logger.warning("Cannot determine Y-level for pathfinding")
                return None
            y_level = int(pos[1])

        # Scan walkability grid
        walkable, y_base = await self._scan_grid(
            min_x,
            max_x,
            min_z,
            max_z,
            y_level,
        )
        if walkable is None:
            return None

        # Ensure start and goal are within bounds
        sx_clamped = max(min_x, min(sx, max_x))
        sz_clamped = max(min_z, min(sz, max_z))
        gx_clamped = max(min_x, min(gx, max_x))
        gz_clamped = max(min_z, min(gz, max_z))

        # Run A*
        path = self._astar(
            walkable,
            width,
            min_x,
            min_z,
            sx_clamped,
            sz_clamped,
            gx_clamped,
            gz_clamped,
        )

        if path is None:
            logger.info("No path found within search area")
            return None

        # Convert grid coordinates back to world coordinates
        # (return as floats for sub-block precision)
        waypoints = [(float(px) + 0.5, float(pz) + 0.5) for px, pz in path]
        logger.info("Path found: %d waypoints", len(waypoints))
        return waypoints

    # ── Grid scanning ─────────────────────────────────────────────────

    async def _scan_grid(
        self,
        min_x: int,
        max_x: int,
        min_z: int,
        max_z: int,
        y_level: int,
    ) -> tuple[dict[tuple[int, int], bool], int] | tuple[None, None]:
        """Scan a rectangular area and return a walkability map.

        Returns
        -------
        (walkable_dict, y_used) where walkable_dict[(x, z)] = True/False.
        Returns (None, None) if the scan fails completely.
        """
        width = max_x - min_x + 1
        height = max_z - min_z + 1
        total = width * height
        logger.debug("Scanning %d×%d = %d blocks for pathfinding", width, height, total)

        # Try multiple Y offsets if the initial level isn't walkable for the start
        for y_offset in range(3):
            result = await self._scan_at_y(min_x, max_x, min_z, max_z, y_level + y_offset)
            if result is not None:
                return result, y_level + y_offset

        # Final attempt at original level
        result = await self._scan_at_y(min_x, max_x, min_z, max_z, y_level)
        if result is not None:
            return result, y_level
        return None, None

    async def _scan_at_y(
        self,
        min_x: int,
        max_x: int,
        min_z: int,
        max_z: int,
        y: int,
    ) -> dict[tuple[int, int], bool] | None:
        """Scan the grid at a fixed Y and return walkability."""

        # Build coordinate list
        coords = []
        for x in range(min_x, max_x + 1):
            for z in range(min_z, max_z + 1):
                coords.append((x, y, z))

        # Fetch blocks in batches
        head_map: dict[tuple[int, int], str] = {}
        feet_map: dict[tuple[int, int], str] = {}
        below_map: dict[tuple[int, int], str] = {}

        try:
            for i in range(0, len(coords), self._batch_size):
                batch = coords[i : i + self._batch_size]
                # Fire all 3 queries concurrently for the batch
                heads = await asyncio.gather(
                    *(self._safe_get_block(x, y + 1, z) for x, y, z in batch),
                    return_exceptions=True,
                )
                feets = await asyncio.gather(
                    *(self._safe_get_block(x, y, z) for x, y, z in batch),
                    return_exceptions=True,
                )
                belows = await asyncio.gather(
                    *(self._safe_get_block(x, y - 1, z) for x, y, z in batch),
                    return_exceptions=True,
                )

                for (x, _, z), h, f, b in zip(batch, heads, feets, belows, strict=False):
                    head_map[(x, z)] = str(h) if not isinstance(h, Exception) else "unknown"
                    feet_map[(x, z)] = str(f) if not isinstance(f, Exception) else "unknown"
                    below_map[(x, z)] = str(b) if not isinstance(b, Exception) else "unknown"
        except Exception as exc:
            logger.warning("Pathfinding grid scan failed: %s", exc)
            return None

        # Evaluate walkability
        from .actions import _is_hazard, _is_passable

        walkable: dict[tuple[int, int], bool] = {}
        for x, z in head_map:
            head = head_map[(x, z)]
            feet = feet_map[(x, z)]
            below = below_map[(x, z)]

            # Player needs passable space at head and feet
            if not _is_passable(head) or not _is_passable(feet):
                walkable[(x, z)] = False
                continue

            # Hazard check
            if _is_hazard(below) or _is_hazard(feet) or _is_hazard(head):
                walkable[(x, z)] = False
                continue

            walkable[(x, z)] = True

        return walkable

    async def _safe_get_block(self, x: int, y: int, z: int) -> str:
        """Get a block, returning 'unknown' on any error."""
        try:
            return await self._mc.get_block(x, y, z)
        except Exception:
            return "unknown"

    # ── A* algorithm ───────────────────────────────────────────────────

    def _astar(
        self,
        walkable: dict[tuple[int, int], bool],
        grid_width: int,
        offset_x: int,
        offset_z: int,
        start_x: int,
        start_z: int,
        goal_x: int,
        goal_z: int,
    ) -> list[tuple[int, int]] | None:
        """A* search on the pre-computed walkability grid.

        Returns a list of (x, z) grid coordinates from start to goal
        (inclusive), or *None* if unreachable.
        """
        if not walkable.get((start_x, start_z), False):
            logger.warning("Start cell (%d, %d) is not walkable", start_x, start_z)
            return None

        if not walkable.get((goal_x, goal_z), False):
            logger.warning("Goal cell (%d, %d) is not walkable", goal_x, goal_z)
            return None

        def heuristic(ax: int, az: int, bx: int, bz: int) -> float:
            """Chebyshev distance (allows 8-directional movement)."""
            return float(max(abs(ax - bx), abs(az - bz)))

        # Priority queue entries: (f_score, counter, x, z)
        start_h = heuristic(start_x, start_z, goal_x, goal_z)
        open_set: list[tuple[float, int, int, int]] = [
            (start_h, 0, start_x, start_z),
        ]
        heapq.heapify(open_set)

        came_from: dict[tuple[int, int], tuple[int, int] | None] = {
            (start_x, start_z): None,
        }
        g_score: dict[tuple[int, int], float] = {
            (start_x, start_z): 0.0,
        }

        counter = 1
        nodes_explored = 0

        # 8-directional neighbours
        neighbours = [
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),  # cardinal
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),  # diagonal
        ]

        while open_set and nodes_explored < self._max_nodes:
            f_current, _, cx, cz = heapq.heappop(open_set)
            current = (cx, cz)
            nodes_explored += 1

            if current == (goal_x, goal_z):
                # Reconstruct path
                return self._reconstruct_path(came_from, current)

            for dx, dz in neighbours:
                nx, nz = cx + dx, cz + dz
                neighbour = (nx, nz)

                # Check bounds (within our scan area)
                if not (offset_x <= nx <= offset_x + grid_width - 1):
                    continue
                # Check if walkable
                if not walkable.get(neighbour, False):
                    continue

                # Movement cost
                edge_cost = _DIAG_COST if dx != 0 and dz != 0 else _STEP_COST
                tentative_g = g_score[current] + edge_cost

                if tentative_g < g_score.get(neighbour, float("inf")):
                    came_from[neighbour] = current
                    g_score[neighbour] = tentative_g
                    f_score = tentative_g + heuristic(nx, nz, goal_x, goal_z)
                    counter += 1
                    heapq.heappush(open_set, (f_score, counter, nx, nz))

        logger.info("A* exhausted after %d nodes — no path to goal", nodes_explored)
        return None

    @staticmethod
    def _reconstruct_path(
        came_from: dict[tuple[int, int], tuple[int, int] | None],
        current: tuple[int, int] | None,
    ) -> list[tuple[int, int]]:
        """Walk backward from current to start, return forward path."""
        path: list[tuple[int, int]] = []
        while current is not None:
            path.append(current)
            current = came_from.get(current)
        path.reverse()
        return path


# ── Integration helper ─────────────────────────────────────────────────────


async def find_walk_path(
    mc: WorldAdapter,
    start_x: float,
    start_z: float,
    goal_x: float,
    goal_z: float,
    y_level: int | None = None,
) -> list[tuple[float, float]] | None:
    """Convenience wrapper: create a Pathfinder and find a path."""
    finder = Pathfinder(mc)
    return await finder.find_path(start_x, start_z, goal_x, goal_z, y_level)
