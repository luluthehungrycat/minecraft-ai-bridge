"""Tests for GoalManager — decomposition, fallback plans, progress tracking."""

from __future__ import annotations

import pytest

from minecraft_ai_bridge.bridge.goal_manager import GoalManager


@pytest.fixture
def mock_llm():
    from tests.conftest import MockLLMClient

    return MockLLMClient()


@pytest.fixture
def gm(mock_llm):
    return GoalManager(llm_client=mock_llm, max_depth=5)


@pytest.mark.asyncio
class TestGoalDecomposition:
    async def test_set_goal_without_llm(self):
        gm2 = GoalManager(llm_client=None)
        root = await gm2.set_goal("Build a house")
        assert root is not None
        assert len(root.sub_goals) > 0

    async def test_set_goal_with_llm(self, gm, mock_llm):
        mock_llm.set_decompose_return(
            [
                {"step": 1, "description": "Gather wood", "expected_actions": ["check"]},
                {"step": 2, "description": "Craft planks", "expected_actions": ["craft"]},
            ]
        )
        root = await gm.set_goal("Build a house")
        assert len(root.sub_goals) == 2
        assert root.sub_goals[0].description == "Gather wood"

    async def test_empty_llm_triggers_fallback(self, gm):
        root = await gm.set_goal("Mine for diamonds")
        assert len(root.sub_goals) > 0

    @pytest.mark.parametrize(
        "malformed",
        [
            [0.5],
            ["Gather wood"],
            [{"step": 1}],
            [{"description": ""}],
            [{"description": 3}],
            [{"description": "Gather wood"}, None],
            {"description": "Gather wood"},
        ],
    )
    async def test_malformed_llm_decomposition_uses_fallback(self, gm, mock_llm, malformed):
        mock_llm.set_decompose_return(malformed)
        fallback = await GoalManager(llm_client=None).set_goal("Explore")

        root = await gm.set_goal("Explore")

        assert [sg.description for sg in root.sub_goals] == [
            sg.description for sg in fallback.sub_goals
        ]
        assert gm.current_goal == root.sub_goals[0].description

    async def test_desc_alias_and_missing_step(self, gm, mock_llm):
        mock_llm.set_decompose_return([{"desc": "Gather wood"}, {"description": "Craft planks"}])
        root = await gm.set_goal("Build a house")
        assert [sg.description for sg in root.sub_goals] == ["Gather wood", "Craft planks"]
        assert [sg.priority for sg in root.sub_goals] == [1, 2]

    async def test_set_from_subgoals(self, gm):
        root = gm.set_goal_from_subgoals(
            "Test",
            [
                {"description": "Step 1"},
                {"description": "Step 2"},
            ],
        )
        assert len(root.sub_goals) == 2


