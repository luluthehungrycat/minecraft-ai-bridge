"""Administrator-configured, durable outbound delivery for journal events.

This is a local service: subscription configuration is supplied at construction
and there is deliberately no subscription-management endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import httpx

from .event_journal import EventCursorExpiredError, EventJournal

__all__ = [
    "WebhookDelivery",
    "WebhookError",
    "HttpxPinnedTransport",
    "PinnedWebhookTransport",
    "WebhookService",
    "WebhookSubscription",
]

_DELIVERY_NAMESPACE = UUID("a5870ad5-4dab-4a1a-9684-88da266770d3")
_DNS_WORKER_LIMIT = 1
_DNS_CAPACITY = threading.BoundedSemaphore(_DNS_WORKER_LIMIT)
_MAX_PINNED_ADDRESSES = 16
_WEBHOOK_TIMEOUT_SECONDS = 10.0
_HTTP_CAPACITY = threading.BoundedSemaphore(4)
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_UNSAFE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


def _finite_positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and value > 0
    except OverflowError:
        return False


class WebhookError(ValueError):
    """Invalid webhook configuration or persisted delivery state."""


class PinnedWebhookTransport(Protocol):
    """Transport operation that connects only to prevalidated IP addresses."""

    async def send_webhook_pinned(
        self,
        *,
        url: str,
        addresses: tuple[str, ...],
        content: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> int: ...


class HttpxPinnedTransport:
    """HTTPX transport that pins TCP to supplied IPs and keeps HTTPS identity."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._transport_factory = transport_factory

    async def send_webhook_pinned(
        self,
        *,
        url: str,
        addresses: tuple[str, ...],
        content: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> int:
        if not _finite_positive_number(timeout):
            raise WebhookError("pinned request timeout must be finite and positive")
        if not addresses or len(addresses) > _MAX_PINNED_ADDRESSES:
            raise WebhookError("pinned address set must contain 1-16 addresses")
        try:
            normalized: tuple[str, ...] = tuple(
                dict.fromkeys(str(ipaddress.ip_address(address)) for address in addresses)
            )
        except ValueError as exc:
            raise WebhookError("pinned address set contains an invalid IP") from exc
        if len(normalized) != len(addresses) or any(_unsafe_ip(address) for address in normalized):
            raise WebhookError("pinned address set contains duplicates or unsafe IPs")
        try:
            original = httpx.URL(url)
        except (TypeError, ValueError) as exc:
            raise WebhookError("invalid pinned webhook URL") from exc
        if (
            original.scheme != "https"
            or not original.host
            or original.username
            or original.password
            or original.fragment
            or original.port not in (None, 443)
        ):
            raise WebhookError("pinned URL must be credential-free HTTPS on port 443")
        hostname = original.host
        if not hostname:
            raise WebhookError("pinned URL has no hostname")
        timeout_value = float(timeout)
        request_timeout = httpx.Timeout(
            timeout_value,
            connect=timeout_value,
            read=timeout_value,
            write=timeout_value,
            pool=timeout_value,
        )
        last_error: httpx.HTTPError | None = None
        if not _HTTP_CAPACITY.acquire(blocking=False):
            raise WebhookError("pinned webhook transport capacity is exhausted")
        try:
            if self._transport_factory:
                transport = self._transport_factory()
            else:
                transport = httpx.AsyncHTTPTransport(
                    trust_env=False,
                    http2=False,
                    retries=0,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                )
            async with asyncio.timeout(timeout_value):
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=request_timeout,
                ) as client:
                    for address in normalized:
                        ip = ipaddress.ip_address(address)
                        host_for_url = f"[{ip}]" if ip.version == 6 else str(ip)
                        pinned_url = original.copy_with(host=host_for_url, port=443)
                        request_headers = httpx.Headers(headers)
                        request_headers["Host"] = hostname
                        request = client.build_request(
                            "POST",
                            pinned_url,
                            content=content,
                            headers=request_headers,
                            timeout=request_timeout,
                            extensions={"sni_hostname": hostname},
                        )
                        try:
                            response = await client.send(
                                request, stream=True, follow_redirects=False
                            )
                        except httpx.HTTPError as exc:
                            last_error = exc
                            continue
                        try:
                            return response.status_code
                        finally:
                            await response.aclose()
        except TimeoutError as exc:
            raise WebhookError("pinned webhook request timed out") from exc
        finally:
            _HTTP_CAPACITY.release()
        if last_error is not None:
            raise last_error
        raise WebhookError("pinned webhook request did not produce a response")


