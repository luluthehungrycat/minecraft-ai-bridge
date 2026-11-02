"""Goal management — decompose high-level goals into executable sub-goals
and track progress through the task tree.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..llm.client import LLMClient
from ..llm.models import AgentGoal

logger = logging.getLogger(__name__)


# ── Hardcoded fallback decompositions for common goals ────────────────
# When the LLM fails to decompose (returns nothing or non-JSON), these
# keyword-matched plans provide a sensible step-by-step breakdown.
#
# Ordering matters: the first matching pattern wins. The most specific
# patterns (nether portal, end portal, redstone, animal farm, villager
# trading) are placed FIRST so they beat the more generic "build", "farm",
# and "mine" patterns when a goal contains both keywords (e.g. "build a
# nether portal" should match the nether plan, not the generic build plan).
_FALLBACK_PLANS: list[tuple[re.Pattern, str, list[str]]] = [
    (
        re.compile(
            r"nether\s+portal|\bnether\b|netherrack|\bhell\b|"
            r"build.*portal|portal.*nether|\bblaze\s+rod|go\s+to\s+hell",
            re.IGNORECASE,
        ),
        "Build a nether portal",
        [
            "Check inventory for diamonds, iron ingots, and flint",
            "Craft a diamond pickaxe if needed (required to mine obsidian)",
            "Mine or locate 10-14 obsidian blocks (diamond pickaxe required)",
            "Find a flat open area to build the portal",
            "Build the obsidian frame: 4 wide x 5 tall rectangle with hollow center",
            "Use flint and steel to light the inside of the portal",
            "Step into the portal to travel to the Nether",
            "Report portal activation and current location",
        ],
    ),
    (
        re.compile(
            r"\bend\s+portal|ender\s+dragon|\bthe\s+end\b|enderman|"
            r"end\s+stone|eyes?\s+of\s+ender|stronghold",
            re.IGNORECASE,
        ),
        "Activate the End portal",
        [
            "Check inventory for ender pearls and blaze powder",
            "Craft eyes of ender by combining ender pearls and blaze powder (12 needed)",
            "Throw an eye of ender and follow its trajectory toward the stronghold",
            "Travel to the stronghold location and dig/staircase down to find the entrance",
            "Navigate through the stronghold to locate the End portal room",
            "Place an eye of ender in each empty portal frame slot (12 frames total)",
            "Enter the activated End portal",
            "Defeat the Ender Dragon (combat loop) or report progress",
        ],
    ),
    (
        re.compile(
            r"redstone|piston|repeater|comparator|automation|"
            r"redstone\s+(lamp|circuit|wire|door|clock|torch)",
            re.IGNORECASE,
        ),
        "Build a redstone contraption",
        [
            "Check inventory for redstone dust, repeaters, pistons, and a lever or button",
            "Craft any missing redstone components",
            "Choose a simple circuit: pulse generator, 2x2 piston door, or redstone lamp",
            "Clear and flatten a 5x5 area for the build",
            "Place redstone dust in the wiring pattern",
            "Place repeaters as needed for timing",
            "Place the input device (lever, button, or pressure plate)",
            "Place the output device (piston, lamp, or door)",
            "Test the circuit by activating the input",
            "Report completion or any issues",
        ],
    ),
    (
        re.compile(
            r"\bbreed\b|ranch|pasture|livestock|\bherd\b|"
            r"\b(pig|cow|chicken|sheep|horse)\b",
            re.IGNORECASE,
        ),
        "Set up an animal farm",
        [
            "Check inventory for wheat, carrots, or seeds (animal breeding food)",
            "Locate suitable animals nearby (cows, pigs, chickens, or sheep)",
            "Build a fenced enclosure (at least 5x5 with a gate)",
            "Lure animals into the enclosure using their preferred food",
            "Ensure at least 2 of the same species are present for breeding",
            "Feed the animals to trigger breeding (heart particles appear)",
            "Wait for baby animals to grow into adults",
            "Set up continuous breeding by adding more animals over time",
            "Optionally harvest adult animals for food using the attack action",
            "Report farm status and animal count",
        ],
    ),
    (
        re.compile(
            r"\b(eat|hungry|starv(e|ing)|starvation|"
            r"feed\s+yourself|need\s+to\s+eat|appetite|"
            r"find\s+food|get\s+food|grab\s+food|locate\s+food|"
            r"cook\s+food|cook\s+some\s+food|make\s+food|"
            r"bake\s+bread|find\s+bread|get\s+bread|"
            r"no\s+food|out\s+of\s+food|low\s+on\s+food|"
            r"get\s+some\s+to\s+eat|find\s+something\s+to\s+eat|"
            r"make\s+bread|cook\s+meat)\b",
            re.IGNORECASE,
        ),
        "Find and eat food",
        [
            "Check hunger with check_hunger",
            "Check inventory for any existing food items (bread, cooked meat, etc.)",
            "If food is in inventory, equip and consume it via drop_item or eat the held item",
            "If no food: scan for nearby animals that can be hunted for food",
            "Craft a furnace if needed for cooking raw meat",
            "Cook raw meat (beef, pork, chicken) into cooked food using the furnace",
            "If no animals nearby, explore until you find a passive mob or village",
            "Eat the food and recheck hunger — repeat if still below 14/20",
            "Report new hunger level and any leftover food in inventory",
        ],
    ),
    (
        re.compile(
            r"\b(flee|escape|retreat|run\s+away|get\s+away|"
            r"get\s+to\s+safety|find\s+shelter|hide|"
            r"out\s+numbered|outmatched)\b",
            re.IGNORECASE,
        ),
        "Flee to safety",
        [
            "Scan surroundings with scan_entities to identify hostile mobs",
            "Identify the direction AWAY from the hostiles (opposite vector)",
            "Sprint 10+ blocks in that direction (sprint action)",
            "Once at a safe distance, scan again — if hostiles still nearby, sprint further",
            "Look for shelter: a cave entrance, a small structure, or elevated terrain",
            "Once safe (no hostiles in radius 16), pause and recheck position",
            "Check health and consider healing if golden apples are in inventory",
            "Resume the original task once the threat has passed",
        ],
    ),
    (
        re.compile(
            r"villager|\btrader\b|trading|emerald|\bmerchant\b|"
            r"(librarian|cleric|fletcher|fletcher|farmer|priest|blacksmith|weaponsmith|"
            r"toolsmith|butcher|leatherworker|mason|wandering\s+trader)",
            re.IGNORECASE,
        ),
        "Set up villager trading",
        [
            "Locate a village (or cure a zombie villager if none is nearby)",
            "Check inventory for emeralds and useful trade items",
            "Identify a useful profession: librarian (books), cleric (ender pearls), farmer (food)",
            "Find a villager without a profession or place the desired job site block",
            "Build or locate a trading hall to keep villagers contained",
            "Trade with villagers to obtain useful items or emeralds",
            "Cure any zombie villagers with a weakness potion and golden apple",
            "Set up a trade refresh loop: break and replace the job site block",
            "Report emeralds earned and trades completed",
        ],
    ),
    (
        re.compile(r"house|build|shelter|home|base", re.IGNORECASE),
        "Build a shelter",
        [
            "Check inventory for building materials",
            "Scan surroundings for a suitable flat location",
            "Gather wood by checking/equipping and chopping nearby trees",
            "Craft a crafting table if you don't have one",
            "Craft wooden planks from logs",
            "Build a flat 5x5 foundation (floor) with dirt or planks",
            "Build 4 walls (3 blocks high) around the foundation",
            "Add a roof (ceiling) with remaining blocks",
            "Place a door in one wall for access",
            "Add interior: crafting table, torches for lighting",
            "Admire the finished house and signal completion",
        ],
    ),
    (
        re.compile(r"diamond|\bore\b|mine|dig|tunnel|excavate", re.IGNORECASE),
        "Mine for resources",
        [
            "Check inventory for tools (pickaxe, shovel)",
            "Craft a wooden pickaxe if needed",
            "Scan for nearby stone to craft stone pickaxe",
            "Teleport near a cave entrance or slope",
            "Break stone to gather cobblestone for better tools",
            "Craft a stone pickaxe",
            "Descend into a cave or tunnel down to find ores",
            "Mine visible coal and iron ore",
            "Craft a furnace and smelt iron ore into ingots",
            "Craft an iron pickaxe (mining level 2 — can break diamond)",
            "Continue mining at depth ~11 for diamond ore",
            "Check inventory and report findings",
        ],
    ),
    (
        re.compile(r"farm|wheat|crop|bread|food|hunger", re.IGNORECASE),
        "Set up a farm",
        [
            "Check inventory for seeds, tools, and water bucket",
            "Scan for a flat area near water",
            "Till soil with a hoe (craft one if needed)",
            "Plant seeds in the tilled soil",
            "Ensure adequate lighting for crops (torches)",
            "Fence the perimeter to protect crops",
            "Wait for crops to grow or craft bone meal to accelerate",
            "Harvest mature crops",
            "Replant seeds for continuous supply",
            "Craft food (bread, etc.) from harvested crops",
        ],
    ),
    (
        re.compile(r"craft.*table|enchant|enchanting|anvil|grindstone", re.IGNORECASE),
        "Set up a workshop",
        [
            "Check inventory for required materials",
            "Craft a crafting table if missing",
            "Place the crafting table in a convenient spot",
            "Craft a furnace for smelting",
            "Place the furnace near the crafting table",
            "Gather wood for chests",
            "Craft and place a chest for storage",
            "Organize resources into chests by category",
            "Signal workshop setup complete",
        ],
    ),
    (
        re.compile(r"explore|find|locate|scout|look|survey|map", re.IGNORECASE),
        "Explore the area",
        [
            "Check position and scan surroundings",
            "Pick a direction (e.g. north) and teleport a short distance",
            "Scan again and note biomes, landmarks, and resources",
            "Teleport further in the same direction, scanning periodically",
            "Note any interesting features (villages, caves, mountains)",
            "Change direction to cover new terrain",
            "Return to starting point",
            "Report notable findings",
        ],
    ),
    (
        re.compile(r"teleport|coordinate|tp|move.*to|go.*to|travel", re.IGNORECASE),
        "Teleport to coordinates",
        [
            "Identify the target coordinates from the goal",
            "Teleport directly to the target coordinates",
            "Confirm arrival by checking position",
            "Proceed with remaining goal instructions at the destination",
        ],
    ),
    (
        re.compile(r"attack|kill|combat|fight|battle|hunt|pvp|murder", re.IGNORECASE),
        "Combat engagement",
        [
            "Locate the target — scan and check player list for their name",
            "Teleport near the target if they are not at your location",
            "Ensure you are holding a weapon (craft one if needed)",
            "Attack the target using the attack action with entity_type set to their name",
            "Monitor their health and your own — retreat or continue as needed",
            "Repeat attacks until the target is defeated or you are killed",
            "Signal combat complete",
        ],
    ),
    (
        re.compile(r"\bsay\s+(hi|hello|hey|greetings)\b|^hi|^hello|chat\b", re.IGNORECASE),
        "Send a chat message",
        [
            "Send the requested message in the in-game chat",
            "Signal the task is complete",
        ],
    ),
    (
        re.compile(r"describe|report|say|tell|announce|chat.*what", re.IGNORECASE),
        "Observe and describe surroundings",
        [
            "Scan surroundings with a radius of at least 10",
            "Check current position, time, and weather",
            "Compose a description including notable blocks, structures, and entities",
            "Post the description to in-game chat",
            "Proceed with any remaining goal instructions",
        ],
    ),
]


class GoalManager:
    """Manages the agent's goal hierarchy.

    - Accepts a high-level goal string
    - Decomposes it into sub-goals (optionally using the LLM)
    - Tracks which sub-goal is active
    - Reports completion status
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        max_depth: int = 5,
    ) -> None:
        self._llm = llm_client
        self._max_depth = max_depth
        self._root: AgentGoal | None = None
        self._current: AgentGoal | None = None

    # ── Goal loading ─────────────────────────────────────────────────

    async def set_goal(self, description: str) -> AgentGoal:
        """Set a new goal, optionally decomposing it into sub-goals."""
        self._root = AgentGoal(
            description=description,
            priority=1,
            depth=0,
        )

        if self._llm:
            logger.info("Decomposing goal: %s", description)
            raw_subgoals = await self._llm.decompose_goal(description)
            subgoals = self._well_formed_subgoals(raw_subgoals)
            if subgoals is None:
                logger.warning(
                    "LLM decomposition is malformed (expected a list of objects with a "
                    "description); trying fallback decomposition."
                )
                subgoals = []
            if subgoals and not self._looks_hallucinated(description, subgoals):
                for i, sg_data in enumerate(subgoals):
                    step = sg_data.get("step")
                    sg = AgentGoal(
                        description=self._subgoal_description(sg_data),
                        priority=step if type(step) is int else i + 1,
                        depth=1,
                        parent_goal=description,
                    )
                    sg._parent_ref = self._root
                    self._root.sub_goals.append(sg)
                logger.info("Decomposed into %d sub-goals", self.sub_goal_count)
                self._current = self._root.active_sub_goal or self._root
                return self._root
            else:
                if subgoals:
                    logger.warning(
                        "LLM decomposition rejected as hallucinated (%d sub-goals); "
                        "trying fallback decomposition.",
                        len(subgoals),
                    )
                else:
                    logger.info("LLM returned no sub-goals; trying fallback decomposition.")
        else:
            logger.info("No LLM client for decomposition; trying fallback decomposition.")

        # Fallback: hardcoded keyword-matched decomposition
        self._fallback_decompose(description)

        self._current = self._root.active_sub_goal or self._root
        return self._root

    @staticmethod
    def _subgoal_description(sg_data: dict[str, Any]) -> str:
        """Return a sub-goal's description, accepting the ``desc`` alias."""
        desc = sg_data.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc
        alias = sg_data.get("desc")
        return alias if isinstance(alias, str) else ""

    @classmethod
    def _well_formed_subgoals(cls, subgoals: object) -> list[dict[str, Any]] | None:
        """Return LLM sub-goals if every item is usable, otherwise ``None``.

        Providers pass the parsed JSON through unchecked, so a model can
        return any JSON value here (for example ``[0.5]``). One malformed item
        makes the whole decomposition suspect, so it is rejected rather than
        partially used.
        """
        if not isinstance(subgoals, list):
            return None
        for sg_data in subgoals:
            if not isinstance(sg_data, dict) or not cls._subgoal_description(sg_data).strip():
                return None
        return subgoals

    def _fallback_decompose(self, description: str) -> None:
        """Match the goal description against hardcoded plans.

        Falls back to a generic step-by-step plan when no keyword pattern
        matches.
        """
        for pattern, _name, steps in _FALLBACK_PLANS:
            if pattern.search(description):
                logger.info("Fallback plan matched: %s (%d steps)", _name, len(steps))
                break
        else:
            # Generic fallback for unrecognized goals.
            # Keep steps concrete by embedding the goal description so the
            # agent can meaningfully complete each one and call "done".
            steps = [
                f"Check position, inventory, and scan surroundings. Goal: {description}",
                f"Take the first concrete action toward the goal: {description}",
                f"Continue progressing on the goal: {description}",
                f"Keep working on remaining objectives: {description}",
                f"Verify everything is complete for: {description}",
                f"Signal the goal is fully done: {description}",
            ]
            logger.info("No keyword match; using generic fallback plan.")

        root = self._root
        if root is None:
            raise RuntimeError("fallback decomposition requires a root goal")
        for i, step_desc in enumerate(steps):
            sg = AgentGoal(
                description=step_desc,
                priority=i + 1,
                depth=1,
                parent_goal=description,
            )
            sg._parent_ref = root
            root.sub_goals.append(sg)
        logger.info("Fallback decomposition: %d sub-goals", len(steps))

    @staticmethod
    def _looks_hallucinated(goal: str, subgoals: list[dict[str, Any]]) -> bool:
        """Sanity-check whether LLM decomposition looks hallucinated.

        An LLM may sometimes invent coordinates, locations, or targets
        that don't exist in the original goal.  When that happens, the
        agent gets stuck waiting for a non-existent objective.

        Returns True if the sub-goals appear to reference coordinates
        or locations not present in the original goal description.
        """
        if not subgoals:
            return False

        # Collect all sub-goal descriptions
        descriptions = [sg.get("description", sg.get("desc", "")) for sg in subgoals]
        combined = " ".join(descriptions).lower()

        # Check for coordinate/location references
        has_coord_refs = bool(
            re.search(
                r"target\s*coord|teleport\s+to|move\s+to\s+coord|"
                r"go\s+to\s+\d|navigate\s+to|travel\s+to|"
                r"locate\s+coord|find\s+coord|reach\s+coord",
                combined,
            )
        )

        # Check if the original goal contains any coordinate references
        goal_lower = goal.lower()
        goal_has_coords = bool(
            re.search(
                r"teleport|coord|\bgo\s+to\b|\bmove\s+to\b|"
                r"\bat\s+\(|\blocate\b|\bnavigate\b",
                goal_lower,
            )
        )

        # If sub-goals reference teleport/move-to-coords but the goal
        # doesn't mention coordinates or teleportation, it's hallucinated.
        if has_coord_refs and not goal_has_coords:
            logger.warning(
                "Hallucination detected: sub-goals reference coordinates but "
                "goal does not mention any."
            )
            return True

        return False

    def set_goal_from_subgoals(self, description: str, subgoals: list[dict[str, Any]]) -> AgentGoal:
        """Set a pre-decomposed goal (no LLM call)."""
        self._root = AgentGoal(description=description, depth=0)
        for i, sg in enumerate(subgoals):
            self._root.sub_goals.append(
                AgentGoal(
                    description=sg.get("description", f"Step {i + 1}"),
                    priority=i + 1,
                    depth=1,
                    parent_goal=description,
                )
            )
        self._current = self._root.active_sub_goal or self._root
        return self._root

    # ── Progress tracking ────────────────────────────────────────────

    def mark_current_complete(self) -> None:
        """Mark the current sub-goal as done and advance to the next.

        Uses the ``_parent_ref`` object reference (set during tree
        construction) to navigate siblings, which correctly handles
        goal trees of arbitrary depth.
        """
        if self._current is None:
            return
        self._current.completed = True
        logger.info("Goal completed: %s", self._current.description)

        # Navigate via object reference instead of string-based lookup
        parent = self._current._parent_ref
        if parent is not None:
            siblings = parent.sub_goals
            idx = next(
                (i for i, sg in enumerate(siblings) if sg is self._current),
                -1,
            )
            if idx + 1 < len(siblings):
                self._current = siblings[idx + 1]
            else:
                parent.completed = True
                self._current = None  # all siblings done
        else:
            # Root goal itself (no parent) — nothing else to do
            self._current = None

    @property
    def current_goal(self) -> str:
        """Human-readable current goal description."""
        if self._current is None:
            return "All goals complete!"
        return self._current.description

    @property
    def is_complete(self) -> bool:
        """All goals are finished."""
        return self._current is None and (self._root is not None and self._root.completed)

    @property
    def sub_goal_count(self) -> int:
        """Number of sub-goals under the root goal."""
        return len(self._root.sub_goals) if self._root else 0

    @property
    def root_description(self) -> str:
        """Description of the root goal, or empty string if none set."""
        return self._root.description if self._root else ""

    @property
    def progress(self) -> str:
        """Summary of goal progress for the LLM prompt."""
        if self._root is None:
            return "No goal set."
        lines = [f"=== Goal: {self._root.description} ==="]
        if not self._root.sub_goals:
            lines.append("Status: in progress")
            return "\n".join(lines)
        for _i, sg in enumerate(self._root.sub_goals, 1):
            status = "✓" if sg.completed else "○"
            active = " ← CURRENT" if sg is self._current else ""
            lines.append(f"  {status} {sg.description}{active}")
        if self._current is None and not self._root.completed:
            lines.append("  → All sub-goals complete!")
        return "\n".join(lines)
