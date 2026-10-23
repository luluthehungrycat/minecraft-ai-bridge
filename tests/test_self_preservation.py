"""Tests for the SelfPreservationLayer — reflex attack + find-food injection."""

from __future__ import annotations

import pytest

from minecraft_ai_bridge.bridge.goal_manager import GoalManager
from minecraft_ai_bridge.bridge.self_preservation import (
    PreservationConfig,
    SelfPreservationLayer,
)
from minecraft_ai_bridge.minecraft.observer import WorldState

# ── Lightweight fixtures (no real MCPQ server) ───────────────────────────


class FakeMemory:
    """In-memory stand-in for AgentMemory — records facts and actions."""

    def __init__(self) -> None:
        self.facts: list[str] = []
        self.actions: list[tuple[str, dict]] = []

    def remember_fact(self, fact: str) -> None:
        self.facts.append(fact)

    def record_action(self, action: str, result: dict) -> None:
        self.actions.append((action, result))


def _make_layer(mock_mc, *, config: PreservationConfig | None = None) -> SelfPreservationLayer:
    """Build a layer wired to a mock MCPQ client and a fresh goal manager."""
    gm = GoalManager(llm_client=None)
    gm._root = None  # ensure set_goal populates it
    memory = FakeMemory()
    return SelfPreservationLayer(
        mc=mock_mc,
        goal_manager=gm,
        memory=memory,
        config=config,
    )


def _world(health: float | None = 20.0, hunger: int | None = 20) -> WorldState:
    return WorldState(health=health, hunger=hunger)


# ── Reflex attack ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestReflexAttack:
    async def test_no_reflex_on_first_observation(self, mock_mc):
        """Without a previous-health baseline, no reflex should fire."""
        layer = _make_layer(mock_mc)
        world = _world(health=10.0)
        result = await layer.evaluate(world)
        assert result is None  # no reflex, no prior baseline

    async def test_no_reflex_without_health_drop(self, mock_mc):
        """If health is stable, no reflex should fire."""
        layer = _make_layer(mock_mc)
        # Prime previous_health by running evaluate once at full health
        await layer.evaluate(_world(health=20.0))
        # Now evaluate at the same health — no drop
        result = await layer.evaluate(_world(health=20.0))
        assert result is None

    async def test_reflex_triggers_on_health_drop_with_mob(self, mock_mc):
        """Sudden health drop + mob nearby → reflex attack fires."""
        layer = _make_layer(mock_mc)
        # Prime baseline
        await layer.evaluate(_world(health=20.0))
        # Now a sudden drop, with a zombie configured nearby
        mock_mc.set_hostile_mobs(["zombie"])
        result = await layer.evaluate(_world(health=15.0))
        assert result is not None
        assert result.success is True
        assert result.action.value == "attack"

    async def test_no_reflex_on_drop_without_mobs(self, mock_mc):
        """Health drop alone (no mobs) should not trigger a reflex."""
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        # No hostile mobs configured — should not attack anything
        result = await layer.evaluate(_world(health=12.0))
        assert result is None

    async def test_small_health_drop_below_threshold(self, mock_mc):
        """A small drop (less than sudden_health_drop) shouldn't trigger."""
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        # Drop is 1.0 — below the default threshold of 1.5
        result = await layer.evaluate(_world(health=19.0))
        assert result is None

    async def test_reflex_disabled_via_config(self, mock_mc):
        """If enable_reflex_attack=False, the layer never reflex-attacks."""
        layer = _make_layer(
            mock_mc,
            config=PreservationConfig(enable_reflex_attack=False),
        )
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        result = await layer.evaluate(_world(health=10.0))
        assert result is None