@pytest.mark.asyncio
class TestFallbackPlans:
    async def test_build_plan(self, gm):
        root = await gm.set_goal("Build a cobblestone house")
        assert len(root.sub_goals) == 11

    async def test_mine_plan(self, gm):
        root = await gm.set_goal("Mine for diamonds")
        assert len(root.sub_goals) == 12

    async def test_farm_plan(self, gm):
        root = await gm.set_goal("Farm wheat for bread")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 10
        assert any("seed" in d.lower() for d in descs)

    async def test_workshop_plan(self, gm):
        root = await gm.set_goal("Craft an enchanting table")
        assert len(root.sub_goals) == 9

    async def test_explore_plan(self, gm):
        root = await gm.set_goal("Explore the area")
        assert len(root.sub_goals) == 8

    async def test_teleport_plan(self, gm):
        root = await gm.set_goal("Teleport to 100 64 -200")
        assert len(root.sub_goals) == 4

    async def test_combat_plan(self, gm):
        root = await gm.set_goal("Kill the zombie")
        assert len(root.sub_goals) == 7

    async def test_describe_plan(self, gm):
        root = await gm.set_goal("Describe your surroundings")
        assert len(root.sub_goals) == 5

    async def test_nether_portal_plan(self, gm):
        root = await gm.set_goal("Build a nether portal")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 8
        assert any("obsidian" in d.lower() for d in descs)
        assert any("flint" in d.lower() for d in descs)
        # Must beat the generic "build" plan (which is 11 steps)
        assert len(descs) != 11

    async def test_nether_portal_alt_phrasings(self, gm):
        """The nether pattern should match common variants."""
        for goal in (
            "Build a nether portal",
            "Go to the nether",
            "Make a portal to hell",
            "Build a hell portal",
            "Get some netherrack",
        ):
            root = await gm.set_goal(goal)
            descs = [s.description for s in root.sub_goals]
            assert len(descs) == 8, f"goal={goal!r} gave {len(descs)} steps"

    async def test_end_portal_plan(self, gm):
        root = await gm.set_goal("Activate the end portal")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 8
        assert any("ender pearl" in d.lower() for d in descs)
        assert any("eye" in d.lower() for d in descs)

    async def test_end_portal_alt_phrasings(self, gm):
        for goal in (
            "Find the end portal",
            "Kill the ender dragon",
            "Go to the end",
            "Find a stronghold",
        ):
            root = await gm.set_goal(goal)
            descs = [s.description for s in root.sub_goals]
            assert len(descs) == 8, f"goal={goal!r} gave {len(descs)} steps"

    async def test_redstone_plan(self, gm):
        root = await gm.set_goal("Build a redstone piston door")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 10
        assert any("redstone" in d.lower() for d in descs)
        assert any("repeater" in d.lower() for d in descs)

    async def test_redstone_alt_phrasings(self, gm):
        for goal in (
            "Build a redstone circuit",
            "Make a redstone lamp timer",
            "Set up a piston automation",
        ):
            root = await gm.set_goal(goal)
            descs = [s.description for s in root.sub_goals]
            assert len(descs) == 10, f"goal={goal!r} gave {len(descs)} steps"

    async def test_animal_farm_plan(self, gm):
        root = await gm.set_goal("Breed some cows")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 10
        assert any("fence" in d.lower() or "enclos" in d.lower() for d in descs)
        assert any("wheat" in d.lower() or "feed" in d.lower() for d in descs)

    async def test_animal_farm_alt_phrasings(self, gm):
        for goal in (
            "Breed some cows",
            "Set up a pig farm",
            "Build a chicken ranch",
            "Create a sheep pasture",
        ):
            root = await gm.set_goal(goal)
            descs = [s.description for s in root.sub_goals]
            assert len(descs) == 10, f"goal={goal!r} gave {len(descs)} steps"
        # And verify it beats the generic crop-farm plan (which is also 10)
        # by checking that enclosure/breeding language is present.
        root = await gm.set_goal("Breed some pigs")
        joined = " ".join(s.description for s in root.sub_goals).lower()
        assert "fence" in joined or "enclos" in joined

    async def test_villager_trading_plan(self, gm):
        root = await gm.set_goal("Trade with villagers for emeralds")
        descs = [s.description for s in root.sub_goals]
        assert len(descs) == 9
        assert any("emerald" in d.lower() for d in descs)
        assert any("village" in d.lower() or "villager" in d.lower() for d in descs)

    async def test_villager_trading_alt_phrasings(self, gm):
        for goal in (
            "Set up a librarian villager",
            "Trade emeralds for books",
            "Cure a zombie villager",
        ):
            root = await gm.set_goal(goal)
            descs = [s.description for s in root.sub_goals]
            assert len(descs) == 9, f"goal={goal!r} gave {len(descs)} steps"

    async def test_new_plans_beat_generic_build(self, gm):
        """Verify the new specific patterns are placed before the generic
        'build' pattern, so a goal like 'build a nether portal' matches the
        nether plan (8 steps), not the build plan (11 steps)."""
        nether_root = await gm.set_goal("Build a nether portal")
        assert len(nether_root.sub_goals) == 8
        redstone_root = await gm.set_goal("Build a redstone clock")
        assert len(redstone_root.sub_goals) == 10
        end_root = await gm.set_goal("Build an end portal")
        assert len(end_root.sub_goals) == 8

    async def test_generic_plan(self, gm):
        root = await gm.set_goal("Do something completely unique")
        assert len(root.sub_goals) == 6
        for sg in root.sub_goals:
            assert "Do something completely unique" in sg.description

    async def test_case_insensitive(self, gm):
        root = await gm.set_goal("BUILD A BRIDGE")
        assert len(root.sub_goals) == 11

    async def test_first_match_wins(self, gm):
        root = await gm.set_goal("Build a mine")
        assert len(root.sub_goals) == 11  # build plan (first match)


@pytest.mark.asyncio
class TestProgressTracking:
    async def test_initial_goal(self, gm):
        await gm.set_goal("Explore")
        assert gm.current_goal is not None
        assert gm.current_goal != "All goals complete!"

    async def test_advance(self, gm):
        await gm.set_goal("Explore")
        first = gm.current_goal
        gm.mark_current_complete()
        second = gm.current_goal
        assert second != first

    async def test_not_complete_initially(self, gm):
        await gm.set_goal("Explore")
        assert not gm.is_complete

    async def test_complete_after_all_done(self, gm):
        await gm.set_goal("Teleport to 0 64 0")
        for _ in range(4):
            gm.mark_current_complete()
        assert gm.is_complete

    async def test_progress_format(self, gm):
        await gm.set_goal("Explore")
        p = gm.progress
        assert "Goal:" in p
        assert "○" in p
        assert "CURRENT" in p

    async def test_progress_completed(self, gm):
        await gm.set_goal("Explore")
        gm.mark_current_complete()
        assert "✓" in gm.progress

    async def test_root_description(self, gm):
        await gm.set_goal("Test goal")
        assert gm.root_description == "Test goal"

    async def test_root_description_empty(self):
        gm2 = GoalManager()
        assert gm2.root_description == ""

    async def test_sub_goal_count(self, gm):
        await gm.set_goal("Explore")
        assert gm.sub_goal_count == 8

    async def test_sub_goal_count_none(self):
        gm2 = GoalManager()
        assert gm2.sub_goal_count == 0

    async def test_mark_complete_none(self, gm):
        gm._current = None
        gm.mark_current_complete()  # no-op, shouldn't crash

    async def test_current_goal_all_done(self, gm):
        gm._current = None
        assert gm.current_goal == "All goals complete!"

    async def test_set_goal_replaces(self, gm):
        await gm.set_goal("Explore")
        c1 = gm.sub_goal_count
        await gm.set_goal("Build")
        assert gm.sub_goal_count != c1
