"""Local durable credentials, ownership, scopes, and controller fencing."""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .action_service import ActionRequest, PolicyDecision


SCOPES = frozenset(
    {
        "steve:observe:self",
        "steve:control",
        "steve:observe:shared",
        "events:read",
        "admin:steves",
        "admin:world",
    }
)

# Scope binding model (RFC-0001 section 5):
# - "steve" scopes are bound to the credential's own Steve and stay subject to
#   the persisted ownership/owner checks.
# - "peer" scopes (steve:observe:shared) require an explicit administrator
#   grant naming the peer Steve; the grant is the authority to cross owners.
# - "service" scopes (events:read) are not tied to one Steve but still require
#   a valid credential bound to an active Steve.
# - "admin" scopes belong to global admin identities only.
_SCOPE_BINDING = {
    "steve:observe:self": "steve",
    "steve:control": "steve",
    "steve:observe:shared": "peer",
    "events:read": "service",
    "admin:steves": "admin",
    "admin:world": "admin",
}


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Credential:
    credential_id: str
    steve_id: str
    owner_id: str
    scopes: frozenset[str]
    expires_at: datetime | None


@dataclass(frozen=True)
class CreatedCredential(Credential):
    token: str


@dataclass(frozen=True)
class ControllerSession:
    session_id: str
    steve_id: str
    credential_id: str
    owner_id: str
    generation: int
    expires_at: datetime | None


@dataclass(frozen=True)
class PeerGrantAuditEntry:
    holder_steve_id: str
    peer_steve_id: str
    event: str
    actor: str
    occurred_at: datetime
    credential_id: str
    time_basis: str


