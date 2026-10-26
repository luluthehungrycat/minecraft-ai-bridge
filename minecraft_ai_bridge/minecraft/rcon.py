"""RCON protocol client for sending commands to a Minecraft server.
.. deprecated::
   The MCPQ plugin (via mc_api.py / mcpq) has replaced RCON for all
   gameplay functionality.  This module is kept as a reference / fallback
   for admin commands only and is NOT actively maintained.  It may be
   removed in a future release.

Implements the Minecraft RCON protocol directly over TCP
(no external dependencies beyond stdlib).

Usage (deprecated)::
    async with RCONClient("localhost", 25575, "password") as rcon:
        result = await rcon.command("/time query daytime")
        print(result)
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# RCON packet types
_TYPE_LOGIN = 3
_TYPE_COMMAND = 2
_TYPE_RESPONSE = 0

# Max payload size the server accepts before truncation
_MAX_PAYLOAD = 1446


class RCONError(Exception):
    """Base RCON exception."""


class RCONAuthError(RCONError):
    """Authentication failed."""


class RCONDisconnectedError(RCONError):
    """Connection lost."""


@dataclass
class RCONPacket:
    """A single RCON protocol packet."""

    request_id: int
    ptype: int
    payload: str


class RCONClient:
    """Async RCON client for Minecraft server communication.

    Usage::

        async with RCONClient("localhost", 25575, "password") as rcon:
            result = await rcon.command("/time query daytime")
            print(result)
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 25575,
        password: str = "",
        reconnect_attempts: int = 3,
        reconnect_delay: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_delay = reconnect_delay

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._connected = False

    # ── Connection lifecycle ────────────────────────────────────────

    async def connect(self) -> None:
        """Open TCP connection and authenticate."""
        last_error: Exception | None = None
        for attempt in range(1, self.reconnect_attempts + 1):
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=10
                )
                await asyncio.wait_for(self._authenticate(), timeout=15)
                self._connected = True
                logger.info("Connected to %s:%d", self.host, self.port)
                return
            except (TimeoutError, OSError) as exc:
                last_error = exc
                logger.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt,
                    self.reconnect_attempts,
                    exc,
                )
                if attempt < self.reconnect_attempts:
                    await asyncio.sleep(self.reconnect_delay)
        raise RCONError(
            f"Could not connect to {self.host}:{self.port} after {self.reconnect_attempts} attempts"
        ) from last_error

    async def disconnect(self) -> None:
        """Close the connection gracefully."""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def __aenter__(self) -> RCONClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    # ── Public API ──────────────────────────────────────────────────

    async def command(self, cmd: str) -> str:
        """Send a command and return the server response text."""
        if not self._connected:
            raise RCONDisconnectedError("Not connected to server")

        # Truncate command if too long
        if len(cmd.encode("utf-8")) > _MAX_PAYLOAD:
            cmd = cmd[:_MAX_PAYLOAD]

        packet = await self._send_packet(_TYPE_COMMAND, cmd)
        if packet.request_id == -1:
            raise RCONError("Command failed: server returned error code")
        return packet.payload

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Internal helpers ────────────────────────────────────────────

    async def _authenticate(self) -> None:
        """Send login packet and verify response."""
        assert self._writer is not None
        assert self._reader is not None

        response = await self._send_packet(_TYPE_LOGIN, self.password)
        if response.request_id == -1:
            raise RCONAuthError("Authentication failed: wrong password?")

    async def _send_packet(self, ptype: int, payload: str) -> RCONPacket:
        """Write a packet to the wire and read the response."""
        assert self._writer is not None
        assert self._reader is not None

        self._request_id += 1
        req_id = self._request_id

        encoded = payload.encode("utf-8")
        # Length = request_id (4) + type (4) + payload + null pad (2)
        length = 4 + 4 + len(encoded) + 2
        data = struct.pack("<lii", length, req_id, ptype)
        data += encoded + b"\x00\x00"

        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionResetError) as exc:
            self._connected = False
            raise RCONDisconnectedError(f"Write failed: {exc}") from exc

        return await self._read_response(req_id)

    async def _read_bytes(self, n: int, timeout: float) -> bytes:
        """Read exactly *n* bytes with a total *timeout*.

        Uses ``reader.read()`` internally (which *is* properly cancellable by
        ``asyncio.wait_for`` on this Python version, unlike ``readexactly``).
        """
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        reader = self._reader
        if reader is None:
            raise ConnectionError("RCON connection is not open")
        remaining = n
        while remaining > 0:
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                raise TimeoutError()
            try:
                chunk = await asyncio.wait_for(reader.read(remaining), timeout=time_left)
            except TimeoutError:
                raise
            if not chunk:
                raise ConnectionResetError("Connection closed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    async def _read_response(self, expected_id: int, timeout: float = 15.0) -> RCONPacket:
        """Read and validate the response packet.

        Raises ``RCONDisconnectedError`` on timeout or connection loss.
        """
        assert self._reader is not None

        try:
            header = await self._read_bytes(8, timeout=timeout)
        except TimeoutError:
            self._connected = False
            raise RCONDisconnectedError(
                f"Response timeout ({timeout}s) — is RCON enabled on the server?"
            ) from None
        except (ConnectionResetError, OSError) as exc:
            self._connected = False
            raise RCONDisconnectedError(f"Read failed: {exc}") from exc

        length, req_id = struct.unpack("<ii", header)

        # Sanity check to avoid allocating enormous payloads
        if length < 0 or length > 4096:
            logger.warning("Suspicious packet length: %d", length)
            raise RCONError(f"Invalid packet length: {length}")

        # remaining = Length - 4  because req_id is part of "length" but already consumed
        remaining = length - 4
        body = await self._read_bytes(remaining, timeout=max(5, timeout))

        # body = [type:4bytes][payload:varies][\x00\x00:2bytes]
        if len(body) < 4:
            raise RCONError("Response too short")
        ptype = struct.unpack("<i", body[:4])[0]
        # Payload is null-terminated; strip trailing nulls
        payload_raw = body[4:]
        payload = payload_raw.rstrip(b"\x00").decode("utf-8", errors="replace")

        # The server may send additional packets (e.g., for long commands).
        # Read any remaining data up to a small internal buffer.
        try:
            while True:
                extra = await asyncio.wait_for(self._reader.read(4096), timeout=0.1)
                if not extra:
                    break
                # Some servers send follow-up packets; we merge text if present.
                payload += extra.rstrip(b"\x00").decode("utf-8", errors="replace")
        except TimeoutError:
            pass  # no more data — normal

        return RCONPacket(request_id=req_id, ptype=ptype, payload=payload)
