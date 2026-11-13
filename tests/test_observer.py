"""Tests for the observer and its NBT parsing functions."""

from __future__ import annotations

from minecraft_ai_bridge.minecraft.observer import (
    InventorySlot,
    _parse_health_value,
    _parse_inventory_nbt,
    _parse_nbt_list,
    _parse_nbt_value,
)

# No global asyncio marker — the async tests in TestObserver are individually marked


class TestObserver:
    """Integration tests using MockMcpqClient."""

    async def test_observe_position(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_position(42.0, 70.0, 100.0)
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.position == (42.0, 70.0, 100.0)

    async def test_observe_inventory(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_inventory(
            [
                {"item_id": "dirt", "count": 64, "slot": 0},
                {"item_id": "stone", "count": 32, "slot": 1},
            ]
        )
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert len(state.inventory) >= 2
        items_found = [s.item_id for s in state.inventory]
        assert "minecraft:dirt" in items_found or "dirt" in items_found

    async def test_observe_health(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_player_nbt("Health", 15.0)
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.health is not None
        assert state.health == 15.0

    async def test_observe_health_fallback_does_not_guess(self, mock_mc, monkeypatch):
        from minecraft_ai_bridge.minecraft.observer import Observer

        original = mock_mc._run_command_blocking

        async def unavailable_health(command: str) -> str:
            if command.startswith("data get entity AIBot Health"):
                return "No entity was found"
            if command.startswith("attribute AIBot minecraft:generic.max_health"):
                return "Has no attribute"
            return await original(command)

        monkeypatch.setattr(mock_mc, "_run_command_blocking", unavailable_health)
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.health is None

    async def test_observe_hunger(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_player_nbt("foodLevel", 12)
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.hunger == 12

    async def test_observe_hunger_from_live_command_envelope(self, mock_mc, monkeypatch):
        """Paper's successful /data output must reach the observer as a number."""
        from minecraft_ai_bridge.minecraft.observer import Observer

        original = mock_mc._run_command_blocking

        async def live_hunger(command: str) -> str:
            if command.startswith("data get entity AIBot foodLevel"):
                return "SmokeSteve has the following entity data: 7"
            return await original(command)

        monkeypatch.setattr(mock_mc, "_run_command_blocking", live_hunger)
        state = await Observer(mock_mc).observe()
        assert state.hunger == 7

    async def test_observe_hunger_default(self, mock_mc):
        """Hunger should default to a sane value when NBT is unavailable."""
        from minecraft_ai_bridge.minecraft.observer import Observer

        obs = Observer(mock_mc)
        state = await obs.observe()
        # Either the action returned a value or it fell back to the default
        # (20/20). Both are acceptable; we just want a number in valid range.
        assert state.hunger is not None
        assert 0 <= state.hunger <= 20

    async def test_observe_hunger_low(self, mock_mc):
        """Low hunger should be observable so the agent can react."""
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_player_nbt("foodLevel", 2)
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.hunger == 2

    async def test_observe_players(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_players(["AIBot", "Player1"])
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert "AIBot" in state.players
        assert "Player1" in state.players

    async def test_observe_biome(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_position(0.0, 65.0, 0.0)
        mock_mc.set_biome("plains")
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.biome is not None

    async def test_observe_position_only(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_position(77.0, 64.0, -50.0)
        obs = Observer(mock_mc)
        pos = await obs.observe_position()
        assert pos == (77.0, 64.0, -50.0)

    async def test_observe_biome_error(self, mock_mc):
        from minecraft_ai_bridge.minecraft.observer import Observer

        mock_mc.set_position(0.0, 65.0, 0.0)
        mock_mc.set_biome(RuntimeError("MCPQ error"))
        obs = Observer(mock_mc)
        state = await obs.observe()
        assert state.biome == "unknown"

    async def test_observe_biome_caches_known_and_unknown_results(self, mock_mc, monkeypatch):
        from minecraft_ai_bridge.minecraft.observer import Observer

        calls = 0
        original = mock_mc.get_biome

        async def counted_biome(x: int, y: int, z: int) -> str:
            nonlocal calls
            calls += 1
            return await original(x, y, z)

        monkeypatch.setattr(mock_mc, "get_biome", counted_biome)
        obs = Observer(mock_mc)

        await obs.observe()
        await obs.observe()
        assert calls == 1

        mock_mc.set_position(16.0, 65.0, 0.0)
        await obs.observe()
        assert calls == 2

        mock_mc.set_biome(RuntimeError("biome unavailable"))
        mock_mc.set_position(32.0, 65.0, 0.0)
        unknown_state = await obs.observe()
        await obs.observe()
        assert unknown_state.biome == "unknown"
        assert calls == 3

    async def test_biome_does_not_infer_from_surface_block(self):
        from minecraft_ai_bridge.minecraft.mc_api import McpqClient

        class FakeClient(McpqClient):
            def __init__(self) -> None:
                super().__init__(player_name="NetherBot")
                self.probes: list[str] = []

            async def get_block(self, x: int, y: int, z: int) -> str:
                raise AssertionError("biome detection must not inspect blocks")

            async def run_command_blocking(self, command: str) -> str:
                self.probes.append(command)
                if command == (
                    "execute as NetherBot at @s if biome 0 65 0 minecraft:forest "
                    "run say __biome_forest__"
                ):
                    return "[Server] __biome_forest__"
                return ""

        client = FakeClient()

        assert await client.get_biome(0, 65, 0) == "forest"
        assert client.probes == [
            "execute as NetherBot at @s if biome 0 65 0 minecraft:plains run say __biome_plains__",
            "execute as NetherBot at @s if biome 0 65 0 minecraft:desert run say __biome_desert__",
            "execute as NetherBot at @s if biome 0 65 0 minecraft:forest run say __biome_forest__",
        ]


class TestNbtValueParser:
    """Tests for _parse_nbt_value function."""

    def test_parse_float_double(self):
        assert _parse_nbt_value("20.0d") == 20.0

    def test_parse_int(self):
        assert _parse_nbt_value("42") == 42

    def test_parse_byte(self):
        assert _parse_nbt_value("64b") == 64

    def test_parse_negative(self):
        assert _parse_nbt_value("-10.5d") == -10.5

    def test_parse_empty(self):
        assert _parse_nbt_value("") is None

    def test_parse_whitespace(self):
        assert _parse_nbt_value("   ") is None

    def test_parse_long(self):
        assert _parse_nbt_value("100L") == 100

    def test_parse_float(self):
        assert _parse_nbt_value("5f") == 5

    def test_parse_non_numeric(self):
        result = _parse_nbt_value("no numbers here")
        assert result == "no numbers here"

    def test_parse_health_raw(self):
        assert _parse_nbt_value("Health: 20.0d") == 20.0
        assert _parse_nbt_value("Health: 0.0d") == 0.0
        assert _parse_nbt_value("15.5d") == 15.5

    def test_parse_health_named_field_and_custom_maximum(self):
        assert _parse_health_value("Health: 40.0d") == 40.0
        assert _parse_health_value("MaxHealth: 100.0d") is None

    def test_parse_health_rejects_invalid_values(self):
        assert _parse_health_value("Health: -1.0d") is None
        assert _parse_health_value("Health: NaNd") is None
        assert _parse_health_value("No entity was found") is None


class TestNbtListParser:
    """Tests for _parse_nbt_list function."""

    def test_parse_position(self):
        result = _parse_nbt_list("[1.0d, 64.0d, 3.0d]")
        assert result == [1.0, 64.0, 3.0]

    def test_parse_empty(self):
        assert _parse_nbt_list("[]") is None

    def test_parse_no_match(self):
        assert _parse_nbt_list("no brackets") is None

    def test_parse_with_prefix(self):
        result = _parse_nbt_list("Position: [10.5d, -3.0d, 0d]")
        assert result == [10.5, -3.0, 0.0]

    def test_parse_int_values(self):
        result = _parse_nbt_list("[1, 2, 3]")
        assert result == [1.0, 2.0, 3.0]


class TestParseInventoryNbt:
    """Tests for _parse_inventory_nbt function."""

    def test_parse_empty(self):
        assert _parse_inventory_nbt("") == []

    def test_parse_empty_inventory(self):
        assert _parse_inventory_nbt("Inventory: []") == []

    def test_parse_single_item(self):
        raw = '[{id:"minecraft:dirt",Count:64b,Slot:0b}]'
        result = _parse_inventory_nbt(raw)
        assert len(result) == 1
        assert result[0].item_id == "minecraft:dirt"
        assert result[0].count == 64
        assert result[0].slot == 0

    def test_parse_multiple_items(self):
        raw = '[{id:"minecraft:dirt",Count:64b,Slot:0b},{id:"minecraft:stone",Count:32b,Slot:1b}]'
        result = _parse_inventory_nbt(raw)
        assert len(result) == 2
        assert result[1].item_id == "minecraft:stone"
        assert result[1].count == 32

    def test_parse_with_prefix(self):
        raw = 'Inventory: [{id:"minecraft:oak_log",Count:8b,Slot:2b}]'
        result = _parse_inventory_nbt(raw)
        assert len(result) == 1
        assert result[0].item_id == "minecraft:oak_log"

    def test_parse_with_damage(self):
        raw = '[{id:"minecraft:diamond_pickaxe",Count:1b,Slot:0b,Damage:100b}]'
        result = _parse_inventory_nbt(raw)
        assert len(result) == 1
        assert result[0].item_id == "minecraft:diamond_pickaxe"

    def test_parse_key_order_case_and_damage(self):
        raw = '[{Slot:2b,id:"minecraft:diamond_pickaxe",count:1b,damage:7s}]'

        result = _parse_inventory_nbt(raw)

        assert result == [
            InventorySlot(
                item_id="minecraft:diamond_pickaxe",
                count=1,
                slot=2,
                damage=7,
            )
        ]

    def test_parse_partial_inventory_keeps_valid_compounds(self):
        raw = (
            '[{id:"minecraft:dirt",Count:64b,Slot:0b},'
            "{broken},"
            '{id:"minecraft:stone",count:32s,slot:1b}]'
        )

        result = _parse_inventory_nbt(raw)

        assert [(slot.item_id, slot.count, slot.slot) for slot in result] == [
            ("minecraft:dirt", 64, 0),
            ("minecraft:stone", 32, 1),
        ]

    def test_parse_signed_offhand_slot(self):
        result = _parse_inventory_nbt('[{id:"minecraft:shield",Count:1b,Slot:-106b}]')
        assert result == [InventorySlot(item_id="minecraft:shield", count=1, slot=-106)]

    def test_parse_malformed(self):
        assert _parse_inventory_nbt("garbage") == []

    def test_parse_partially_valid(self):
        raw = '[{id:"minecraft:dirt",Count:64b,Slot:0b},garbage]'
        result = _parse_inventory_nbt(raw)
        assert len(result) >= 1  # should parse valid part

    def test_inventory_slot_display_name(self):
        slot = InventorySlot(item_id="minecraft:oak_planks", count=16, slot=0)
        assert slot.display_name == "oak planks"

    def test_inventory_slot_no_namespace(self):
        slot = InventorySlot(item_id="diamond", count=1, slot=5)
        assert slot.display_name == "diamond"
