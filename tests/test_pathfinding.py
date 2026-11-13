"""Tests for the A* pathfinding module."""

from __future__ import annotations

import pytest

from minecraft_ai_bridge.minecraft.pathfinding import Pathfinder, find_walk_path
from tests.conftest import MockMcpqClient


class TestPathfinderFindPath:
    """Test the pathfinder's core path-finding capability."""

    async def test_negative_fractional_positions_use_containing_block(self):
        mc = MockMcpqClient()
        path = await Pathfinder(mc).find_path(-0.5, -0.5, -4.5, -4.5, y_level=65)
        assert path is not None
        assert path[0] == (-0.5, -0.5)
        assert path[-1] == (-4.5, -4.5)

    @pytest.mark.asyncio
    async def test_find_path_open_terrain(self) -> None:
        """Should find a direct straight-line path across open air."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)
        # Empty world = all air, free path

        finder = Pathfinder(mc, max_nodes=200)
        path = await finder.find_path(0.0, 0.0, 10.0, 10.0, y_level=65)

        assert path is not None
        assert len(path) >= 2  # at least start + goal
        # First waypoint should be near start
        assert abs(path[0][0] - 0.5) < 1.0
        assert abs(path[0][1] - 0.5) < 1.0
        # Last waypoint should be near goal
        assert abs(path[-1][0] - 10.5) < 1.0
        assert abs(path[-1][1] - 10.5) < 1.0

    @pytest.mark.asyncio
    async def test_find_path_around_wall(self) -> None:
        """Should navigate around a wall instead of walking through it."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        # Build a wall from x=5 to x=6, z=3 to z=8 at y=65 (feet) and y=66 (head)
        for z in range(3, 9):
            await mc.set_block("stone", 5, 65, z)
            await mc.set_block("stone", 5, 66, z)
            await mc.set_block("stone", 6, 65, z)
            await mc.set_block("stone", 6, 66, z)

        finder = Pathfinder(mc, max_nodes=500)
        path = await finder.find_path(0.0, 0.0, 10.0, 10.0, y_level=65)

        assert path is not None, "Should find a path around the wall"
        # The path should go around the wall at x=5..6
        # Check that no waypoint is inside the wall
        for wx, wz in path:
            ix, iz = int(wx), int(wz)
            assert not (5 <= ix <= 6 and 3 <= iz <= 8), (
                f"Waypoint ({wx:.1f}, {wz:.1f}) is inside the wall"
            )

    @pytest.mark.asyncio
    async def test_no_path_impassable(self) -> None:
        """Should return None when the goal is completely walled off."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        # Seal off the goal area with a solid box
        for x in range(8, 13):
            for z in range(8, 13):
                await mc.set_block("stone", x, 65, z)
                await mc.set_block("stone", x, 66, z)

        finder = Pathfinder(mc, max_nodes=200)
        path = await finder.find_path(0.0, 0.0, 10.0, 10.0, y_level=65)

        assert path is None, "Should return None when goal is unreachable"

    @pytest.mark.asyncio
    async def test_hazard_avoidance(self) -> None:
        """Should not path through hazardous blocks (lava, fire)."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        # Place lava between start and goal
        await mc.set_block("lava", 3, 64, 3)
        await mc.set_block("lava", 4, 64, 3)
        await mc.set_block("lava", 3, 64, 4)
        await mc.set_block("lava", 4, 64, 4)

        finder = Pathfinder(mc, max_nodes=400)
        path = await finder.find_path(0.0, 0.0, 6.0, 6.0, y_level=65)

        assert path is not None, "Should find a path around lava"
        # Check no waypoint has lava below
        for wx, wz in path:
            ix, iz = int(wx), int(wz)
            below = await mc.get_block(ix, 64, iz)
            assert "lava" not in below, f"Waypoint ({wx:.1f}, {wz:.1f}) has lava below"

    @pytest.mark.asyncio
    async def test_mcpq_failure_handling(self) -> None:
        """Should handle MCPQ get_block failures gracefully."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        # Patch get_block to raise on one coordinate
        original_get_block = mc.get_block

        async def flaky_get_block(x: int, y: int, z: int) -> str:
            if x == 5 and z == 5:
                raise RuntimeError("MCPQ timeout")
            return await original_get_block(x, y, z)

        mc.get_block = flaky_get_block  # type: ignore[assignment]

        finder = Pathfinder(mc, max_nodes=200)
        path = await finder.find_path(0.0, 0.0, 10.0, 10.0, y_level=65)

        # Should still find a path despite the flaky block read
        assert path is not None

    @pytest.mark.asyncio
    async def test_no_y_level_provided(self) -> None:
        """Should auto-detect Y-level from player position."""
        mc = MockMcpqClient()
        mc.set_position(12.0, 72.0, -5.0)

        finder = Pathfinder(mc, max_nodes=200)
        path = await finder.find_path(12.0, -5.0, 20.0, 5.0)

        assert path is not None
        assert len(path) >= 2


class TestFindWalkPath:
    """Test the convenience wrapper ``find_walk_path``."""

    @pytest.mark.asyncio
    async def test_convenience_wrapper(self) -> None:
        """Should create a Pathfinder and find a path."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        path = await find_walk_path(mc, 0.0, 0.0, 15.0, 15.0, y_level=65)
        assert path is not None
        assert len(path) >= 2

    @pytest.mark.asyncio
    async def test_pathfinder_reuse(self) -> None:
        """Multiple calls should work on the same Pathfinder instance."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        finder = Pathfinder(mc, max_nodes=200)

        path1 = await finder.find_path(0.0, 0.0, 5.0, 5.0, y_level=65)
        assert path1 is not None

        # Change position and find another path
        mc.set_position(10.0, 65.0, 10.0)
        path2 = await finder.find_path(10.0, 10.0, 20.0, 20.0, y_level=65)
        assert path2 is not None


class TestAStarEdgeCases:
    """Edge-case behavior of the A* implementation."""

    @pytest.mark.asyncio
    async def test_same_start_and_goal(self) -> None:
        """Start == goal should return a single-point path."""
        mc = MockMcpqClient()
        mc.set_position(5.0, 65.0, 5.0)

        finder = Pathfinder(mc, max_nodes=100)
        path = await finder.find_path(5.0, 5.0, 5.0, 5.0, y_level=65)

        assert path is not None
        assert len(path) >= 1

    @pytest.mark.asyncio
    async def test_diagonal_movement(self) -> None:
        """Should use diagonal moves for a more direct path."""
        mc = MockMcpqClient()
        mc.set_position(0.0, 65.0, 0.0)

        finder = Pathfinder(mc, max_nodes=200)
        path = await finder.find_path(0.0, 0.0, 10.0, 10.0, y_level=65)

        assert path is not None
        # Diagonal path should be more direct than axis-aligned.
        # With 8-directional A*, the path should route directly diagonal
        # which takes ~10-14 steps (each step ~1.4 blocks).
        assert len(path) <= 16, f"Diagonal path should be efficient, got {len(path)} steps"
