from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from minecraft_ai_bridge.bridge.chat_commands import ChatCommandHandler
from minecraft_ai_bridge.bridge.orchestrator import Orchestrator
from minecraft_ai_bridge.config import AppConfig, AuthzConfig
from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.minecraft.observer import WorldState
from minecraft_ai_bridge.runtime import AuthzService, PolicyDecision
from tests.conftest import MockMcpqClient


async def _wait_for_thread_event(event: Event, *, timeout: float = 1) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            raise TimeoutError("thread event was not set in time")
        await asyncio.sleep(0.005)


async def _run_in_dedicated_thread(function, *args):
    """Exercise blocking cleanup without involving the loop's default executor."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def run():
        try:
            value = function(*args)
        except BaseException as exc:
            loop.call_soon_threadsafe(result.set_exception, exc)
        else:
            loop.call_soon_threadsafe(result.set_result, value)

    Thread(target=run, daemon=True).start()
    return await result


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
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-world-admin-token")
    monkeypatch.setenv("AUTHZ_STEVE_ID", "EnvSteve")
    config = AppConfig.from_yaml(config_path)
    assert config.authz.controller_token.get_secret_value() == "mab-test-token"
    assert config.authz.world_admin_token.get_secret_value() == "mab-world-admin-token"
    assert config.authz.enabled

    config = AppConfig.from_yaml(tmp_path / "missing-config.yaml")
    assert config.authz.enabled
    assert config.authz.steve_id == "EnvSteve"

    config_path.write_text(
        "authz:\n  enabled: true\n  steve_id: AIBot\n  controller_token: must-not-be-in-yaml\n"
    )
    with pytest.raises(ValueError, match="environment variable"):
        AppConfig.from_yaml(config_path)

    config_path.write_text("authz:\n  world_admin_token: must-not-be-in-yaml\n")
    with pytest.raises(ValueError, match="environment variable"):
        AppConfig.from_yaml(config_path)

    monkeypatch.delenv("AUTHZ_ENABLED", raising=False)
    monkeypatch.delenv("AUTHZ_CONTROLLER_TOKEN", raising=False)
    monkeypatch.delenv("AUTHZ_WORLD_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("authz_enabled", "true")
    monkeypatch.setenv("authz_steve_id", "LowerCaseSteve")
    monkeypatch.setenv("authz_controller_token", "mab-lowercase-token")
    monkeypatch.setenv("authz_world_admin_token", "mab-lowercase-world-admin")
    config = AppConfig.from_yaml(tmp_path / "missing-config.yaml")
    assert config.authz.enabled
    assert config.authz.steve_id == "LowerCaseSteve"
    assert config.authz.controller_token.get_secret_value() == "mab-lowercase-token"


def test_enabled_authz_requires_world_admin_token(monkeypatch):
    monkeypatch.delenv("AUTHZ_WORLD_ADMIN_TOKEN", raising=False)
    config = AppConfig()
    config.authz.enabled = True
    config.authz.steve_id = "AIBot"
    config.authz.controller_token = SecretStr("mab-controller")

    with pytest.raises(ValueError, match="AUTHZ_WORLD_ADMIN_TOKEN"):
        config.authz.validate_controller_credentials()

    config.authz.world_admin_token = SecretStr("mab-world-admin")
    config.authz.validate_controller_credentials()


def test_authz_suspended_shutdown_must_be_finite_and_non_negative():
    with pytest.raises(ValueError, match="suspended shutdown"):
        AuthzConfig(suspended_shutdown_seconds=-1)
    with pytest.raises(ValueError, match="suspended shutdown"):
        AuthzConfig(suspended_shutdown_seconds=float("inf"))
    assert AuthzConfig(suspended_shutdown_seconds=0).suspended_shutdown_seconds == 0


def test_chat_allowlists_load_from_environment_as_json_arrays(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_CONTROL_ALLOWED_NAMES", '["Player"]')
    monkeypatch.setenv("CHAT_CONTROL_ALLOWED_UUIDS", '["00000000-0000-0000-0000-000000000001"]')

    config = AppConfig.from_yaml(tmp_path / "missing-config.yaml")

    assert config.chat_control.allowed_names == ["Player"]
    assert config.chat_control.allowed_uuids == ["00000000-0000-0000-0000-000000000001"]


@pytest.mark.asyncio
async def test_configured_orchestrator_gates_actions_and_releases_session(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")

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
    # This test covers config-driven gating and release, while heartbeat
    # renewal has separate timing/contention regressions below.
    monkeypatch.setattr(Orchestrator, "_start_session_renewal", lambda _self: None)
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def skip_safe_spawn(_self) -> None:
        return None

    monkeypatch.setattr(Orchestrator, "_prepare_safe_spawn", skip_safe_spawn)
    orchestrator = Orchestrator(config)
    await asyncio.wait_for(orchestrator._connect(), timeout=5)

    assert orchestrator._authz_session is not None
    session = orchestrator._authz_session
    allowed = await orchestrator._action_service.execute(ActionType.CHAT, {"message": "allowed"})
    assert allowed.success
    assert not orchestrator._authz_service._db.in_transaction
    assert orchestrator._authz_service._lock.acquire(blocking=False)
    orchestrator._authz_service._lock.release()

    cleanup_started = Event()
    cleanup_released = Event()
    cleanup_closed = Event()
    original_release = orchestrator._authz_service.release_session
    original_close = orchestrator._authz_service.close

    def tracked_release(active_session):
        cleanup_started.set()
        try:
            original_release(active_session)
        finally:
            cleanup_released.set()

    def tracked_close():
        try:
            original_close()
        finally:
            cleanup_closed.set()

    monkeypatch.setattr(orchestrator._authz_service, "release_session", tracked_release)
    monkeypatch.setattr(orchestrator._authz_service, "close", tracked_close)
    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    disconnect_task = asyncio.create_task(orchestrator._disconnect())
    await _wait_for_thread_event(cleanup_started)
    await _wait_for_thread_event(cleanup_released)
    await _wait_for_thread_event(cleanup_closed)
    await asyncio.wait_for(disconnect_task, timeout=5)
    assert cleanup_started.is_set()
    assert cleanup_released.is_set()
    assert cleanup_closed.is_set()
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
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")
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

    async def skip_safe_spawn(_self) -> None:
        return None

    monkeypatch.setattr(Orchestrator, "_prepare_safe_spawn", skip_safe_spawn)
    orchestrator = Orchestrator(config)
    await asyncio.wait_for(orchestrator._connect(), timeout=5)
    accepted = await asyncio.wait_for(
        orchestrator._action_service.execute(
            ActionType.CHAT,
            {"message": "before revocation"},
        ),
        timeout=5,
    )
    assert accepted.success
    chat_count = len(world.chat_messages_sent)

    orchestrator._authz_service.revoke_credential(token)
    rejected = await asyncio.wait_for(
        orchestrator._action_service.execute(
            ActionType.CHAT,
            {"message": "after revocation"},
        ),
        timeout=5,
    )
    assert not rejected.success
    assert len(world.chat_messages_sent) == chat_count
    assert not orchestrator._authz_service._db.in_transaction
    assert orchestrator._authz_service._lock.acquire(blocking=False)
    orchestrator._authz_service._lock.release()
    await asyncio.wait_for(orchestrator._stop_session_renewal(), timeout=1)
    release_finished = Event()
    close_finished = Event()
    authz_service = orchestrator._authz_service
    original_release = authz_service.release_session
    original_close = authz_service.close

    def tracked_release(active_session):
        try:
            original_release(active_session)
        finally:
            release_finished.set()

    def tracked_close():
        try:
            original_close()
        finally:
            close_finished.set()

    monkeypatch.setattr(authz_service, "release_session", tracked_release)
    monkeypatch.setattr(authz_service, "close", tracked_close)
    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    disconnect = asyncio.create_task(orchestrator._disconnect())
    await _wait_for_thread_event(release_finished)
    await _wait_for_thread_event(close_finished)
    await asyncio.wait_for(disconnect, timeout=5)


@pytest.mark.asyncio
async def test_authz_is_revalidated_after_awaited_caller_policy(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")
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

    async def skip_safe_spawn(_self) -> None:
        return None

    monkeypatch.setattr(Orchestrator, "_prepare_safe_spawn", skip_safe_spawn)
    orchestrator = Orchestrator(config, action_policy=revoke_then_allow)
    await asyncio.wait_for(orchestrator._connect(), timeout=5)
    messages_before = len(world.chat_messages_sent)
    result = await asyncio.wait_for(
        orchestrator._action_service.execute(
            ActionType.CHAT,
            {"message": "must not dispatch"},
        ),
        timeout=5,
    )
    assert not result.success
    assert len(world.chat_messages_sent) == messages_before
    release_finished = Event()
    close_finished = Event()
    authz_service = orchestrator._authz_service
    original_release = authz_service.release_session
    original_close = authz_service.close

    def tracked_release(active_session):
        try:
            original_release(active_session)
        finally:
            release_finished.set()

    def tracked_close():
        try:
            original_close()
        finally:
            close_finished.set()

    monkeypatch.setattr(authz_service, "release_session", tracked_release)
    monkeypatch.setattr(authz_service, "close", tracked_close)
    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    disconnect = asyncio.create_task(orchestrator._disconnect())
    await _wait_for_thread_event(release_finished)
    await _wait_for_thread_event(close_finished)
    await asyncio.wait_for(disconnect, timeout=5)


@pytest.mark.asyncio
async def test_expired_controller_pauses_strategy_but_keeps_safety_until_stop(
    tmp_path, monkeypatch
):
    database = tmp_path / "authz.sqlite3"
    with AuthzService(database) as auth:
        auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
        admin = auth.create_admin_credential("root")
        credential = auth.create_credential(
            "AIBot", "owner", {"steve:control"}, admin_token=admin.token
        )
        token = credential.token
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.bridge.cycle_delay = 0.001
    config.bridge.max_iterations = 100
    config.chat_control.server_online_mode = True
    config.chat_control.allowed_names = ["Operator"]

    class CountingLLM:
        def __init__(self):
            self.decisions = 0

        async def decide(self, **_kwargs):
            self.decisions += 1
            return None

        async def decompose_goal(self, _goal):
            return []

    orchestrator = Orchestrator(config)
    llm = CountingLLM()
    orchestrator._llm = llm
    orchestrator._goals._llm = llm
    world = MockMcpqClient()
    orchestrator._world = world
    orchestrator._max_iterations = 100

    async def observe_safely() -> WorldState:
        return WorldState()

    orchestrator._observe = observe_safely
    orchestrator._observe_for_safety = observe_safely

    async def set_goal(_goal):
        await asyncio.sleep(0.01)
        orchestrator._goals.set_goal_from_subgoals("test goal", [{"description": "test task"}])

    orchestrator._goals.set_goal = set_goal
    safety_checks = 0
    three_safety_checks = asyncio.Event()

    async def connect_with_expired_session():
        auth = AuthzService(database)
        session_expiry = datetime.now(UTC) + timedelta(seconds=30)
        session = auth.acquire_session(
            token,
            "AIBot",
            owner_id="owner",
            lease_expires_at=session_expiry,
        )
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        session = replace(session, expires_at=expired_at)
        auth._db.execute(
            "UPDATE steves SET session_expires=? WHERE id=?",
            (expired_at.isoformat(), "AIBot"),
        )
        orchestrator._authz_service = auth
        orchestrator._authz_session = session

        async def count_safety_checks(_world: WorldState):
            nonlocal safety_checks
            safety_checks += 1
            if safety_checks >= 3:
                three_safety_checks.set()
            return None

        orchestrator._preservation = type(
            "FakePreservation", (), {"evaluate": staticmethod(count_safety_checks)}
        )()

    orchestrator._connect = connect_with_expired_session

    stop_event_sent = False

    async def poll_for_authorized_stop():
        nonlocal stop_event_sent
        if three_safety_checks.is_set() and not stop_event_sent:
            stop_event_sent = True
            return [
                SimpleNamespace(
                    player=SimpleNamespace(name="Operator"),
                    message="!stop",
                )
            ]
        return []

    world.get_chat_events = poll_for_authorized_stop
    orchestrator._cmd_handler = ChatCommandHandler(orchestrator)

    async def close_expired_test_service():
        if orchestrator._authz_service is not None:
            orchestrator._authz_service.close()
            orchestrator._authz_service = None

    # This regression targets safety continuation and real chat authorization;
    # separate cleanup tests exercise the production threaded release/close path.
    orchestrator._disconnect = close_expired_test_service

    await asyncio.wait_for(orchestrator.run("test goal"), timeout=3)

    assert orchestrator._strategic_authority_lost
    assert safety_checks >= 3
    assert stop_event_sent
    assert orchestrator._stop_requested
    assert llm.decisions == 0
    assert orchestrator._consecutive_failures == 0
    assert orchestrator._safety_task is None


@pytest.mark.asyncio
async def test_orchestrator_renews_only_while_controller_authority_remains_valid(
    tmp_path, monkeypatch
):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    auth = AuthzService(database)
    session = auth.acquire_session(
        token,
        "AIBot",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1.5),
    )
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.authz.session_lease_seconds = 1.5
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(config)
    orchestrator._authz_service = auth
    orchestrator._authz_session = session

    orchestrator._start_session_renewal()

    async def wait_until_renewed():
        while orchestrator._authz_session == session:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_renewed(), timeout=2)
        renewed = orchestrator._authz_session
        assert renewed is not None
        assert renewed.session_id == session.session_id
        assert renewed.generation == session.generation
        assert renewed.expires_at is not None and renewed.expires_at > session.expires_at

        with AuthzService(database) as revoke_auth:
            revoke_auth.revoke_credential(token)
        await asyncio.wait_for(orchestrator._strategic_authority_lost_event.wait(), timeout=2)
        assert orchestrator._strategic_authority_lost
    finally:
        await orchestrator._stop_session_renewal()
        auth.close()


@pytest.mark.asyncio
async def test_renewal_contention_signals_authority_loss_at_session_deadline(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    auth = AuthzService(database)
    session = auth.acquire_session(
        token,
        "AIBot",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.9),
    )
    writer = sqlite3.connect(database, isolation_level=None, timeout=30)
    writer.execute("BEGIN IMMEDIATE")
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.authz.session_lease_seconds = 0.9
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(config)
    orchestrator._authz_service = auth
    orchestrator._authz_session = session
    orchestrator._start_session_renewal()

    try:
        await asyncio.wait_for(orchestrator._strategic_authority_lost_event.wait(), timeout=2)
        assert orchestrator._strategic_authority_lost
        assert orchestrator._authz_session == session
    finally:
        await orchestrator._stop_session_renewal()
        writer.execute("ROLLBACK")
        writer.close()
        auth.close()


@pytest.mark.asyncio
async def test_ownership_capped_heartbeat_stays_active_until_session_deadline(
    tmp_path, monkeypatch
):
    database = tmp_path / "authz.sqlite3"
    auth = AuthzService(database)
    auth.register_steve("seed", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    ownership_expiry = datetime.now(UTC) + timedelta(seconds=2)
    auth.register_steve(
        "AIBot",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=ownership_expiry,
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "AIBot", "owner", {"steve:control"}, admin_token=admin.token
    )
    session = auth.acquire_session(
        credential.token,
        "AIBot",
        owner_id="owner",
        lease_expires_at=ownership_expiry,
    )
    config = AppConfig()
    config.authz.enabled = True
    config.authz.session_lease_seconds = 0.3
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(config)
    orchestrator._authz_service = auth
    orchestrator._authz_session = session
    orchestrator._start_session_renewal()

    try:
        await asyncio.sleep(0.4)
        assert not orchestrator._strategic_authority_lost
        assert orchestrator._session_renewal_task is not None
        assert not orchestrator._session_renewal_task.done()
        assert auth.validate_session(session)
    finally:
        await orchestrator._stop_session_renewal()
        auth.close()


@pytest.mark.asyncio
async def test_authz_lock_contention_suspends_without_blocking_event_loop(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    auth = AuthzService(database)
    session = auth.acquire_session(
        token,
        "AIBot",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    config = AppConfig()
    config.authz.enabled = True
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(config)
    orchestrator._authz_service = auth
    orchestrator._authz_session = session
    entered = Event()
    release = Event()

    def hold_service_lock():
        try:
            with auth._lock:
                entered.set()
                release.wait(timeout=2)
        finally:
            holder_done.set()

    holder_done = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_service_lock)
        await _wait_for_thread_event(entered)
        started = monotonic()
        try:
            assert orchestrator._suspend_strategy_if_session_invalid()
            assert monotonic() - started < 0.25
            # Contention defers strategy without permanently revoking it.
            assert not orchestrator._strategic_authority_lost
        finally:
            release.set()
            await _wait_for_thread_event(holder_done)
            holder.result()
            auth.close()


@pytest.mark.asyncio
async def test_disconnect_performs_authz_cleanup_off_event_loop(tmp_path, monkeypatch):
    config = AppConfig()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(config)
    release_started = Event()
    release = Event()
    closed_event = Event()

    class BlockingAuthz:
        closed = False

        def release_session(self, _session):
            release_started.set()
            release.wait(timeout=2)

        def close(self):
            self.closed = True
            closed_event.set()

    auth = BlockingAuthz()
    orchestrator._authz_service = auth
    orchestrator._authz_session = object()

    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    cleanup = asyncio.create_task(orchestrator._disconnect())
    await _wait_for_thread_event(release_started)
    started = monotonic()
    try:
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.25)
        assert monotonic() - started < 0.25
        assert not cleanup.done()
    finally:
        release.set()
    await _wait_for_thread_event(closed_event)
    await asyncio.wait_for(cleanup, timeout=2)

    assert orchestrator._authz_service is None
    assert orchestrator._authz_session is None
    assert auth.closed


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_authz_cleanup_worker(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(AppConfig())
    release_started = Event()
    release = Event()
    closed_event = Event()

    class BlockingAuthz:
        def release_session(self, _session):
            release_started.set()
            release.wait(timeout=2)

        def close(self):
            closed_event.set()

    auth = BlockingAuthz()
    orchestrator._authz_service = auth
    orchestrator._authz_session = object()

    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    cleanup = asyncio.create_task(orchestrator._disconnect())
    await _wait_for_thread_event(release_started)

    cleanup.cancel()
    await asyncio.sleep(0.02)
    assert not cleanup.done()
    cleanup.cancel()
    await asyncio.sleep(0.02)
    assert not cleanup.done()
    assert orchestrator._authz_service is auth
    assert orchestrator._authz_session is not None

    release.set()
    await _wait_for_thread_event(closed_event)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cleanup, timeout=2)

    assert orchestrator._authz_service is None
    assert orchestrator._authz_session is None


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_renewal_stop_still_closes_authz(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    orchestrator = Orchestrator(AppConfig())
    renewal_stop_started = asyncio.Event()
    allow_renewal_stop = asyncio.Event()
    safety_stopped = asyncio.Event()
    authz_closed = Event()

    class BlockingAuthz:
        def release_session(self, _session):
            return None

        def close(self):
            authz_closed.set()

    orchestrator._authz_service = BlockingAuthz()
    orchestrator._authz_session = object()

    async def blocked_renewal_stop():
        renewal_stop_started.set()
        await allow_renewal_stop.wait()

    async def stop_safety():
        safety_stopped.set()

    monkeypatch.setattr(orchestrator, "_stop_session_renewal", blocked_renewal_stop)
    monkeypatch.setattr(orchestrator, "_stop_safety_supervisor", stop_safety)
    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)

    shutdown = asyncio.create_task(orchestrator._finish_shutdown())
    await asyncio.wait_for(renewal_stop_started.wait(), timeout=1)
    shutdown.cancel()
    await asyncio.sleep(0.02)
    shutdown.cancel()
    await asyncio.sleep(0.02)
    assert not shutdown.done()
    assert orchestrator._authz_service is not None

    allow_renewal_stop.set()
    await asyncio.wait_for(safety_stopped.wait(), timeout=1)
    await _wait_for_thread_event(authz_closed)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown, timeout=2)
    assert orchestrator._authz_service is None
    assert orchestrator._authz_session is None


@pytest.mark.asyncio
async def test_session_loss_cancels_blocked_initial_goal_decomposition(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.enabled = True
    orchestrator = Orchestrator(config)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_goal(_goal):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    orchestrator._goals.set_goal = blocked_goal
    task = asyncio.create_task(orchestrator._set_goal_or_authority_loss("goal"))
    await asyncio.wait_for(started.wait(), timeout=1)
    orchestrator._mark_strategic_authority_lost("test revocation")

    await asyncio.wait_for(task, timeout=1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_shutdown_cancels_blocked_initial_goal_decomposition(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.enabled = True
    orchestrator = Orchestrator(config)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_goal(_goal):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    orchestrator._goals.set_goal = blocked_goal
    task = asyncio.create_task(orchestrator._set_goal_or_authority_loss("goal"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_authz_release_error_does_not_skip_other_cleanup(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database))
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")
    config = AppConfig()
    config.authz.enabled = True
    config.authz.database_path = str(database)
    config.authz.steve_id = "AIBot"
    config.bridge.event_journal_path = str(tmp_path / "events.sqlite3")
    monkeypatch.setattr(
        "minecraft_ai_bridge.bridge.orchestrator.McpqClient",
        lambda **_kwargs: world,
    )

    async def skip_safe_spawn(_self) -> None:
        return None

    monkeypatch.setattr(Orchestrator, "_prepare_safe_spawn", skip_safe_spawn)
    orchestrator = Orchestrator(config)
    await asyncio.wait_for(orchestrator._connect(), timeout=5)

    def failed_release(_session):
        raise sqlite3.OperationalError("simulated release failure")

    monkeypatch.setattr(orchestrator._authz_service, "release_session", failed_release)
    monkeypatch.setattr(asyncio, "to_thread", _run_in_dedicated_thread)
    with pytest.raises(sqlite3.OperationalError, match="simulated release failure"):
        await asyncio.wait_for(orchestrator._disconnect(), timeout=5)
    assert not world.connected
    assert orchestrator._event_journal is None


@pytest.mark.asyncio
async def test_authz_gate_requires_control_scope_before_startup_mutations(tmp_path, monkeypatch):
    database = tmp_path / "authz.sqlite3"
    token = _create_controller(str(database), scopes={"steve:observe:self"})
    world = MockMcpqClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTHZ_CONTROLLER_TOKEN", token)
    # Required by enabled Authz; it holds no admin:world grant in this database.
    monkeypatch.setenv("AUTHZ_WORLD_ADMIN_TOKEN", "mab-unregistered-world-admin")
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

    real_sleep = asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("minecraft_ai_bridge.bridge.orchestrator.asyncio.sleep", no_sleep)
    orchestrator = Orchestrator(config)
    with pytest.raises(PermissionError, match="control scope"):
        await asyncio.wait_for(orchestrator._connect(), timeout=5)
    assert world.commands_ran == []
    # _connect fails before the journal and runtime services are initialized;
    # keep this assertion focused on the denied startup mutation path.
    if orchestrator._authz_service is not None:
        orchestrator._authz_service.close()
    if orchestrator._mc is not None:
        await orchestrator._mc.disconnect()


def _authz_orchestrator_with_validation(monkeypatch, valid, *, expires_in: float):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.enabled = True
    orchestrator = Orchestrator(config)
    orchestrator._authz_service = SimpleNamespace(try_validate_session=lambda _session: valid)
    orchestrator._authz_session = SimpleNamespace(
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in)
    )
    return orchestrator


@pytest.mark.parametrize(
    ("valid", "expires_in", "latched"),
    [(None, 30, False), (None, -1, True), (False, 30, True), (True, 30, False)],
)
def test_unverifiable_session_defers_until_lease_expiry(monkeypatch, valid, expires_in, latched):
    orchestrator = _authz_orchestrator_with_validation(monkeypatch, valid, expires_in=expires_in)

    assert orchestrator._suspend_strategy_if_session_invalid() is (valid is not True)
    assert orchestrator._strategic_authority_lost is latched
    assert orchestrator._strategic_authority_lost_event.is_set() is latched


@pytest.mark.asyncio
async def test_renewal_permission_error_latches_authority_loss(monkeypatch):
    orchestrator = _authz_orchestrator_with_validation(monkeypatch, None, expires_in=30)
    orchestrator._config.authz.session_lease_seconds = 0.03

    def deny_renewal(_session, *, lease_seconds):
        del lease_seconds
        raise PermissionError("controller session is no longer valid")

    orchestrator._authz_service.renew_session = deny_renewal
    orchestrator._start_session_renewal()
    try:
        await asyncio.wait_for(orchestrator._session_renewal_task, timeout=1)
    finally:
        await orchestrator._stop_session_renewal()

    assert orchestrator._strategic_authority_lost


@pytest.mark.asyncio
async def test_authority_loss_wait_ends_after_grace_without_chat_stop(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.enabled = True
    config.authz.suspended_shutdown_seconds = 0.05
    config.bridge.cycle_delay = 0.01
    orchestrator = Orchestrator(config)
    polls = 0

    async def failing_poll(*, allowed_commands):
        nonlocal polls
        del allowed_commands
        polls += 1
        raise RuntimeError("chat unavailable")

    orchestrator._cmd_handler = SimpleNamespace(poll=failing_poll)

    assert not orchestrator._chat_stop_available()
    await asyncio.wait_for(orchestrator._wait_for_authorized_stop(), timeout=1)
    assert polls >= 2
    assert not orchestrator._stop_requested


@pytest.mark.asyncio
async def test_authority_loss_wait_ends_early_on_authorized_stop(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = AppConfig()
    config.authz.suspended_shutdown_seconds = 60
    config.bridge.cycle_delay = 0.01
    orchestrator = Orchestrator(config)

    async def stop_poll(*, allowed_commands):
        assert allowed_commands == frozenset({"!stop"})
        orchestrator._stop_requested = True

    orchestrator._cmd_handler = SimpleNamespace(poll=stop_poll)

    await asyncio.wait_for(orchestrator._wait_for_authorized_stop(), timeout=1)
    assert orchestrator._stop_requested