@dataclass(frozen=True)
class WebhookSubscription:
    """Trusted administrator-supplied endpoint and delivery policy."""

    subscription_id: str
    url: str
    secret: bytes
    allowed_hosts: frozenset[str]
    events_per_minute: int = 60
    max_attempts: int = 8
    initial_retry_seconds: float = 1.0
    max_retry_seconds: float = 300.0
    event_types: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if (
            not self.subscription_id
            or len(self.subscription_id) > 100
            or any(c not in _ID_CHARS for c in self.subscription_id)
        ):
            raise WebhookError("subscription_id must use 1-100 safe ASCII characters")
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise WebhookError("webhook secret must contain at least 32 bytes")
        if not self.allowed_hosts:
            raise WebhookError("an explicit host allowlist is required")
        if (
            isinstance(self.events_per_minute, bool)
            or not isinstance(self.events_per_minute, int)
            or not 1 <= self.events_per_minute <= 10_000
            or isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 100
        ):
            raise WebhookError("rate limit must be 1-10000 and max_attempts 1-100")
        retry_intervals = (self.initial_retry_seconds, self.max_retry_seconds)
        if any(not _finite_positive_number(value) for value in retry_intervals):
            raise WebhookError("retry intervals must be finite positive numbers")
        if self.max_retry_seconds < self.initial_retry_seconds:
            raise WebhookError("retry interval cap must be >= positive initial interval")
        _validate_url(self.url, self.allowed_hosts)


@dataclass(frozen=True)
class WebhookDelivery:
    subscription_id: str
    delivery_id: str
    event_id: str
    cursor: int
    state: str
    attempts: int
    next_attempt_at: float
    last_status: int | None
    last_error: str | None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", errors="strict")


def _unsafe_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not address.is_global or any(address in network for network in _UNSAFE_NETWORKS)


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise WebhookError("invalid webhook URL") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise WebhookError("webhook URL must be HTTPS, credential-free, fragment-free, port 443")
    host = hostname.rstrip(".").lower()
    if host not in {item.rstrip(".").lower() for item in allowed_hosts}:
        raise WebhookError("webhook host is not administrator-allowlisted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and _unsafe_ip(str(address)):
        raise WebhookError("unsafe literal webhook IP")
    return host


def _check_resolved_addresses(host: str) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebhookError("webhook host DNS resolution failed") from exc
    raw_addresses = {answer[4][0] for answer in answers}
    if any(not isinstance(address, str) for address in raw_addresses):
        raise WebhookError("webhook DNS returned an invalid address")
    addresses = tuple(sorted(address for address in raw_addresses if isinstance(address, str)))
    if (
        not addresses
        or len(addresses) > _MAX_PINNED_ADDRESSES
        or any(_unsafe_ip(address) for address in addresses)
    ):
        raise WebhookError("webhook host resolves to an unsafe IP address")
    return addresses


async def _resolve_addresses(host: str, *, timeout: float) -> tuple[str, ...]:
    """Resolve off-loop with a process-wide worker and submission bound.

    Admission is nonblocking and a slot remains held until the resolver exits,
    even when its async waiter times out or is cancelled. The worker is a
    daemon because the platform resolver itself cannot be interrupted.
    """
    if not _DNS_CAPACITY.acquire(blocking=False):
        raise WebhookError("webhook DNS resolver capacity is exhausted")
    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[str, ...]] = loop.create_future()

    def resolve() -> None:
        resolved: tuple[str, ...] | None = None
        error: Exception | None = None
        try:
            resolved = _check_resolved_addresses(host)
        except Exception as exc:
            error = exc
        finally:
            # The blocking resolver has returned before admission is reopened.
            _DNS_CAPACITY.release()
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(_finish_resolution, result, resolved, error)

    try:
        threading.Thread(target=resolve, name="webhook-dns", daemon=True).start()
    except RuntimeError as exc:
        _DNS_CAPACITY.release()
        raise WebhookError("webhook DNS worker could not be started") from exc
    try:
        return await asyncio.wait_for(result, timeout)
    except TimeoutError as exc:
        raise WebhookError("webhook DNS resolution timed out") from exc


