"""McpqClient connection lifecycle."""

from __future__ import annotations

import threading

import pytest

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


async def test_failed_connect_attempts_clean_up_their_client(monkeypatch):
    from minecraft_ai_bridge.minecraft import mc_api

    created: list[_FakeMinecraft] = []

    class Unverifiable(_FakeMinecraft):
        def __init__(self, **_kwargs) -> None:
            super().__init__()
            created.append(self)

        def getMinecraftVersion(self) -> str:  # noqa: N802 - mirrors the mcpq API
            raise RuntimeError("plugin not ready")

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mc_api, "Minecraft", Unverifiable)
    monkeypatch.setattr(mc_api.asyncio, "sleep", no_wait)
    client = McpqClient(host="localhost", port=1789, player_name="AIBot")

    with pytest.raises(ConnectionError):
        await client.connect(retries=3)

    assert len(created) == 3
    assert [fake.cleanups for fake in created] == [1, 1, 1]
    assert client._mc is None
