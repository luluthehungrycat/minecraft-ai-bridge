"""Tests for in-game chat command parsing and dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from minecraft_ai_bridge.bridge.chat_commands import (
    _COMMAND_RE,
    COMMANDS,
    ChatCommandHandler,
)
from minecraft_ai_bridge.bridge.memory import AgentMemory
from minecraft_ai_bridge.config import ChatControlConfig


class TestCommandParsing:
    """Regex-based chat command parser."""

    def test_match_basic(self):
        m = _COMMAND_RE.match("!status")
        assert m is not None
        assert m.group(1).strip() == "!status"

    def test_match_with_args(self):
        m = _COMMAND_RE.match("!goal Build a house")
        assert m is not None
        assert m.group(1).strip() == "!goal"
        assert "Build a house" in m.group(2)

    def test_regular_chat(self):
        m = _COMMAND_RE.match("Hello everyone!")
        assert m is None

    def test_empty(self):
        m = _COMMAND_RE.match("")
        assert m is None

    def test_with_slash_prefix(self):
        m = _COMMAND_RE.match("/!stop")
        assert m is not None
        assert m.group(1).strip() == "/!stop"

    def test_unicode_player(self):
        m = _COMMAND_RE.match("!status")
        assert m is not None

    def test_commands_set(self):
        assert "!status" in COMMANDS
        assert "!stop" in COMMANDS
        assert "!goal" in COMMANDS
        assert "!goto" in COMMANDS
        assert "!follow" in COMMANDS
        assert "!come" in COMMANDS
        assert "!help" in COMMANDS
        assert "!nonexistent" not in COMMANDS

    def test_chat_control_allowlist_validates_identity_formats(self):
        configured = ChatControlConfig(
            allowed_uuids=["00000000-0000-0000-0000-000000000001"],
            allowed_names=["Authorized_1"],
        )
        assert configured.allowed_uuids == ["00000000-0000-0000-0000-000000000001"]
        assert configured.allowed_names == ["Authorized_1"]
        with pytest.raises(ValueError, match="valid UUIDs"):
            ChatControlConfig(allowed_uuids=["not-a-uuid"])
        with pytest.raises(ValueError, match="Minecraft usernames"):
            ChatControlConfig(allowed_names=["Player With Spaces"])


class TestHarness:
    """Minimal orchestrator substitute."""

    def __init__(self):
        from tests.conftest import MockMcpqClient

        self._mc = MockMcpqClient()
        self._stop_requested = False
        self._follow_target = None
        self._memory = AgentMemory(window=5)
        self._goals = None
        self._turn = 0
        self._internal_action_authority = object()
        self._config = SimpleNamespace(
            chat_control=SimpleNamespace(allowed_uuids=[], allowed_names=["Player"])
        )


@pytest.fixture
def harness():
    return TestHarness()


@pytest.mark.asyncio
class TestCommandDispatch:
    async def test_poll_authorizes_using_event_player_not_message_markup(self, harness):
        handler = ChatCommandHandler(harness)
        harness._mc.set_chat_events(
            [SimpleNamespace(player=SimpleNamespace(name="Player"), message="!stop")]
        )
        await handler.poll()
        assert harness._stop_requested

    async def test_poll_denies_unlisted_player_even_with_permissive_action_policy(self, harness):
        harness._action_policy = lambda *_args, **_kwargs: True
        handler = ChatCommandHandler(harness)
        harness._mc.set_chat_events(
            [SimpleNamespace(player=SimpleNamespace(name="Intruder"), message="!stop")]
        )
        await handler.poll()
        assert not harness._stop_requested

    async def test_unlisted_uuid_cannot_fall_back_to_an_allowlisted_name(self, harness):
        harness._config.chat_control.allowed_uuids = ["trusted-uuid"]
        harness._config.chat_control.allowed_names = ["Player"]
        handler = ChatCommandHandler(harness)
        harness._mc.set_chat_events(
            [
                SimpleNamespace(
                    player=SimpleNamespace(uuid="untrusted-uuid", name="Player"),
                    message="!stop",
                )
            ]
        )
        await handler.poll()
        assert not harness._stop_requested

    async def test_spoofed_markup_and_missing_metadata_do_not_authorize(self, harness):
        handler = ChatCommandHandler(harness)
        harness._mc.set_chat_events(
            [
                SimpleNamespace(player=SimpleNamespace(name="Intruder"), message="<Player> !stop"),
                SimpleNamespace(player=None, message="!stop"),
            ]
        )
        await handler.poll()
        assert not harness._stop_requested

    async def test_direct_control_dispatch_requires_identity_and_removed_allowlist_revokes(
        self, harness
    ):
        handler = ChatCommandHandler(harness)
        await handler.handle_command("!stop", "", "Player")
        assert not harness._stop_requested
        harness._config.chat_control.allowed_names.clear()
        await handler.poll()  # no event; current allowlist is read on each dispatch below
        await handler.handle_command(
            "!stop", "", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert not harness._stop_requested

    async def test_handle_stop(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!stop", "", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert harness._stop_requested
        assert any("Shutting down" in m for m in harness._mc.chat_messages_sent)

    async def test_handle_help(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command("!help", "", "Player")
        assert any("!status" in m for m in harness._mc.chat_messages_sent)

    async def test_handle_goto_no_args(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!goto", "", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert any("Usage" in m for m in harness._mc.chat_messages_sent)

    async def test_handle_goto(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!goto", "Player2", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        harness._mc.assert_command_contains("tp AIBot Player2")

    async def test_handle_follow(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!follow", "Player2", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert harness._follow_target == "Player2"

    async def test_handle_follow_no_args(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!follow", "", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert any("Usage" in m for m in harness._mc.chat_messages_sent)

    async def test_handle_goal_no_args(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command(
            "!goal", "", "Player", trusted_player=SimpleNamespace(name="Player")
        )
        assert any("Usage" in m for m in harness._mc.chat_messages_sent)

    async def test_unknown_command(self, harness):
        handler = ChatCommandHandler(harness)
        await handler.handle_command("!fake", "", "Player")
        # No crash expected

    async def test_process_valid_line(self, harness):
        handler = ChatCommandHandler(harness)
        await handler._process_line("<Player> !status")
        # Should not crash

    async def test_process_non_command(self, harness):
        handler = ChatCommandHandler(harness)
        await handler._process_line("<Player> just chatting")
        # Should be a no-op