def _finish_resolution(
    future: asyncio.Future[tuple[str, ...]],
    result: tuple[str, ...] | None,
    error: Exception | None,
) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        assert result is not None
        future.set_result(result)


class WebhookService:
    """SQLite-backed, per-subscription at-least-once HTTP delivery worker.

    ``run_once`` sends at most one currently due item per subscription. By
    default, delivery uses :class:`HttpxPinnedTransport`; tests or deployments
    may inject another implementation of :class:`PinnedWebhookTransport`.
    """

    def __init__(
        self,
        journal: EventJournal,
        db_path: str,
        *,
        subscriptions: tuple[WebhookSubscription, ...] | list[WebhookSubscription],
        pinned_transport: PinnedWebhookTransport | None = None,
        max_body_bytes: int = 256_000,
        dns_timeout_seconds: float = 2.0,
        clock=time.time,
    ) -> None:
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise WebhookError("max_body_bytes must be an integer")
        if not 1 <= max_body_bytes <= 16_000_000:
            raise WebhookError("max_body_bytes must be 1-16000000")
        if not _finite_positive_number(dns_timeout_seconds):
            raise WebhookError("dns_timeout_seconds must be finite and positive")
        ids = [item.subscription_id for item in subscriptions]
        if len(set(ids)) != len(ids):
            raise WebhookError("subscription IDs must be unique")
        self.journal = journal
        self._pinned_transport = (
            pinned_transport if pinned_transport is not None else HttpxPinnedTransport()
        )
        self.max_body_bytes = max_body_bytes
        self.dns_timeout_seconds = float(dns_timeout_seconds)
        self._clock = clock
        self._subscriptions = {item.subscription_id: item for item in subscriptions}
        self._run_lock = asyncio.Lock()
        self._db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                subscription_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
                event_id TEXT NOT NULL, cursor INTEGER NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
                last_status INTEGER, last_error TEXT,
                PRIMARY KEY(subscription_id, delivery_id));
            CREATE INDEX IF NOT EXISTS webhook_due ON webhook_deliveries
                (subscription_id, state, next_attempt_at, cursor);
            CREATE TABLE IF NOT EXISTS webhook_rate (
                subscription_id TEXT NOT NULL, sent_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS webhook_rate_window
                ON webhook_rate(subscription_id, sent_at);
        """)
        for subscription in subscriptions:
            _validate_url(subscription.url, subscription.allowed_hosts)

    def close(self) -> None:
        self._db.close()

    def _delivery_id(self, subscription_id: str, event_id: str) -> str:
        return str(uuid5(_DELIVERY_NAMESPACE, f"{subscription_id}:{event_id}"))

    def _ensure_pending(self, subscription: WebhookSubscription) -> None:
        consumer = f"webhook-{subscription.subscription_id}"
        try:
            records = self.journal.get_events(consumer, limit=1)
        except EventCursorExpiredError:
            self._reset_expired_consumer(subscription, consumer)
            records = self.journal.get_events(consumer, limit=1)
        for record in records:
            if (
                subscription.event_types is not None
                and record.event.type not in subscription.event_types
            ):
                # This is the journal head, so skipping it cannot cross an older
                # pending delivery.
                self._ack_cursor(subscription, consumer, record.cursor)
                return
            delivery_id = self._delivery_id(subscription.subscription_id, record.event.event_id)
            self._db.execute(
                """INSERT OR IGNORE INTO webhook_deliveries
                (subscription_id, delivery_id, event_id, cursor, state, next_attempt_at)
                VALUES (?, ?, ?, ?, 'pending', ?)""",
                (
                    subscription.subscription_id,
                    delivery_id,
                    record.event.event_id,
                    record.cursor,
                    self._clock(),
                ),
            )
            return

    def _reset_expired_consumer(self, subscription: WebhookSubscription, consumer: str) -> int:
        """Skip pruned history and dead-letter its locally pending payloads."""
        self.journal.reset_consumer(consumer)
        retained_floor = self.journal.consumer_ack_cursor(consumer)
        self._db.execute(
            "UPDATE webhook_deliveries SET state='dead_letter', attempts=attempts+1, "
            "last_error='journal retention expired before delivery' "
            "WHERE subscription_id=? AND state='pending' AND cursor<=?",
            (subscription.subscription_id, retained_floor),
        )
        return retained_floor

    def _ack_cursor(self, subscription: WebhookSubscription, consumer: str, cursor: int) -> None:
        try:
            self.journal.ack_event(consumer, cursor)
        except EventCursorExpiredError:
            # The delivery state is already terminal; advance to the retained
            # floor instead of replaying an accepted side effect.
            self._reset_expired_consumer(subscription, consumer)

    def _reconcile_terminal(self, subscription: WebhookSubscription) -> None:
        """Replay local terminal state into the separate journal cursor."""
        consumer = f"webhook-{subscription.subscription_id}"
        while True:
            rows = self._db.execute(
                "SELECT cursor, state FROM webhook_deliveries "
                "WHERE subscription_id=? ORDER BY cursor",
                (subscription.subscription_id,),
            ).fetchall()
            acked_cursor = self.journal.consumer_ack_cursor(consumer)
            expired_reset = False
            for row in rows:
                cursor = int(row["cursor"])
                if cursor <= acked_cursor:
                    continue
                if row["state"] == "pending":
                    # Never acknowledge past an earlier uncompleted delivery.
                    return
                if row["state"] not in {"delivered", "dead_letter"}:
                    raise WebhookError(f"unknown persisted delivery state {row['state']!r}")
                try:
                    self.journal.ack_event(consumer, cursor)
                except EventCursorExpiredError:
                    # Reset advances to the journal's retained floor. Pending
                    # payloads behind that floor are now irrecoverably lost.
                    self._reset_expired_consumer(subscription, consumer)
                    expired_reset = True
                    break
                acked_cursor = cursor
            if not expired_reset:
                return

    async def _send_pinned(
        self,
        *,
        url: str,
        addresses: tuple[str, ...],
        content: bytes,
        headers: dict[str, str],
    ) -> int:
        sender = getattr(self._pinned_transport, "send_webhook_pinned", None)
        if sender is None:
            raise WebhookError(
                "HTTP transport has no pinned webhook request operation; request was not sent"
            )
        result = sender(
            url=url,
            addresses=addresses,
            content=content,
            headers=headers,
            timeout=10.0,
        )
        if not hasattr(result, "__await__"):
            raise WebhookError("pinned webhook transport operation must be async")
        status_code = await result
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise WebhookError("pinned webhook transport must return an HTTP status integer")
        return status_code

    def _record(self, row: sqlite3.Row) -> WebhookDelivery:
        return WebhookDelivery(
            row["subscription_id"],
            row["delivery_id"],
            row["event_id"],
            row["cursor"],
            row["state"],
            row["attempts"],
            row["next_attempt_at"],
            row["last_status"],
            row["last_error"],
        )

    def deliveries(
        self, subscription_id: str | None = None, *, state: str | None = None
    ) -> list[WebhookDelivery]:
        clauses, params = [], []
        if subscription_id is not None:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        if state is not None:
            if state not in {"pending", "delivered", "dead_letter"}:
                raise WebhookError("unknown delivery state")
            clauses.append("state = ?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._db.execute(
            "SELECT * FROM webhook_deliveries" + where + " ORDER BY cursor, subscription_id", params
        ).fetchall()
        return [self._record(row) for row in rows]

    def dead_letters(self, subscription_id: str | None = None) -> list[WebhookDelivery]:
        return self.deliveries(subscription_id, state="dead_letter")

    async def run_once(self) -> int:
        """Serialize a delivery pass and return the number of HTTP attempts."""
        async with self._run_lock:
            return await self._run_once()

    async def _run_once(self) -> int:
        """Attempt due deliveries while this service owns its pass lock."""
        attempts_made = 0
        for subscription in self._subscriptions.values():
            consumer = f"webhook-{subscription.subscription_id}"
            self._reconcile_terminal(subscription)
            self._ensure_pending(subscription)
            now = self._clock()
            row = self._db.execute(
                """SELECT * FROM webhook_deliveries
                WHERE subscription_id = ? AND state = 'pending'
                ORDER BY cursor LIMIT 1""",
                (subscription.subscription_id,),
            ).fetchone()
            if row is None:
                continue
            delivery = self._record(row)
            if delivery.next_attempt_at > now:
                continue
            records = self.journal.get_events(consumer, limit=1)
            event_record = next((item for item in records if item.cursor == delivery.cursor), None)
            if event_record is None:
                # Retention may have expired this subscription's cursor; fail closed.
                self._db.execute(
                    "UPDATE webhook_deliveries SET state='dead_letter', last_error=? "
                    "WHERE subscription_id=? AND delivery_id=?",
                    (
                        "journal event expired before delivery",
                        subscription.subscription_id,
                        delivery.delivery_id,
                    ),
                )
                self._ack_cursor(subscription, consumer, delivery.cursor)
                continue
            body = _canonical_json(event_record.event.model_dump(mode="json"))
            if len(body) > self.max_body_bytes:
                self._terminal(
                    subscription, delivery, "event body exceeds configured maximum", None
                )
                continue
            if not callable(getattr(self._pinned_transport, "send_webhook_pinned", None)):
                self._retry_or_dead(
                    subscription,
                    delivery,
                    "HTTP transport has no pinned webhook request operation; request was not sent",
                    None,
                )
                continue
            self._db.execute("DELETE FROM webhook_rate WHERE sent_at <= ?", (now - 60.0,))
            count = self._db.execute(
                "SELECT COUNT(*) FROM webhook_rate WHERE subscription_id=?",
                (subscription.subscription_id,),
            ).fetchone()[0]
            if count >= subscription.events_per_minute:
                continue
            host = _validate_url(subscription.url, subscription.allowed_hosts)
            try:
                addresses = await _resolve_addresses(host, timeout=self.dns_timeout_seconds)
            except WebhookError as exc:
                self._retry_or_dead(subscription, delivery, str(exc)[:500], None)
                continue
            timestamp = str(int(now))
            signature = hmac.new(
                subscription.secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256
            ).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Delivery-Id": delivery.delivery_id,
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": f"sha256={signature}",
            }
            attempts_made += 1
            self._db.execute(
                "INSERT INTO webhook_rate(subscription_id,sent_at) VALUES (?,?)",
                (subscription.subscription_id, now),
            )
            try:
                status_code = await self._send_pinned(
                    url=subscription.url,
                    addresses=addresses,
                    content=body,
                    headers=headers,
                )
                if 200 <= status_code < 300:
                    self._db.execute(
                        "UPDATE webhook_deliveries SET state='delivered', attempts=attempts+1, "
                        "last_status=?, last_error=NULL WHERE subscription_id=? "
                        "AND delivery_id=? AND state='pending'",
                        (status_code, subscription.subscription_id, delivery.delivery_id),
                    )
                    self._ack_cursor(subscription, consumer, delivery.cursor)
                else:
                    self._retry_or_dead(subscription, delivery, f"HTTP {status_code}", status_code)
            except (httpx.HTTPError, WebhookError) as exc:
                self._retry_or_dead(subscription, delivery, str(exc)[:500], None)
        return attempts_made

    def _terminal(
        self,
        subscription: WebhookSubscription,
        delivery: WebhookDelivery,
        error: str,
        status: int | None,
    ) -> None:
        self._db.execute(
            "UPDATE webhook_deliveries SET state='dead_letter', attempts=attempts+1, "
            "last_status=?, last_error=? WHERE subscription_id=? AND delivery_id=? "
            "AND state='pending'",
            (status, error, subscription.subscription_id, delivery.delivery_id),
        )
        self._ack_cursor(subscription, f"webhook-{subscription.subscription_id}", delivery.cursor)

    def _retry_or_dead(
        self,
        subscription: WebhookSubscription,
        delivery: WebhookDelivery,
        error: str,
        status: int | None,
    ) -> None:
        next_attempt = delivery.attempts + 1
        if next_attempt >= subscription.max_attempts:
            self._terminal(subscription, delivery, error, status)
            return
        delay = min(
            subscription.initial_retry_seconds * (2**delivery.attempts),
            subscription.max_retry_seconds,
        )
        self._db.execute(
            "UPDATE webhook_deliveries SET attempts=?, next_attempt_at=?, last_status=?, "
            "last_error=? WHERE subscription_id=? AND delivery_id=? AND state='pending'",
            (
                next_attempt,
                self._clock() + delay,
                status,
                error,
                subscription.subscription_id,
                delivery.delivery_id,
            ),
        )
