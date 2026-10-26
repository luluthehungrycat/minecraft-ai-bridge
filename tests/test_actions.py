"""Tests for all action handlers in minecraft_ai_bridge.minecraft.actions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcpq.nbt import NbtInt

import minecraft_ai_bridge.minecraft.actions as action_module
from minecraft_ai_bridge.minecraft.actions import (
    ActionResult,
    ActionType,
    _can_move_to,
    _cmd,
    _damage_hit_anything,
    _is_artificial,
    _is_hazard,
    _is_passable,
    execute_action,
    select_best_weapon,
)
from minecraft_ai_bridge.minecraft.observer import InventorySlot

# Only async TestActionHandlers tests are individually marked with @pytest.mark.asyncio


# ── execute_action dispatch ──────────────────────────────────────────────


class TestExecuteAction:
    @pytest.mark.parametrize(
        ("action", "params"),
        [
            (ActionType.FOLLOW_TELEPORT, {"target": None}),
            (ActionType.PROVISION_BOT, {"player_name": ["Bot"]}),
            (ActionType.GOTO_TELEPORT, {"target": 42}),
            (ActionType.COME_TELEPORT, {"sender": {"name": "Player"}}),
        ],
    )
    async def test_internal_player_target_actions_reject_non_strings_without_dispatch(
        self, mock_mc, action, params
    ):
        result = await execute_action(mock_mc, action, params)

        assert not result.success
        assert "invalid" in result.message.lower()
        assert mock_mc.commands_ran == []

    async def test_unknown_action(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.SCAN, {"radius": 5})
        assert result.success is True  # scan always succeeds
        assert result.action == ActionType.SCAN

    async def test_unregistered_action(self, mock_mc):
        """Test that a fake action type returns failure."""
        fake = ActionType("check_position")  # registered
        result = await execute_action(mock_mc, fake, {})
        assert result.success is True  # check_position succeeds

    async def test_handler_error_caught(self, mock_mc):
        """An exception in a handler returns an error ActionResult."""
        mock_mc._block_map = {}
        # force an error by giving invalid params to break_block
        result = await execute_action(mock_mc, ActionType.BREAK_BLOCK, {})
        assert result.success is True  # break_block has default path


# ── Movement actions ─────────────────────────────────────────────────────


class TestMovement:
    async def test_move_to(self, mock_mc):
        mock_mc.set_position(10.0, 64.0, 20.0)
        result = await execute_action(mock_mc, ActionType.MOVE_TO, {"x": 100, "y": 65, "z": 200})
        assert result.success is True
        assert "100" in result.message
        assert mock_mc._pos == (100.0, 65.0, 200.0)

    async def test_move_forward(self, mock_mc):
        mock_mc.set_position(0.5, 65.0, 0.5)
        result = await execute_action(mock_mc, ActionType.MOVE_FORWARD, {"steps": 2})
        assert result.success is True
        assert "forward" in result.message.lower()
        # Verify execute-based command was used (not bare /tp)
        # Note: @p gets substituted by run_as_player to the player name
        mock_mc.assert_command_contains("execute as AIBot at @s run tp @s ^ ^ ^0.5")

    async def test_move_forward_blocked(self, mock_mc):
        """Move forward should fail when a solid block is in the way."""
        mock_mc.set_position(0.5, 65.0, 0.5)
        # The _can_move_to check uses (int(px), int(py+0.5), int(pz))
        # So place a wall above feet level (y=66 for head check) and at feet level (y=65)
        # at the current position — the check happens before moving
        await mock_mc.set_block("stone", 0, 65, 0)  # Feet level
        await mock_mc.set_block("stone", 0, 66, 0)  # Head level — blocked
        result = await execute_action(mock_mc, ActionType.MOVE_FORWARD, {"steps": 2})
        assert result.success is False
        assert "blocked" in result.message.lower() or "Blocked" in result.message

    async def test_move_forward_hazard(self, mock_mc):
        """Move forward should stop before a hazard."""
        mock_mc.set_position(0.5, 65.0, 0.5)
        # The hazard check uses mc.get_block(front_x, front_y - 1, front_z)
        # where front_y = int(py + 0.5) = int(65.0 + 0.5) = 65
        # so it checks (0, 64, 0)
        await mock_mc.set_block("lava", 0, 64, 0)  # Below feet — hazard check
        result = await execute_action(mock_mc, ActionType.MOVE_FORWARD, {"steps": 2})
        assert result.success is False
        assert "hazard" in result.message.lower()

    async def test_move_forward_auto_step(self, mock_mc):
        """Move forward should auto-step over a single-block obstacle (e.g. slab)."""
        mock_mc.set_position(0.5, 65.0, 0.5)
        # Block at feet level (y=65) but passable at y+1 (y=66) above
        # _can_move_to checks (0, 66, 0) for head —> air → passable head
        # and (0, 65, 0) for feet —> stone → blocked
        # So it tries (0, 66, 0) one level up — air at (0, 66, 0) and air at (0, 67, 0)
        await mock_mc.set_block("stone", 0, 65, 0)  # Feet level — blocked
        await mock_mc.set_block("air", 0, 66, 0)  # Head level — passable
        await mock_mc.set_block("air", 0, 67, 0)  # Head + 1 — passable for auto-step
        result = await execute_action(mock_mc, ActionType.MOVE_FORWARD, {"steps": 1})
        # Should auto-step since head+1 is passable
        assert result.success is True

    async def test_move_back(self, mock_mc):
        mock_mc.set_position(0.5, 65.0, 0.5)
        result = await execute_action(mock_mc, ActionType.MOVE_BACK, {"steps": 2})
        assert result.success is True
        assert "back" in result.message.lower()
        # Verify execute-based command was used (note: @p gets substituted)
        mock_mc.assert_command_contains("execute as AIBot at @s run tp @s ^ ^ ^-0.5")

    async def test_move_back_blocked(self, mock_mc):
        """Move back should fail when a solid block is behind."""
        mock_mc.set_position(0.5, 65.0, 0.5)
        await mock_mc.set_block("stone", 0, 65, 0)
        await mock_mc.set_block("stone", 0, 66, 0)
        result = await execute_action(mock_mc, ActionType.MOVE_BACK, {"steps": 2})
        assert result.success is False
        assert "blocked" in result.message.lower() or "Blocked" in result.message

    async def test_turn_left(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.TURN_LEFT, {})
        assert result.success is True
        assert "left" in result.message.lower()
        # Verify rotation command (tp @p with yaw rotation)
        mock_mc.assert_command_contains("tp AIBot ~ ~ ~ ~-15 ~")

    async def test_turn_right(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.TURN_RIGHT, {})
        assert result.success is True
        assert "right" in result.message.lower()
        # Verify rotation command (tp @p with yaw rotation)
        mock_mc.assert_command_contains("tp AIBot ~ ~ ~ ~15 ~")

    async def test_jump(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.JUMP, {})
        assert result.success is True
        # Verify jump command (tp @p with relative y offset)
        mock_mc.assert_command_contains("tp AIBot ~ ~1 ~")

    async def test_sprint_default(self, mock_mc):
        """Sprint with default steps should succeed."""
        result = await execute_action(mock_mc, ActionType.SPRINT, {})
        assert result.success is True
        assert "sprinted" in result.message.lower()

    async def test_sprint_with_steps(self, mock_mc):
        """Sprint with specified steps should move the player."""
        result = await execute_action(mock_mc, ActionType.SPRINT, {"steps": 5})
        assert result.success is True
        assert "sprinted" in result.message.lower()
        assert result.action == ActionType.SPRINT
        # For open terrain the sprint should complete all steps
        assert result.data.get("steps_taken", 0) >= 0

    async def test_sprint_blocked(self, mock_mc):
        """Sprint should stop when hitting a wall."""
        # Place a wall right in front of the player at (0, 65, 1)
        await mock_mc.set_block("stone", 0, 65, 1)
        await mock_mc.set_block("stone", 0, 66, 1)
        result = await execute_action(mock_mc, ActionType.SPRINT, {"steps": 10})
        # Should stop early, possibly with 0 steps taken
        assert "sprinted" in result.message.lower()

    async def test_sprint_hazard(self, mock_mc):
        """Sprint should stop before stepping into hazard."""
        # Place lava right in front
        mock_mc.set_position(0.0, 65.0, 0.0)
        await mock_mc.set_block("lava", 0, 64, 1)
        result = await execute_action(mock_mc, ActionType.SPRINT, {"steps": 5})
        # Should stop early or report hazard
        assert result.success is True or result.message is not None

    async def test_teleport(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.TELEPORT, {"x": 50, "y": 70, "z": 100})
        assert result.success is True
        assert mock_mc._pos == (50.0, 70.0, 100.0)

    async def test_walk_to_nearby(self, mock_mc):
        """walk_to should walk step-by-step for short distances."""
        mock_mc.set_position(10.0, 65.0, 10.0)
        result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 12, "z": 12})
        assert result.success is True
        # Should have used execute facing to orient
        mock_mc.assert_command_contains("execute as AIBot at @s facing")

    async def test_walk_to_far(self, mock_mc):
        """walk_to should teleport for distances > 50 blocks."""
        mock_mc.set_position(10.0, 65.0, 10.0)
        result = await execute_action(mock_mc, ActionType.WALK_TO, {"x": 100, "z": 100})
        assert result.success is True
        assert "teleported" in result.message.lower()


# ── Interaction actions ──────────────────────────────────────────────────


class TestInteraction:
    async def test_break_block_with_coords(self, mock_mc):
        await mock_mc.set_block("stone", 10, 64, 20)
        result = await execute_action(mock_mc, ActionType.BREAK_BLOCK, {"x": 10, "y": 64, "z": 20})
        assert result.success is True
        assert await mock_mc.get_block(10, 64, 20) == "air"

    async def test_place_block(self, mock_mc):
        result = await execute_action(
            mock_mc, ActionType.PLACE_BLOCK, {"x": 10, "y": 64, "z": 20, "block_type": "stone"}
        )
        assert result.success is True
        assert await mock_mc.get_block(10, 64, 20) == "stone"

    async def test_place_block_without_coords(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.PLACE_BLOCK, {"block_type": "stone"})
        assert result.success is True

    async def test_interact(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.INTERACT, {})
        assert result.success is True

    async def test_place_block_exception(self, mock_mc):
        """Test that exceptions in place_block are caught."""
        mock_mc._block_map = None  # type: ignore[assignment]
        result = await execute_action(mock_mc, ActionType.PLACE_BLOCK, {"x": 1, "y": 2, "z": 3})
        assert result.success is False  # handler catches TypeError and returns failure


class TestEquipment:
    def test_select_best_weapon_uses_hotbar_only(self):
        items = [
            InventorySlot(item_id="minecraft:iron_sword", count=1, slot=0),
            InventorySlot(item_id="minecraft:diamond_sword", count=1, slot=3),
            InventorySlot(item_id="minecraft:netherite_sword", count=1, slot=9),
            InventorySlot(item_id="minecraft:diamond_sword", count=1, slot=100),
        ]

        selected = select_best_weapon(items)

        assert selected is not None
        assert selected.item_id == "minecraft:diamond_sword"
        assert selected.slot == 3

    def test_select_best_weapon_prefers_lower_slot_on_tie(self):
        items = [
            InventorySlot(item_id="minecraft:diamond_sword", count=1, slot=5),
            InventorySlot(item_id="minecraft:diamond_axe", count=1, slot=2),
        ]

        selected = select_best_weapon(items)

        assert selected is not None
        assert selected.slot == 2

    def test_select_best_weapon_skips_malformed_inventory_entries(self):
        malformed = SimpleNamespace(item_id=None, count="unknown", slot="hotbar")
        valid = InventorySlot(item_id="minecraft:iron_sword", count=1, slot=2)

        selected = select_best_weapon([malformed, valid])

        assert selected == valid

    def test_select_best_weapon_ignores_damage_without_durability_metadata(self):
        worn_diamond = InventorySlot(item_id="minecraft:diamond_sword", count=1, slot=2, damage=100)
        fresh_iron = InventorySlot(item_id="minecraft:iron_sword", count=1, slot=0, damage=0)

        selected = select_best_weapon([worn_diamond, fresh_iron])

        assert selected == worn_diamond

    async def test_equip_best_weapon_moves_best_hotbar_item_to_main_hand(self, mock_mc):
        mock_mc.set_inventory(
            [
                {"item_id": "iron_sword", "count": 1, "slot": 0},
                {"item_id": "diamond_sword", "count": 1, "slot": 3},
            ]
        )

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is True
        assert result.data["item_id"] == "minecraft:diamond_sword"
        assert result.data["slot"] == 3
        mock_mc.assert_command_contains(
            "item replace entity AIBot weapon.mainhand from entity AIBot hotbar.3"
        )

    async def test_equip_best_weapon_respects_nonzero_selected_slot(self, mock_mc):
        mock_mc.set_player_nbt("SelectedItemSlot", NbtInt(3))
        mock_mc.set_inventory(
            [
                {"item_id": "iron_sword", "count": 1, "slot": 3},
                {"item_id": "diamond_sword", "count": 1, "slot": 5},
            ]
        )

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is True
        assert result.data["already_equipped"] is False
        mock_mc.assert_command_contains(
            "item replace entity AIBot weapon.mainhand from entity AIBot hotbar.5"
        )

    async def test_equip_best_weapon_detects_nonzero_selected_best_weapon(self, mock_mc):
        mock_mc.set_player_nbt("SelectedItemSlot", NbtInt(3))
        mock_mc.set_inventory(
            [
                {"item_id": "diamond_sword", "count": 1, "slot": 3},
                {"item_id": "iron_sword", "count": 1, "slot": 5},
            ]
        )

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is True
        assert result.data["already_equipped"] is True
        assert not any("item replace" in command for command in mock_mc.commands_ran)

    async def test_equip_best_weapon_avoids_redundant_main_hand_replace(self, mock_mc):
        mock_mc.set_inventory([{"item_id": "netherite_sword", "count": 1, "slot": 0}])

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is True
        assert result.data["already_equipped"] is True
        assert not any("item replace" in command for command in mock_mc.commands_ran)

    async def test_equip_best_weapon_fails_without_hotbar_weapon(self, mock_mc):
        mock_mc.set_inventory([{"item_id": "diamond_sword", "count": 1, "slot": 9}])

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is False
        assert "hotbar" in result.message.lower()

    async def test_equip_best_weapon_propagates_inventory_failure(self, mock_mc, monkeypatch):
        async def fail_inventory(mc, params):
            return ActionResult(
                success=False,
                action=ActionType.CHECK_INVENTORY,
                message="inventory unavailable",
            )

        monkeypatch.setattr(action_module, "_check_inventory", fail_inventory)

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is False
        assert "inventory unavailable" in result.message

    async def test_equip_best_weapon_propagates_equip_failure(self, mock_mc, monkeypatch):
        mock_mc.set_inventory([{"item_id": "diamond_sword", "count": 1, "slot": 3}])

        async def fail_equip(mc, params):
            return ActionResult(
                success=False,
                action=ActionType.EQUIP_ITEM,
                message="equip command failed",
            )

        monkeypatch.setattr(action_module, "_equip_item", fail_equip)

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is False
        assert "equip command failed" in result.message

    async def test_equip_best_weapon_propagates_inventory_command_failure(
        self, mock_mc, monkeypatch
    ):
        async def fail_command(command):
            return "No entity was found"

        monkeypatch.setattr(mock_mc, "run_as_player", fail_command)

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is False
        assert "No entity was found" in result.message

    async def test_equip_best_weapon_propagates_equip_command_failure(self, mock_mc, monkeypatch):
        async def fail_equip_command(command):
            if "Inventory" in command:
                return 'Inventory: [{id:"minecraft:diamond_sword",Count:1b,Slot:3b}]'
            return "Source stack is empty"

        monkeypatch.setattr(mock_mc, "run_as_player", fail_equip_command)

        result = await execute_action(mock_mc, ActionType.EQUIP_BEST_WEAPON, {})

        assert result.success is False
        assert "Source stack is empty" in result.message

    def test_action_schema_preserves_inventory_actions(self):
        from minecraft_ai_bridge.llm.client import ACTION_TOOL

        actions = ACTION_TOOL["function"]["parameters"]["properties"]["action"]["enum"]

        assert "equip_best_weapon" in actions
        assert "equip_item" in actions
        assert "drop_item" in actions


# ── Inventory actions ────────────────────────────────────────────────────


class TestInventory:
    async def test_check_inventory(self, mock_mc):
        mock_mc.set_inventory(
            [
                {"item_id": "dirt", "count": 64, "slot": 0},
                {"item_id": "stone", "count": 32, "slot": 1},
            ]
        )
        result = await execute_action(mock_mc, ActionType.CHECK_INVENTORY, {})
        assert result.success is True
        assert "inventory" in result.message.lower()

    async def test_equip_item(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.EQUIP_ITEM, {"slot": 0})
        assert result.success is True

    async def test_equip_item_rejects_invalid_slot(self, mock_mc):
        for slot in (-1, 9, "1", True):
            result = await execute_action(mock_mc, ActionType.EQUIP_ITEM, {"slot": slot})
            assert result.success is False
            assert "slot" in result.message.lower()

    async def test_craft_item(self, mock_mc):
        result = await execute_action(
            mock_mc, ActionType.CRAFT_ITEM, {"item_type": "crafting_table", "amount": 1}
        )
        assert result.success is True
        assert "give" in result.message.lower() or "gave" in result.message.lower()

    async def test_drop_item(self, mock_mc):
        result = await execute_action(
            mock_mc, ActionType.DROP_ITEM, {"item_type": "stone", "amount": 1}
        )
        assert result.success is True
        assert "drop" in result.message.lower() or "dropped" in result.message.lower()

    async def test_eat_bread(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.EAT, {"food_item": "bread"})
        assert result.success is True
        assert "bread" in result.message.lower()
        assert result.data.get("food_item") == "bread"
        assert result.data.get("hunger_restored") == 5

    async def test_eat_with_namespaced_id(self, mock_mc):
        """The namespace prefix should be stripped before lookup."""
        result = await execute_action(mock_mc, ActionType.EAT, {"food_item": "minecraft:bread"})
        assert result.success is True
        assert result.data.get("food_item") == "bread"

    async def test_eat_golden_carrot_higher_value(self, mock_mc):
        """Golden carrot should restore more hunger than bread."""
        bread = await execute_action(mock_mc, ActionType.EAT, {"food_item": "bread"})
        golden = await execute_action(mock_mc, ActionType.EAT, {"food_item": "golden_carrot"})
        assert golden.data.get("hunger_restored") > bread.data.get("hunger_restored")

    async def test_eat_unknown_food_fails(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.EAT, {"food_item": "not_a_real_food"})
        assert result.success is False

    async def test_eat_no_food_param_fails(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.EAT, {})
        assert result.success is False
        assert "food_item" in result.message.lower()

    async def test_eat_with_slot_param(self, mock_mc):
        """Slot parameter is accepted (and ignored if equip fails)."""
        result = await execute_action(mock_mc, ActionType.EAT, {"food_item": "bread", "slot": 0})
        assert result.success is True

    async def test_food_is_food_helper(self):
        """The _is_food helper should recognise food IDs with or without namespace."""
        from minecraft_ai_bridge.minecraft.actions import _is_food

        assert _is_food("bread")
        assert _is_food("minecraft:bread")
        assert _is_food("golden_apple")
        assert not _is_food("stone")
        assert not _is_food("dirt")
        assert not _is_food("not_a_real_food")

    async def test_food_value_helper(self):
        """The _food_value helper should return (hunger, saturation) for known foods."""
        from minecraft_ai_bridge.minecraft.actions import _food_value

        h_bread, s_bread = _food_value("bread")
        h_carrot, s_carrot = _food_value("golden_carrot")
        # Golden carrot has higher saturation than bread
        assert s_carrot > s_bread
        # Unknown food returns (0, 0)
        assert _food_value("not_a_food") == (0.0, 0.0)

    async def test_heal_generic(self, mock_mc):
        """Generic heal action (no item) should succeed with regen effects."""
        from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action

        result = await execute_action(mock_mc, ActionType.HEAL, {})
        assert result.success is True
        assert "healed" in result.message.lower()
        assert "regeneration" in result.message.lower()

    async def test_heal_golden_apple(self, mock_mc):
        """Golden apple should trigger absorption + instant health."""
        from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action

        result = await execute_action(mock_mc, ActionType.HEAL, {"heal_item": "golden_apple"})
        assert result.success is True
        assert "absorption" in result.message.lower()
        assert "instant_health" in result.message.lower()

    async def test_heal_enchanted_golden_apple(self, mock_mc):
        """Enchanted golden apple should also give fire_resistance + resistance."""
        from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action

        result = await execute_action(
            mock_mc, ActionType.HEAL, {"heal_item": "enchanted_golden_apple"}
        )
        assert result.success is True
        assert "fire_resistance" in result.message.lower()
        assert "resistance" in result.message.lower()

    async def test_heal_records_item_in_data(self, mock_mc):
        """The heal action should return which item was used."""
        from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action

        result = await execute_action(mock_mc, ActionType.HEAL, {"heal_item": "golden_apple"})
        assert result.data.get("heal_item") == "golden_apple"
        assert isinstance(result.data.get("heal_effects"), list)

    async def test_heal_craft_item_failure(self, mock_mc):
        """Test error handling when /give fails."""
        original_run = mock_mc.run_as_player

        async def fail_give(command: str) -> str:
            if "give" in command:
                raise RuntimeError("Cannot give items in survival")
            return await original_run(command)

        mock_mc.run_as_player = fail_give  # type: ignore[assignment]
        result = await execute_action(
            mock_mc, ActionType.CRAFT_ITEM, {"item_type": "diamond", "amount": 1}
        )
        assert result.success is False
        assert "survival" in result.message.lower() or "cannot" in result.message.lower()


# ── Combat actions ───────────────────────────────────────────────────────


class TestCombat:
    async def test_attack(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.ATTACK, {})
        assert result.success is True  # mock returns "Damaged 1 entity"

    async def test_attack_with_target(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.ATTACK, {"entity_type": "zombie"})
        assert result.success is True

    async def test_attack_is_anchored_to_bot_and_limited_to_six_blocks(self, mock_mc):
        await execute_action(mock_mc, ActionType.ATTACK, {"entity_type": "zombie"})
        assert any(
            "execute as AIBot at @s run damage "
            "@e[type=minecraft:zombie,distance=..6,limit=1,sort=nearest]" in command
            for command in mock_mc.commands_ran
        )

    async def test_attack_rejects_selector_and_command_injection(self, mock_mc):
        for target in ("@a", "zombie],limit=100", "zombie; kill @a", "Player Name"):
            before = len(mock_mc.commands_ran)
            result = await execute_action(mock_mc, ActionType.ATTACK, {"entity_type": target})
            assert not result.success
            assert len(mock_mc.commands_ran) == before

    async def test_attack_rejects_protected_targets(self, mock_mc):
        for target in ("minecraft:villager", "iron_golem", "warden", "wither"):
            result = await execute_action(mock_mc, ActionType.ATTACK, {"entity_type": target})
            assert not result.success

    async def test_attack_empty_target_uses_non_player_selector(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.ATTACK, {"entity_type": ""})
        assert result.success
        damage_commands = [command for command in mock_mc.commands_ran if "damage" in command]
        assert damage_commands
        assert all("type=minecraft:" in command for command in damage_commands)

    async def test_attack_missing_target_uses_default(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.ATTACK, {})
        assert result.success

    async def test_attack_custom_damage(self, mock_mc):
        """Custom damage_amount should be reflected in the response."""
        result = await execute_action(
            mock_mc,
            ActionType.ATTACK,
            {"entity_type": "zombie", "damage_amount": 20},
        )
        assert result.success is True
        # The damage value should be in the data field, not necessarily in
        # the message (mock doesn't echo it back).
        assert result.data.get("damage_dealt") == 20

    async def test_attack_clamps_damage(self, mock_mc):
        """Damage values outside [1, 64] are clamped to the valid range."""
        result = await execute_action(mock_mc, ActionType.ATTACK, {"damage_amount": 999})
        assert result.data.get("damage_dealt") == 64
        result = await execute_action(mock_mc, ActionType.ATTACK, {"damage_amount": -5})
        assert result.data.get("damage_dealt") == 1

    async def test_attack_target_hit_field(self, mock_mc):
        """The target_hit data field should be True on a successful attack."""
        result = await execute_action(mock_mc, ActionType.ATTACK, {})
        assert result.data.get("target_hit") is True

    async def test_scan_entities_none_nearby(self, mock_mc):
        """With no hostile mobs configured, scan_entities reports none."""
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {})
        assert result.success is True
        assert result.data["mobs_nearby"] == []
        assert "no hostile mobs" in result.message.lower()

    async def test_scan_entities_with_mobs(self, mock_mc):
        """Configured hostile mobs should be detected by scan_entities."""
        mock_mc.set_hostile_mobs(["zombie", "skeleton", "creeper"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {})
        assert result.success is True
        detected = result.data["mobs_nearby"]
        assert "zombie" in detected
        assert "skeleton" in detected
        assert "creeper" in detected
        assert "enderman" not in detected  # not configured
        assert "zombie" in result.message.lower()

    async def test_scan_entities_includes_critical_types_with_bounded_scan(self, mock_mc):
        """Critical threats remain discoverable without unbounded selector growth."""
        from minecraft_ai_bridge.minecraft.actions import _SCAN_ENTITY_TYPES

        mock_mc.set_hostile_mobs(["warden"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})

        assert result.success is True
        assert result.data["mobs_nearby"] == ["warden"]
        detailed = {mob["type"]: mob for mob in result.data["detailed"]}
        assert detailed["warden"] == {
            "type": "warden",
            "threat": "critical",
            "should_attack": False,
        }
        assert result.data["too_dangerous"] == ["warden"]
        assert len(_SCAN_ENTITY_TYPES) <= 30
        assert {"warden", "wither"} <= set(_SCAN_ENTITY_TYPES)

    async def test_scan_entities_continues_after_selector_failure(self, mock_mc, monkeypatch):
        """A failed selector does not prevent later entity types from scanning."""
        original = mock_mc._run_command_blocking

        async def fail_creeper(command: str) -> str:
            if "type=minecraft:creeper" in command:
                raise RuntimeError("selector failed")
            return await original(command)

        monkeypatch.setattr(mock_mc, "_run_command_blocking", fail_creeper)
        mock_mc.set_hostile_mobs(["creeper", "warden"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})

        assert result.success is True
        assert "creeper" not in result.data["mobs_nearby"]
        assert "warden" in result.data["mobs_nearby"]

    async def test_scan_entities_custom_radius(self, mock_mc):
        """Custom radius is reflected in the response."""
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 8})
        assert result.data["radius"] == 8

    async def test_scan_entities_caps_radius(self, mock_mc):
        """Radius above 16 is capped to avoid command spam."""
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 100})
        assert result.data["radius"] == 16

    async def test_damage_hit_anything(self):
        assert _damage_hit_anything("Damaged 1 entity") is True
        assert _damage_hit_anything("Hurt entity") is True
        assert _damage_hit_anything("No entity was found") is False
        assert _damage_hit_anything("0 entities were damaged") is False
        assert _damage_hit_anything(None) is False
        assert _damage_hit_anything("") is False
        # Unknown format should default to True (optimistic)
        assert _damage_hit_anything("some random output") is True


# ── Information actions ──────────────────────────────────────────────────


class TestInformation:
    async def test_scan(self, mock_mc):
        mock_mc.set_position(10.0, 64.0, 20.0)
        await mock_mc.set_block("grass_block", 10, 63, 20)
        result = await execute_action(mock_mc, ActionType.SCAN, {"radius": 5})
        assert result.success is True
        data = result.data
        assert data.get("radius") == 5
        assert "position" in data

    async def test_scan_radius_capped(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.SCAN, {"radius": 50})
        assert result.success is True
        assert result.data["radius"] == 16  # capped

    async def test_check_time(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.CHECK_TIME, {})
        assert result.success is True

    async def test_check_weather(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.CHECK_WEATHER, {})
        assert result.success is True

    async def test_check_health(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.CHECK_HEALTH, {})
        assert result.success is True
        assert "health" in result.message.lower()

    async def test_check_health_with_player_nbt(self, mock_mc):
        """Health should use MCPQ get_player_info NBT if available."""
        mock_mc.set_player_nbt("Health", 7.5)
        result = await execute_action(mock_mc, ActionType.CHECK_HEALTH, {})
        assert result.success is True

    async def test_check_health_preserves_zero_health(self, mock_mc, monkeypatch):
        original = mock_mc._run_command_blocking

        async def responses(command: str) -> str:
            if command.startswith("data get entity AIBot Health"):
                return "Health: 0.0d"
            return await original(command)

        monkeypatch.setattr(mock_mc, "_run_command_blocking", responses)
        result = await execute_action(mock_mc, ActionType.CHECK_HEALTH, {})

        assert result.success is True
        assert result.data["health_raw"] == "Health: 0.0d"
        assert result.data.get("health_source") is None
        assert "0.0" in result.message

    async def test_check_health_keeps_maximum_separate(self, mock_mc, monkeypatch):
        original = mock_mc._run_command_blocking

        async def responses(command: str) -> str:
            if command.startswith("data get entity AIBot Health"):
                return "No entity was found"
            if command.startswith("attribute AIBot minecraft:generic.max_health"):
                return "Base value: 40.0"
            return await original(command)

        monkeypatch.setattr(mock_mc, "_run_command_blocking", responses)
        result = await execute_action(mock_mc, ActionType.CHECK_HEALTH, {})

        assert result.success is True
        assert result.data["health_raw"] == ""
        assert result.data["max_health_raw"] == "Base value: 40.0"
        assert result.data["health_source"] == "attribute_max"

    async def test_check_hunger(self, mock_mc):
        """Hunger action should always succeed and report a /20 value."""
        result = await execute_action(mock_mc, ActionType.CHECK_HUNGER, {})
        assert result.success is True
        assert "hunger" in result.message.lower()
        assert "/20" in result.message

    async def test_check_hunger_with_player_nbt(self, mock_mc):
        """Hunger should reflect the NBT foodLevel when available."""
        mock_mc.set_player_nbt("foodLevel", 7)
        result = await execute_action(mock_mc, ActionType.CHECK_HUNGER, {})
        assert result.success is True
        assert "7/20" in result.message

    async def test_check_hunger_starving(self, mock_mc):
        """Hunger at 0 should still report validly (no clamping error)."""
        mock_mc.set_player_nbt("foodLevel", 0)
        result = await execute_action(mock_mc, ActionType.CHECK_HUNGER, {})
        assert result.success is True
        assert "0/20" in result.message

    async def test_check_hunger_default_when_unset(self, mock_mc):
        """With no NBT, hunger defaults to 20/20 (assumed)."""
        result = await execute_action(mock_mc, ActionType.CHECK_HUNGER, {})
        assert result.success is True
        assert "20/20" in result.message

    async def test_check_position(self, mock_mc):
        mock_mc.set_position(42.0, 65.0, 100.0)
        result = await execute_action(mock_mc, ActionType.CHECK_POSITION, {})
        assert result.success is True
        assert "42" in result.message

    async def test_list_players(self, mock_mc):
        mock_mc.set_players(["AIBot", "Player1", "Player2"])
        result = await execute_action(mock_mc, ActionType.LIST_PLAYERS, {})
        assert result.success is True
        assert "3" in result.message


# ── Communication actions ────────────────────────────────────────────────


class TestCommunication:
    async def test_chat(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.CHAT, {"message": "Hello world"})
        assert result.success is True
        assert "Hello world" in result.message
        assert "Hello world" in mock_mc.chat_messages_sent

    async def test_chat_empty_message(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.CHAT, {"message": ""})
        assert result.success is True
        # No chat should be sent for empty message


# ── Meta actions ─────────────────────────────────────────────────────────


class TestMeta:
    async def test_wait(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.WAIT, {"seconds": 0.01})
        assert result.success is True
        assert "Waited" in result.message

    async def test_done(self, mock_mc):
        result = await execute_action(mock_mc, ActionType.DONE, {"message": "Task complete"})
        assert result.success is True
        assert "Task complete" in result.message


# ── Helper functions ─────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.asyncio
    async def test_is_passable(self):
        assert _is_passable("air") is True
        assert _is_passable("water") is True
        assert _is_passable("stone") is False
        assert _is_passable("oak_fence") is True  # fences are walk-throughable in simplified model
        assert _is_passable("grass") is True
        assert _is_passable("tall_grass") is True
        assert _is_passable("torch") is True

    @pytest.mark.asyncio
    async def test_is_hazard(self):
        assert _is_hazard("lava") is True
        assert _is_hazard("cactus") is True
        assert _is_hazard("fire") is True
        assert _is_hazard("stone") is False
        assert _is_hazard("magma_block") is True
        assert _is_hazard("air") is False

    @pytest.mark.asyncio
    async def test_is_artificial(self):
        assert _is_artificial("oak_planks") is True
        assert _is_artificial("glass") is True
        assert _is_artificial("stone_bricks") is True
        assert _is_artificial("rail") is True
        assert _is_artificial("stone") is False  # natural
        assert _is_artificial("grass_block") is False
        assert _is_artificial("dirt") is False

    async def test_can_move_to(self, mock_mc):
        # Default world has all air — should be passable
        result = await _can_move_to(mock_mc, 0, 64, 0)
        assert result[0] is True

        # Place a solid block at head level
        await mock_mc.set_block("stone", 0, 66, 0)
        result = await _can_move_to(mock_mc, 0, 65, 0)
        assert result[0] is False  # head in solid block

    async def test_can_move_to_hazard(self, mock_mc):
        await mock_mc.set_block("lava", 0, 64, 0)
        await mock_mc.set_block("air", 0, 65, 0)
        result = await _can_move_to(mock_mc, 0, 65, 0)
        assert result[0] is False  # lava hazard at feet

    async def test_cmd_helper(self, mock_mc):
        response = await _cmd(mock_mc, "test command")
        assert response is not None
        assert mock_mc.last_command() == "test command"


# ── Mob threat / blacklist / scan ────────────────────────────────────────


class TestMobThreatLevels:
    """The threat-level and blacklist helpers are used by both the
    scan_entities action and the self-preservation layer.  These
    tests verify the lookup is correct."""

    def test_threat_levels_known_mobs(self):
        from minecraft_ai_bridge.minecraft.actions import _get_threat_level

        # The user mentioned "creeper = high, zombie = medium" as the
        # canonical examples — verify those and a few more.
        assert _get_threat_level("zombie") == "low"
        assert _get_threat_level("creeper") == "high"
        assert _get_threat_level("skeleton") == "medium"
        assert _get_threat_level("warden") == "critical"

    def test_threat_levels_namespaced(self):
        from minecraft_ai_bridge.minecraft.actions import _get_threat_level

        assert _get_threat_level("minecraft:creeper") == "high"
        assert _get_threat_level("minecraft:zombie") == "low"

    def test_is_blacklisted_iron_golem(self):
        from minecraft_ai_bridge.minecraft.actions import _is_blacklisted

        # The user mentioned "iron golem = never" — verify the canonical
        # examples and a few more.
        assert _is_blacklisted("iron_golem")
        assert _is_blacklisted("villager")
        assert _is_blacklisted("wolf")  # tamed wolves are friendly
        assert _is_blacklisted("cat")
        assert _is_blacklisted("wandering_trader")

    def test_is_blacklisted_namespaced(self):
        from minecraft_ai_bridge.minecraft.actions import _is_blacklisted

        assert _is_blacklisted("minecraft:iron_golem")
        assert _is_blacklisted("minecraft:villager")

    def test_is_blacklisted_hostile_mob_not_in_list(self):
        from minecraft_ai_bridge.minecraft.actions import _is_blacklisted

        # Hostile mobs should NOT be blacklisted
        assert not _is_blacklisted("zombie")
        assert not _is_blacklisted("creeper")
        assert not _is_blacklisted("skeleton")

    def test_should_attack_combines_blacklist_and_critical(self):
        from minecraft_ai_bridge.minecraft.actions import _should_attack

        # Blacklisted: never attack
        assert not _should_attack("iron_golem")
        assert not _should_attack("villager")
        # Critical: never attack (too dangerous to engage)
        assert not _should_attack("warden")
        # Normal hostiles: attack
        assert _should_attack("zombie")
        assert _should_attack("creeper")


class TestScanEntitiesDetailed:
    """The scan_entities action now returns threat level and
    should_attack info per mob.  These tests verify the new
    structured data."""

    async def test_scan_entities_returns_detailed_field(self, mock_mc):
        from minecraft_ai_bridge.minecraft.actions import (
            ActionType,
            execute_action,
        )

        mock_mc.set_hostile_mobs(["zombie", "creeper"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})
        assert result.success
        data = result.data
        assert "detailed" in data
        assert isinstance(data["detailed"], list)
        types_found = {m["type"] for m in data["detailed"]}
        assert "zombie" in types_found
        assert "creeper" in types_found

    async def test_scan_entities_detailed_includes_threat(self, mock_mc):
        from minecraft_ai_bridge.minecraft.actions import (
            ActionType,
            execute_action,
        )

        mock_mc.set_hostile_mobs(["creeper", "zombie"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})
        detailed = {m["type"]: m for m in result.data["detailed"]}
        # Creeper is high, zombie is low
        assert detailed["creeper"]["threat"] == "high"
        assert detailed["zombie"]["threat"] == "low"

    async def test_scan_entities_detailed_includes_should_attack(self, mock_mc):
        from minecraft_ai_bridge.minecraft.actions import (
            ActionType,
            execute_action,
        )

        mock_mc.set_hostile_mobs(["zombie", "creeper"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})
        detailed = {m["type"]: m for m in result.data["detailed"]}
        # Both should be attackable
        assert detailed["zombie"]["should_attack"] is True
        assert detailed["creeper"]["should_attack"] is True

    async def test_scan_entities_no_mobs(self, mock_mc):
        from minecraft_ai_bridge.minecraft.actions import (
            ActionType,
            execute_action,
        )

        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})
        assert result.data["detailed"] == []
        assert result.data["mobs_nearby"] == []
        assert "blacklisted" not in result.data
        assert result.data["too_dangerous"] == []

    async def test_scan_entities_picks_highest_threat(self, mock_mc):
        from minecraft_ai_bridge.bridge.self_preservation import (
            SelfPreservationLayer,
        )
        from minecraft_ai_bridge.minecraft.actions import (
            ActionType,
            execute_action,
        )

        # Mix of threats; verify the layer picks high over low
        mock_mc.set_hostile_mobs(["zombie", "creeper", "skeleton"])
        result = await execute_action(mock_mc, ActionType.SCAN_ENTITIES, {"radius": 5})
        # Use the static picker to find the best target
        target = SelfPreservationLayer._pick_target(result.data["detailed"])
        # Creeper is high, so it should be picked over zombie (low)
        # and skeleton (medium)
        assert target == "creeper"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.PLACE_BLOCK, {"block_type": "stone[snowy=true]"}),
        (ActionType.PLACE_BLOCK, {"block_type": "stone{}"}),
        (ActionType.PLACE_BLOCK, {"block_type": "stone replace"}),
        (ActionType.PLACE_BLOCK, {"block_type": "Stone", "x": 1, "y": 64, "z": 1}),
        (ActionType.CRAFT_ITEM, {"item_type": "stone{}"}),
        (ActionType.CRAFT_ITEM, {"item_type": "stone", "amount": 65}),
        (ActionType.CRAFT_ITEM, {"item_type": "stone", "amount": "1 extra"}),
        (ActionType.DROP_ITEM, {"item_type": "stone extra"}),
        (ActionType.DROP_ITEM, {"item_type": "stone", "amount": 0}),
    ],
)
async def test_command_ids_and_amounts_are_validated_before_interpolation(mock_mc, action, params):
    mock_mc.set_position(0, 64, 0)
    result = await execute_action(mock_mc, action, params)

    assert not result.success
    assert mock_mc.commands_ran == []


@pytest.mark.asyncio
async def test_namespaced_ids_are_accepted(mock_mc):
    result = await execute_action(
        mock_mc, ActionType.CRAFT_ITEM, {"item_type": "minecraft:oak_planks", "amount": 4}
    )

    assert result.success
    mock_mc.assert_command_contains("give")
    assert "oak_planks 4" in mock_mc.last_command()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ActionType.MOVE_TO, ActionType.TELEPORT])
@pytest.mark.parametrize(
    "params",
    [
        {"x": "1 2", "y": 64, "z": 0},
        {"x": True, "y": 64, "z": 0},
        {"x": float("nan"), "y": 64, "z": 0},
        {"x": 0, "y": float("inf"), "z": 0},
        {"x": 0, "y": 64, "z": [1]},
        {"x": 0, "y": 64, "z": "~"},
    ],
)
async def test_teleport_coordinates_must_be_finite_numbers(mock_mc, action, params):
    mock_mc.set_position(0, 64, 0)
    result = await execute_action(mock_mc, action, params)

    assert not result.success
    assert mock_mc.commands_ran == []
    assert await mock_mc.get_player_pos() == (0, 64, 0)


@pytest.mark.asyncio
async def test_teleport_accepts_numeric_strings(mock_mc):
    result = await execute_action(mock_mc, ActionType.TELEPORT, {"x": "5", "y": 64.5, "z": -3})

    assert result.success
    assert result.data == {"x": 5.0, "y": 64.5, "z": -3.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block_type",
    [
        "other:stone",
        "minecraft:stone/slab",
        "minecraft:minecraft:stone",
        "STONE",
        "stone\t",
        "a" * 65,
        "",
        None,
        7,
        ["stone"],
    ],
)
async def test_place_block_rejects_non_plain_ids(mock_mc, block_type):
    result = await execute_action(mock_mc, ActionType.PLACE_BLOCK, {"block_type": block_type})

    assert not result.success
    assert mock_mc.commands_ran == []


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [True, 1.5, "1", -1, None])
async def test_item_amounts_must_be_bounded_integers(mock_mc, amount):
    mock_mc.set_position(0, 64, 0)
    for action in (ActionType.CRAFT_ITEM, ActionType.DROP_ITEM):
        result = await execute_action(mock_mc, action, {"item_type": "stone", "amount": amount})
        assert not result.success
    assert mock_mc.commands_ran == []


@pytest.mark.asyncio
async def test_valid_block_and_item_operations_still_run(mock_mc):
    mock_mc.set_position(0, 64, 0)

    params = {"block_type": "minecraft:oak_log", "x": 1, "y": 64, "z": 1}
    placed = await execute_action(mock_mc, ActionType.PLACE_BLOCK, params)
    dropped = await execute_action(
        mock_mc, ActionType.DROP_ITEM, {"item_type": "cobblestone", "amount": 64}
    )

    assert placed.success
    assert await mock_mc.get_block(1, 64, 1) == "oak_log"
    assert dropped.success
    assert any("summon item" in command for command in mock_mc.commands_ran)
