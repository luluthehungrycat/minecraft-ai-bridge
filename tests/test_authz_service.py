import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import patch

import pytest

from minecraft_ai_bridge.minecraft.actions import ActionType
from minecraft_ai_bridge.runtime import ActionService, AuthzService
from tests.conftest import MockMcpqClient


def setup_auth(tmp_path):
    auth = AuthzService(tmp_path / "auth.sqlite")
    auth.register_steve("s1", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")
    created = auth.create_credential(
        "s1", "owner", {"steve:observe:self", "steve:control"}, admin_token=admin.token
    )
    return auth, created, admin


def test_credentials_are_hashed_and_scoped(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    assert auth.authenticate(cred.token).credential_id == cred.credential_id
    assert auth.check_scope(cred.token, "steve:control", steve_id="s1")
    assert not auth.check_scope(cred.token, "admin:steves", steve_id="s1")
    auth.close()
    assert cred.token.encode() not in (tmp_path / "auth.sqlite").read_bytes()


def test_fencing_exclusive_sessions_and_action_policy(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    session = auth.acquire_session(cred.token, "s1", owner_id="owner")
    with pytest.raises(PermissionError):
        auth.acquire_session(cred.token, "s1", owner_id="owner")
    policy = auth.action_policy(session)
    assert policy
    auth.release_session(session)
    assert not auth.validate_session(session)
    next_session = auth.acquire_session(cred.token, "s1", owner_id="owner")
    assert next_session.generation > session.generation
    auth.close()


def test_controller_session_renewal_extends_current_session_without_refencing(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    first_expiry = datetime.now(UTC) + timedelta(seconds=30)
    session = auth.acquire_session(
        cred.token, "s1", owner_id="owner", lease_expires_at=first_expiry
    )
    from minecraft_ai_bridge.runtime.action_service import ActionRequest

    active_session = session
    policy = auth.action_policy(lambda: active_session)
    assert policy(ActionRequest(ActionType.WAIT)).allowed
    renewed = auth.renew_session(session, lease_seconds=90)
    active_session = renewed

    assert renewed.session_id == session.session_id
    assert renewed.generation == session.generation
    assert renewed.expires_at is not None and renewed.expires_at > first_expiry
    assert renewed.expires_at <= datetime.now(UTC) + timedelta(seconds=90)
    assert not auth.validate_session(session)
    assert auth.validate_session(renewed)
    assert policy(ActionRequest(ActionType.WAIT)).allowed
    auth.close()


def test_controller_session_renewal_rejects_expired_or_revoked_authority(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    expiry = datetime.now(UTC) + timedelta(seconds=30)
    session = auth.acquire_session(cred.token, "s1", owner_id="owner", lease_expires_at=expiry)
    auth._db.execute(
        "UPDATE steves SET session_expires=? WHERE id='s1'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )

    with pytest.raises(PermissionError, match="session is no longer valid"):
        auth.renew_session(session, lease_seconds=90)
    auth.close()

    auth, cred, _ = setup_auth(tmp_path / "revoked")
    session = auth.acquire_session(
        cred.token,
        "s1",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    auth.revoke_credential(cred.token)
    with pytest.raises(PermissionError, match="session is no longer valid"):
        auth.renew_session(session, lease_seconds=90)
    auth.close()


def test_controller_session_renewal_is_capped_by_leased_ownership(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    ownership_expiry = datetime.now(UTC) + timedelta(seconds=45)
    auth.register_steve(
        "s2",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=ownership_expiry,
        admin_token=admin.token,
    )
    leased_credential = auth.create_credential(
        "s2", "owner", {"steve:control"}, admin_token=admin.token
    )
    session = auth.acquire_session(
        leased_credential.token,
        "s2",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )

    renewed = auth.renew_session(session, lease_seconds=90)

    assert renewed.expires_at is not None
    assert renewed.expires_at <= ownership_expiry
    assert auth.validate_session(renewed)
    auth.close()


def test_controller_session_renewal_keeps_valid_session_at_ownership_cap(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    ownership_expiry = datetime.now(UTC) + timedelta(seconds=45)
    auth.register_steve(
        "s2",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=ownership_expiry,
        admin_token=admin.token,
    )
    credential = auth.create_credential("s2", "owner", {"steve:control"}, admin_token=admin.token)
    session = auth.acquire_session(
        credential.token,
        "s2",
        owner_id="owner",
        lease_expires_at=ownership_expiry,
    )

    renewed = auth.renew_session(session, lease_seconds=90)

    assert renewed == session
    assert auth.validate_session(renewed)
    auth.close()


def test_controller_session_renewal_fails_fast_on_external_sqlite_writer(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    session = auth.acquire_session(
        cred.token,
        "s1",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    writer = sqlite3.connect(tmp_path / "auth.sqlite", isolation_level=None, timeout=30)
    writer.execute("BEGIN IMMEDIATE")
    started = monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            auth.renew_session(session, lease_seconds=90)
        assert monotonic() - started < 0.5
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        auth.close()


def test_controller_session_renewal_fails_fast_on_service_lock_contention(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    session = auth.acquire_session(
        cred.token,
        "s1",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    entered = Event()
    release = Event()

    def hold_service_lock():
        with auth._lock:
            entered.set()
            release.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_service_lock)
        assert entered.wait(timeout=1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="service is busy"):
                auth.renew_session(session, lease_seconds=90)
        finally:
            release.set()
            holder.result(timeout=1)
    auth.close()


def test_try_validate_session_returns_busy_without_waiting(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    session = auth.acquire_session(
        cred.token,
        "s1",
        owner_id="owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    entered = Event()
    release = Event()

    def hold_service_lock():
        with auth._lock:
            entered.set()
            release.wait(timeout=2)

    assert auth.try_validate_session(session) is True
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_service_lock)
        assert entered.wait(timeout=1)
        try:
            assert auth.try_validate_session(session) is None
        finally:
            release.set()
            holder.result(timeout=1)
    auth.close()


def test_lease_expiry_and_admin_transfer(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    exp = datetime.now(UTC) + timedelta(seconds=30)
    leased = auth.register_steve(
        "s2",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=exp,
        admin_token=admin.token,
    )
    assert leased.steve_id == "s2"
    s = auth.acquire_session(cred.token, "s1", owner_id="owner")
    auth.transfer_ownership(admin.token, "s1", new_owner_id="other")
    assert not auth.validate_session(s)
    auth.close()


def test_unclaimed_first_claim_is_atomic(tmp_path):
    auth = AuthzService(tmp_path / "claim.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="root")
    admin = auth.create_admin_credential("root")
    auth.register_steve("free", admin_token=admin.token)
    first = auth.create_credential("free", "alice", {"steve:control"}, admin_token=admin.token)
    second = auth.create_credential("free", "bob", {"steve:control"}, admin_token=admin.token)

    def claim(token):
        try:
            auth.claim_ownership(token, "free")
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first.token, second.token)))
    assert sum(results) == 1
    winner = first if results[0] else second
    loser = second if results[0] else first
    with pytest.raises(PermissionError):
        auth.claim_ownership(loser.token, "free")
    session = auth.acquire_session(winner.token, "free", owner_id=winner.owner_id)
    assert auth.validate_session(session)
    auth.close()


def test_session_validation_binds_persisted_fields_and_lease(tmp_path):
    ownership_expiry = datetime.now(UTC) + timedelta(minutes=5)
    auth, _, admin = setup_auth(tmp_path)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=ownership_expiry,
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "leased", "owner", {"steve:control"}, admin_token=admin.token
    )
    with pytest.raises(ValueError):
        auth.acquire_session(credential.token, "leased", owner_id="owner")
    with pytest.raises(ValueError):
        auth.acquire_session(
            credential.token,
            "leased",
            owner_id="owner",
            lease_expires_at=ownership_expiry + timedelta(seconds=1),
        )
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    session = auth.acquire_session(
        credential.token, "leased", owner_id="owner", lease_expires_at=expiry
    )
    assert auth.validate_session(session)
    assert not auth.validate_session(replace(session, credential_id="forged"))
    assert not auth.validate_session(replace(session, owner_id="forged"))
    assert not auth.validate_session(replace(session, expires_at=expiry + timedelta(seconds=1)))
    auth._db.execute(
        "UPDATE steves SET session_expires=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "leased"),
    )
    assert not auth.validate_session(session)
    auth.close()


@pytest.mark.parametrize("invalid_identity", ["", " \t"])
def test_authz_rejects_blank_owner_and_admin_identities(tmp_path, invalid_identity):
    auth, credential, admin = setup_auth(tmp_path)
    with pytest.raises(ValueError, match="must be non-empty"):
        auth.create_credential("s1", invalid_identity, {"steve:control"}, admin_token=admin.token)
    with pytest.raises(ValueError, match="must be non-empty"):
        auth.create_admin_credential(invalid_identity)
    with pytest.raises(ValueError, match="must be non-empty"):
        auth.grant_admin_credential(admin.token, invalid_identity, {"admin:steves"})
    with pytest.raises(ValueError, match="must be non-empty"):
        auth.transfer_ownership(admin.token, "s1", new_owner_id=invalid_identity)
    auth.close()


def test_peer_audit_migration_does_not_duplicate_rotated_grant(tmp_path):
    database = tmp_path / "audit-migration-after-rotation.sqlite"
    auth = AuthzService(database)
    auth.register_steve("s1", ownership_mode="persistent", owner_id="owner")
    auth.register_steve("s2", ownership_mode="persistent", owner_id="peer")
    admin = auth.create_admin_credential("root")
    credential = auth.create_credential(
        "s1",
        "owner",
        {"steve:observe:shared"},
        admin_token=admin.token,
    )
    auth.grant_peer_observation(admin.token, credential.credential_id, "s2")
    replacement = auth.rotate_credential(credential.token)
    auth._db.execute("DELETE FROM schema_migrations WHERE version=1")
    auth.close()

    reopened = AuthzService(database)
    entries = reopened.list_peer_observation_audit(admin.token, "s1")
    assert [(entry.event, entry.peer_steve_id) for entry in entries] == [("granted", "s2")]
    assert reopened.list_peer_observation_grants(admin.token, replacement.credential_id) == ("s2",)
    reopened.close()


def test_admin_bootstrap_and_credential_revoke_authority(tmp_path):
    auth, owner_credential, admin = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.create_credential("s1", "owner", {"admin:steves"})
    with pytest.raises(PermissionError):
        auth.create_admin_credential("intruder")
    with pytest.raises(ValueError):
        auth.create_credential("s1", "delegate", {"admin:world"}, admin_token=admin.token)
    extra = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"})
    with pytest.raises(PermissionError):
        auth.revoke_credential(extra.token, authority_token=owner_credential.token)
    auth.revoke_credential(extra.token, authority_token=admin.token)
    assert not auth.check_scope(extra.token, "admin:world", steve_id="s1")
    auth.close()


def test_admin_identity_is_global_and_admin_world_stays_distinct(tmp_path):
    auth = AuthzService(tmp_path / "global-admin.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve(
        "remote", ownership_mode="persistent", owner_id="remote-owner", admin_token=admin.token
    )
    remote = auth.create_credential(
        "remote", "remote-owner", {"steve:control"}, admin_token=admin.token
    )
    auth.transfer_ownership(admin.token, "remote", new_owner_id="new-owner")
    with pytest.raises(PermissionError):
        auth.acquire_session(remote.token, "remote", owner_id="remote-owner")
    world_admin = auth.grant_admin_credential(admin.token, "world-operator", {"admin:world"})
    with pytest.raises(PermissionError):
        auth.transfer_ownership(world_admin.token, "remote", new_owner_id="wrong")
    auth.revoke_credential(remote.token, authority_token=admin.token)
    assert not auth.check_scope(remote.token, "steve:control", steve_id="remote")
    auth.close()


def test_non_admin_cannot_transfer_and_rotation_fences_old_session(tmp_path):
    auth, credential, _ = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.transfer_ownership(credential.token, "s1", new_owner_id="attacker")
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    rotated = auth.rotate_credential(credential.token)
    assert not auth.validate_session(session)
    replacement_session = auth.acquire_session(rotated.token, "s1", owner_id="owner")
    assert replacement_session.generation > session.generation
    auth.close()


def test_scope_binding_rotation_revocation_and_expiry(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    assert not auth.check_scope(cred.token, "steve:control", steve_id="someone-else")
    assert not auth.check_scope(cred.token, "", steve_id="s1")
    rotated = auth.rotate_credential(cred.token)
    with pytest.raises(PermissionError):
        auth.authenticate(cred.token)
    assert auth.authenticate(rotated.token)
    expiring = auth.create_credential(
        "s1",
        "owner",
        {"steve:observe:self"},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        admin_token=admin.token,
    )
    with pytest.raises(PermissionError):
        auth.authenticate(expiring.token)
    auth.revoke_credential(rotated.token)
    assert not auth.check_scope(rotated.token, "steve:control", steve_id="s1")
    auth.close()


@pytest.mark.asyncio
async def test_action_policy_rechecks_revocation(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    session = auth.acquire_session(cred.token, "s1", owner_id="owner")
    policy = auth.action_policy(session)
    from minecraft_ai_bridge.minecraft.actions import ActionType
    from minecraft_ai_bridge.runtime import ActionService
    from minecraft_ai_bridge.runtime.action_service import ActionRequest
    from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
    from tests.conftest import MockMcpqClient

    assert policy(ActionRequest(ActionType.WAIT)).allowed
    service = ActionService(McpqWorldAdapter(MockMcpqClient()), policy=policy)
    assert (await service.execute(ActionType.WAIT, {"seconds": 0.0})).success
    auth.revoke_credential(cred.token)
    assert not policy(ActionRequest(ActionType.WAIT)).allowed
    assert not (await service.execute(ActionType.WAIT, {"seconds": 0.0})).success
    auth.close()


def test_check_scope_fails_closed_for_former_owner_after_transfer(tmp_path):
    """Scope checks must follow the persisted owner, not just the credential.

    After an admin transfers "s1", the former owner's credential still parses,
    but every Steve-bound scope must be false while the new owner's scopes are
    true. This is checked directly on check_scope, with no acquire_session call
    involved, because check_scope is the observation-scope gate.
    """
    auth, former, admin = setup_auth(tmp_path)
    auth.transfer_ownership(admin.token, "s1", new_owner_id="new-owner")
    current = auth.create_credential(
        "s1", "new-owner", {"steve:observe:self", "steve:control"}, admin_token=admin.token
    )
    # The former owner still authenticates, but the persisted owner no longer
    # matches, so both its observe and control scopes fail closed.
    assert auth.authenticate(former.token).owner_id == "owner"
    assert not auth.check_scope(former.token, "steve:observe:self", steve_id="s1")
    assert not auth.check_scope(former.token, "steve:control", steve_id="s1")
    # The current owner's scopes are true.
    assert auth.check_scope(current.token, "steve:observe:self", steve_id="s1")
    assert auth.check_scope(current.token, "steve:control", steve_id="s1")
    auth.close()


def test_check_scope_fails_closed_when_ownership_expires(tmp_path):
    """An expired persisted lease invalidates Steve-bound scopes fail-closed."""
    auth, cred, admin = setup_auth(tmp_path)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        admin_token=admin.token,
    )
    leased = auth.create_credential(
        "leased", "owner", {"steve:observe:self", "steve:control"}, admin_token=admin.token
    )
    assert auth.check_scope(leased.token, "steve:control", steve_id="leased")
    auth._db.execute(
        "UPDATE steves SET expires=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "leased"),
    )
    assert not auth.check_scope(leased.token, "steve:observe:self", steve_id="leased")
    assert not auth.check_scope(leased.token, "steve:control", steve_id="leased")
    auth.close()


def test_admin_scopes_still_require_a_global_admin_identity(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    assert auth.check_scope(admin.token, "admin:steves", steve_id="s1")
    assert not auth.check_scope(cred.token, "admin:steves", steve_id="s1")
    auth.close()


def test_transaction_releases_lock_when_begin_fails(tmp_path):
    """A failed BEGIN IMMEDIATE must not leak the write lock.

    Provoke the failure by patching the BEGIN statement itself rather than
    waiting on a real database lock, then confirm later calls still succeed.
    """
    auth, cred, admin = setup_auth(tmp_path)
    with (
        patch.object(
            AuthzService,
            "_begin_write",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
        pytest.raises(sqlite3.OperationalError),
    ):
        auth.create_credential("s1", "owner", {"steve:control"}, admin_token=admin.token)
    # The write lock must be free again.
    assert auth._lock.acquire(blocking=False)
    auth._lock.release()
    auth.close()


def test_transaction_preserves_body_error_when_rollback_already_happened(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    connection = auth._db

    class AutoRollbackConnection:
        @property
        def in_transaction(self):
            return connection.in_transaction

        def execute(self, statement, *args):
            if statement == "ROLLBACK":
                connection.execute(statement, *args)
                raise sqlite3.OperationalError("transaction already rolled back")
            return connection.execute(statement, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    auth._db = AutoRollbackConnection()
    with pytest.raises(ValueError, match="operation failed"), auth._tx():
        raise ValueError("operation failed")
    auth._db = connection
    assert auth.authenticate(cred.token).credential_id == cred.credential_id
    auth.close()


def test_transaction_lock_free_after_begin_failure_allows_later_calls(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    real_begin = AuthzService._begin_write
    calls = {"n": 0}

    def failing_once(self):
        if calls["n"] == 0:
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real_begin(self)

    with (
        patch.object(AuthzService, "_begin_write", failing_once),
        pytest.raises(sqlite3.OperationalError),
    ):
        auth.create_credential("s1", "owner", {"steve:control"}, admin_token=admin.token)
    # Later calls must still work, proving the RLock was released and rollback
    # left the connection usable.
    later = auth.create_credential("s1", "owner", {"steve:observe:self"}, admin_token=admin.token)
    assert auth.check_scope(later.token, "steve:observe:self", steve_id="s1")
    auth.close()


def test_commit_failure_rolls_back_and_releases_transaction_lock(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    real_commit = AuthzService._commit
    calls = {"n": 0}

    def failing_once(self):
        if calls["n"] == 0:
            calls["n"] += 1
            raise sqlite3.OperationalError("commit failed")
        real_commit(self)

    with (
        patch.object(AuthzService, "_commit", failing_once),
        pytest.raises(sqlite3.OperationalError, match="commit failed"),
    ):
        auth.create_credential("s1", "owner", {"steve:observe:self"}, admin_token=admin.token)
    # The failed transaction was rolled back and the connection remains usable.
    later = auth.create_credential("s1", "owner", {"steve:control"}, admin_token=admin.token)
    assert auth.check_scope(later.token, "steve:control", steve_id="s1")
    auth.close()


def test_admin_credential_rotates_and_old_token_stops_working(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    rotated = auth.rotate_credential(admin.token)
    with pytest.raises(PermissionError):
        auth.authenticate(admin.token)
    assert auth.authenticate(rotated.token).owner_id == "root"
    assert rotated.scopes == frozenset({"admin:steves"})
    assert auth.check_scope(rotated.token, "admin:steves", steve_id="s1")
    auth.close()


def test_admin_rotation_preserves_scopes_and_expiry(tmp_path):
    auth = AuthzService(tmp_path / "admin-rotate.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    expiry = datetime.now(UTC) + timedelta(hours=1)
    scoped = auth.grant_admin_credential(
        admin.token, "world-op", {"admin:world"}, expires_at=expiry
    )
    rotated = auth.rotate_credential(scoped.token)
    assert rotated.owner_id == "world-op"
    assert rotated.scopes == frozenset({"admin:world"})
    assert rotated.expires_at == expiry
    with pytest.raises(PermissionError):
        auth.authenticate(scoped.token)
    assert auth.authenticate(rotated.token)
    auth.close()


def test_admin_rotation_and_confirmed_replacement_self_revoke(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    # A sole admin cannot self-revoke; it must first rotate to a replacement
    # (or grant a peer) and confirm the replacement works.
    with pytest.raises(PermissionError):
        auth.revoke_credential(admin.token)
    assert auth.authenticate(admin.token)
    replacement = auth.rotate_credential(admin.token)
    assert auth.authenticate(replacement.token).owner_id == "root"
    assert "admin:steves" in replacement.scopes
    with pytest.raises(PermissionError):
        auth.revoke_credential(replacement.token)
    second = auth.grant_admin_credential(replacement.token, "second-root", {"admin:steves"})
    auth.revoke_credential(replacement.token)
    with pytest.raises(PermissionError):
        auth.authenticate(replacement.token)
    assert auth.authenticate(second.token)
    auth.close()


def test_ordinary_credential_cannot_rotate_or_revoke_admin(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    # Rotation is self-only: presenting an ordinary Steve credential replaces
    # that credential and never creates or alters an admin row.
    auth.rotate_credential(cred.token)
    assert auth.authenticate(admin.token)
    assert not auth._db.execute("SELECT 1 FROM admins WHERE id=?", (cred.credential_id,)).fetchone()
    with pytest.raises(PermissionError):
        auth.revoke_credential(admin.token, authority_token=cred.token)
    assert auth.authenticate(admin.token)
    auth.close()


def test_observe_only_cannot_revoke_control(tmp_path):
    auth, control, admin = setup_auth(tmp_path)
    observer = auth.create_credential(
        "s1", "owner", {"steve:observe:self"}, admin_token=admin.token
    )
    with pytest.raises(PermissionError):
        auth.revoke_credential(control.token, authority_token=observer.token)
    assert auth.check_scope(control.token, "steve:control", steve_id="s1")
    auth.revoke_credential(observer.token)
    assert not auth.check_scope(observer.token, "steve:observe:self", steve_id="s1")
    auth.close()


def test_control_can_revoke_itself(tmp_path):
    auth, control, _ = setup_auth(tmp_path)
    auth.revoke_credential(control.token)
    assert not auth.check_scope(control.token, "steve:control", steve_id="s1")
    auth.close()


def test_owner_cannot_revoke_sibling_of_other_owner_even_with_shared_steve(tmp_path):
    auth = AuthzService(tmp_path / "shared-steve.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    # An unclaimed Steve can hold independent observer keys for different owners.
    auth.register_steve("shared", admin_token=admin.token)
    alice = auth.create_credential(
        "shared", "alice", {"steve:observe:self"}, admin_token=admin.token
    )
    bob = auth.create_credential("shared", "bob", {"steve:observe:self"}, admin_token=admin.token)
    with pytest.raises(PermissionError):
        auth.revoke_credential(bob.token, authority_token=alice.token)
    assert auth.authenticate(bob.token)
    auth.close()


def test_admin_steves_can_revoke_owner_control_and_admin(tmp_path):
    auth, owner_credential, admin = setup_auth(tmp_path)
    control = auth.create_credential("s1", "owner", {"steve:control"}, admin_token=admin.token)
    extra_admin = auth.grant_admin_credential(admin.token, "second-root", {"admin:steves"})
    auth.revoke_credential(owner_credential.token, authority_token=admin.token)
    auth.revoke_credential(control.token, authority_token=admin.token)
    auth.revoke_credential(extra_admin.token, authority_token=admin.token)
    assert not auth.check_scope(owner_credential.token, "steve:control", steve_id="s1")
    assert not auth.check_scope(control.token, "steve:control", steve_id="s1")
    with pytest.raises(PermissionError):
        auth.authenticate(extra_admin.token)
    auth.close()


def test_admin_world_only_cannot_revoke_an_admin(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"})
    with pytest.raises(PermissionError):
        auth.revoke_credential(admin.token, authority_token=world_admin.token)
    assert auth.authenticate(admin.token)
    auth.close()


def test_admin_world_only_can_self_revoke(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"})
    auth.revoke_credential(world_admin.token)
    with pytest.raises(PermissionError):
        auth.authenticate(world_admin.token)
    assert auth.check_scope(admin.token, "admin:steves", steve_id="s1")
    auth.close()


def test_last_admin_cannot_self_revoke_and_peer_admin_can(tmp_path):
    """The final usable admin:steves credential cannot revoke itself.

    With a second valid admin:steves peer present, self-revocation is allowed
    for either one; when only one remains, revocation is refused so the admin
    surface always keeps a recovery path.
    """
    auth, _, admin = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.revoke_credential(admin.token)
    assert auth.authenticate(admin.token)
    second = auth.grant_admin_credential(admin.token, "second-root", {"admin:steves"})
    auth.revoke_credential(admin.token)
    with pytest.raises(PermissionError):
        auth.authenticate(admin.token)
    # The surviving peer can still revoke itself only while it is not alone.
    replacement = auth.grant_admin_credential(second.token, "third-root", {"admin:steves"})
    auth.revoke_credential(second.token)
    with pytest.raises(PermissionError):
        auth.revoke_credential(replacement.token)
    assert auth.authenticate(replacement.token)
    auth.close()


def test_bootstrap_refused_while_valid_admin_exists(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.create_admin_credential("second-bootstrap")
    assert auth.authenticate(admin.token)
    auth.close()


def test_bootstrap_permitted_when_no_valid_admin_remains(tmp_path):
    """Losing the sole admin is recoverable without hand-editing SQLite.

    A revoked sole admin leaves the admin table non-empty, but no valid
    credential, so the trusted local bootstrap path must be permitted again.
    The recovered bootstrap admin provisions, transfers, and revokes normally.
    """
    auth, _, admin = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.revoke_credential(admin.token)
    auth._db.execute("UPDATE admins SET revoked=1 WHERE id=?", (admin.credential_id,))
    with pytest.raises(PermissionError):
        auth.authenticate(admin.token)
    recovered = auth.create_admin_credential("root")
    assert auth.authenticate(recovered.token).owner_id == "root"
    assert "admin:steves" in recovered.scopes
    # The recovered admin has full authority again.
    new_credential = auth.create_credential(
        "s1", "owner", {"steve:control"}, admin_token=recovered.token
    )
    assert auth.check_scope(new_credential.token, "steve:control", steve_id="s1")
    auth.transfer_ownership(recovered.token, "s1", new_owner_id="other")
    with pytest.raises(PermissionError):
        auth.create_admin_credential("another-bootstrap")
    auth.close()


def test_world_only_admin_does_not_block_recovery_or_bootstrap_alone(tmp_path):
    auth = AuthzService(tmp_path / "world-only-recovery.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    with pytest.raises(ValueError, match="must include admin:steves"):
        auth.create_admin_credential("world-only", {"admin:world"})

    admin = auth.create_admin_credential("root")
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"})
    # Simulate loss/expiry of the only provisioning authority while a distinct
    # world administrator remains valid.
    auth._db.execute("UPDATE admins SET revoked=1 WHERE id=?", (admin.credential_id,))
    with pytest.raises(ValueError, match="must include admin:steves"):
        auth.create_admin_credential("world-only-recovery", {"admin:world"})
    recovered = auth.create_admin_credential("recovered-root")
    assert auth.check_scope(world_admin.token, "admin:world", steve_id="seed")
    auth.register_steve("new", admin_token=recovered.token)
    auth.close()


def test_non_admin_credential_cannot_bootstrap(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    with pytest.raises(PermissionError):
        auth.create_admin_credential(cred.owner_id)
    assert auth.authenticate(cred.token)
    assert auth.authenticate(admin.token)
    auth.close()


def test_steve_ids_must_be_nonempty_and_not_admin_sentinel(tmp_path):
    auth = AuthzService(tmp_path / "nonempty-steve.sqlite")
    with pytest.raises(ValueError, match="Steve ID must be non-empty"):
        auth.register_steve("")
    with pytest.raises(ValueError, match="Steve ID must be non-empty"):
        auth.register_steve("   ")
    admin = auth.create_admin_credential("root")
    with pytest.raises(ValueError, match="Steve ID must be non-empty"):
        auth.create_credential("", "owner", {"steve:control"}, admin_token=admin.token)
    auth.close()


def test_admin_can_reactivate_deactivated_steve_with_new_generation(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    session = auth.acquire_session(cred.token, "s1", owner_id="owner")
    auth.deactivate_steve(admin.token, "s1")
    assert not auth.check_scope(cred.token, "steve:control", steve_id="s1")
    auth.reactivate_steve(admin.token, "s1")
    assert auth.check_scope(cred.token, "steve:control", steve_id="s1")
    assert not auth.validate_session(session)
    renewed = auth.acquire_session(cred.token, "s1", owner_id="owner")
    assert renewed.generation > session.generation
    auth.reactivate_steve(admin.token, "s1")
    assert auth.validate_session(renewed)
    with pytest.raises(PermissionError, match="unknown Steve"):
        auth.reactivate_steve(admin.token, "missing")
    auth.close()


def test_reactivate_does_not_renew_expired_lease(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "leased", "owner", {"steve:control"}, admin_token=admin.token
    )
    auth.deactivate_steve(admin.token, "leased")
    auth._db.execute(
        "UPDATE steves SET expires=? WHERE id='leased'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    auth.reactivate_steve(admin.token, "leased")
    # The row is active again, but its expired lease still denies scopes.
    assert not auth.check_scope(credential.token, "steve:control", steve_id="leased")
    auth.transfer_ownership(
        admin.token,
        "leased",
        new_owner_id="new-owner",
        ownership_mode="leased",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    credential = auth.create_credential(
        "leased", "new-owner", {"steve:control"}, admin_token=admin.token
    )
    assert auth.check_scope(credential.token, "steve:control", steve_id="leased")
    auth.close()


def test_observe_shared_requires_explicit_peer_grant(tmp_path):
    """observe:shared succeeds only for an explicitly granted peer Steve."""
    auth = AuthzService(tmp_path / "shared.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve("s1", ownership_mode="persistent", owner_id="a", admin_token=admin.token)
    auth.register_steve("s2", ownership_mode="persistent", owner_id="b", admin_token=admin.token)
    shared = auth.create_credential("s1", "a", {"steve:observe:shared"}, admin_token=admin.token)

    # Without a grant, the shared scope fails closed for every peer target.
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="s2")
    auth.grant_peer_observation(admin.token, shared.credential_id, "s2")
    assert auth.check_scope(shared.token, "steve:observe:shared", steve_id="s2")
    # A peer Steve that was never granted stays closed.
    auth.register_steve("s3", ownership_mode="persistent", owner_id="c", admin_token=admin.token)
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="s3")
    # The holder's own Steve is not implicitly observable through this scope.
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="s1")
    # Deactivating the peer Steve closes the grant.
    auth.deactivate_steve(admin.token, "s2")
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="s2")
    auth.close()


def test_peer_grants_are_idempotent_attributed_listable_and_revocable(tmp_path):
    database = tmp_path / "peer-grant-lifecycle.sqlite"
    auth = AuthzService(database)
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("actual-admin-principal")
    auth.register_steve(
        "holder", ownership_mode="persistent", owner_id="alice", admin_token=admin.token
    )
    auth.register_steve(
        "peer", ownership_mode="persistent", owner_id="bob", admin_token=admin.token
    )
    credential = auth.create_credential(
        "holder", "alice", {"steve:observe:shared"}, admin_token=admin.token
    )
    auth.grant_peer_observation(admin.token, credential.credential_id, "peer")
    auth.grant_peer_observation(admin.token, credential.credential_id, "peer")
    assert auth.list_peer_observation_grants(admin.token, credential.credential_id) == ("peer",)
    grant = auth._db.execute(
        "SELECT granted_by FROM peer_grants WHERE credential=? AND steve='peer'",
        (credential.credential_id,),
    ).fetchone()
    assert grant["granted_by"] == "actual-admin-principal"
    history = auth.list_peer_observation_audit(admin.token, "holder")
    assert len(history) == 1
    assert (history[0].event, history[0].actor, history[0].peer_steve_id) == (
        "granted",
        "actual-admin-principal",
        "peer",
    )
    assert history[0].holder_steve_id == "holder"
    assert history[0].credential_id == credential.credential_id
    assert history[0].occurred_at.tzinfo is not None
    with pytest.raises(PermissionError):
        auth.list_peer_observation_audit(credential.token, "holder")

    assert auth.check_scope(credential.token, "steve:observe:shared", steve_id="peer")
    rotated = auth.rotate_credential(credential.token)
    assert auth.revoke_peer_observation(admin.token, rotated.credential_id, "peer")
    assert not auth.revoke_peer_observation(admin.token, rotated.credential_id, "peer")
    assert auth.list_peer_observation_grants(admin.token, rotated.credential_id) == ()
    assert not auth.check_scope(rotated.token, "steve:observe:shared", steve_id="peer")
    history = auth.list_peer_observation_audit(admin.token, "holder")
    assert [entry.event for entry in history] == ["granted", "revoked"]
    assert [entry.actor for entry in history] == [
        "actual-admin-principal",
        "actual-admin-principal",
    ]
    assert history[1].credential_id == rotated.credential_id
    assert history[0].occurred_at <= history[1].occurred_at
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        auth._db.execute("UPDATE peer_grant_audit SET actor='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        auth._db.execute("DELETE FROM peer_grant_audit")
    auth.close()
    with AuthzService(database) as reopened:
        persisted_history = reopened.list_peer_observation_audit(admin.token, "holder")
        assert [entry.event for entry in persisted_history] == ["granted", "revoked"]
        assert [entry.actor for entry in persisted_history] == [
            "actual-admin-principal",
            "actual-admin-principal",
        ]
    _assert_legacy_peer_grants_get_one_time_audit_baseline(tmp_path)


def _assert_legacy_peer_grants_get_one_time_audit_baseline(tmp_path: Path) -> None:
    database = tmp_path / "legacy-peer-grants.sqlite"
    auth = AuthzService(database)
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("migration-admin")
    auth.register_steve(
        "holder", ownership_mode="persistent", owner_id="alice", admin_token=admin.token
    )
    auth.register_steve(
        "peer", ownership_mode="persistent", owner_id="bob", admin_token=admin.token
    )
    credential = auth.create_credential(
        "holder", "alice", {"steve:observe:shared"}, admin_token=admin.token
    )
    auth.grant_peer_observation(admin.token, credential.credential_id, "peer")
    auth.close()

    # Recreate the pre-migration state: active grant, no audit history,
    # migration marker, or time_basis column.
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER peer_grant_audit_no_update")
    connection.execute("DROP TRIGGER peer_grant_audit_no_delete")
    connection.execute("DELETE FROM peer_grant_audit")
    connection.execute("DELETE FROM schema_migrations WHERE version=1")
    connection.execute("ALTER TABLE peer_grant_audit DROP COLUMN time_basis")
    connection.commit()
    connection.close()

    with AuthzService(database) as migrated:
        baseline = migrated.list_peer_observation_audit(admin.token, "holder")
        assert len(baseline) == 1
        assert (
            baseline[0].event,
            baseline[0].actor,
            baseline[0].credential_id,
            baseline[0].time_basis,
        ) == (
            "granted",
            "migration-admin",
            credential.credential_id,
            "migration_baseline_original_time_unknown",
        )
        assert baseline[0].occurred_at.tzinfo is not None

    # Opening the migrated database again must not add another baseline.
    with AuthzService(database) as reopened:
        assert len(reopened.list_peer_observation_audit(admin.token, "holder")) == 1
        assert reopened.revoke_peer_observation(admin.token, credential.credential_id, "peer")
        history = reopened.list_peer_observation_audit(admin.token, "holder")
        assert [entry.event for entry in history] == ["granted", "revoked"]
        assert history[0].time_basis == "migration_baseline_original_time_unknown"
        assert history[1].time_basis == "event_time"

    with AuthzService(database) as final_open:
        final_history = final_open.list_peer_observation_audit(admin.token, "holder")
        assert [entry.event for entry in final_history] == ["granted", "revoked"]
        assert (
            final_open._db.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=1"
            ).fetchone()[0]
            == 1
        )


def test_observe_shared_fails_closed_for_holder_and_expired_ownership(tmp_path):
    """The holder Steve must stay valid and the peer target stays grant-only."""
    auth = AuthzService(tmp_path / "shared-close.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    auth.register_steve(
        "s1",
        ownership_mode="leased",
        owner_id="a",
        ownership_expires_at=expiry,
        admin_token=admin.token,
    )
    auth.register_steve("s2", ownership_mode="persistent", owner_id="b", admin_token=admin.token)
    shared = auth.create_credential("s1", "a", {"steve:observe:shared"}, admin_token=admin.token)
    auth.grant_peer_observation(admin.token, shared.credential_id, "s2")
    assert auth.check_scope(shared.token, "steve:observe:shared", steve_id="s2")
    # If the holder's own Steve lease lapses, the shared scope fails closed.
    auth._db.execute(
        "UPDATE steves SET expires=? WHERE id='s1'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="s2")
    auth.close()


def test_shared_and_events_scopes_reject_former_owner_after_transfer(tmp_path):
    auth = AuthzService(tmp_path / "origin-owner.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve(
        "origin", ownership_mode="persistent", owner_id="alice", admin_token=admin.token
    )
    auth.register_steve(
        "peer", ownership_mode="persistent", owner_id="bob", admin_token=admin.token
    )
    shared = auth.create_credential(
        "origin", "alice", {"steve:observe:shared"}, admin_token=admin.token
    )
    events = auth.create_credential("origin", "alice", {"events:read"}, admin_token=admin.token)
    auth.grant_peer_observation(admin.token, shared.credential_id, "peer")
    assert auth.check_scope(shared.token, "steve:observe:shared", steve_id="peer")
    assert auth.check_scope(events.token, "events:read", steve_id="peer")

    auth.transfer_ownership(admin.token, "origin", new_owner_id="carol")
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="peer")
    assert not auth.check_scope(events.token, "events:read", steve_id="peer")
    auth.close()


def test_rotation_preserves_peer_observation_grants(tmp_path):
    auth = AuthzService(tmp_path / "rotate-peer-grant.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve(
        "holder", ownership_mode="persistent", owner_id="alice", admin_token=admin.token
    )
    auth.register_steve(
        "peer", ownership_mode="persistent", owner_id="bob", admin_token=admin.token
    )
    shared = auth.create_credential(
        "holder", "alice", {"steve:observe:shared"}, admin_token=admin.token
    )
    auth.grant_peer_observation(admin.token, shared.credential_id, "peer")
    rotated = auth.rotate_credential(shared.token)
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="peer")
    assert auth.check_scope(rotated.token, "steve:observe:shared", steve_id="peer")
    assert (
        auth._db.execute("SELECT credential FROM peer_grants WHERE steve='peer'").fetchone()[0]
        == rotated.credential_id
    )
    auth.close()


def test_check_scope_holds_lock_across_authentication_and_decision(tmp_path):
    auth, cred, _ = setup_auth(tmp_path)
    authenticated = Event()
    resume = Event()
    original_authenticate = auth.authenticate

    def paused_authenticate(token):
        result = original_authenticate(token)
        authenticated.set()
        assert resume.wait(timeout=2)
        return result

    auth.authenticate = paused_authenticate
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(auth.check_scope, cred.token, "steve:control", steve_id="s1")
        try:
            assert authenticated.wait(timeout=2)
            # A different thread cannot revoke or otherwise mutate persisted
            # authorization state until this decision has finished.
            assert not auth._lock.acquire(blocking=False)
        finally:
            resume.set()
        assert check.result(timeout=2)
    auth.authenticate = original_authenticate
    auth.revoke_credential(cred.token)
    assert not auth.check_scope(cred.token, "steve:control", steve_id="s1")
    auth.close()


def test_check_scope_uses_sqlite_snapshot_across_service_connections(tmp_path):
    database = tmp_path / "scope-snapshot.sqlite"
    auth = AuthzService(database)
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve(
        "holder", ownership_mode="persistent", owner_id="alice", admin_token=admin.token
    )
    auth.register_steve(
        "peer", ownership_mode="persistent", owner_id="bob", admin_token=admin.token
    )
    shared = auth.create_credential(
        "holder", "alice", {"steve:observe:shared"}, admin_token=admin.token
    )
    auth.grant_peer_observation(admin.token, shared.credential_id, "peer")
    other_connection = AuthzService(database)
    original_authenticate = auth.authenticate

    def revoke_after_authentication(token):
        credential = original_authenticate(token)
        # This commits on another SQLite connection after the auth query but
        # before check_scope reads the grant. The current decision must use one
        # pinned snapshot, then the next decision must observe the revocation.
        assert other_connection.revoke_peer_observation(admin.token, shared.credential_id, "peer")
        return credential

    auth.authenticate = revoke_after_authentication
    assert auth.check_scope(shared.token, "steve:observe:shared", steve_id="peer")
    auth.authenticate = original_authenticate
    assert not auth.check_scope(shared.token, "steve:observe:shared", steve_id="peer")
    other_connection.close()
    auth.close()


def test_world_admin_can_self_revoke_without_steves_authority_loss(tmp_path):
    auth, _, admin = setup_auth(tmp_path)
    world_admin = auth.grant_admin_credential(admin.token, "world-op", {"admin:world"})
    auth.revoke_credential(world_admin.token)
    with pytest.raises(PermissionError):
        auth.authenticate(world_admin.token)
    assert auth.check_scope(admin.token, "admin:steves", steve_id="s1")
    auth.close()


def test_events_read_is_service_level_and_not_steve_bound(tmp_path):
    """events:read succeeds without matching the query's steve_id target."""
    auth = AuthzService(tmp_path / "events.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve("s1", ownership_mode="persistent", owner_id="a", admin_token=admin.token)
    reader = auth.create_credential("s1", "a", {"events:read"}, admin_token=admin.token)
    # Service scope: any steve_id target is acceptable for an authorized read.
    assert auth.check_scope(reader.token, "events:read", steve_id="s1")
    assert auth.check_scope(reader.token, "events:read", steve_id="s2")
    assert auth.check_scope(reader.token, "events:read", steve_id="")
    # The credential must still be bound to a live Steve.
    auth.deactivate_steve(admin.token, "s1")
    assert not auth.check_scope(reader.token, "events:read", steve_id="s1")
    auth.close()


def test_scope_binding_matrix_and_transfer_stays_fail_closed(tmp_path):
    """The scope model is explicit: Steve bound, peer grant bound, service."""
    auth = AuthzService(tmp_path / "binding.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve("s1", ownership_mode="persistent", owner_id="a", admin_token=admin.token)
    auth.register_steve("s2", ownership_mode="persistent", owner_id="b", admin_token=admin.token)
    cred = auth.create_credential(
        "s1", "a", {"steve:observe:self", "steve:control"}, admin_token=admin.token
    )
    shared = auth.create_credential("s1", "a", {"steve:observe:shared"}, admin_token=admin.token)
    # Steve-bound scopes only answer for the credential's own Steve.
    assert auth.check_scope(cred.token, "steve:observe:self", steve_id="s1")
    assert not auth.check_scope(cred.token, "steve:observe:self", steve_id="s2")
    assert not auth.check_scope(shared.token, "steve:control", steve_id="s2")
    # observe:shared never grants access through an unshared scope either.
    assert not auth.check_scope(cred.token, "steve:observe:shared", steve_id="s2")
    # After an ownership transfer, the former owner's observe:self fails closed.
    auth.transfer_ownership(admin.token, "s1", new_owner_id="b")
    assert not auth.check_scope(cred.token, "steve:observe:self", steve_id="s1")
    auth.close()


def test_leased_ownership_renews_forward_and_fences_old_session(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    first = datetime.now(UTC) + timedelta(minutes=5)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=first,
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "leased", "owner", {"steve:control"}, admin_token=admin.token
    )
    session_expiry = datetime.now(UTC) + timedelta(minutes=1)
    session = auth.acquire_session(
        credential.token, "leased", owner_id="owner", lease_expires_at=session_expiry
    )
    assert auth.validate_session(session)
    renewed = first + timedelta(minutes=30)
    auth.renew_ownership(credential.token, "leased", owner_id="owner", expires_at=renewed)
    # The persisted lease changed, so a session bound to the previous state stops
    # validating (fencing effect).
    assert not auth.validate_session(session)
    row = auth._db.execute("SELECT expires FROM steves WHERE id='leased'").fetchone()
    assert datetime.fromisoformat(row["expires"]) == renewed
    follow_up = auth.acquire_session(
        credential.token, "leased", owner_id="owner", lease_expires_at=session_expiry
    )
    assert auth.validate_session(follow_up)
    auth.close()


def test_renewal_rejects_shrinking_past_and_naive_expiry(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    lease = datetime.now(UTC) + timedelta(minutes=10)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=lease,
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "leased", "owner", {"steve:control"}, admin_token=admin.token
    )
    with pytest.raises(ValueError):
        auth.renew_ownership(
            credential.token, "leased", owner_id="owner", expires_at=lease - timedelta(minutes=1)
        )
    with pytest.raises(ValueError):
        auth.renew_ownership(
            credential.token,
            "leased",
            owner_id="owner",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        auth.renew_ownership(
            credential.token,
            "leased",
            owner_id="owner",
            expires_at=datetime(2030, 1, 1),  # naive
        )
    row = auth._db.execute("SELECT expires FROM steves WHERE id='leased'").fetchone()
    assert datetime.fromisoformat(row["expires"]) == lease
    auth.close()


def test_renewal_rejects_inactive_unclaimed_wrong_owner_and_expired(tmp_path):
    auth = AuthzService(tmp_path / "renew-reject.sqlite")
    auth.register_steve("seed", ownership_mode="persistent", owner_id="seed-owner")
    admin = auth.create_admin_credential("root")
    auth.register_steve("free", admin_token=admin.token)
    free = auth.create_credential("free", "alice", {"steve:control"}, admin_token=admin.token)
    with pytest.raises(PermissionError):
        auth.renew_ownership(free.token, "free", owner_id="alice")

    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="alice",
        ownership_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        admin_token=admin.token,
    )
    leased = auth.create_credential("leased", "alice", {"steve:control"}, admin_token=admin.token)
    with pytest.raises(PermissionError):
        auth.renew_ownership(leased.token, "leased", owner_id="bob")
    auth.deactivate_steve(admin.token, "leased")
    with pytest.raises(PermissionError):
        auth.renew_ownership(
            leased.token,
            "leased",
            owner_id="alice",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    auth.close()


def test_renewal_cannot_resurrect_expired_lease(tmp_path):
    auth, cred, admin = setup_auth(tmp_path)
    auth.register_steve(
        "leased",
        ownership_mode="leased",
        owner_id="owner",
        ownership_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        admin_token=admin.token,
    )
    credential = auth.create_credential(
        "leased", "owner", {"steve:control"}, admin_token=admin.token
    )
    auth._db.execute(
        "UPDATE steves SET expires=? WHERE id='leased'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    with pytest.raises(PermissionError):
        auth.renew_ownership(
            credential.token,
            "leased",
            owner_id="owner",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    auth.close()


def test_persistent_owner_can_renew_without_expiry(tmp_path):
    auth, credential, _ = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    auth.renew_ownership(credential.token, "s1", owner_id="owner")
    # Renewal advances the fencing generation, so the prior session is fenced.
    assert not auth.validate_session(session)
    updated = auth.acquire_session(credential.token, "s1", owner_id="owner")
    assert updated.generation > session.generation
    with pytest.raises(ValueError):
        auth.renew_ownership(
            credential.token,
            "s1",
            owner_id="owner",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidator", ["revoke", "expire", "transfer", "fence"])
async def test_cached_action_replay_revalidates_controller_session(tmp_path, invalidator):
    auth, credential, admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    world = MockMcpqClient()
    service = ActionService(world, policy=auth.action_policy(session))

    first = await service.execute(
        ActionType.CHAT,
        {"message": "recorded once"},
        request_id="same-request",
    )
    assert first.success
    assert world.chat_messages_sent == ["recorded once"]

    if invalidator == "revoke":
        auth.revoke_credential(credential.token)
    elif invalidator == "expire":
        auth._db.execute(
            "UPDATE steves SET session_expires=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "s1"),
        )
    elif invalidator == "transfer":
        auth.transfer_ownership(admin.token, "s1", new_owner_id="new-owner")
    else:
        auth.release_session(session)
        auth.acquire_session(credential.token, "s1", owner_id="owner")

    replay = await service.execute(
        ActionType.CHAT,
        {"message": "recorded once"},
        request_id="same-request",
    )
    assert replay.success is False
    assert "authorization" in replay.message or "fenced" in replay.message
    assert world.chat_messages_sent == ["recorded once"]
    assert service.audit_records[-1].allowed is False
    auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidator", ["revoke", "expire", "transfer", "fence"])
async def test_inflight_action_replay_revalidates_after_dispatch_completes(tmp_path, invalidator):
    import asyncio

    auth, credential, admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    world = MockMcpqClient()
    chat_started = asyncio.Event()
    release_chat = asyncio.Event()
    follower_checked_initial_policy = asyncio.Event()
    follower_rechecked_policy = asyncio.Event()
    policy_calls = 0
    original_post_to_chat = world.post_to_chat

    async def blocked_chat(message):
        chat_started.set()
        await release_chat.wait()
        await original_post_to_chat(message)

    world.post_to_chat = blocked_chat
    base_policy = auth.action_policy(session)

    def policy(request):
        nonlocal policy_calls
        policy_calls += 1
        decision = base_policy(request)
        if policy_calls == 3:
            follower_checked_initial_policy.set()
        elif policy_calls == 4:
            follower_rechecked_policy.set()
        return decision

    service = ActionService(world, policy=policy)
    leader = asyncio.create_task(
        service.execute(ActionType.CHAT, {"message": "private result"}, request_id="inflight")
    )
    follower = None
    try:
        await asyncio.wait_for(chat_started.wait(), timeout=1)
        follower = asyncio.create_task(
            service.execute(
                ActionType.CHAT,
                {"message": "private result"},
                request_id="inflight",
            )
        )
        await asyncio.wait_for(follower_checked_initial_policy.wait(), timeout=1)

        if invalidator == "revoke":
            auth.revoke_credential(credential.token)
        elif invalidator == "expire":
            auth._db.execute(
                "UPDATE steves SET session_expires=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "s1"),
            )
        elif invalidator == "transfer":
            auth.transfer_ownership(admin.token, "s1", new_owner_id="new-owner")
        else:
            auth.release_session(session)
            auth.acquire_session(credential.token, "s1", owner_id="owner")
    finally:
        release_chat.set()
        pending = [leader] if follower is None else [leader, follower]
        await asyncio.gather(*pending, return_exceptions=True)

    first = leader.result()
    assert follower is not None
    replay = follower.result()

    assert first.success
    assert not replay.success
    assert "private result" not in replay.message
    assert "private result" not in repr(replay.data)
    assert world.chat_messages_sent == ["private result"]
    assert follower_rechecked_policy.is_set()
    assert service.audit_records[-1].allowed is False
    auth.close()


@pytest.mark.asyncio
async def test_same_request_id_is_not_replayed_to_another_controller(tmp_path):
    auth, first_credential, admin = setup_auth(tmp_path)
    auth.register_steve("s2", ownership_mode="persistent", owner_id="peer", admin_token=admin.token)
    second_credential = auth.create_credential(
        "s2", "peer", {"steve:control"}, admin_token=admin.token
    )
    first_session = auth.acquire_session(first_credential.token, "s1", owner_id="owner")
    current_session = [first_session]

    def policy(request):
        return auth.action_policy(current_session[0])(request)

    world = MockMcpqClient()
    service = ActionService(world, policy=policy)
    first = await service.execute(
        ActionType.CHAT,
        {"message": "private result"},
        request_id="reused-id",
    )
    assert first.success
    auth.release_session(first_session)
    second_session = auth.acquire_session(second_credential.token, "s2", owner_id="peer")
    current_session[0] = second_session

    replay = await service.execute(
        ActionType.CHAT,
        {"message": "private result"},
        request_id="reused-id",
    )
    assert replay.success is False
    assert "another authorized identity" in replay.message
    assert "private result" not in replay.message
    assert world.chat_messages_sent == ["private result"]
    auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        ActionType.MOVE_TO,
        ActionType.MOVE_FORWARD,
        ActionType.MOVE_BACK,
        ActionType.WALK_TO,
        ActionType.TURN_LEFT,
        ActionType.TURN_RIGHT,
        ActionType.JUMP,
        ActionType.SPRINT,
        ActionType.TELEPORT,
        ActionType.BREAK_BLOCK,
        ActionType.PLACE_BLOCK,
        ActionType.INTERACT,
        ActionType.EQUIP_ITEM,
        ActionType.EQUIP_BEST_WEAPON,
        ActionType.CRAFT_ITEM,
        ActionType.DROP_ITEM,
        ActionType.EAT,
        ActionType.HEAL,
        ActionType.ATTACK,
    ],
)
async def test_control_session_cannot_run_privileged_simulated_action(tmp_path, action):
    auth, credential, _admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    world = MockMcpqClient()
    service = ActionService(world, policy=auth.action_policy(session))

    result = await service.execute(action, {})

    assert result.success is False
    assert "admin:world" in result.message
    assert world.commands_ran == []
    auth.close()


@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.GOTO_TELEPORT, {"target": "TargetPlayer"}),
        (ActionType.PROVISION_BOT, {"player_name": "AIBot"}),
        (ActionType.PREPARE_SAFE_SPAWN, {}),
    ],
)
@pytest.mark.asyncio
async def test_control_session_cannot_run_internal_world_action_without_admin_world(
    tmp_path, action, params
):
    auth, credential, _admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    world = MockMcpqClient()
    authority = object()
    service = ActionService(
        world,
        policy=auth.action_policy(session),
        internal_authority=authority,
    )

    result = await service.execute(action, params, authority=authority)

    assert not result.success
    assert "admin:world" in result.message
    assert world.commands_ran == []
    auth.close()


@pytest.mark.asyncio
async def test_internal_world_action_runs_with_controller_and_world_admin(tmp_path):
    auth, credential, admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    world_admin = auth.grant_admin_credential(admin.token, "world-operator", {"admin:world"})
    world = MockMcpqClient()
    authority = object()
    service = ActionService(
        world,
        policy=auth.action_policy(session, admin_token=world_admin.token),
        internal_authority=authority,
    )

    result = await service.execute(
        ActionType.GOTO_TELEPORT,
        {"target": "TargetPlayer"},
        authority=authority,
    )

    assert result.success
    world.assert_command_contains("tp AIBot TargetPlayer")
    auth.close()


@pytest.mark.asyncio
async def test_narrow_safety_authority_survives_controller_expiry(tmp_path):
    auth, credential, _admin = setup_auth(tmp_path)
    session = auth.acquire_session(credential.token, "s1", owner_id="owner")
    auth._db.execute(
        "UPDATE steves SET session_expires=? WHERE id='s1'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    world = MockMcpqClient()
    safety_authority = object()
    service = ActionService(
        world,
        policy=auth.action_policy(session),
        safety_authority=safety_authority,
    )

    heal = await service.execute(
        ActionType.HEAL,
        {"heal_item": "golden_apple"},
        safety_authority=safety_authority,
    )
    place = await service.execute(
        ActionType.PLACE_BLOCK,
        {"x": 1, "y": 64, "z": 1, "block_type": "stone"},
        safety_authority=safety_authority,
    )

    assert heal.success
    assert any("effect give AIBot regeneration" in command for command in world.commands_ran)
    assert place.success is False
    assert not any("setblock" in command for command in world.commands_ran)
    auth.close()
