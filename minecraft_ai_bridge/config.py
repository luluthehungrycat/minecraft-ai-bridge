"""Configuration management via YAML + environment variable overrides."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal, get_origin
from uuid import UUID

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
    world_admin_token: SecretStr = SecretStr("")
    session_lease_seconds: float = 3600
    # Safety-only operation after losing controller authority ends after
    # this many seconds unless an authorized in-game !stop arrives first.
    suspended_shutdown_seconds: float = 300

    model_config = SettingsConfigDict(env_prefix="authz_")

    @field_validator("session_lease_seconds")
    @classmethod
    def validate_session_lease(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("authz session lease must be a finite positive number")
        return value

    @field_validator("suspended_shutdown_seconds")
    @classmethod
    def validate_suspended_shutdown(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("authz suspended shutdown must be a finite non-negative number")
        return value

    def validate_controller_credentials(self) -> None:
        if not self.enabled:
            return
        if not self.steve_id.strip() or not self.controller_token.get_secret_value():
            raise ValueError("enabled authz requires steve_id and AUTHZ_CONTROLLER_TOKEN")
        # Startup provisioning, safe spawn, follow, and !goto/!come are
        # privileged internal actions that Authz gates on admin:world.
        if not self.world_admin_token.get_secret_value().strip():
            raise ValueError(
                "enabled authz requires AUTHZ_WORLD_ADMIN_TOKEN for bot provisioning "
                "and internal control actions"
            )


class ChatControlConfig(BaseSettings):
    """Allowlist and server identity mode for state-changing chat commands."""

    allowed_uuids: list[str] = Field(default_factory=list)
    allowed_names: list[str] = Field(default_factory=list)
    server_online_mode: bool = False

    model_config = SettingsConfigDict(env_prefix="chat_control_")

    @field_validator("allowed_uuids")
    @classmethod
    def normalize_allowed_uuids(cls, values: list[str]) -> list[str]:
        try:
            return [str(UUID(value.strip())) for value in values]
        except (AttributeError, ValueError) as exc:
            raise ValueError("chat control UUIDs must be valid UUIDs") from exc

    @field_validator("allowed_names")
    @classmethod
    def validate_allowed_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_]{1,16}", value) for value in values):
            raise ValueError("chat control names must be Minecraft usernames")
        return values


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
    chat_control: ChatControlConfig = Field(default_factory=ChatControlConfig)
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
        for section_name in (
            "minecraft",
            "mc_api",
            "llm",
            "bridge",
            "authz",
            "chat_control",
            "goals",
        ):
            yaml_data = raw.get(section_name, {})
            if section_name == "authz":
                for secret_field in ("controller_token", "world_admin_token"):
                    if secret_field in yaml_data:
                        raise ValueError(
                            f"authz.{secret_field} must be supplied by an environment variable"
                        )

            # Gather env-var overrides for this section
            section_model_cls = type(getattr(cfg, section_name))
            prefix: str = section_model_cls.model_config.get("env_prefix", "")
            env_data: dict = {}
            for field_name in section_model_cls.model_fields:
                env_val = env_lookup.get((prefix + field_name).casefold())
                if env_val is not None:
                    field = section_model_cls.model_fields[field_name]
                    if get_origin(field.annotation) is list:
                        try:
                            env_val = json.loads(env_val)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"{prefix}{field_name} must be a JSON array") from exc
                        if not isinstance(env_val, list):
                            raise ValueError(f"{prefix}{field_name} must be a JSON array")
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