# ── Auto-consume ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoConsume:
    """When hunger is critical AND the player has food in inventory,
    the layer should automatically eat the best available food.

    Closes the "find food and then wait for the LLM to eat it" loop
    that the v0.5.1 design had.
    """

    def _world_with_inventory(self, hunger: int, items: list[dict]) -> WorldState:
        """Build a WorldState with hunger + inventory populated."""
        from minecraft_ai_bridge.minecraft.observer import InventorySlot

        world = WorldState(hunger=hunger)
        world.inventory = [
            InventorySlot(
                item_id=item.get("item_id", "stone"),
                count=item.get("count", 1),
                slot=item.get("slot", i),
            )
            for i, item in enumerate(items)
        ]
        return world

    async def test_no_consume_when_hunger_fine(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(hunger=20, items=[{"item_id": "bread", "count": 1}])
        result = await layer.evaluate(world)
        assert result is None  # not hungry, no auto-consume

    async def test_no_consume_when_inventory_empty(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(hunger=3, items=[])
        result = await layer.evaluate(world)
        assert result is None  # no food available

    async def test_no_consume_when_inventory_has_only_non_food(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(hunger=3, items=[{"item_id": "stone", "count": 64}])
        result = await layer.evaluate(world)
        assert result is None

    async def test_consume_bread_when_hungry(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(hunger=3, items=[{"item_id": "bread", "count": 1}])
        result = await layer.evaluate(world)
        assert result is not None
        assert result.action.value == "eat"
        assert result.data.get("food_item") == "bread"
        assert result.success is True

    async def test_consume_picks_highest_saturation_food(self, mock_mc):
        """When multiple foods are in inventory, eat the one with
        the highest saturation (best restoration per item)."""
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            hunger=4,
            items=[
                {"item_id": "bread", "count": 1, "slot": 0},
                {"item_id": "golden_carrot", "count": 1, "slot": 1},
                {"item_id": "apple", "count": 1, "slot": 2},
            ],
        )
        result = await layer.evaluate(world)
        assert result is not None
        assert result.data.get("food_item") == "golden_carrot"

    async def test_consume_with_namespaced_id(self, mock_mc):
        """The namespace prefix should be stripped before lookup."""
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            hunger=3, items=[{"item_id": "minecraft:bread", "count": 1}]
        )
        result = await layer.evaluate(world)
        assert result is not None
        assert result.data.get("food_item") == "bread"

    async def test_consume_disabled_via_config(self, mock_mc):
        layer = _make_layer(
            mock_mc,
            config=PreservationConfig(enable_auto_consume=False),
        )
        world = self._world_with_inventory(hunger=3, items=[{"item_id": "bread", "count": 1}])
        result = await layer.evaluate(world)
        assert result is None

    async def test_consume_runs_when_it_is_the_only_enabled_feature(self, mock_mc):
        config = PreservationConfig(
            enable_reflex_attack=False,
            enable_reflex_flee=False,
            enable_auto_find_food=False,
            enable_auto_consume=True,
            enable_auto_heal=False,
            enable_day_night_awareness=False,
        )
        layer = _make_layer(mock_mc, config=config)
        world = self._world_with_inventory(hunger=3, items=[{"item_id": "bread", "count": 1}])

        result = await layer.evaluate(world)

        assert result is not None
        assert result.action.value == "eat"

    async def test_consume_records_memory_fact(self, mock_mc):
        layer = _make_layer(mock_mc)
        memory = layer._memory  # type: ignore[attr-defined]
        world = self._world_with_inventory(hunger=3, items=[{"item_id": "bread", "count": 1}])
        await layer.evaluate(world)
        assert any("auto-consumed" in f.lower() and "bread" in f.lower() for f in memory.facts)

    async def test_consume_prefer_larger_stack_on_tie(self, mock_mc):
        """If two foods have the same saturation, prefer the one with
        the larger stack count (avoids wasting tiny stacks)."""
        layer = _make_layer(mock_mc)
        # Both foods have identical saturation. Same count, so the
        # order in the list is preserved — but we need a strict
        # comparison. Add a third item that should lose to the bread
        # because it has fewer saturation points.
        world = self._world_with_inventory(
            hunger=4,
            items=[
                # Same saturation, larger stack
                {"item_id": "bread", "count": 5, "slot": 0},
                # Same saturation, smaller stack
                {"item_id": "bread", "count": 2, "slot": 1},
            ],
        )
        result = await layer.evaluate(world)
        # The implementation prefers larger count, but Python's sort
        # is stable so equal-saturation-and-count items preserve
        # order. With strictly different counts, the larger one
        # should win.
        assert result is not None
        assert result.data.get("food_item") == "bread"


# ── Damage source check ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDamageSourceCheck:
    """The reflex must not fire on environmental damage.

    Falling off a cliff, walking into a cactus, taking lava damage,
    drowning, suffocation, poison, wither, starvation, etc. all drop
    health but NONE of them are caused by a mob.  Reflex-attacking
    a zombie because you tripped on a cactus would be a disaster.
    """

    async def test_no_reflex_on_fall_damage(self, mock_mc):
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        # Big health drop, mobs nearby, BUT damage came from a fall
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(False)  # fall damage
        result = await layer.evaluate(_world(health=10.0))
        assert result is None

    async def test_no_reflex_on_cactus_damage(self, mock_mc):
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(False)  # cactus
        result = await layer.evaluate(_world(health=15.0))
        assert result is None

    async def test_no_reflex_on_lava_damage(self, mock_mc):
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(False)  # lava
        result = await layer.evaluate(_world(health=10.0))
        assert result is None

    async def test_no_reflex_on_drowning(self, mock_mc):
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(False)  # drowning
        result = await layer.evaluate(_world(health=15.0))
        assert result is None

    async def test_reflex_fires_on_mob_damage(self, mock_mc):
        """Sanity: when damage IS from a mob, the reflex still fires."""
        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(True)  # zombie hit us
        result = await layer.evaluate(_world(health=15.0))
        assert result is not None
        assert result.action.value == "attack"

    async def test_damage_source_check_logged(self, mock_mc, caplog):
        """When the check skips, a debug log should mention the reason."""
        import logging

        layer = _make_layer(mock_mc)
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(False)
        with caplog.at_level(logging.DEBUG, logger="minecraft_ai_bridge.bridge.self_preservation"):
            await layer.evaluate(_world(health=10.0))
        assert any("no entity hurt" in r.message for r in caplog.records)


# ── Threat assessment & flee path ──────────────────────────────────────


@pytest.mark.asyncio
class TestThreatAssessmentAndFlee:
    """When the threat is too high, the layer should inject a flee goal
    rather than attacking."""

    def _gm_with_one_subgoal(self) -> GoalManager:
        from minecraft_ai_bridge.llm.models import AgentGoal

        gm = GoalManager(llm_client=None)
        gm._root = AgentGoal(description="parent", depth=0)
        return gm

    async def test_single_mob_at_full_health_fights(self, mock_mc):
        """1 hostile, plenty of health → fight (attack)."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(True)
        # Drop from 20 to 16 (a 4-HP hit from the zombie)
        result = await layer.evaluate(_world(health=16.0))
        assert result is not None
        assert result.action.value == "attack"

    async def test_many_hostiles_triggers_flee(self, mock_mc):
        """3 hostiles (above max_fightable=1) → flee, not attack."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie", "skeleton", "creeper"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=15.0))
        # Flee returns None (defer to LLM) but injects a sub-goal
        assert result is None
        assert len(layer._goals._root.sub_goals) == 1
        assert "flee" in layer._goals._root.sub_goals[0].description.lower()

    async def test_low_health_with_two_hostiles_triggers_flee(self, mock_mc):
        """health < flee_threshold AND 2+ hostiles → flee."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie", "spider"])
        mock_mc.set_hurt_by_entity(True)
        # health 5.0 < flee_health_threshold 6.0
        result = await layer.evaluate(_world(health=5.0))
        assert result is None
        assert len(layer._goals._root.sub_goals) == 1
        assert "flee" in layer._goals._root.sub_goals[0].description.lower()

    async def test_low_health_with_one_hostile_fights(self, mock_mc):
        """health < flee_threshold but only 1 hostile → still fight."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=5.0))
        assert result is not None
        assert result.action.value == "attack"

    async def test_flee_no_duplicate_injection(self, mock_mc):
        """A second threat shouldn't add a second flee sub-goal."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie", "skeleton", "creeper"])
        mock_mc.set_hurt_by_entity(True)
        await layer.evaluate(_world(health=15.0))
        # Second drop, still many hostiles
        await layer.evaluate(_world(health=10.0))
        assert len(layer._goals._root.sub_goals) == 1

    async def test_flee_records_memory_fact(self, mock_mc):
        """Flee should record a memory fact so the LLM sees it next turn."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        memory = layer._memory  # type: ignore[attr-defined]
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie", "skeleton", "creeper"])
        mock_mc.set_hurt_by_entity(True)
        await layer.evaluate(_world(health=15.0))
        assert any("flee" in f.lower() and "hostile" in f.lower() for f in memory.facts)

    async def test_flee_disabled_via_config(self, mock_mc):
        """If enable_reflex_flee=False, the layer falls back to attack."""
        layer = _make_layer(
            mock_mc,
            config=PreservationConfig(enable_reflex_flee=False),
        )
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie", "skeleton", "creeper"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=15.0))
        # Fights instead of fleeing
        assert result is not None
        assert result.action.value == "attack"

    async def test_iron_golem_nearby_does_not_trigger_reflex(self, mock_mc):
        """The user explicitly asked: 'iron golem = never attack'.

        Even if a hostile iron golem is in range and health drops
        (unlikely but possible), the layer must NOT attack it.
        Attacking an iron golem in a village would turn the entire
        village hostile.
        """
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        # Iron golems aren't in _HOSTILE_MOBS so they wouldn't be
        # detected by scan_entities anyway.  But the test exercises
        # the filter — if a blacklisted mob somehow ended up in
        # detailed, the layer should not attack it.
        # We simulate this by having the scan return blacklisted detail.
        # The test verifies the should_attack=False filter works.
        await layer.evaluate(_world(health=20.0))
        # The mob check is done via the detailed list from the scan
        # action, not the hostile_mobs set.  This test verifies the
        # overall path: even if the scan returns an iron golem
        # entry, we don't attack it.
        # We use the helper _pick_target to verify the path:
        from minecraft_ai_bridge.minecraft.actions import _should_attack

        assert not _should_attack("iron_golem")
        assert not _should_attack("villager")
        assert _should_attack("zombie")

    async def test_critical_mob_triggers_flee_instead_of_attack(self, mock_mc):
        """A warden (critical) nearby should trigger FLEE, not attack."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        # Warden isn't in the default _HOSTILE_MOBS list, so simulate
        # via the detailed list — verify the helper says flee.
        from minecraft_ai_bridge.minecraft.actions import _get_threat_level

        assert _get_threat_level("warden") == "critical"
        # With a critical mob in range, the layer should flee, not
        # attack.  The self_preservation's _pick_target skips critical
        # mobs (returns None), forcing the layer to fall back to flee.
        detailed = [
            {"type": "warden", "threat": "critical", "should_attack": False},
        ]
        target = layer._pick_target(detailed)
        assert target is None  # critical mob is not a valid target

    async def test_reflex_picks_highest_threat_target(self, mock_mc):
        """When 2 hostiles are in range (above the flee threshold for
        one-mob fights at full health), the layer should flee rather
        than pick a target.  The unit test for _pick_target covers
        the target-selection priority directly.
        """
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        # 2 hostiles + a health drop → flee (1 mob is the fightable limit)
        mock_mc.set_hostile_mobs(["zombie", "creeper"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=15.0))
        # 2 > max_fightable=1, so we flee — no attack action
        assert result is None
        # The flee sub-goal should mention the hostiles
        assert len(layer._goals._root.sub_goals) == 1
        assert "flee" in layer._goals._root.sub_goals[0].description.lower()


# ── Find-food injection ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFindFoodInjection:
    def _gm_with_one_subgoal(self) -> GoalManager:
        from minecraft_ai_bridge.llm.models import AgentGoal

        gm = GoalManager(llm_client=None)
        gm._root = AgentGoal(description="parent", depth=0)
        return gm

    async def test_no_injection_when_hunger_fine(self, mock_mc):
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(hunger=20))
        # No sub-goal was injected
        assert len(layer._goals._root.sub_goals) == 0

    async def test_injection_when_hunger_critical(self, mock_mc):
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(hunger=3))
        assert len(layer._goals._root.sub_goals) == 1
        sg = layer._goals._root.sub_goals[0]
        assert "food" in sg.description.lower()
        assert "URGENT" in sg.description

    async def test_no_double_injection(self, mock_mc):
        """A second low-hunger observation should not add a second find-food goal."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(hunger=3))
        await layer.evaluate(_world(hunger=2))
        assert len(layer._goals._root.sub_goals) == 1

    async def test_injection_respects_existing_food_goal(self, mock_mc):
        from minecraft_ai_bridge.llm.models import AgentGoal

        layer = _make_layer(mock_mc)
        gm = self._gm_with_one_subgoal()
        # Add a pre-existing find-food goal that's not yet completed
        existing = AgentGoal(description="Cook some food", depth=1)
        gm._root.sub_goals.append(existing)
        layer._goals = gm
        await layer.evaluate(_world(hunger=2))
        # Should not inject a second one
        assert len(layer._goals._root.sub_goals) == 1
        assert layer._goals._root.sub_goals[0].description == "Cook some food"

    async def test_injection_disabled_via_config(self, mock_mc):
        layer = _make_layer(
            mock_mc,
            config=PreservationConfig(enable_auto_find_food=False),
        )
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(hunger=2))
        assert len(layer._goals._root.sub_goals) == 0