class AuthzService:
    """SQLite-backed local authorization service. Not a transport or watchdog."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        path = str(database)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS steves(
                id TEXT PRIMARY KEY, active INTEGER NOT NULL, mode TEXT NOT NULL,
                owner TEXT, expires TEXT, generation INTEGER NOT NULL DEFAULT 0,
                session TEXT, session_credential TEXT, session_expires TEXT
            );
            CREATE TABLE IF NOT EXISTS credentials(
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                steve TEXT NOT NULL REFERENCES steves(id), owner TEXT NOT NULL,
                scopes TEXT NOT NULL, expires TEXT, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS admins(
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                principal TEXT NOT NULL, scopes TEXT NOT NULL, expires TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS peer_grants(
                credential TEXT NOT NULL REFERENCES credentials(id),
                steve TEXT NOT NULL REFERENCES steves(id),
                granted_by TEXT NOT NULL,
                PRIMARY KEY(credential, steve)
            );
            CREATE TABLE IF NOT EXISTS peer_grant_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                holder_steve TEXT NOT NULL,
                peer_steve TEXT NOT NULL,
                event TEXT NOT NULL CHECK(event IN ('granted','revoked')),
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                credential TEXT NOT NULL,
                time_basis TEXT NOT NULL DEFAULT 'event_time'
            );
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS peer_grant_audit_no_update
            BEFORE UPDATE ON peer_grant_audit
            BEGIN
                SELECT RAISE(ABORT, 'peer grant audit is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS peer_grant_audit_no_delete
            BEFORE DELETE ON peer_grant_audit
            BEGIN
                SELECT RAISE(ABORT, 'peer grant audit is append-only');
            END;
            """
        )
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(steves)")}
        if "session_credential" not in columns:
            self._db.execute("ALTER TABLE steves ADD COLUMN session_credential TEXT")
        if "session_expires" not in columns:
            self._db.execute("ALTER TABLE steves ADD COLUMN session_expires TEXT")
        self._migrate_peer_grant_audit_baseline()

    def _migrate_peer_grant_audit_baseline(self) -> None:
        """Record active legacy peer grants once, marking their time as unknown.

        The baseline row's ``occurred_at`` is the migration time, not the
        original grant time. ``time_basis`` makes that limitation explicit.
        The migration marker and baseline rows commit atomically.
        """
        self._lock.acquire()
        try:
            self._begin_write()
            version = 1
            applied = self._db.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if applied:
                self._commit()
                return

            audit_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(peer_grant_audit)")
            }
            if "time_basis" not in audit_columns:
                self._db.execute(
                    "ALTER TABLE peer_grant_audit ADD COLUMN time_basis TEXT NOT NULL "
                    "DEFAULT 'event_time'"
                )

            migration_time = _dt(_now())
            self._db.execute(
                "INSERT INTO peer_grant_audit "
                "(holder_steve,peer_steve,event,actor,occurred_at,credential,time_basis) "
                "SELECT c.steve,g.steve,'granted',g.granted_by,?,g.credential,"
                "'migration_baseline_original_time_unknown' "
                "FROM peer_grants AS g JOIN credentials AS c ON c.id=g.credential "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM peer_grant_audit AS a "
                "WHERE a.holder_steve=c.steve AND a.peer_steve=g.steve "
                "AND a.event='granted')",
                (migration_time,),
            )
            self._db.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, migration_time),
            )
            self._commit()
        except BaseException:
            if self._db.in_transaction:
                with suppress(sqlite3.Error):
                    self._db.execute("ROLLBACK")
            raise
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> AuthzService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _tx(self):
        class Transaction:
            def __init__(self, outer):
                self.outer = outer

            def __enter__(self):
                self.outer._lock.acquire()
                try:
                    self.outer._begin_write()
                except BaseException:
                    # Never leak the write lock when the transaction cannot start.
                    with suppress(sqlite3.Error):
                        self.outer._db.execute("ROLLBACK")
                    self.outer._lock.release()
                    raise
                return self.outer._db

            def __exit__(self, typ, val, tb):
                try:
                    if typ:
                        try:
                            if self.outer._db.in_transaction:
                                self.outer._db.execute("ROLLBACK")
                        except sqlite3.Error:
                            # Some SQLite failures auto-rollback. Preserve the
                            # operation's original exception, and close the
                            # connection if it still reports an open transaction.
                            try:
                                transaction_open = self.outer._db.in_transaction
                            except sqlite3.Error:
                                transaction_open = True
                            if transaction_open:
                                with suppress(sqlite3.Error):
                                    self.outer._db.close()
                    else:
                        try:
                            self.outer._commit()
                        except BaseException:
                            # A failed COMMIT may leave SQLite's transaction
                            # active. Roll it back before releasing the lock so
                            # the next operation can begin cleanly.
                            with suppress(sqlite3.Error):
                                self.outer._db.execute("ROLLBACK")
                            raise
                finally:
                    self.outer._lock.release()

        return Transaction(self)

    @contextmanager
    def _try_tx(self) -> Iterator[sqlite3.Connection]:
        """Begin a write transaction without waiting on local or SQLite locks."""
        if not self._lock.acquire(blocking=False):
            raise sqlite3.OperationalError("authorization service is busy")
        previous_timeout: int | None = None
        try:
            previous_timeout = int(self._db.execute("PRAGMA busy_timeout").fetchone()[0])
            self._db.execute("PRAGMA busy_timeout=0")
            self._begin_write()
            try:
                yield self._db
                self._commit()
            except BaseException:
                if self._db.in_transaction:
                    with suppress(sqlite3.Error):
                        self._db.execute("ROLLBACK")
                raise
        finally:
            if previous_timeout is not None:
                with suppress(sqlite3.Error):
                    self._db.execute(f"PRAGMA busy_timeout={previous_timeout}")
            self._lock.release()

    def _begin_write(self) -> None:
        """Start a serialized immediate write transaction on the connection."""
        self._db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        """Commit the current serialized write transaction."""
        self._db.execute("COMMIT")

    def register_steve(
        self,
        steve_id: str,
        *,
        ownership_mode: str = "unclaimed",
        owner_id: str | None = None,
        ownership_expires_at: datetime | None = None,
        active: bool = True,
        admin_token: str | None = None,
    ):
        if not steve_id or not steve_id.strip():
            raise ValueError("Steve ID must be non-empty")
        if owner_id is not None:
            _require_identity(owner_id, "owner ID")
        if ownership_mode not in {"persistent", "leased", "unclaimed"}:
            raise ValueError("invalid ownership mode")
        expiry = _dt(ownership_expires_at)
        if ownership_mode == "leased" and (not owner_id or not expiry or _expired(expiry)):
            raise ValueError("leased ownership requires owner and expiry")
        if ownership_mode == "persistent" and not owner_id:
            raise ValueError("persistent ownership requires owner")
        if ownership_mode == "persistent" and expiry is not None:
            raise ValueError("persistent ownership cannot expire")
        if ownership_mode == "unclaimed" and owner_id is not None:
            raise ValueError("unclaimed Steve cannot have an owner")
        if ownership_mode == "unclaimed" and expiry is not None:
            raise ValueError("unclaimed ownership cannot have an expiry")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM admins LIMIT 1").fetchone() and (
                not admin_token or not _has_admin(db, admin_token)
            ):
                raise PermissionError("admin:steves required to provision Steves")
            existing = db.execute("SELECT * FROM steves WHERE id=?", (steve_id,)).fetchone()
            if existing:
                if (
                    existing["active"],
                    existing["mode"],
                    existing["owner"],
                    existing["expires"],
                ) != (int(active), ownership_mode, owner_id, expiry):
                    raise ValueError(
                        "Steve registration is stable; use authorized transfer "
                        "for ownership changes"
                    )
            else:
                db.execute(
                    "INSERT INTO steves(id,active,mode,owner,expires) VALUES(?,?,?,?,?)",
                    (steve_id, int(active), ownership_mode, owner_id, expiry),
                )
        return SteveRegistration(steve_id, ownership_mode, owner_id, ownership_expires_at, active)

    def create_credential(
        self,
        steve_id: str,
        owner_id: str,
        scopes: set[str] | frozenset[str],
        *,
        expires_at: datetime | None = None,
        admin_token: str | None = None,
    ) -> CreatedCredential:
        if not steve_id or not steve_id.strip():
            raise ValueError("Steve ID must be non-empty")
        _require_identity(owner_id, "owner ID")
        if not scopes or not set(scopes) <= SCOPES:
            raise ValueError("credential requires valid explicit scopes")
        token = "mab_" + secrets.token_urlsafe(32)
        cid = str(uuid4())
        with self._tx() as db:
            row = db.execute("SELECT * FROM steves WHERE id=?", (steve_id,)).fetchone()
            if not row or not row["active"]:
                raise PermissionError("Steve is missing or inactive")
            if not admin_token or not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required to provision credentials")
            if {"admin:steves", "admin:world"} & set(scopes):
                raise ValueError("admin scopes require an admin identity credential")
            if row["mode"] != "unclaimed" and row["owner"] != owner_id:
                raise PermissionError("owner binding invalid")
            if _expired(row["expires"]):
                raise PermissionError("ownership expired")
            db.execute(
                "INSERT INTO credentials(id,token_hash,steve,owner,scopes,expires) "
                "VALUES(?,?,?,?,?,?)",
                (cid, _hash(token), steve_id, owner_id, "\n".join(sorted(scopes)), _dt(expires_at)),
            )
        return CreatedCredential(cid, steve_id, owner_id, frozenset(scopes), expires_at, token)

    def create_admin_credential(
        self,
        admin_id: str,
        scopes: set[str] | frozenset[str] = frozenset({"admin:steves"}),
    ) -> CreatedCredential:
        """Bootstrap or recover a global admin from trusted local setup code.

        The call is refused while a valid (unrevoked, unexpired)
        ``admin:steves`` credential exists. An ``admin:world``-only credential
        does not block recovery of provisioning authority. Recovery must
        create an ``admin:steves`` credential; requesting only ``admin:world``
        is refused when no valid ``admin:steves`` exists. No token is
        presented or required; this is local setup/operator access, not a
        network enrollment mechanism.
        """
        _require_identity(admin_id, "admin ID")
        admin_scopes = {"admin:steves", "admin:world"}
        if not scopes or not set(scopes) <= admin_scopes:
            raise ValueError("bootstrap credentials accept only explicit admin scopes")
        if "admin:steves" not in scopes:
            raise ValueError("bootstrap credentials must include admin:steves")
        token = "mab_" + secrets.token_urlsafe(32)
        credential_id = str(uuid4())
        with self._tx() as db:
            if _has_valid_admin_steves(db):
                raise PermissionError(
                    "admin bootstrap refused while a valid admin:steves credential exists"
                )
            db.execute(
                "INSERT INTO admins(id,token_hash,principal,scopes) VALUES(?,?,?,?)",
                (credential_id, _hash(token), admin_id, "\n".join(sorted(scopes))),
            )
        return CreatedCredential(credential_id, "", admin_id, frozenset(scopes), None, token)

    def grant_admin_credential(
        self,
        admin_token: str,
        admin_id: str,
        scopes: set[str] | frozenset[str],
        *,
        expires_at: datetime | None = None,
    ) -> CreatedCredential:
        _require_identity(admin_id, "admin ID")
        admin_scopes = {"admin:steves", "admin:world"}
        if not scopes or not set(scopes) <= admin_scopes:
            raise ValueError("admin credentials accept only explicit admin scopes")
        expiry = _dt(expires_at)
        if expiry is not None and _expired(expiry):
            raise ValueError("admin credential expiry must be in the future")
        token = "mab_" + secrets.token_urlsafe(32)
        credential_id = str(uuid4())
        with self._tx() as db:
            if not _has_admin(db, admin_token, required_scope="admin:steves"):
                raise PermissionError("admin:steves required")
            db.execute(
                "INSERT INTO admins(id,token_hash,principal,scopes,expires) VALUES(?,?,?,?,?)",
                (credential_id, _hash(token), admin_id, "\n".join(sorted(scopes)), expiry),
            )
        return CreatedCredential(credential_id, "", admin_id, frozenset(scopes), expires_at, token)

    def authenticate(self, token: str) -> Credential:
        with self._lock:
            admin = self._db.execute(
                "SELECT * FROM admins WHERE token_hash=?", (_hash(token),)
            ).fetchone()
            if admin:
                if admin["revoked"] or _expired(admin["expires"]):
                    raise PermissionError("invalid credential")
                return Credential(
                    admin["id"],
                    "",
                    admin["principal"],
                    frozenset(admin["scopes"].splitlines()),
                    datetime.fromisoformat(admin["expires"]) if admin["expires"] else None,
                )
            row = self._db.execute(
                "SELECT * FROM credentials WHERE token_hash=?", (_hash(token),)
            ).fetchone()
            if not row or row["revoked"] or _expired(row["expires"]):
                raise PermissionError("invalid credential")
            steve = self._db.execute(
                "SELECT active,expires,mode FROM steves WHERE id=?", (row["steve"],)
            ).fetchone()
            if not steve or not steve["active"] or _expired(steve["expires"]):
                raise PermissionError("inactive Steve")
            return _credential(row)

    def check_scope(self, token: str, scope: str, *, steve_id: str) -> bool:
        # Keep credential authentication and all binding/grant reads under one
        # in-process lock and SQLite read transaction. The RLock lets
        # authenticate() take its own lock and excludes same-instance writers;
        # the explicit transaction pins a database snapshot against commits
        # from another AuthzService connection/process.
        with self._lock:
            self._db.execute("BEGIN")
            try:
                try:
                    credential = self.authenticate(token)
                except PermissionError:
                    return False
                if scope not in SCOPES or scope not in credential.scopes:
                    return False
                if scope.startswith("admin:"):
                    # Admin scopes remain gated on a global admin principal,
                    # never on a Steve-bound credential.
                    return credential.steve_id == ""
                binding = _SCOPE_BINDING[scope]
                if binding == "peer":
                    # observe:shared is an opt-in peer grant. The originating
                    # Steve must remain owned by this credential; the target
                    # peer is authorized only by its explicit active grant.
                    own = self._db.execute(
                        "SELECT active,mode,owner,expires FROM steves WHERE id=?",
                        (credential.steve_id,),
                    ).fetchone()
                    grant = self._db.execute(
                        "SELECT 1 FROM peer_grants WHERE credential=? AND steve=?",
                        (credential.credential_id, steve_id),
                    ).fetchone()
                    row = self._db.execute(
                        "SELECT active,expires FROM steves WHERE id=?", (steve_id,)
                    ).fetchone()
                    return bool(
                        grant
                        and own
                        and own["active"]
                        and own["mode"] != "unclaimed"
                        and own["owner"] == credential.owner_id
                        and not _expired(own["expires"])
                        and row
                        and row["active"]
                        and not _expired(row["expires"])
                    )
                if binding == "service":
                    # events:read is not tied to a requested Steve, but its
                    # credential must remain bound to its active current owner.
                    own = self._db.execute(
                        "SELECT active,mode,owner,expires FROM steves WHERE id=?",
                        (credential.steve_id,),
                    ).fetchone()
                    return bool(
                        own
                        and own["active"]
                        and own["mode"] != "unclaimed"
                        and own["owner"] == credential.owner_id
                        and not _expired(own["expires"])
                    )
                if credential.steve_id != steve_id:
                    return False
                row = self._db.execute(
                    "SELECT active,mode,owner,expires FROM steves WHERE id=?", (steve_id,)
                ).fetchone()
                return bool(
                    row
                    and row["active"]
                    and row["mode"] != "unclaimed"
                    and row["owner"] is not None
                    and row["owner"] == credential.owner_id
                    and not _expired(row["expires"])
                )
            finally:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")

    def grant_peer_observation(
        self,
        admin_token: str,
        credential_id: str,
        peer_steve_id: str,
    ) -> None:
        """Allow a credential to observe a named peer Steve (opt-in).

        Requires a global admin holding admin:steves. The peer Steve must be
        registered and active; the credential must exist and not be revoked.
        Repeating an existing grant is idempotent. Grants are tied to the
        credential and can be listed or revoked by an administrator.
        """
        with self._tx() as db:
            if not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required to grant peer observation")
            administrator = db.execute(
                "SELECT principal FROM admins WHERE token_hash=?", (_hash(admin_token),)
            ).fetchone()
            credential = db.execute(
                "SELECT revoked FROM credentials WHERE id=?", (credential_id,)
            ).fetchone()
            if not credential or credential["revoked"]:
                raise PermissionError("unknown or revoked credential")
            peer = db.execute("SELECT active FROM steves WHERE id=?", (peer_steve_id,)).fetchone()
            if not peer or not peer["active"]:
                raise PermissionError("unknown or inactive peer Steve")
            cursor = db.execute(
                "INSERT INTO peer_grants(credential,steve,granted_by) VALUES(?,?,?) "
                "ON CONFLICT(credential,steve) DO NOTHING",
                (credential_id, peer_steve_id, administrator["principal"]),
            )
            if cursor.rowcount:
                db.execute(
                    "INSERT INTO peer_grant_audit "
                    "(holder_steve,peer_steve,event,actor,occurred_at,credential) "
                    "VALUES(?,?, 'granted',?,?,?)",
                    (
                        self._credential_holder(db, credential_id),
                        peer_steve_id,
                        administrator["principal"],
                        _dt(_now()),
                        credential_id,
                    ),
                )

    @staticmethod
    def _credential_holder(db: sqlite3.Connection, credential_id: str) -> str:
        row = db.execute("SELECT steve FROM credentials WHERE id=?", (credential_id,)).fetchone()
        if row is None:
            raise PermissionError("unknown credential")
        return str(row["steve"])

    def list_peer_observation_grants(self, admin_token: str, credential_id: str) -> tuple[str, ...]:
        """List the granted peer Steve IDs for a credential."""
        with self._lock:
            if not _has_admin(self._db, admin_token):
                raise PermissionError("admin:steves required to list peer grants")
            exists = self._db.execute(
                "SELECT 1 FROM credentials WHERE id=?", (credential_id,)
            ).fetchone()
            if not exists:
                raise PermissionError("unknown credential")
            rows = self._db.execute(
                "SELECT steve FROM peer_grants WHERE credential=? ORDER BY steve",
                (credential_id,),
            ).fetchall()
            return tuple(row["steve"] for row in rows)

    def revoke_peer_observation(
        self, admin_token: str, credential_id: str, peer_steve_id: str
    ) -> bool:
        """Revoke one explicitly granted peer observation relationship."""
        with self._tx() as db:
            if not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required to revoke peer grants")
            exists = db.execute("SELECT 1 FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if not exists:
                raise PermissionError("unknown credential")
            administrator = db.execute(
                "SELECT principal FROM admins WHERE token_hash=?", (_hash(admin_token),)
            ).fetchone()
            holder = self._credential_holder(db, credential_id)
            cursor = db.execute(
                "DELETE FROM peer_grants WHERE credential=? AND steve=?",
                (credential_id, peer_steve_id),
            )
            if cursor.rowcount:
                db.execute(
                    "INSERT INTO peer_grant_audit "
                    "(holder_steve,peer_steve,event,actor,occurred_at,credential) "
                    "VALUES(?,?, 'revoked',?,?,?)",
                    (
                        holder,
                        peer_steve_id,
                        administrator["principal"],
                        _dt(_now()),
                        credential_id,
                    ),
                )
            return bool(cursor.rowcount)

    def list_peer_observation_audit(
        self, admin_token: str, holder_steve_id: str
    ) -> tuple[PeerGrantAuditEntry, ...]:
        """List durable peer-grant history for a holder Steve.

        History is keyed by the stable holder Steve ID rather than credential ID,
        so it remains available after credential rotation.
        """
        with self._lock:
            if not _has_admin(self._db, admin_token):
                raise PermissionError("admin:steves required to read peer grant audit")
            rows = self._db.execute(
                "SELECT holder_steve,peer_steve,event,actor,occurred_at,credential,time_basis "
                "FROM peer_grant_audit WHERE holder_steve=? ORDER BY id",
                (holder_steve_id,),
            ).fetchall()
            return tuple(
                PeerGrantAuditEntry(
                    holder_steve_id=row["holder_steve"],
                    peer_steve_id=row["peer_steve"],
                    event=row["event"],
                    actor=row["actor"],
                    occurred_at=datetime.fromisoformat(row["occurred_at"]),
                    credential_id=row["credential"],
                    time_basis=row["time_basis"],
                )
                for row in rows
            )

    def revoke_credential(self, token: str, *, authority_token: str | None = None) -> None:
        with self._tx() as db:
            target = db.execute(
                "SELECT * FROM credentials WHERE token_hash=?", (_hash(token),)
            ).fetchone()
            admin_target = db.execute(
                "SELECT * FROM admins WHERE token_hash=?", (_hash(token),)
            ).fetchone()
            if not target and not admin_target:
                raise PermissionError("unknown credential")
            # Self revocation never needs an authority token. Revoking any other
            # credential requires a valid global admin holding admin:steves;
            # ownership or shared Steve binding is not authority to revoke a
            # sibling credential (RFC-0001 section 5 assigns revoke to admins).
            is_self = authority_token is None or authority_token == token
            if (
                authority_token is not None
                and authority_token != token
                and not _has_admin(db, authority_token, "admin:steves")
            ):
                raise PermissionError("admin:steves required to revoke another credential")
            if is_self:
                presented = admin_target if admin_target else target
                if presented and (presented["revoked"] or _expired(presented["expires"])):
                    raise PermissionError("invalid credential")
                if admin_target and "admin:steves" in admin_target["scopes"].splitlines():
                    valid_steves_admins = [
                        row
                        for row in db.execute(
                            "SELECT scopes,revoked,expires FROM admins"
                        ).fetchall()
                        if (
                            not row["revoked"]
                            and not _expired(row["expires"])
                            and "admin:steves" in row["scopes"].splitlines()
                        )
                    ]
                    if len(valid_steves_admins) <= 1:
                        raise PermissionError(
                            "refusing to revoke the last valid admin:steves "
                            "credential; create and confirm a replacement admin "
                            "credential first"
                        )
            if target:
                db.execute("UPDATE credentials SET revoked=1 WHERE id=?", (target["id"],))
            elif admin_target:
                db.execute("UPDATE admins SET revoked=1 WHERE id=?", (admin_target["id"],))

    def deactivate_steve(self, admin_token: str, steve_id: str) -> None:
        with self._tx() as db:
            if not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required")
            cursor = db.execute(
                "UPDATE steves SET active=0,session=NULL,session_credential=NULL,"
                "session_expires=NULL,"
                "generation=generation+1 WHERE id=?",
                (steve_id,),
            )
            if not cursor.rowcount:
                raise PermissionError("unknown Steve")

    def reactivate_steve(self, admin_token: str, steve_id: str) -> None:
        """Reactivate a registered Steve and advance its fencing generation.

        Reactivation does not renew an expired ownership lease. Its scopes
        remain unusable until an administrator transfers ownership.
        """
        if not steve_id or not steve_id.strip():
            raise ValueError("Steve ID must be non-empty")
        with self._tx() as db:
            if not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required")
            row = db.execute("SELECT active,expires FROM steves WHERE id=?", (steve_id,)).fetchone()
            if not row:
                raise PermissionError("unknown Steve")
            if row["active"]:
                return
            db.execute(
                "UPDATE steves SET active=1,session=NULL,session_credential=NULL,"
                "session_expires=NULL,generation=generation+1 WHERE id=?",
                (steve_id,),
            )

    def rotate_credential(self, token: str) -> CreatedCredential:
        replacement = "mab_" + secrets.token_urlsafe(32)
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM credentials WHERE token_hash=?", (_hash(token),)
            ).fetchone()
            if not row:
                # Admin credentials self-rotate: the presented token has the
                # right to replace itself, preserving admin_id, scopes and
                # expiry and revoking the old token exactly once.
                admin_row = db.execute(
                    "SELECT * FROM admins WHERE token_hash=?", (_hash(token),)
                ).fetchone()
                if not admin_row or admin_row["revoked"] or _expired(admin_row["expires"]):
                    raise PermissionError("invalid credential")
                db.execute("UPDATE admins SET revoked=1 WHERE id=?", (admin_row["id"],))
                new_admin_id = str(uuid4())
                db.execute(
                    "INSERT INTO admins(id,token_hash,principal,scopes,expires) VALUES(?,?,?,?,?)",
                    (
                        new_admin_id,
                        _hash(replacement),
                        admin_row["principal"],
                        admin_row["scopes"],
                        admin_row["expires"],
                    ),
                )
                return CreatedCredential(
                    new_admin_id,
                    "",
                    admin_row["principal"],
                    frozenset(admin_row["scopes"].splitlines()),
                    datetime.fromisoformat(admin_row["expires"]) if admin_row["expires"] else None,
                    replacement,
                )
            if row["revoked"] or _expired(row["expires"]):
                raise PermissionError("invalid credential")
            steve = db.execute("SELECT active FROM steves WHERE id=?", (row["steve"],)).fetchone()
            if not steve or not steve["active"]:
                raise PermissionError("inactive Steve")
            db.execute("UPDATE credentials SET revoked=1 WHERE id=?", (row["id"],))
            new_id = str(uuid4())
            db.execute(
                "INSERT INTO credentials(id,token_hash,steve,owner,scopes,expires) "
                "VALUES(?,?,?,?,?,?)",
                (
                    new_id,
                    _hash(replacement),
                    row["steve"],
                    row["owner"],
                    row["scopes"],
                    row["expires"],
                ),
            )
            # Grants are credential-bound. Move them inside the same write
            # transaction as credential replacement so rotation preserves the
            # holder's explicitly approved peer visibility without a gap.
            db.execute(
                "UPDATE peer_grants SET credential=? WHERE credential=?",
                (new_id, row["id"]),
            )
            return CreatedCredential(
                new_id,
                row["steve"],
                row["owner"],
                frozenset(row["scopes"].splitlines()),
                datetime.fromisoformat(row["expires"]) if row["expires"] else None,
                replacement,
            )

    def acquire_session(
        self, token: str, steve_id: str, *, owner_id: str, lease_expires_at: datetime | None = None
    ) -> ControllerSession:
        credential = self.authenticate(token)
        if credential.steve_id != steve_id or "steve:control" not in credential.scopes:
            raise PermissionError("control scope or Steve binding required")
        with self._tx() as db:
            row = db.execute("SELECT * FROM steves WHERE id=?", (steve_id,)).fetchone()
            current_credential = db.execute(
                "SELECT * FROM credentials WHERE id=?", (credential.credential_id,)
            ).fetchone()
            if (
                not current_credential
                or current_credential["revoked"]
                or _expired(current_credential["expires"])
                or current_credential["steve"] != steve_id
                or current_credential["owner"] != owner_id
                or "steve:control" not in current_credential["scopes"].splitlines()
            ):
                raise PermissionError("controller credential is no longer valid")
            if (
                not row
                or not row["active"]
                or row["owner"] != owner_id
                or owner_id != credential.owner_id
            ):
                raise PermissionError("Steve owner binding invalid")
            if row["mode"] == "unclaimed" or _expired(row["expires"]):
                raise PermissionError("Steve ownership expired or unclaimed")
            session_expiry = _dt(lease_expires_at)
            if session_expiry is not None and _expired(session_expiry):
                raise ValueError("controller session expiry must be in the future")
            if row["mode"] == "leased" and (
                session_expiry is None
                or datetime.fromisoformat(session_expiry) > datetime.fromisoformat(row["expires"])
            ):
                raise ValueError(
                    "leased ownership requires a session lease within ownership expiry"
                )
            if row["session"]:
                active_credential = db.execute(
                    "SELECT revoked,expires FROM credentials WHERE id=?",
                    (row["session_credential"],),
                ).fetchone()
                if (
                    active_credential
                    and not active_credential["revoked"]
                    and not _expired(active_credential["expires"])
                    and not _expired(row["session_expires"])
                ):
                    raise PermissionError(
                        "controller session already active; after confirming the prior "
                        "controller process has stopped, an admin can deactivate and "
                        "reactivate this Steve to clear a stale session"
                    )
            generation = row["generation"] + 1
            sid = str(uuid4())
            db.execute(
                "UPDATE steves SET generation=?,session=?,session_credential=?,"
                "session_expires=? WHERE id=?",
                (generation, sid, credential.credential_id, session_expiry, steve_id),
            )
            return ControllerSession(
                sid, steve_id, credential.credential_id, owner_id, generation, lease_expires_at
            )

    def claim_ownership(
        self,
        token: str,
        steve_id: str,
        *,
        ownership_mode: str = "persistent",
        expires_at: datetime | None = None,
    ) -> None:
        """Atomically claim an unclaimed Steve using its scoped controller key."""
        credential = self.authenticate(token)
        if credential.steve_id != steve_id or "steve:control" not in credential.scopes:
            raise PermissionError("control scope or Steve binding required")
        expiry = _dt(expires_at)
        if ownership_mode not in {"persistent", "leased"}:
            raise ValueError("claim mode must be persistent or leased")
        if ownership_mode == "leased" and (expiry is None or _expired(expiry)):
            raise ValueError("leased claim requires a future expiry")
        if ownership_mode == "persistent" and expiry is not None:
            raise ValueError("persistent claim cannot expire")
        with self._tx() as db:
            row = db.execute("SELECT * FROM steves WHERE id=?", (steve_id,)).fetchone()
            current_credential = db.execute(
                "SELECT * FROM credentials WHERE id=?", (credential.credential_id,)
            ).fetchone()
            if (
                not row
                or not row["active"]
                or row["mode"] != "unclaimed"
                or row["owner"] is not None
                or row["session"] is not None
                or not current_credential
                or current_credential["revoked"]
                or _expired(current_credential["expires"])
            ):
                raise PermissionError("Steve is unavailable or already claimed")
            db.execute(
                "UPDATE steves SET mode=?,owner=?,expires=? WHERE id=? AND mode='unclaimed'",
                (ownership_mode, credential.owner_id, expiry, steve_id),
            )

    def renew_ownership(
        self,
        token: str,
        steve_id: str,
        *,
        owner_id: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Bounded atomic renewal of an owned Steve's lease (RFC-0001 section 5).

        The current owner extends its own lease forward without reassignment.
        The new expiry must be timezone aware, strictly in the future, and
        strictly later than the persisted one, so renewal can never shorten a
        lease or move it backward. A lease that has already expired cannot be
        renewed; post-expiry recovery stays with an administrator transfer.
        ``persistent`` ownership has no expiry and is renewed without one, which
        re-validates the ownership binding and advances the fencing generation.
        """
        _require_identity(owner_id, "owner ID")
        credential = self.authenticate(token)
        if credential.steve_id != steve_id or "steve:control" not in credential.scopes:
            raise PermissionError("control scope or Steve binding required")
        expiry = _dt(expires_at)
        if expiry is not None and _expired(expiry):
            raise ValueError("renewed ownership expiry must be in the future")
        with self._tx() as db:
            row = db.execute("SELECT * FROM steves WHERE id=?", (steve_id,)).fetchone()
            current_credential = db.execute(
                "SELECT * FROM credentials WHERE id=?", (credential.credential_id,)
            ).fetchone()
            if (
                not current_credential
                or current_credential["revoked"]
                or _expired(current_credential["expires"])
                or current_credential["steve"] != steve_id
                or current_credential["owner"] != owner_id
                or "steve:control" not in current_credential["scopes"].splitlines()
            ):
                raise PermissionError("renewing credential is no longer valid")
            if (
                not row
                or not row["active"]
                or row["mode"] == "unclaimed"
                or row["owner"] != owner_id
                or owner_id != credential.owner_id
            ):
                raise PermissionError("Steve owner binding invalid")
            if _expired(row["expires"]):
                raise PermissionError("ownership already expired; admin transfer required")
            if row["mode"] == "persistent":
                if expiry is not None:
                    raise ValueError("persistent ownership has no lease to renew")
            else:
                if expiry is None:
                    raise ValueError("leased ownership renewal requires a future expiry")
                if row["expires"] is None:
                    raise ValueError("renewal must extend the lease forward")
                if datetime.fromisoformat(expiry) <= datetime.fromisoformat(row["expires"]):
                    raise ValueError("renewal must extend the lease forward")
            db.execute(
                "UPDATE steves SET expires=?,session=NULL,session_credential=NULL,"
                "session_expires=NULL,generation=generation+1 WHERE id=?",
                (expiry, steve_id),
            )

    def validate_session(
        self,
        session: ControllerSession,
        *,
        required_admin_scope: str | None = None,
        admin_token: str | None = None,
    ) -> bool:
        with self._lock:
            owns_transaction = not self._db.in_transaction
            if owns_transaction:
                self._db.execute("BEGIN")
            try:
                row = self._db.execute(
                    "SELECT s.active,s.session,s.session_credential,s.generation,"
                    "s.owner AS steve_owner,"
                    "s.mode,s.expires,s.session_expires,c.steve AS credential_steve,"
                    "c.owner AS credential_owner,c.revoked AS credential_revoked,"
                    "c.expires AS credential_expires,c.scopes AS credential_scopes "
                    "FROM steves AS s JOIN credentials AS c ON c.id=? WHERE s.id=?",
                    (session.credential_id, session.steve_id),
                ).fetchone()
                valid_session = bool(
                    row
                    and row["active"]
                    and row["session"] == session.session_id
                    and row["session_credential"] == session.credential_id
                    and row["generation"] == session.generation
                    and row["steve_owner"] == session.owner_id
                    and row["mode"] != "unclaimed"
                    and not _expired(row["expires"])
                    and row["session_expires"] == _dt(session.expires_at)
                    and not _expired(row["session_expires"])
                    and (row["mode"] != "leased" or row["session_expires"] is not None)
                    and (
                        row["mode"] != "leased"
                        or datetime.fromisoformat(row["session_expires"])
                        <= datetime.fromisoformat(row["expires"])
                    )
                    and row["credential_steve"] == session.steve_id
                    and row["credential_owner"] == session.owner_id
                    and not row["credential_revoked"]
                    and not _expired(row["credential_expires"])
                    and "steve:control" in row["credential_scopes"].splitlines()
                )
                if not valid_session:
                    return False
                if required_admin_scope is None:
                    return True
                if admin_token is None:
                    return False
                admin = self._db.execute(
                    "SELECT scopes,revoked,expires FROM admins WHERE token_hash=?",
                    (_hash(admin_token),),
                ).fetchone()
                return bool(
                    admin
                    and not admin["revoked"]
                    and not _expired(admin["expires"])
                    and required_admin_scope in admin["scopes"].splitlines()
                )
            finally:
                if owns_transaction and self._db.in_transaction:
                    self._db.execute("ROLLBACK")

    def try_validate_session(
        self,
        session: ControllerSession,
        *,
        required_admin_scope: str | None = None,
        admin_token: str | None = None,
    ) -> bool | None:
        """Validate without waiting for another local Authz operation.

        ``None`` means the service lock is busy. Callers on an asyncio event
        loop can treat that as an authorization failure instead of blocking
        unrelated safety and shutdown tasks.
        """
        if not self._lock.acquire(blocking=False):
            return None
        previous_timeout: int | None = None
        try:
            previous_timeout = int(self._db.execute("PRAGMA busy_timeout").fetchone()[0])
            self._db.execute("PRAGMA busy_timeout=0")
            try:
                return self.validate_session(
                    session,
                    required_admin_scope=required_admin_scope,
                    admin_token=admin_token,
                )
            except sqlite3.Error:
                return None
        finally:
            if previous_timeout is not None:
                with suppress(sqlite3.Error):
                    self._db.execute(f"PRAGMA busy_timeout={previous_timeout}")
            self._lock.release()

    def renew_session(
        self,
        session: ControllerSession,
        *,
        lease_seconds: float,
    ) -> ControllerSession:
        """Extend a still-valid finite session without changing its fencing ID.

        Renewal is serialized with revocation and ownership changes. Expired,
        revoked, transferred, or otherwise stale sessions cannot be revived.
        Leased Steve ownership caps the renewed session expiry.
        """
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("controller session renewal lease must be finite and positive")

        with self._try_tx() as db:
            if not self.validate_session(session):
                raise PermissionError("controller session is no longer valid")
            if session.expires_at is None:
                raise ValueError("only finite controller sessions can be renewed")
            now = datetime.now(UTC)
            if session.expires_at <= now:
                raise PermissionError("controller session expired before renewal")

            row = db.execute(
                "SELECT mode,expires,session_expires FROM steves WHERE id=?",
                (session.steve_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("controller Steve is unavailable")

            renewed_expiry = now + timedelta(seconds=lease_seconds)
            if row["mode"] == "leased" and row["expires"] is not None:
                ownership_expiry = datetime.fromisoformat(row["expires"])
                renewed_expiry = min(renewed_expiry, ownership_expiry)
            if renewed_expiry <= session.expires_at:
                # The current lease may already reach the ownership deadline.
                # Preserve it as a valid no-op; the heartbeat will continue
                # checking until its actual expiry instead of suspending early.
                return session

            stored_expiry = _dt(renewed_expiry)
            updated = db.execute(
                "UPDATE steves SET session_expires=? WHERE id=? AND session=? "
                "AND session_credential=? AND generation=? AND session_expires=?",
                (
                    stored_expiry,
                    session.steve_id,
                    session.session_id,
                    session.credential_id,
                    session.generation,
                    row["session_expires"],
                ),
            )
            if updated.rowcount != 1:
                raise PermissionError("controller session changed during renewal")

            return ControllerSession(
                session.session_id,
                session.steve_id,
                session.credential_id,
                session.owner_id,
                session.generation,
                renewed_expiry,
            )

    def release_session(self, session: ControllerSession) -> None:
        with self._tx() as db:
            if not self.validate_session(session):
                raise PermissionError("stale controller session")
            db.execute(
                "UPDATE steves SET session=NULL,session_credential=NULL,"
                "session_expires=NULL WHERE id=?",
                (session.steve_id,),
            )

    def transfer_ownership(
        self,
        admin_token: str,
        steve_id: str,
        *,
        new_owner_id: str,
        ownership_mode: str = "persistent",
        expires_at: datetime | None = None,
    ) -> None:
        _require_identity(new_owner_id, "owner ID")
        expiry = _dt(expires_at)
        if ownership_mode not in {"persistent", "leased"} or (
            ownership_mode == "leased" and not expiry
        ):
            raise ValueError("invalid transfer ownership mode/expiry")
        if ownership_mode == "leased" and _expired(expiry):
            raise ValueError("leased transfer expiry must be in the future")
        if ownership_mode == "persistent" and expiry is not None:
            raise ValueError("persistent transfer cannot expire")
        with self._tx() as db:
            if not _has_admin(db, admin_token):
                raise PermissionError("admin:steves required")
            row = db.execute(
                "SELECT id FROM steves WHERE id=? AND active=1", (steve_id,)
            ).fetchone()
            if not row:
                raise PermissionError("inactive or unknown Steve")
            db.execute(
                "UPDATE steves SET owner=?,mode=?,expires=?,session=NULL,session_credential=NULL,"
                "session_expires=NULL,"
                "generation=generation+1 WHERE id=?",
                (new_owner_id, ownership_mode, expiry, steve_id),
            )

    def action_policy(
        self,
        session: ControllerSession | Callable[[], ControllerSession | None],
        *,
        admin_token: str | None = None,
        blocking: bool = False,
    ):
        """Return an ActionService policy bound to a controller session.

        By default the policy validates without waiting for the service lock
        (``try_validate_session``) and denies when the lock is busy, so it is
        safe to call on an event loop. With ``blocking=True`` it waits for
        the lock instead, so concurrent requests are not denied by contention;
        call that variant from a worker thread, never on the event loop.
        """
        from .action_service import (
            _PRIVILEGED_AUTHORIZATION_CLASSES,
            PolicyDecision,
            classify_action,
        )

        def policy(request: ActionRequest) -> PolicyDecision:
            active_session = session() if callable(session) else session
            privileged = classify_action(request.action) in _PRIVILEGED_AUTHORIZATION_CLASSES
            authorized = bool(
                active_session
                and (self.validate_session if blocking else self.try_validate_session)(
                    active_session,
                    required_admin_scope="admin:world" if privileged else None,
                    admin_token=admin_token,
                )
                is True
            )
            return PolicyDecision(
                authorized,
                (
                    "admin:world required for privileged backend action"
                    if privileged and not authorized
                    else "controller session invalid or fenced"
                ),
                allow_privileged=authorized if privileged else False,
                identity_scope=(
                    (
                        "controller",
                        active_session.steve_id,
                        active_session.owner_id,
                        active_session.credential_id,
                        active_session.session_id,
                        str(active_session.generation),
                    )
                    if active_session is not None
                    else ("controller", "invalid")
                ),
            )

        return policy


@dataclass(frozen=True)
class SteveRegistration:
    steve_id: str
    ownership_mode: str
    owner_id: str | None
    ownership_expires_at: datetime | None
    active: bool


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")


def _expired(value: str | None) -> bool:
    return value is not None and datetime.fromisoformat(value) <= _now()


def _credential(row: sqlite3.Row) -> Credential:
    return Credential(
        row["id"],
        row["steve"],
        row["owner"],
        frozenset(row["scopes"].splitlines()),
        datetime.fromisoformat(row["expires"]) if row["expires"] else None,
    )


def _has_admin(db: sqlite3.Connection, token: str, required_scope: str = "admin:steves") -> bool:
    row = db.execute(
        "SELECT scopes,revoked,expires FROM admins WHERE token_hash=?",
        (_hash(token),),
    ).fetchone()
    return bool(
        row
        and not row["revoked"]
        and not _expired(row["expires"])
        and required_scope in row["scopes"].splitlines()
    )


def _has_valid_admin_steves(db: sqlite3.Connection) -> bool:
    """True when a valid admin:steves credential preserves recovery authority."""
    for row in db.execute("SELECT scopes,revoked,expires FROM admins").fetchall():
        if (
            not row["revoked"]
            and not _expired(row["expires"])
            and "admin:steves" in row["scopes"].splitlines()
        ):
            return True
    return False
