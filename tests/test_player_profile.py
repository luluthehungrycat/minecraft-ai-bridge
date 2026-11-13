"""Secret-free distribution contract for the optional Minecraft-only Hermes profile."""

from __future__ import annotations

from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "profiles" / "steve-player"


def test_player_template_enables_only_minecraft_tools():
    config = yaml.safe_load((TEMPLATE / "config.yaml").read_text())
    assert config["toolsets"] == []
    assert all(value == ["mcp-minecraft"] for value in config["platform_toolsets"].values())
    assert set(config["mcp_servers"]) == {"minecraft"}
    server = config["mcp_servers"]["minecraft"]
    assert len(server["tools"]["include"]) == 13
    assert all(name.startswith("minecraft_") for name in server["tools"]["include"])
    assert not server["tools"]["resources"] and not server["tools"]["prompts"]
    assert not server["sampling"]["enabled"] and not server["elicitation"]["enabled"]
    assert not config["memory"]["memory_enabled"]
    assert not config["memory"]["user_profile_enabled"]


def test_player_template_keeps_credentials_external():
    config = yaml.safe_load((TEMPLATE / "config.yaml").read_text())
    server = config["mcp_servers"]["minecraft"]
    assert server["url"] == "${MAB_MCP_URL}"
    assert server["headers"] == {"Authorization": "Bearer ${MAB_CONTROLLER_TOKEN}"}
    text = (TEMPLATE / "config.yaml").read_text()
    assert "AUTHZ_WORLD_ADMIN_TOKEN" not in text
    assert "mab_" not in text
    assert not (TEMPLATE / ".env").exists()


def test_player_template_pins_requested_inference_routes():
    config = yaml.safe_load((TEMPLATE / "config.yaml").read_text())
    assert config["model"]["provider"] == "openai-codex"
    assert config["model"]["default"] == "gpt-6-luna"
    assert config["fallback_providers"] == [
        {"provider": "claude-subscription-directsdk-experimental", "model": "claude-haiku-5-5"}
    ]
    manifest = yaml.safe_load((TEMPLATE / "distribution.yaml").read_text())
    assert manifest["name"] == "steve-player"
    assert manifest["version"] == "0.1.0"