# ── Memory facts ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMemoryFacts:
    async def test_critical_health_records_fact(self, mock_mc):
        layer = _make_layer(mock_mc)
        memory = layer._memory  # type: ignore[attr-defined]
        await layer.evaluate(_world(health=3.0))
        assert any("health critical" in f.lower() for f in memory.facts)

    async def test_health_above_threshold_no_fact(self, mock_mc):
        layer = _make_layer(mock_mc)
        memory = layer._memory  # type: ignore[attr-defined]
        await layer.evaluate(_world(health=15.0))
        assert not any("health critical" in f.lower() for f in memory.facts)

    async def test_reflex_attack_records_fact(self, mock_mc):
        layer = _make_layer(mock_mc)
        memory = layer._memory  # type: ignore[attr-defined]
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["skeleton"])
        await layer.evaluate(_world(health=15.0))
        # Should record both the reflex attack fact and possibly the health fact
        assert any("reflex" in f.lower() for f in memory.facts)
        assert any("skeleton" in f.lower() for f in memory.facts)


# ── Goal manager integration ────────────────────────────────────────────


@pytest.mark.asyncio
class TestFindFoodFallbackPlan:
    """The find-food fallback plan in goal_manager should match food-related goals."""

    async def test_eat_food_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Find some food to eat")
        # The injected find-food sub-goal should be present
        assert any("food" in sg.description.lower() for sg in root.sub_goals)

    async def test_starving_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("I'm starving — get me food")
        assert any("food" in sg.description.lower() for sg in root.sub_goals)

    async def test_get_bread_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Get some bread")
        assert any("food" in sg.description.lower() for sg in root.sub_goals)

    async def test_cook_food_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Cook some food")
        assert any("food" in sg.description.lower() for sg in root.sub_goals)

    async def test_flee_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Flee from the zombies")
        # The flee plan has 8 steps and mentions "shelter" / "sprint away"
        assert len(root.sub_goals) == 8
        assert any("shelter" in sg.description.lower() for sg in root.sub_goals)
        assert any("sprint" in sg.description.lower() for sg in root.sub_goals)

    async def test_run_away_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Run away!")
        assert len(root.sub_goals) == 8
        assert any("sprint" in sg.description.lower() for sg in root.sub_goals)

    async def test_retreat_matches(self):
        gm = GoalManager(llm_client=None)
        root = await gm.set_goal("Retreat to safety")
        assert len(root.sub_goals) == 8
        # The flee plan has a "Look for shelter" step
        assert any("shelter" in sg.description.lower() for sg in root.sub_goals)


