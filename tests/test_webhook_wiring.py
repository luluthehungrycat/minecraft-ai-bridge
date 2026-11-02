"""Webhook subscriptions from admin config, the delivery loop, and MCP wiring (#18)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from minecraft_ai_bridge.config import AppConfig, WebhooksConfig
from minecraft_ai_bridge.runtime import webhooks as webhook_runtime
from minecraft_ai_bridge.runtime.contracts import Event, EventSeverity
from minecraft_ai_bridge.runtime.event_journal import EventJournal
from minecraft_ai_bridge.runtime.webhooks import (
    WebhookError,
    WebhookService,
    webhook_subscriptions,
)

SECRET = "x" * 32


def _write_config(tmp_path: Path, webhooks: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({"webhooks": webhooks}))  # JSON is valid YAML
    return path


def _hook(**overrides: object) -> dict:
    values: dict[str, object] = {
        "id": "ops",
        "url": "https://hooks.example.test/minecraft",
        "secret_env": "OPS_HOOK_SECRET",
    }
    values.update(overrides)
    return values


def _event(event_id: str, event_type: str = "steve.damaged") -> Event:
    return Event(
        event_id=event_id,
        type=event_type,
        severity=EventSeverity.MEDIUM,
        steve_id="AIBot",
        source="test",
        payload={},
    )


class FakeTransport:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict] = []
        self.sent = asyncio.Event()

    async def send_webhook_pinned(self, *, url, addresses, content, headers, timeout) -> int:
        self.bodies.append(json.loads(content))
        self.sent.set()
        return self.status


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        webhook_runtime, "_check_resolved_addresses", lambda host: ("93.184.216.34",)
    )


# ── Config ──────────────────────────────────────────────────────────────


def test_subscriptions_load_from_yaml(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "database_path": "data/hooks.sqlite3",
            "poll_interval": 0.5,
            "subscriptions": [_hook(event_types=["steve.damaged"], max_attempts=3)],
        },
    )
    config = AppConfig.from_yaml(path).webhooks
    assert config.database_path == "data/hooks.sqlite3"
    assert config.poll_interval == 0.5
    (hook,) = config.subscriptions
    assert (hook.id, hook.secret_env, hook.event_types, hook.max_attempts) == (
        "ops",
        "OPS_HOOK_SECRET",
        ["steve.damaged"],
        3,
    )


def test_no_webhooks_by_default() -> None:
    assert AppConfig().webhooks.subscriptions == []


def test_inline_secret_in_yaml_is_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"subscriptions": [_hook(secret=SECRET)]})
    with pytest.raises(ValueError, match="environment variable"):
        AppConfig.from_yaml(path)


@pytest.mark.parametrize(
    "webhooks",
    [
        {"subscriptions": [_hook(secret_env="NOT A NAME")]},
        {"subscriptions": [_hook(unknown_option=True)]},
        {"poll_interval": 0},
        {"poll_interval": float("nan")},
        {"poll_interval": 120},
    ],
)
def test_invalid_webhook_config_is_rejected(tmp_path: Path, webhooks: dict) -> None:
    with pytest.raises(ValidationError):
        AppConfig.from_yaml(_write_config(tmp_path, webhooks))


def test_subscriptions_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOKS_SUBSCRIPTIONS", json.dumps([_hook()]))
    config = AppConfig.from_yaml(tmp_path / "missing.yaml").webhooks
    assert [hook.id for hook in config.subscriptions] == ["ops"]


# ── Building subscriptions ──────────────────────────────────────────────


def _config(*hooks: dict) -> WebhooksConfig:
    return WebhooksConfig(subscriptions=list(hooks))


def test_subscription_reads_its_secret_from_the_environment() -> None:
    (subscription,) = webhook_subscriptions(
        _config(_hook(event_types=["steve.damaged"])), {"OPS_HOOK_SECRET": SECRET}
    )
    assert subscription.subscription_id == "ops"
    assert subscription.secret == SECRET.encode()
    assert subscription.event_types == frozenset({"steve.damaged"})
    # Without explicit allowed_hosts, only the URL's own host is allowed.
    assert subscription.allowed_hosts == frozenset({"hooks.example.test"})


def test_missing_secret_names_the_subscription_and_variable() -> None:
    with pytest.raises(WebhookError, match="'ops'.*OPS_HOOK_SECRET"):
        webhook_subscriptions(_config(_hook()), {})


@pytest.mark.parametrize(
    ("hook", "secret", "message"),
    [
        (_hook(), "short", "32 bytes"),
        (_hook(url="http://hooks.example.test/x"), SECRET, "HTTPS"),
        (_hook(allowed_hosts=["other.example.test"]), SECRET, "allowlisted"),
        (_hook(url="https://127.0.0.1/x"), SECRET, "unsafe"),
        (_hook(id="bad id"), SECRET, "safe ASCII"),
    ],
)
def test_invalid_subscription_is_reported_with_its_id(hook: dict, secret: str, message: str):
    with pytest.raises(WebhookError, match=message) as excinfo:
        webhook_subscriptions(_config(hook), {"OPS_HOOK_SECRET": secret})
    assert repr(hook["id"]) in str(excinfo.value)


# ── Delivery loop ───────────────────────────────────────────────────────


async def _until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_one_pass_skips_a_run_of_filtered_events(tmp_path: Path, public_dns) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    transport = FakeTransport()
    (subscription,) = webhook_subscriptions(
        _config(_hook(event_types=["steve.health_low"])), {"OPS_HOOK_SECRET": SECRET}
    )
    service = WebhookService(
        journal,
        str(tmp_path / "hooks.sqlite3"),
        subscriptions=[subscription],
        pinned_transport=transport,
    )
    try:
        for index in range(20):
            journal.append(_event(f"skip-{index}"))
        journal.append(_event("wanted", "steve.health_low"))
        assert await service.run_once() == 1
        assert [body["event_id"] for body in transport.bodies] == ["wanted"]
    finally:
        service.close()
        journal.close()


@pytest.mark.asyncio
async def test_run_delivers_events_appended_while_running(tmp_path: Path, public_dns) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    transport = FakeTransport()
    service = WebhookService(
        journal,
        str(tmp_path / "hooks.sqlite3"),
        subscriptions=webhook_subscriptions(_config(_hook()), {"OPS_HOOK_SECRET": SECRET}),
        pinned_transport=transport,
    )
    sender = asyncio.create_task(service.run(interval=0.01))
    try:
        await asyncio.sleep(0.05)
        for index in range(3):
            journal.append(_event(f"live-{index}"))
        await _until(lambda: len(transport.bodies) == 3)
        assert [body["event_id"] for body in transport.bodies] == ["live-0", "live-1", "live-2"]
        assert all(d.state == "delivered" for d in service.deliveries())
    finally:
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        service.close()
        journal.close()


@pytest.mark.asyncio
async def test_run_survives_a_failing_pass(
    tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    transport = FakeTransport()
    service = WebhookService(
        journal,
        str(tmp_path / "hooks.sqlite3"),
        subscriptions=webhook_subscriptions(_config(_hook()), {"OPS_HOOK_SECRET": SECRET}),
        pinned_transport=transport,
    )
    original = service.run_once
    failures = 0

    async def flaky() -> int:
        nonlocal failures
        if failures < 2:
            failures += 1
            raise sqlite3.OperationalError("database is locked")
        return await original()

    monkeypatch.setattr(service, "run_once", flaky)
    journal.append(_event("after-failure"))
    sender = asyncio.create_task(service.run(interval=0.01))
    try:
        await asyncio.wait_for(transport.sent.wait(), 2)
        assert failures == 2
        assert "Webhook delivery pass failed" in caplog.text
    finally:
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        service.close()
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
async def test_run_rejects_invalid_intervals(tmp_path: Path, interval: float) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    service = WebhookService(journal, str(tmp_path / "hooks.sqlite3"), subscriptions=[])
    try:
        with pytest.raises(WebhookError):
            await service.run(interval=interval)
    finally:
        service.close()
        journal.close()
