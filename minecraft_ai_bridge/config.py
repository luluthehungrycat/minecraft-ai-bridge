"""Configuration management via YAML + environment variable overrides."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MinecraftConfig(BaseSettings):
    host: str = "localhost"
    # RCON settings (kept as optional fallback for admin commands)
    rcon_port: int = 25575
    rcon_password: str = ""
    player_name: str = "AIBot"

    model_config = SettingsConfigDict(env_prefix="minecraft_")


class MCPQConfig(BaseSettings):
    """Connection settings for the MCPQ plugin on a Paper server.

    MCPQ replaces both pyCraft (bot) and most RCON usage — it gives the
    bridge direct world-manipulation and player-control APIs via gRPC.
    """

    host: str = "localhost"
    port: int = 1789
    player_name: str = "AIBot"

    model_config = SettingsConfigDict(env_prefix="mc_api_")


class LLMConfig(BaseSettings):
    provider: Literal["openai", "anthropic", "ollama", "openrouter", "opencode_server"] = "openai"
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 2048

    # Provider-specific keys set via env vars
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # OpenRouter (OpenAI-compatible proxy)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str = ""
    openrouter_title: str = "Minecraft AI Bridge"

    # OpenCode Server (session-based inference API)
    opencode_server_url: str = "http://localhost:4096"
    opencode_server_api_key: str = ""
    opencode_server_model: str = "big-pickle"

    model_config = SettingsConfigDict(env_prefix="llm_")

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def fallback_openai(cls, v: str) -> str:
        if not v:
            import os

            return os.getenv("OPENAI_API_KEY", "")
        return v

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def fallback_anthropic(cls, v: str) -> str:
        if not v:
            import os

            return os.getenv("ANTHROPIC_API_KEY", "")
        return v

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def fallback_openrouter(cls, v: str) -> str:
        if not v:
            import os

            return os.getenv("OPENROUTER_API_KEY", "")
        return v


class BridgeConfig(BaseSettings):
    max_iterations: int = 100
    cycle_delay: float = 1.0
    memory_window: int = 20
    verbose: bool = True
    event_journal_path: str = "data/safety-events.sqlite3"

    model_config = SettingsConfigDict(env_prefix="bridge_")


class AuthzConfig(BaseSettings):
    """Optional local credential gate for the built-in controller session."""

    enabled: bool = False
    database_path: str = "data/authz.sqlite3"
    steve_id: str = ""
    controller_token: SecretStr = SecretStr("")
    session_lease_seconds: float = 3600

    model_config = SettingsConfigDict(env_prefix="authz_")

    @field_validator("session_lease_seconds")
    @classmethod
    def validate_session_lease(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("authz session lease must be a finite positive number")
        return value

    def validate_controller_credentials(self) -> None:
        if self.enabled and (
            not self.steve_id.strip() or not self.controller_token.get_secret_value()
        ):
            raise ValueError("enabled authz requires steve_id and AUTHZ_CONTROLLER_TOKEN")


class GoalConfig(BaseSettings):
    default: str = "Explore the world and gather resources"
    max_depth: int = 5

    model_config = SettingsConfigDict(env_prefix="goals_")


class AppConfig(BaseSettings):
    minecraft: MinecraftConfig = Field(default_factory=MinecraftConfig)
    mc_api: MCPQConfig = Field(default_factory=MCPQConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    authz: AuthzConfig = Field(default_factory=AuthzConfig)
    goals: GoalConfig = Field(default_factory=GoalConfig)

    model_config = SettingsConfigDict(env_prefix="app_")

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> AppConfig:
        """Load config from a YAML file with env-var overrides.

        Priority: environment vars > YAML > field defaults.
        """
        import os as _os

        path = Path(path)

        # Start from pure env-resolved config (env > defaults)
        # Defer cross-field AuthzConfig validation until YAML and environment
        # values have been merged. Otherwise env-enabled authz can fail on
        # this intermediate object before its Steve ID is read from YAML.
        cfg = cls(authz=AuthzConfig(enabled=False))

        if path.exists():
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        env_lookup = {key.casefold(): value for key, value in _os.environ.items()}
        for section_name in ("minecraft", "mc_api", "llm", "bridge", "authz", "goals"):
            yaml_data = raw.get(section_name, {})
            if section_name == "authz" and "controller_token" in yaml_data:
                raise ValueError(
                    "authz.controller_token must be supplied by an environment variable"
                )

            # Gather env-var overrides for this section
            section_model_cls = type(getattr(cfg, section_name))
            prefix: str = section_model_cls.model_config.get("env_prefix", "")
            env_data: dict = {}
            for field_name in section_model_cls.model_fields:
                env_val = env_lookup.get((prefix + field_name).casefold())
                if env_val is not None:
                    env_data[field_name] = env_val

            # Merge: env > yaml > defaults
            # Re-creating the model applies proper type coercion (e.g.
            # string "1789" → int 1789) which model_copy(update=...) skips.
            merged_data = {**yaml_data, **env_data}
            if merged_data:
                merged = section_model_cls(**merged_data)
                setattr(cfg, section_name, merged)

        cfg.authz.validate_controller_credentials()
        return cfg
