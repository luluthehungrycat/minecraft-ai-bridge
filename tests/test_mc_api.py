"""McpqClient connection lifecycle."""

from __future__ import annotations

import threading

from minecraft_ai_bridge.minecraft.mc_api import McpqClient


class _FakeMinecraft:
    def __init__(self, *, fail: bool = False) -> None:
        self.cleanups = 0
        self.cleanup_threads: list[int] = []
        self._fail = fail

    def _cleanup(self) -> None:
        self.cleanups += 1
        self.cleanup_threads.append(threading.get_ident())
        if self._fail:
            raise RuntimeError("channel already broken")


async def test_disconnect_cleans_up_mcpq_off_the_event_loop():
    client = McpqClient(host="localhost", port=1789, player_name="AIBot")
    fake = _FakeMinecraft()
    client._mc = fake  # type: ignore[assignment]

    await client.disconnect()
    await client.disconnect()  # idempotent

    assert fake.cleanups == 1
    assert fake.cleanup_threads != [threading.get_ident()]
    assert client._mc is None


async def test_disconnect_survives_cleanup_failure():
    client = McpqClient(host="localhost", port=1789, player_name="AIBot")
    client._mc = _FakeMinecraft(fail=True)  # type: ignore[assignment]

    await client.disconnect()

    assert client._mc is None
