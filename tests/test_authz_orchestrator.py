from __future__ import annotations

import asyncio

import pytest

from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import AuthzService, PolicyDecision
from tests.conftest import MockMcpqClient


def _create_controller(database: str, *, scopes: set[str] | None = None) -> str:
    auth = AuthzService(database)
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    credential = auth.create_credential(
        "AIBot",
        "owner",
        scopes or {"steve:control"},
        admin_token=admin.token,
    )
    auth.close()
    return credential.token


def test_authz_config_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTHZ_ENABLED", raising=False)
    assert AppConfig().authz.enabled is False


def test_authz_controller_token_is_loaded_from_environment_not_yaml(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "authz:\n  database_path: authz.sqlite3\n  steve_id: AIBot\n  session_lease_seconds: 120\n"
    )
    monkeypatch.setenv("AUTHZ_ENABLED", "true")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", "mab-test-token")
    monkeypatch.setenv("AUTHZ_STEVE_ID", "EnvSteve")
    config = AppConfig.from_yaml(config_path)
    assert config.authz.controller_token.get_secret_value() == "mab-test-token"
    assert config.authz.enabled

    config = AppConfig.from_yaml(tmp_path / "missing-config.yaml")
    assert config.authz.enabled
    assert config.authz.steve_id == "EnvSteve"

    config_path.write_text(
        "authz:\n  enabled: true\n  steve_id: AIBot\n  controller_token: must-not-be-in-yaml\n"
    )
    with pytest.raises(ValueError, match="environment variable"):
        AppConfig.from_yaml(config_path)

    monkeypatch.delenv("AUTHZ_ENABLED", raising=False)
    monkeypatch.delenv("AUTHZ_CONTROLLER_TOKEN", raising=False)
    monkeypatch.setenv("authz_enabled", "true")
    monkeypatch.setenv("authz_steve_id", "LowerCaseSteve")
    monkeypatch.setenv("authz_controller_token", "mab-lowercase-token")
    config = AppConfig.from_yaml(tmp_path / "missing-config.yaml")
    assert config.authz.enabled
    assert config.authz.steve_id == "LowerCaseSteve"
    assert config.authz.controller_token.get_secret_value() == "mab-lowercase-token"


@pytest.mark.asyncio
async def test_configured_orchestrator_gates_actions_and_releases_session(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "authz:\n"
        "  enabled: true\n"
        f"  database_path: {database}\n"
        "  steve_id: AIBot\n"
        "  session_lease_seconds: 120\n"
        "bridge:\n"
        f"  event_journal_path: {tmp_path / 'events.sqlite3'}\n"
    )
    config = AppConfig.from_yaml(config_path)
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", no_sleep)
    orchestrator = Orchestrator(config)
    await orchestrator._connect()

    assert orchestrator._authz_session is not None
    session = orchestrator._authz_session
    allowed = await orchestrator._action_service.execute(ActionType.CHAT, {"message": "allowed"})
    assert allowed.success

    await orchestrator._disconnect()
    assert orchestrator._authz_service is None
    with AuthzService(database) as auth:
        assert not auth.validate_session(session)
        credential = auth.authenticate(token)
        replacement = auth.acquire_session(
            token,
            "AIBot",
            owner_id=credential.owner_id,
            lease_expires_at=session.expires_at,
        )
        assert auth.validate_session(replacement)
        auth.release_session(replacement)


@pytest.mark.asyncio
async def test_configured_authz_revocation_denies_next_action(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.authz.session_lease_seconds = 120
    config.bridge.event_journal_path = str(tmp_path / "events.sqlite3")
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", no_sleep)
    orchestrator = Orchestrator(config)
    await orchestrator._connect()
    accepted = await orchestrator._action_service.execute(
        ActionType.CHAT,
        {"message": "before revocation"},
    )
    assert accepted.success
    chat_count = len(world.chat_messages_sent)

    orchestrator._authz_service.revoke_credential(token)
    rejected = await orchestrator._action_service.execute(
        ActionType.CHAT,
        {"message": "after revocation"},
    )
    assert not rejected.success
    assert len(world.chat_messages_sent) == chat_count
    await orchestrator._disconnect()


@pytest.mark.asyncio
async def test_authz_is_revalidated_after_awaited_caller_policy(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.bridge.event_journal_path = str(tmp_path / "events.sqlite3")
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def revoke_then_allow(_request):
        await asyncio.sleep(0)
        orchestrator._authz_service.revoke_credential(token)
        return PolicyDecision(allowed=True)

    orchestrator = Orchestrator(config, action_policy=revoke_then_allow)
    await orchestrator._connect()
    messages_before = len(world.chat_messages_sent)
    result = await orchestrator._action_service.execute(
        ActionType.CHAT,
        {"message": "must not dispatch"},
    )
    assert not result.success
    assert len(world.chat_messages_sent) == messages_before
    await orchestrator._disconnect()


@pytest.mark.asyncio
async def test_authz_release_error_does_not_skip_other_cleanup(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.bridge.event_journal_path = str(tmp_path / "events.sqlite3")
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", no_sleep)
    orchestrator = Orchestrator(config)
    await orchestrator._connect()

    def failed_release(_session):
        raise sqlite3.OperationalError("simulated release failure")

    monkeypatch.setattr(orchestrator._authz_service, "release_session", failed_release)
    with pytest.raises(sqlite3.OperationalError, match="simulated release failure"):
        await orchestrator._disconnect()
    assert not world.connected
    assert orchestrator._event_journal is None


@pytest.mark.asyncio
async def test_authz_gate_requires_control_scope_before_startup_mutations(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database), scopes={"steve:observe:self"})
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.authz.session_lease_seconds = 120
    config.bridge.event_journal_path = str(tmp_path / "events.sqlite3")
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", no_sleep)
    orchestrator = Orchestrator(config)
    with pytest.raises(PermissionError, match="control scope"):
        await orchestrator._connect()
    assert world.commands_ran == []
    await orchestrator._disconnect()
