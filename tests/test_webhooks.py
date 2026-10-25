"""Offline deterministic tests for configured outbound webhook delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import ssl
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest

from minecraft_ai_bridge.runtime import webhooks as webhook_runtime
from minecraft_ai_bridge.runtime.contracts import Event, EventSeverity
from minecraft_ai_bridge.runtime.event_journal import EventJournal
from minecraft_ai_bridge.runtime.webhooks import WebhookError, WebhookService, WebhookSubscription


def _subscription(**kwargs) -> WebhookSubscription:
    values = dict(
        subscription_id="ops-hook",
        url="https://hooks.example.test/events",
        secret=b"s" * 32,
        allowed_hosts=frozenset({"hooks.example.test"}),
        initial_retry_seconds=2,
        max_retry_seconds=5,
        max_attempts=3,
    )
    values.update(kwargs)
    return WebhookSubscription(**values)


def _event(event_id="event-1") -> Event:
    return Event(
        event_id=event_id,
        type="player.died",
        severity=EventSeverity.HIGH,
        occurred_at=datetime(2026, 10, 23, tzinfo=UTC),
        source="test",
        payload={"cause": "unknown"},
    )


def _public_dns(monkeypatch):
    monkeypatch.setattr(
        webhook_runtime,
        "_check_resolved_addresses",
        lambda host: ("93.184.216.34",),
    )


async def _wait_for_dns_capacity_release(timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not webhook_runtime._DNS_CAPACITY.acquire(blocking=False):
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("timed-out resolver did not release its capacity slot")
        await asyncio.sleep(0.005)
    webhook_runtime._DNS_CAPACITY.release()


@asynccontextmanager
async def _client(handler, *, pinned=True):
    if not pinned:
        yield object()
        return
    transport = httpx.MockTransport(handler)
    yield webhook_runtime.HttpxPinnedTransport(transport_factory=lambda: transport)


@pytest.mark.asyncio
async def test_httpx_pinned_transport_uses_ip_host_and_tls_name_without_redirects():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/"})

    transport = webhook_runtime.HttpxPinnedTransport(
        transport_factory=lambda: httpx.MockTransport(handler)
    )
    status = await transport.send_webhook_pinned(
        url="https://hooks.example.test/events?batch=2",
        addresses=("93.184.216.34",),
        content=b"{}",
        headers={"X-Test": "yes", "host": "attacker.invalid"},
        timeout=1.0,
    )

    assert status == 302
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://93.184.216.34/events?batch=2"
    assert request.headers["Host"] == "hooks.example.test"
    assert request.extensions["sni_hostname"] == "hooks.example.test"
    assert request.extensions["timeout"] == {
        "connect": 1.0,
        "read": 1.0,
        "write": 1.0,
        "pool": 1.0,
    }


@pytest.mark.asyncio
async def test_default_pinned_transport_rejects_unsafe_address_before_connecting():
    transport = webhook_runtime.HttpxPinnedTransport()

    with pytest.raises(WebhookError, match="unsafe IP"):
        await transport.send_webhook_pinned(
            url="https://hooks.example.test/events",
            addresses=("127.0.0.1",),
            content=b"payload",
            headers={},
            timeout=1.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("certificate_hostname", "hostname_matches"),
    [("hooks.example.test", True), ("wrong.example.test", False)],
)
async def test_httpx_pinned_transport_performs_tls_to_pinned_ip_with_original_identity(
    tmp_path, monkeypatch, certificate_hostname, hostname_matches
):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI is required to create an ephemeral test certificate")

    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            f"/CN={certificate_hostname}",
            "-addext",
            f"subjectAltName=DNS:{certificate_hostname}",
        ],
        check=True,
        capture_output=True,
    )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    observed_sni = []
    server_context.set_servername_callback(
        lambda _socket, server_name, _context: observed_sni.append(server_name)
    )
    observed_requests = []

    async def handle(reader, writer):
        request_line = await reader.readline()
        headers = {}
        while line := await reader.readline():
            if line == b"\r\n":
                break
            name, value = line.decode("ascii").split(":", 1)
            headers[name.lower()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = await reader.readexactly(content_length) if content_length else b""
        observed_requests.append((request_line.decode("ascii").strip(), headers, body))
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    except OSError as exc:
        pytest.skip(f"local TLS sockets are unavailable in this environment: {exc}")
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(webhook_runtime, "_unsafe_ip", lambda _address: False)
    client_context = ssl.create_default_context(cafile=str(cert_path))

    class LocalPortTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            local_url = request.url.copy_with(port=port)
            local_request = httpx.Request(
                request.method,
                local_url,
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
            return await super().handle_async_request(local_request)

    default_transport_options = []

    def build_test_transport(**options):
        default_transport_options.append(options)
        return LocalPortTransport(verify=client_context, **options)

    # Exercise HttpxPinnedTransport's production default-factory branch while
    # substituting only the local test CA and test-server port mapping.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", build_test_transport)
    transport = webhook_runtime.HttpxPinnedTransport()

    try:
        request_args = {
            "url": "https://hooks.example.test/events?batch=2",
            "addresses": ("127.0.0.1",),
            "content": b"payload",
            "headers": {"X-Test": "yes"},
            "timeout": 2.0,
        }
        if hostname_matches:
            status = await transport.send_webhook_pinned(**request_args)
            assert status == 204
        else:
            with pytest.raises(httpx.HTTPError):
                await transport.send_webhook_pinned(**request_args)
        assert len(default_transport_options) == 1
        options = default_transport_options[0]
        assert options["trust_env"] is False
        assert options["http2"] is False
        assert options["retries"] == 0
        assert options["limits"].max_connections == 1
        assert options["limits"].max_keepalive_connections == 0
        assert observed_sni == ["hooks.example.test"]
        if hostname_matches:
            assert len(observed_requests) == 1
            assert observed_requests[0][0] == "POST /events?batch=2 HTTP/1.1"
            assert observed_requests[0][2] == b"payload"
            assert observed_requests[0][1]["host"] == "hooks.example.test"
        else:
            assert observed_requests == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_signature_timestamp_delivery_id_and_success_ack(tmp_path, monkeypatch) -> None:
    _public_dns(monkeypatch)
    now = [1_800_000_000.0]
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(204)

    async with _client(handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: now[0],
        )
        assert await service.run_once() == 1
        req = requests[0]
        body = req.content
        assert req.headers["X-Webhook-Timestamp"] == str(int(now[0]))
        delivery_id = req.headers["X-Webhook-Delivery-Id"]
        assert delivery_id == service.deliveries()[0].delivery_id
        expected = hmac.new(
            b"s" * 32, str(int(now[0])).encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        assert req.headers["X-Webhook-Signature"] == f"sha256={expected}"
        assert service.deliveries()[0].state == "delivered"
        assert journal.get_events("webhook-ops-hook") == []
        service.close()
    journal.close()


@pytest.mark.asyncio
async def test_overlapping_delivery_passes_cannot_overwrite_terminal_state(tmp_path, monkeypatch):
    _public_dns(monkeypatch)
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())
    entered_send = asyncio.Event()
    release_send = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        entered_send.set()
        await asyncio.wait_for(release_send.wait(), timeout=5)
        return httpx.Response(204)

    async with _client(handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
        )
        first = asyncio.create_task(service.run_once())
        second = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(entered_send.wait(), timeout=5)
            await asyncio.sleep(0)
            assert len(requests) == 1
            release_send.set()
            assert await asyncio.wait_for(first, timeout=5) == 1
            assert await asyncio.wait_for(second, timeout=5) == 0
            assert len(requests) == 1
            delivery = service.deliveries()[0]
            assert delivery.state == "delivered"
            assert delivery.attempts == 1
            assert journal.get_events("webhook-ops-hook") == []
        finally:
            release_send.set()
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            service.close()
    journal.close()


@pytest.mark.asyncio
async def test_retry_schedule_restart_and_stable_id_for_uncertain_duplicate(
    tmp_path, monkeypatch
) -> None:
    _public_dns(monkeypatch)
    now = [100.0]
    journal_path, delivery_path = tmp_path / "events.db", tmp_path / "deliveries.db"
    journal = EventJournal(journal_path)
    journal.append(_event())
    request_ids = []

    async def uncertain(request):
        request_ids.append(request.headers["X-Webhook-Delivery-Id"])
        return httpx.Response(503 if len(request_ids) == 1 else 204)

    async with _client(uncertain) as client:
        service = WebhookService(
            journal,
            str(delivery_path),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: now[0],
        )
        assert await service.run_once() == 1
        pending = service.deliveries()[0]
        assert pending.state == "pending" and pending.attempts == 1
        assert pending.next_attempt_at == 102
        service.close()
        restored = WebhookService(
            journal,
            str(delivery_path),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: now[0],
        )
        assert restored.deliveries()[0] == pending
        now[0] = 102
        assert await restored.run_once() == 1
        assert request_ids[0] == request_ids[1]
        assert restored.deliveries()[0].state == "delivered"
        restored.close()
    journal.close()


@pytest.mark.asyncio
async def test_max_attempts_dead_letters_and_acknowledges(tmp_path, monkeypatch) -> None:
    _public_dns(monkeypatch)
    now = [10.0]
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())

    async def reject(request):
        return httpx.Response(500)

    async with _client(reject) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: now[0],
        )
        for tick in (10, 12, 16):
            now[0] = tick
            await service.run_once()
        dead = service.dead_letters()
        assert len(dead) == 1 and dead[0].attempts == 3 and dead[0].last_status == 500
        assert journal.get_events("webhook-ops-hook") == []
        service.close()
    journal.close()


@pytest.mark.asyncio
async def test_rate_limit_and_body_limit_are_terminal(tmp_path, monkeypatch) -> None:
    _public_dns(monkeypatch)
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event("event-1"))
    journal.append(_event("event-2"))
    calls = []

    async def accept(request):
        calls.append(request)
        return httpx.Response(200)

    async with _client(accept) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription(events_per_minute=1)],
            pinned_transport=client,
            max_body_bytes=1024,
            clock=lambda: 100,
        )
        assert await service.run_once() == 1
        assert await service.run_once() == 0
        assert len(calls) == 1
        service.close()
    journal.close()

    journal = EventJournal(tmp_path / "oversize.db")
    journal.append(_event())
    async with _client(accept) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "oversize-delivery.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            max_body_bytes=1,
        )
        assert await service.run_once() == 0
        assert service.dead_letters()[0].last_error == "event body exceeds configured maximum"
        service.close()
    journal.close()


@pytest.mark.parametrize(
    "url,hosts",
    [
        ("http://hooks.example.test/path", frozenset({"hooks.example.test"})),
        ("https://user:pass@hooks.example.test/path", frozenset({"hooks.example.test"})),
        ("https://hooks.example.test/path#fragment", frozenset({"hooks.example.test"})),
        ("https://127.0.0.1/path", frozenset({"127.0.0.1"})),
        ("https://evil.example.test/path", frozenset({"hooks.example.test"})),
    ],
)
def test_unsafe_or_unallowlisted_url_rejected(url, hosts) -> None:
    with pytest.raises(WebhookError):
        _subscription(url=url, allowed_hosts=hosts)


@pytest.mark.asyncio
async def test_private_dns_resolution_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        webhook_runtime,
        "_check_resolved_addresses",
        lambda host: (_ for _ in ()).throw(
            WebhookError("webhook host resolves to an unsafe IP address")
        ),
    )
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())

    async def fail_if_called(request):
        pytest.fail("unsafe endpoint reached transport")

    async with _client(fail_if_called) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
        )
        assert await service.run_once() == 0
        pending = service.deliveries()[0]
        assert "unsafe IP" in pending.last_error
        assert pending.state == "pending" and pending.attempts == 1
        service.close()
    journal.close()


@pytest.mark.asyncio
async def test_retry_backoff_blocks_later_cursor_until_head_is_acked(tmp_path, monkeypatch):
    _public_dns(monkeypatch)
    now = [10.0]
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event("event-a"))
    journal.append(_event("event-b"))
    sent = []

    async def handler(request):
        event_id = json.loads(request.content)["event_id"]
        sent.append(event_id)
        return httpx.Response(503 if len(sent) == 1 else 204)

    async with _client(handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: now[0],
        )
        assert await service.run_once() == 1
        assert sent == ["event-a"]
        # Simulate a later queued row left by a previous worker version. The
        # earliest pending row remains authoritative even when B is already due.
        service._db.execute(
            "INSERT INTO webhook_deliveries "
            "(subscription_id, delivery_id, event_id, cursor, state, next_attempt_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (
                "ops-hook",
                service._delivery_id("ops-hook", "event-b"),
                "event-b",
                2,
                now[0],
            ),
        )
        now[0] = 11
        assert await service.run_once() == 0
        assert sent == ["event-a"]
        now[0] = 12
        assert await service.run_once() == 1
        assert sent == ["event-a", "event-a"]
        assert await service.run_once() == 1
        assert sent == ["event-a", "event-a", "event-b"]
        service.close()
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["delivered", "dead_letter"])
async def test_restart_reconciles_terminal_state_before_journal_ack(
    tmp_path, monkeypatch, terminal
):
    _public_dns(monkeypatch)
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(204)

    async with _client(handler) as client:
        body_limit = 1 if terminal == "dead_letter" else 256_000
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            max_body_bytes=body_limit,
        )
        real_ack = journal.ack_event

        def crash_before_ack(consumer_id, cursor):
            raise RuntimeError("simulated crash between databases")

        journal.ack_event = crash_before_ack
        with pytest.raises(RuntimeError, match="between databases"):
            await service.run_once()
        assert service.deliveries()[0].state == terminal
        journal.ack_event = real_ack
        service.close()

        restored = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            max_body_bytes=body_limit,
        )
        await restored.run_once()
        assert journal.get_events("webhook-ops-hook") == []
        assert restored.deliveries()[0].state == terminal
        assert len(calls) == (1 if terminal == "delivered" else 0)
        restored.close()
    journal.close()


@pytest.mark.asyncio
async def test_hostname_delivery_fails_closed_without_pinning_transport(tmp_path, monkeypatch):
    _public_dns(monkeypatch)
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(204)

    async with _client(handler, pinned=False) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            clock=lambda: 100.0,
        )
        assert await service.run_once() == 0
        assert requests == []
        delivery = service.deliveries()[0]
        assert delivery.state == "pending"
        assert delivery.attempts == 1
        assert "no pinned webhook request operation" in delivery.last_error
        service.close()
    journal.close()


@pytest.mark.asyncio
async def test_retention_during_awaited_send_recovers_without_replaying_success(
    tmp_path, monkeypatch
):
    async def resolve_public(_host, *, timeout):
        del timeout
        return ("93.184.216.34",)

    monkeypatch.setattr("minecraft_ai_bridge.runtime.webhooks._resolve_addresses", resolve_public)
    journal = EventJournal(tmp_path / "events.db", max_events=1)
    journal.append(_event("event-before-prune"))
    entered_send = asyncio.Event()
    release_send = asyncio.Event()
    sent = []

    async def handler(request):
        event_id = json.loads(request.content)["event_id"]
        sent.append(event_id)
        if event_id == "event-before-prune":
            entered_send.set()
            await asyncio.wait_for(release_send.wait(), timeout=5)
        return httpx.Response(204)

    async with _client(handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[
                _subscription(),
                _subscription(subscription_id="second-hook", secret=b"t" * 32),
            ],
            pinned_transport=client,
        )
        in_flight = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(entered_send.wait(), timeout=5)
            journal.append(_event("event-after-prune"))
            release_send.set()
            assert await asyncio.wait_for(in_flight, timeout=5) == 2

            # The first subscription resets to the retained floor, while the
            # later subscription still runs in this pass.
            assert sent == ["event-before-prune", "event-after-prune"]
            assert await asyncio.wait_for(service.run_once(), timeout=5) == 1
            assert sent == [
                "event-before-prune",
                "event-after-prune",
                "event-after-prune",
            ]
            assert all(delivery.state == "delivered" for delivery in service.deliveries())
            assert journal.consumer_ack_cursor("webhook-ops-hook") == 2
            assert journal.consumer_ack_cursor("webhook-second-hook") == 2
        finally:
            release_send.set()
            if not in_flight.done():
                in_flight.cancel()
                await asyncio.gather(in_flight, return_exceptions=True)
            service.close()
    journal.close()


@pytest.mark.asyncio
async def test_slow_dns_is_bounded_off_loop_and_observable(tmp_path, monkeypatch):
    release_lookup = threading.Event()
    lookup_started = threading.Event()
    lookup_finished = threading.Event()
    requests = []

    def slow_lookup(*args, **kwargs):
        lookup_started.set()
        try:
            release_lookup.wait()
            return ("93.184.216.34",)
        finally:
            lookup_finished.set()

    monkeypatch.setattr(webhook_runtime, "_check_resolved_addresses", slow_lookup)
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())

    async def handler(request):
        requests.append(request)
        return httpx.Response(204)

    async with _client(handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            dns_timeout_seconds=0.02,
            clock=lambda: 100.0,
        )
        loop_responsive = asyncio.Event()

        async def heartbeat():
            await asyncio.sleep(0.005)
            loop_responsive.set()

        try:
            task = asyncio.create_task(service.run_once())
            await heartbeat()
            assert lookup_started.is_set()
            assert loop_responsive.is_set()
            assert await task == 0
            delivery = service.deliveries()[0]
            assert delivery.state == "pending"
            assert delivery.attempts == 1
            assert "DNS resolution timed out" in delivery.last_error
            assert requests == []
        finally:
            release_lookup.set()
            deadline = asyncio.get_running_loop().time() + 2
            while not lookup_finished.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            assert lookup_finished.is_set()
            await _wait_for_dns_capacity_release()
            service.close()
    journal.close()


@pytest.mark.asyncio
async def test_repeated_dns_timeouts_do_not_spawn_unbounded_workers(tmp_path, monkeypatch):
    release_lookup = threading.Event()
    lookup_finished = threading.Event()
    lookup_count = 0
    lookup_lock = threading.Lock()

    def slow_lookup(*args, **kwargs):
        nonlocal lookup_count
        del args, kwargs
        with lookup_lock:
            lookup_count += 1
        try:
            release_lookup.wait(timeout=2)
            return ("93.184.216.34",)
        finally:
            lookup_finished.set()

    monkeypatch.setattr(webhook_runtime, "_check_resolved_addresses", slow_lookup)
    now = [100.0]
    journal = EventJournal(tmp_path / "events.db")
    journal.append(_event())

    async def unused_handler(request):
        pytest.fail(f"unexpected webhook request: {request!r}")

    async with _client(unused_handler) as client:
        service = WebhookService(
            journal,
            str(tmp_path / "deliveries.db"),
            subscriptions=[_subscription()],
            pinned_transport=client,
            dns_timeout_seconds=0.01,
            clock=lambda: now[0],
        )
        try:
            for _ in range(5):
                await asyncio.wait_for(service.run_once(), timeout=2)
                now[0] += 100
            assert lookup_count == 1
            assert service.deliveries()[0].last_error == (
                "webhook DNS resolver capacity is exhausted"
            )
        finally:
            release_lookup.set()
            deadline = time.monotonic() + 2
            while not lookup_finished.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert lookup_finished.is_set()
            await _wait_for_dns_capacity_release()
            service.close()
    journal.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_retry_seconds", float("nan")),
        ("initial_retry_seconds", float("inf")),
        ("initial_retry_seconds", float("-inf")),
        ("max_retry_seconds", float("nan")),
        ("max_retry_seconds", float("inf")),
        ("max_retry_seconds", float("-inf")),
    ],
)
def test_retry_intervals_reject_non_finite_values(field, value):
    with pytest.raises(WebhookError, match="finite positive"):
        _subscription(**{field: value})
