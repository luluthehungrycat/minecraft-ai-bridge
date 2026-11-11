"""RFC-0001 Phase D refactor: shared observation protocol vs MCPQ commands."""

from __future__ import annotations

from typing import Any

from minecraft_ai_bridge.minecraft.actions import ActionType, execute_action
from minecraft_ai_bridge.minecraft.observer import Observer
from minecraft_ai_bridge.runtime import ActionStatus, AuthzService, ControllerService
from minecraft_ai_bridge.runtime.contracts import (
    BackendKind,
    Capabilities,
    CraftingMode,
    MovementMode,
    SupportLevel,
)
from minecraft_ai_bridge.runtime.world_adapter import (
    McpqCommandAdapter,
    McpqWorldAdapter,
    WorldAdapter,
)
from tests.conftest import MockMcpqClient
from tests.test_controller_service import _request

COMMAND_METHODS = ("run_as_player", "run_command_blocking", "teleport_player", "set_block")


class ReadOnlyWorld:
    """Implements only the shared WorldAdapter surface, like a player client."""

    def __init__(self, client: MockMcpqClient) -> None:
        self._client = client

    @property
    def connected(self) -> bool:
        return True

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend=BackendKind.MINEFLAYER,
            movement=MovementMode.PHYSICAL,
            crafting=CraftingMode.VANILLA,
            actions={ActionType.CHAT: SupportLevel.SUPPORTED},
        )

    def __getattr__(self, name: str) -> Any:
        if name in COMMAND_METHODS:
            raise AttributeError(name)
        return getattr(self._client, name)


def test_shared_protocol_has_no_command_methods():
    for name in COMMAND_METHODS:
        assert not hasattr(WorldAdapter, name)
        assert hasattr(McpqCommandAdapter, name)


def test_mcpq_adapter_implements_both_protocols():
    adapter = McpqWorldAdapter(MockMcpqClient())  # type: ignore[arg-type]
    assert isinstance(adapter, WorldAdapter)
    assert isinstance(adapter, McpqCommandAdapter)


async def test_observing_a_backend_without_handlers_sends_no_mcpq_commands():
    """Observer reads through the backend's check_* handlers; none exist yet
    for a non-MCPQ backend, so it gets an empty state instead of falling
    back to MCPQ commands the adapter does not have."""
    client = MockMcpqClient()
    client.set_position(3, 70, -4)
    world = ReadOnlyWorld(client)
    commands_before = list(client.commands_ran)

    state = await Observer(world).observe()

    assert state.position is None
    assert client.commands_ran == commands_before
    assert await Observer(world).observe_position() == (3, 70, -4)


async def test_a_non_mcpq_backend_never_reaches_mcpq_handlers(tmp_path):
    client = MockMcpqClient()
    result = await execute_action(ReadOnlyWorld(client), ActionType.CHAT, {"message": "x"})
    assert not result.success
    assert client.chat_messages_sent == []

    auth = AuthzService(tmp_path / "authz.sqlite3")
    try:
        auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
        admin = auth.create_admin_credential("root")
        token = auth.create_credential(
            "AIBot", "owner", {"steve:control"}, admin_token=admin.token
        ).token
        service = ControllerService(ReadOnlyWorld(client), auth, "AIBot")
        session = await service.open_session(token)

        chat = await service.submit_action(
            token, _request(session, ActionType.CHAT, {"message": "x"})
        )
        walk = await service.submit_action(
            token, _request(session, ActionType.WALK_TO, {"x": 1, "z": 1}, "w")
        )

        assert chat.status is ActionStatus.FAILED
        assert walk.status is ActionStatus.REJECTED
        assert walk.reason_code == "ACTION_NOT_SUPPORTED"
        assert client.chat_messages_sent == []
    finally:
        auth.close()