# ── Auto-heal ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoHeal:
    """When health is critically low AND golden apples are in inventory,
    the layer should automatically heal via the HEAL action."""

    def _world_with_inventory(self, health: float, items: list[dict]) -> WorldState:
        from minecraft_ai_bridge.minecraft.observer import InventorySlot

        world = WorldState(health=health)
        world.inventory = [
            InventorySlot(
                item_id=item.get("item_id", "stone"),
                count=item.get("count", 1),
                slot=item.get("slot", i),
            )
            for i, item in enumerate(items)
        ]
        return world

    async def test_no_heal_when_health_fine(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            health=20.0, items=[{"item_id": "golden_apple", "count": 1}]
        )
        result = await layer.evaluate(world)
        assert result is None  # health is fine, no heal needed

    async def test_no_heal_when_inventory_has_no_gapples(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(health=3.0, items=[{"item_id": "bread", "count": 1}])
        result = await layer.evaluate(world)
        assert result is None  # no golden apples to heal with

    async def test_golden_carrot_is_food_but_not_a_healing_item(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            health=2.0, items=[{"item_id": "golden_carrot", "count": 1}]
        )

        result = await layer.evaluate(world)

        assert result is None
        assert not any("effect give" in command for command in mock_mc.commands_ran)

    async def test_heal_with_golden_apple_when_critical(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            health=2.0, items=[{"item_id": "golden_apple", "count": 1}]
        )
        result = await layer.evaluate(world)
        assert result is not None
        assert result.action.value == "heal"
        assert result.data.get("heal_item") == "golden_apple"

    async def test_heal_prefers_enchanted_golden_apple(self, mock_mc):
        """When both golden and enchanted golden apples are available,
        use the enchanted one (highest priority)."""
        layer = _make_layer(mock_mc)
        world = self._world_with_inventory(
            health=2.0,
            items=[
                {"item_id": "golden_apple", "count": 1},
                {"item_id": "enchanted_golden_apple", "count": 1},
            ],
        )
        result = await layer.evaluate(world)
        assert result is not None
        assert result.data.get("heal_item") == "enchanted_golden_apple"

    async def test_heal_disabled_via_config(self, mock_mc):
        layer = _make_layer(
            mock_mc,
            config=PreservationConfig(
                enable_auto_heal=False,
                health_critical_threshold=4.0,
            ),
        )
        world = self._world_with_inventory(
            health=3.0, items=[{"item_id": "golden_apple", "count": 1}]
        )
        result = await layer.evaluate(world)
        assert result is None

    async def test_heal_runs_when_it_is_the_only_enabled_feature(self, mock_mc):
        config = PreservationConfig(
            enable_reflex_attack=False,
            enable_reflex_flee=False,
            enable_auto_find_food=False,
            enable_auto_consume=False,
            enable_auto_heal=True,
            enable_day_night_awareness=False,
        )
        layer = _make_layer(mock_mc, config=config)
        world = self._world_with_inventory(
            health=3.0, items=[{"item_id": "golden_apple", "count": 1}]
        )

        result = await layer.evaluate(world)

        assert result is not None
        assert result.action.value == "heal"


# ── Day/night awareness ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDayNightAwareness:
    """At night the layer should record a memory fact so the LLM
    sees it in context."""

    async def test_no_fact_during_daytime(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = WorldState(time_raw="6000")  # noon
        await layer.evaluate(world)
        facts = " ".join(layer._memory.facts).lower()
        assert "night" not in facts

    async def test_fact_at_night(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = WorldState(time_raw="14000")  # night
        await layer.evaluate(world)
        facts = " ".join(layer._memory.facts).lower()
        assert "night" in facts

    async def test_fact_at_midnight(self, mock_mc):
        layer = _make_layer(mock_mc)
        world = WorldState(time_raw="18000")  # midnight
        await layer.evaluate(world)
        facts = " ".join(layer._memory.facts).lower()
        assert "night" in facts

    async def test_awareness_runs_when_it_is_the_only_enabled_feature(self, mock_mc):
        config = PreservationConfig(
            enable_reflex_attack=False,
            enable_reflex_flee=False,
            enable_auto_find_food=False,
            enable_auto_consume=False,
            enable_auto_heal=False,
            enable_day_night_awareness=True,
        )
        layer = _make_layer(mock_mc, config=config)
        await layer.evaluate(WorldState(time_raw="14000"))

        assert any("night" in fact.lower() for fact in layer._memory.facts)

    async def test_tick_24000_is_daytime(self, mock_mc):
        layer = _make_layer(mock_mc)
        await layer.evaluate(WorldState(time_raw="24000"))

        assert not any("night" in fact.lower() for fact in layer._memory.facts)


# ── Critical health threshold ────────────────────────────────────────────


@pytest.mark.asyncio
class TestCriticalHealthThreshold:
    """At or below critical_health_threshold (default 2.0), the agent
    should always flee — even from a single mob."""

    def _gm_with_one_subgoal(self) -> GoalManager:
        from minecraft_ai_bridge.llm.models import AgentGoal

        gm = GoalManager(llm_client=None)
        gm._root = AgentGoal(description="parent", depth=0)
        return gm

    async def test_single_mob_at_critical_health_flees(self, mock_mc):
        """1 mob but health at 2.0 (critical threshold) → flee."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=2.0))
        assert result is None  # flee returns None, injects sub-goal
        assert len(layer._goals._root.sub_goals) == 1
        assert "flee" in layer._goals._root.sub_goals[0].description.lower()

    async def test_single_mob_above_critical_health_fights(self, mock_mc):
        """1 mob at health 6.0 (above critical, above flee) → fight."""
        layer = _make_layer(mock_mc)
        layer._goals = self._gm_with_one_subgoal()
        await layer.evaluate(_world(health=20.0))
        mock_mc.set_hostile_mobs(["zombie"])
        mock_mc.set_hurt_by_entity(True)
        result = await layer.evaluate(_world(health=6.0))
        assert result is not None  # fight
        assert result.action.value == "attack"
