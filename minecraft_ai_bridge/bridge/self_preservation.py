"""Self-preservation layer — runs after observation, before the LLM thinks.

The orchestrator's main loop is LLM-driven: the model sees the world
state, decides what action to take, and the bridge executes it.  That
works for deliberate planning but has a latency problem for reflex
behaviours.  If a hostile mob walks up and hits the player, waiting
for the LLM to "ponder on what to do" can mean taking several more
hits before the agent responds.

This layer fills that gap.  After each observation cycle, it makes a
few cheap checks and either:

* **Reflex attack** — if the player's health just dropped AND the
  damage came from an entity AND a hostile mob is nearby AND the
  threat is manageable, attack it *immediately* without round-tripping
  through the LLM.  The damage-source check is critical: it skips the
  reflex when the player fell off a cliff, walked into a cactus, took
  lava damage, drowned, suffocated, was hit by their own TNT, etc.
  Those situations need different responses (or none at all), not
  "go attack a zombie."
* **Reflex flee** — if the threat is *too* high (multiple hostiles,
  critically low health, etc.), inject an URGENT "flee to safety"
  sub-goal at the front of the goal list.  The LLM then takes over
  and decides how to escape.  When the agent is safe, the original
  task naturally becomes current again — that's the regroup.
* **Find-food injection** — if hunger is critically low and no
  find-food sub-goal is already pending, inject one.
* **Memory fact** — if health is critical, record a fact so the LLM
  sees the warning in its context on the next turn.

The layer is opt-in via ``PreservationConfig`` and never blocks the
LLM from running — it can only inject an action result, never
silently swallow one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..llm.models import AgentGoal
from ..minecraft.actions import (
    ActionResult,
    ActionType,
    _food_value,
    _is_food,
    execute_action,
)
from ..minecraft.observer import InventorySlot, WorldState
from ..runtime import ActionService, WorldAdapter

if TYPE_CHECKING:
    from .goal_manager import GoalManager
    from .memory import AgentMemory

logger = logging.getLogger(__name__)


@dataclass
class PreservationConfig:
    """Tuning knobs for the self-preservation layer.

    All thresholds are in Minecraft units (health and hunger are
    0-20 each; a sudden health drop is in HP lost between turns).
    """

    # When health drops below this, log a memory fact so the LLM sees it.
    health_critical_threshold: float = 4.0
    # When hunger drops below this, inject a "find food" sub-goal.
    hunger_critical_threshold: int = 6
    # How far to scan for hostile mobs when looking for a reflex target.
    mob_alert_radius: int = 5
    # Minimum health drop (HP) in one turn to trigger a reflex attack.
    sudden_health_drop: float = 1.5
    # Damage amount for reflex attacks. 8 = 4 hearts — enough to scare
    # most mobs but not enough to one-shot a creeper next to the player.
    reflex_damage: int = 8
    # Threat thresholds.  Above these, prefer flee over fight.
    max_fightable_mobs: int = 1
    flee_health_threshold: float = 6.0
    # Below this health, the agent will always flee (even from a single mob)
    # rather than fight.
    critical_health_threshold: float = 2.0
    # Feature flags
    enable_reflex_attack: bool = True
    enable_reflex_flee: bool = True
    enable_auto_find_food: bool = True
    enable_auto_consume: bool = True
    enable_auto_heal: bool = True
    enable_day_night_awareness: bool = True


# Result of a threat assessment.  Used to decide fight-vs-flee.
THREAT_FIGHT = "fight"
THREAT_FLEE = "flee"


class SelfPreservationLayer:
    """Inserts reflex actions and urgent sub-goals between observe and think.

    Owned by the :class:`Orchestrator`.  Single instance per agent run
    so it can track the previous-turn health for surprise-attack
    detection.

    Usage from the orchestrator::

        reflex = await self._preservation.evaluate(world)
        if reflex is not None:
            result = reflex           # skip the LLM this turn
        else:
            ...                        # normal LLM-driven flow
    """

    def __init__(
        self,
        mc: WorldAdapter,
        goal_manager: GoalManager | None,
        memory: AgentMemory | None,
        config: PreservationConfig | None = None,
        action_service: ActionService | None = None,
        safety_authority: object | None = None,
    ) -> None:
        self._mc = mc
        self._goals = goal_manager
        self._memory = memory
        self._config = config or PreservationConfig()
        # Shared policy/audit boundary for self-preservation mutations.
        # The orchestrator supplies its ActionService instance so reflex
        # ATTACK, auto EAT, and auto HEAL pass the same policy hook and
        # audit trace as LLM-selected actions. When None (legacy direct
        # construction), those mutations fall back to the raw dispatcher
        # and remain outside the boundary. Read-only observation calls
        # (SCAN_ENTITIES, get_hurt_by_entity) are never routed through
        # the service.
        self._action_service = action_service
        self._safety_authority = safety_authority
        # Without a goal manager (an external controller drives the bot),
        # flee and find-food sub-goals are not injected; without memory,
        # facts for the LLM are dropped. Reflexes still run.
        # Last seen health — used to detect sudden drops. None means
        # the first observation hasn't happened yet.
        self._previous_health: float | None = None

    def _remember(self, fact: str) -> None:
        if self._memory is not None:
            self._memory.remember_fact(fact)

    async def _dispatch(
        self,
        action: ActionType,
        params: dict[str, Any],
    ) -> ActionResult:
        """Dispatch a self-preservation mutation through the action gate.

        Reflex ATTACK, auto EAT, and auto HEAL are the layer's mutating
        actions: when the orchestrator supplied its shared ActionService
        they go through the same policy hook and audit trace as
        LLM-selected actions. Without a service (legacy direct
        construction) they dispatch directly, preserving the previous
        behavior. Observation calls are never dispatched through here.
        """
        if self._action_service is not None:
            return await self._action_service.execute(
                action,
                params,
                safety_authority=self._safety_authority,
                preemptive=True,
            )
        return await execute_action(self._mc, action, params)

    async def evaluate(self, world: WorldState) -> ActionResult | None:
        """Run the preservation checks against the latest world state.

        Returns an :class:`ActionResult` if the layer wants to act
        immediately (suppressing the LLM for this turn), or ``None``
        to defer to normal LLM-driven flow.
        """
        any_enabled = (
            self._config.enable_reflex_attack
            or self._config.enable_reflex_flee
            or self._config.enable_auto_find_food
            or self._config.enable_auto_consume
            or self._config.enable_auto_heal
            or self._config.enable_day_night_awareness
        )
        if not any_enabled:
            return None

        # Reflex attack/flee takes priority — if we're getting hit,
        # deal with that first, worry about food second.
        reflex: ActionResult | None = None
        if self._config.enable_reflex_attack or self._config.enable_reflex_flee:
            reflex = await self._maybe_handle_threat(world)

        # Auto-injection happens regardless of whether we acted above —
        # the LLM still gets to think on the next turn.
        if self._config.enable_auto_find_food:
            self._maybe_inject_find_food(world)

        # Auto-consume: if hungry AND have food, eat it immediately.
        # Returns an ActionResult to skip the LLM this turn; falls
        # through if no food is in inventory.
        if self._config.enable_auto_consume and reflex is None:
            reflex = await self._maybe_auto_consume(world)

        # Auto-heal: if health is critically low AND golden apples
        # (or similar healing items) are in inventory, use them without
        # waiting for the LLM to decide.
        if self._config.enable_auto_heal and reflex is None:
            reflex = await self._maybe_auto_heal(world)

        # Day/night awareness: when night falls, record a memory fact
        # so the LLM sees it in its context on the next turn.
        if self._config.enable_day_night_awareness:
            self._maybe_note_time_of_day(world)

        # Memory hint for the LLM context window.
        if world.health is not None and world.health < self._config.health_critical_threshold:
            self._remember(f"Health critical: {world.health}/20 — find safety or heal")

        # Track health for next-turn drop detection. We do this after
        # the reflex logic so the drop is measured against the previous
        # turn's value, not the one we just observed.
        if world.health is not None:
            self._previous_health = world.health

        return reflex

    # ── Threat handling (reflex attack / reflex flee) ──────────────

    async def _maybe_handle_threat(self, world: WorldState) -> ActionResult | None:
        """Decide between reflex attack, reflex flee, and no action.

        The decision tree:
          1. Did the player's health just drop? If not, no threat.
          2. Was the drop caused by an entity? If not (fall, cactus,
             fire, etc.), this is accidental damage — no reflex.
          3. Are any hostile mobs nearby? If not, the threat is gone
             and we don't reflex-attack.
          4. Is the threat manageable? If yes, attack the nearest mob.
             If no (too many hostiles, too low health), inject a
             "flee to safety" sub-goal and let the LLM take over.
        """
        # Step 1: health drop?
        if self._previous_health is None or world.health is None:
            return None
        drop = self._previous_health - world.health
        if drop < self._config.sudden_health_drop:
            return None

        # Step 2: damage source check — was the player hit by an entity?
        # This is the critical guard against reflex-on-falling-damage,
        # reflex-on-cactus, etc.  Uses /data get entity @p LastHurtByEntity.
        try:
            hurt_by_entity = await self._mc.get_hurt_by_entity()
        except Exception as exc:
            logger.debug("get_hurt_by_entity check failed: %s", exc)
            hurt_by_entity = False
        if not hurt_by_entity:
            logger.debug(
                "Health dropped by %.1f but no entity hurt the player — "
                "skipping reflex (likely fall/cactus/fire/etc.)",
                drop,
            )
            return None

        # Step 3: any hostile mobs nearby?
        scan = await execute_action(
            self._mc,
            ActionType.SCAN_ENTITIES,
            {"radius": self._config.mob_alert_radius},
        )
        if not scan.success:
            return None
        mobs: list[str] = scan.data.get("mobs_nearby", [])
        if not mobs:
            return None
        # Use the detailed list (from PR #9 / context-aware combat) to
        # pick a target that is actually safe to attack.  The
        # `should_attack` field is False for blacklisted mobs (iron
        # golems, villagers, tamed animals) and critical-threat mobs
        # (warden, wither) — never engage those reflexively.
        detailed: list[dict] = scan.data.get("detailed", [])
        attackable = [m for m in detailed if m.get("should_attack", True)]
        too_dangerous: list[str] = scan.data.get("too_dangerous", [])
        if not attackable:
            # Either everything is blacklisted or too dangerous.  If
            # there's a too-dangerous mob nearby, prefer flee.
            if too_dangerous and self._config.enable_reflex_flee:
                return await self._flee(world, too_dangerous, drop)
            # Otherwise, there's nothing safe to attack — just skip.
            return None

        # Step 4: threat assessment.  Count only ATTACKABLE mobs —
        # blacklisted ones (iron golems, villagers, tamed animals)
        # shouldn't count against the threat threshold because
        # they're not actual aggressors.
        attackable_types = [m.get("type", "") for m in attackable]
        verdict = self._evaluate_threat(attackable_types, world)
        if verdict == THREAT_FLEE and self._config.enable_reflex_flee:
            return await self._flee(world, attackable_types, drop)
        if self._config.enable_reflex_attack:
            # FIGHT verdict, or FLEE verdict with reflex_flee disabled
            # (in which case we fall back to fight — better than nothing).
            # Use the rich detailed list to pick the highest-threat
            # attackable target — never the first mob blindly.
            target = self._pick_target(detailed)
            if target is None and attackable_types:
                target = attackable_types[0]
            if target:
                return await self._attack_nearest(world, [target], drop)

        return None

    def _evaluate_threat(self, mobs: list[str], world: WorldState) -> str:
        """Decide whether to fight the hostiles or flee from them.

        The heuristic (in priority order):
          1. If health is at or below ``critical_health_threshold``
             (default 2.0 HP = 1 heart), flee even from a single mob.
             At this level the next hit will likely kill the player,
             so fight is never the right choice.
          2. If there are more than ``max_fightable_mobs`` hostiles in
             range, flee — even a maxed-out player can't safely take
             on a swarm.
          3. If health is below ``flee_health_threshold`` and there's
             more than one hostile, flee.
          4. Otherwise, fight.
        """
        if world.health is not None and world.health <= self._config.critical_health_threshold:
            return THREAT_FLEE
        if len(mobs) > self._config.max_fightable_mobs:
            return THREAT_FLEE
        if (
            world.health is not None
            and world.health < self._config.flee_health_threshold
            and len(mobs) > 1
        ):
            return THREAT_FLEE
        return THREAT_FIGHT

    async def _attack_nearest(
        self,
        world: WorldState,
        mobs: list[str],
        drop: float,
    ) -> ActionResult | None:
        """Attack the highest-threat hostile mob and record the reflex.

        The LLM will see the attack result on the next turn and can
        decide whether to keep fighting or back off.

        ``mobs`` is the simple ``mobs_nearby`` list from the scan
        result.  We pick the first one — this is the conservative
        fallback.  Higher-priority callers can pass a richer target
        list via the :func:`_attack_target` variant.
        """
        if not mobs:
            return None
        target = mobs[0]
        attack = await self._dispatch(
            ActionType.ATTACK,
            {
                "entity_type": target,
                "damage_amount": self._config.reflex_damage,
            },
        )
        if attack.success:
            self._remember(f"Reflexive attack on {target} after health drop of {drop:.1f} HP")
            logger.info("Reflex attack on %s (health dropped %.1f HP)", target, drop)
        return attack

    @staticmethod
    def _pick_target(detailed: list[dict]) -> str | None:
        """Pick the best target from the detailed scan_entities list.

        Prefers the highest threat level among the attackable mobs:
        high > medium > low.  Within the same threat level, picks
        the first one (stable, deterministic).  Returns None if no
        attackable mob is in the list.
        """
        threat_order = {"high": 0, "medium": 1, "low": 2}
        candidates = [m for m in detailed if m.get("should_attack", True)]
        if not candidates:
            return None
        candidates.sort(key=lambda m: threat_order.get(m.get("threat", "medium"), 1))
        return str(candidates[0].get("type", ""))

    async def _flee(
        self,
        world: WorldState,
        mobs: list[str],
        drop: float,
    ) -> ActionResult | None:
        """Inject a 'flee to safety' sub-goal and defer to the LLM.

        Returning ``None`` here means the orchestrator will run the
        LLM's regular think-act loop this turn.  The LLM will see
        the new URGENT sub-goal at the front of the list and start
        executing it (typically: scan for a safe direction, sprint
        away, find shelter, check that the coast is clear).
        """
        if not self._inject_flee_goal(mobs, drop):
            # Couldn't inject (e.g. goal manager has no root).  Fall
            # back to a fight so we at least do *something* hostile.
            return await self._attack_nearest(world, mobs, drop)

        self._remember(
            f"Threat too high ({len(mobs)} hostiles, health "
            f"{world.health}/20) — injected URGENT flee-to-safety sub-goal"
        )
        logger.info(
            "Injecting flee-to-safety sub-goal (%d hostiles, health %.1f)",
            len(mobs),
            world.health or 0.0,
        )
        return None  # let the LLM think on the next action

    def _inject_flee_goal(self, mobs: list[str], drop: float) -> bool:
        """Insert a high-priority 'flee to safety' sub-goal at the front.

        Returns True if a sub-goal was inserted, False if one was
        already pending (no duplicate) or the goal manager has no
        root yet.  The sub-goal goes at the front of the list so the
        GoalManager's active_sub_goal surfaces it immediately on the
        next turn — the original task is naturally suspended and
        resumes when this sub-goal completes (that's the regroup).
        """
        root = self._goals._root if self._goals is not None else None
        if root is None:
            return False

        for sg in root.sub_goals:
            if not sg.completed and "flee" in sg.description.lower():
                return False  # already pending

        mob_list = ", ".join(mobs[:3])
        if len(mobs) > 3:
            mob_list += f" (+{len(mobs) - 3} more)"

        new_goal = AgentGoal(
            description=(
                f"URGENT: Flee to safety from {len(mobs)} hostile mob(s) "
                f"({mob_list}) — health dropped {drop:.1f} HP. "
                f"Sprint away, find shelter, then regroup on the original task."
            ),
            priority=0,
            depth=1,
            parent_goal="self-preservation",
        )
        new_goal._parent_ref = root
        root.sub_goals.insert(0, new_goal)
        return True

    # ── Find-food injection ────────────────────────────────────────

    def _maybe_inject_find_food(self, world: WorldState) -> None:
        """Inject a high-priority 'find food' sub-goal when hunger is low.

        The new sub-goal is inserted at the FRONT of the goal list so
        the GoalManager's active_sub_goal (which returns the first
        incomplete sub-goal) will surface it on the next turn.  We
        skip injection if a find-food sub-goal is already pending so
        we don't pollute the goal tree with duplicates.
        """
        if world.hunger is None or world.hunger >= self._config.hunger_critical_threshold:
            return

        root = self._goals._root if self._goals is not None else None
        if root is None:
            return

        for sg in root.sub_goals:
            if "food" in sg.description.lower() and not sg.completed:
                return  # already pending

        new_goal = AgentGoal(
            description="URGENT: Find and eat food (hunger is critical)",
            priority=0,
            depth=1,
            parent_goal="self-preservation",
        )
        new_goal._parent_ref = root
        root.sub_goals.insert(0, new_goal)
        self._remember(f"Hunger critical: {world.hunger}/20 — injected find-food sub-goal")
        logger.info("Injected URGENT find-food sub-goal (hunger=%d)", world.hunger)

    # ── Auto-consume ──────────────────────────────────────────────

    async def _maybe_auto_consume(self, world: WorldState) -> ActionResult | None:
        """Eat the best available food when hunger is critical.

        Behaviour:
          * If hunger is at or above ``hunger_critical_threshold``,
            return None (no need to eat).
          * Otherwise, scan the player's inventory for any edible
            item (see ``_is_food`` and ``_FOOD_ITEMS`` in
            ``minecraft/actions.py``).  If none, return None — the
            find-food sub-goal will be injected on a later turn
            (or already was).
          * Pick the food with the highest saturation value
            (best restoration per item).  Ties broken by count.
          * Issue an ``EAT`` action with the chosen item.  The
            action's underlying implementation applies a saturation
            effect to restore hunger without requiring a real
            client to right-click the food.

        Returns the eat :class:`ActionResult` so the orchestrator
        uses it as the action for this turn (skipping the LLM).
        """
        if world.hunger is None or world.hunger >= self._config.hunger_critical_threshold:
            return None

        best = self._pick_best_food(world.inventory)
        if best is None:
            return None  # no food in inventory, can't auto-consume

        item, slot = best
        eat = await self._dispatch(
            ActionType.EAT,
            {"food_item": item, "slot": slot},
        )
        if eat.success:
            self._remember(f"Auto-consumed {item} (hunger was {world.hunger}/20)")
            logger.info("Auto-consumed %s (hunger was %d/20)", item, world.hunger)
        return eat

    @staticmethod
    def _pick_best_food(
        inventory: list[InventorySlot],
    ) -> tuple[str, int] | None:
        """Return (item_id, slot) for the best food in inventory.

        "Best" means highest saturation, breaking ties by largest
        stack count.  Returns None if no edible item is present.
        """
        candidates: list[tuple[float, int, str, int]] = []
        for slot in inventory:
            if not _is_food(slot.item_id):
                continue
            _hunger, sat = _food_value(slot.item_id)
            candidates.append((sat, slot.count, slot.item_id, slot.slot))

        if not candidates:
            return None

        # Sort descending: highest saturation first; on tie, largest count.
        # Python's sort is stable, so within the same saturation, the
        # original order is preserved — but we want the highest count
        # to win, so we sort by (-sat, -count).
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        _sat, _count, item_id, slot_no = candidates[0]
        # Strip the namespace so the EAT action sees the bare ID
        return item_id.replace("minecraft:", ""), slot_no

    # ── Auto-heal ─────────────────────────────────────────────────

    async def _maybe_auto_heal(self, world: WorldState) -> ActionResult | None:
        """Auto-heal when health is critical and a supported apple is present.

        Uses the ``HEAL`` action rather than ``EAT``, which applies
        regeneration, absorption, and instant-health effects.
        Skips if no golden apple or enchanted golden apple is in inventory.

        ``enable_auto_heal`` must be True in ``PreservationConfig``
        (default True).
        """
        if world.health is None or world.health >= self._config.health_critical_threshold:
            return None

        best = self._pick_healing_item(world.inventory)
        if best is None:
            return None

        item, slot = best
        heal = await self._dispatch(
            ActionType.HEAL,
            {"heal_item": item, "slot": slot},
        )
        if heal.success:
            self._remember(f"Auto-healed with {item} (health was {world.health}/20)")
            logger.info("Auto-healed with %s (health was %.1f/20)", item, world.health)
        return heal

    @staticmethod
    def _pick_healing_item(inventory: list[InventorySlot]) -> tuple[str, int] | None:
        """Return (item_id, slot) for the best healing item in inventory.

        Priority: enchanted_golden_apple > golden_apple.
        Returns None if no healing item is present.
        """
        priority = {
            "enchanted_golden_apple": 3,
            "golden_apple": 2,
        }
        best_priority = -1
        best_item: str | None = None
        best_slot = -1

        for slot in inventory:
            bare = slot.item_id.lower().replace("minecraft:", "")
            pri = priority.get(bare, -1)
            if pri > best_priority:
                best_priority = pri
                best_item = bare
                best_slot = slot.slot

        if best_item is None:
            return None
        return best_item, best_slot

    # ── Day/night awareness ───────────────────────────────────────

    def _maybe_note_time_of_day(self, world: WorldState) -> None:
        """Record a memory fact when it's night (time > 13 000 ticks).

        The LLM sees this fact on the next turn and can choose to
        stay put, find shelter, or adopt more defensive behaviours.
        Daytime facts are not recorded (the LLM doesn't need reminding
        that it's daytime — that's the default).
        """
        raw = world.time_raw
        if not raw:
            return
        try:
            ticks = int(raw.strip())
        except (ValueError, TypeError):
            return

        # Minecraft day cycle: 0 = dawn, ~6000 = noon, ~13000 = dusk, ~18000 = midnight
        if 13000 <= ticks < 24000:
            self._remember(
                f"It's night-time (in-game time {ticks}). "
                "Night is dangerous — hostile mobs spawn in the dark. "
                "Consider staying in shelter until dawn."
            )
            logger.debug("Recorded night-time fact (ticks=%d)", ticks)
