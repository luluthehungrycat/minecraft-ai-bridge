"""ControllerService event polling and controller.changed events (#18)."""

from __future__ import annotations

import sqlite3

import pytest

from minecraft_ai_bridge.runtime import (
    AuthzService,
    ControllerService,
    Event,
    EventCursorExpiredError,
    EventJournal,
)
from minecraft_ai_bridge.runtime.contracts import EventSeverity
from minecraft_ai_bridge.runtime.event_journal import MAX_EVENTS_PER_FETCH
from minecraft_ai_bridge.runtime.world_adapter import McpqWorldAdapter
from tests.conftest import MockMcpqClient


@pytest.fixture
def setup(tmp_path):
    auth = AuthzService(tmp_path / "authz.sqlite3")
    auth.register_steve("AIBot", ownership_mode="persistent", owner_id="owner")
    auth.register_steve("Other", ownership_mode="persistent", owner_id="owner")
    admin = auth.create_admin_credential("root")

    def credential(steve: str, *scopes: str) -> str:
        return auth.create_credential(steve, "owner", set(scopes), admin_token=admin.token).token

    journal = EventJournal(tmp_path / "events.sqlite3")
    world = MockMcpqClient()
    world.set_position(0, 64, 0)
    tokens = {
        "reader": credential("AIBot", "steve:control", "events:read"),
        "second_reader": credential("AIBot", "events:read"),
        "no_scope": credential("AIBot", "steve:control"),
        "other_steve": credential("Other", "events:read"),
    }
    yield auth, journal, world, tokens
    journal.close()
    auth.close()


def _service(auth, journal, world) -> ControllerService:
    return ControllerService(McpqWorldAdapter(world), auth, "AIBot", event_journal=journal)


def _event(event_id: str, steve_id: str | None = "AIBot") -> Event:
    return Event(
        event_id=event_id,
        type="test.event",
        severity=EventSeverity.INFO,
        steve_id=steve_id,
        source="test",
    )


async def test_events_need_events_read_for_this_steve(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)

    for name in ("no_scope", "other_steve"):
        with pytest.raises(PermissionError, match="events:read"):
            await service.get_events(tokens[name])
        with pytest.raises(PermissionError, match="events:read"):
            await service.ack_events(tokens[name], 1)
    with pytest.raises(PermissionError):
        await service.get_events("not-a-token")


async def test_a_service_without_a_journal_refuses_event_calls(setup):
    auth, _, world, tokens = setup
    service = ControllerService(McpqWorldAdapter(world), auth, "AIBot")

    with pytest.raises(ValueError, match="does not record events"):
        await service.get_events(tokens["reader"])
    # Sessions still work without a journal.
    session = await service.open_session(tokens["reader"])
    await service.close_session(tokens["reader"], session.session_id)


async def test_session_changes_are_recorded_and_repeat_until_acknowledged(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)
    session = await service.open_session(tokens["reader"])
    await service.close_session(tokens["reader"], session.session_id)

    first = await service.get_events(tokens["reader"])
    again = await service.get_events(tokens["reader"])

    assert [r.event.payload["change"] for r in first.events] == ["opened", "released"]
    assert [r.cursor for r in again.events] == [r.cursor for r in first.events]
    opened = first.events[0].event
    assert opened.type == "controller.changed"
    assert opened.steve_id == "AIBot"
    assert opened.payload["controller_id"] == session.controller_id
    assert opened.payload["generation"] == session.generation
    assert not first.reset

    await service.ack_events(tokens["reader"], first.events[0].cursor)
    rest = await service.get_events(tokens["reader"])
    assert [r.event.payload["change"] for r in rest.events] == ["released"]
    await service.ack_events(tokens["reader"], rest.events[0].cursor)
    assert (await service.get_events(tokens["reader"])).events == []


async def test_each_credential_has_its_own_cursor(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)
    journal.append_many([_event("e1"), _event("e2")])

    mine = await service.get_events(tokens["reader"])
    await service.ack_events(tokens["reader"], mine.events[-1].cursor)

    theirs = await service.get_events(tokens["second_reader"])
    assert [r.event.event_id for r in theirs.events] == ["e1", "e2"]
    assert (await service.get_events(tokens["reader"])).events == []


async def test_other_steves_and_unattributed_events_are_never_delivered(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)
    journal.append_many(
        [
            _event("foreign-1", "Other"),
            _event("unattributed", None),
            _event("mine-1"),
            _event("foreign-2", "Other"),
            _event("mine-2"),
            _event("mine-3"),
        ]
    )

    page = await service.get_events(tokens["reader"], limit=2)
    assert [r.event.event_id for r in page.events] == ["mine-1", "mine-2"]
    await service.ack_events(tokens["reader"], page.events[-1].cursor)
    page = await service.get_events(tokens["reader"], limit=2)
    assert [r.event.event_id for r in page.events] == ["mine-3"]


async def test_a_poll_skips_long_runs_of_other_steves_events(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)
    foreign = [_event(f"foreign-{i}", "Other") for i in range(MAX_EVENTS_PER_FETCH * 2 + 5)]
    journal.append_many([*foreign, _event("mine")])

    page = await service.get_events(tokens["reader"])

    assert [r.event.event_id for r in page.events] == ["mine"]


async def test_an_expired_cursor_is_reported_and_can_be_reset(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)
    journal.append(_event("old"))
    assert len((await service.get_events(tokens["reader"])).events) == 1
    journal.append_many([_event("new-1"), _event("new-2")])
    journal.prune(keep=1)

    with pytest.raises(EventCursorExpiredError):
        await service.get_events(tokens["reader"])
    page = await service.get_events(tokens["reader"], reset_expired=True)

    assert page.reset
    assert [r.event.event_id for r in page.events] == ["new-2"]
    assert not (await service.get_events(tokens["reader"])).reset


@pytest.mark.parametrize("limit", [0, MAX_EVENTS_PER_FETCH + 1, True, 1.5])
async def test_poll_limits_are_validated(setup, limit):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)

    with pytest.raises(ValueError, match="limit"):
        await service.get_events(tokens["reader"], limit=limit)


async def test_acknowledging_an_unissued_cursor_is_refused(setup):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)

    with pytest.raises(ValueError, match="not been issued"):
        await service.ack_events(tokens["reader"], 5)


async def test_a_failing_journal_never_fails_a_session_change(setup, monkeypatch):
    auth, journal, world, tokens = setup
    service = _service(auth, journal, world)

    def broken(_event):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(journal, "append", broken)
    session = await service.open_session(tokens["reader"])
    await service.close_session(tokens["reader"], session.session_id)
