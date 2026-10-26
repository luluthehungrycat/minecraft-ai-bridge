"""LLM client factory wiring."""

from __future__ import annotations

from minecraft_ai_bridge.config import AppConfig
from minecraft_ai_bridge.llm.client import OpenRouterClient, create_llm_client


def test_openrouter_client_uses_configured_base_url():
    config = AppConfig()
    config.llm.provider = "openrouter"
    config.llm.openrouter_api_key = "test-key"
    config.llm.openrouter_base_url = "https://router.requesty.ai/v1"

    client = create_llm_client(config)

    assert isinstance(client, OpenRouterClient)
    assert str(client._client.base_url).rstrip("/") == "https://router.requesty.ai/v1"


def test_openrouter_client_defaults_to_openrouter():
    config = AppConfig()
    config.llm.provider = "openrouter"
    config.llm.openrouter_api_key = "test-key"

    client = create_llm_client(config)

    assert str(client._client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
