"""Deterministic tests for the durable runtime event journal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minecraft_ai_bridge.runtime.contracts import Event, EventSeverity
from minecraft_ai_bridge.runtime.event_journal import (
    EventCursorExpiredError,
    EventIdConflictError,
    EventJournal,
)


def make_event(event_id: str, *, detail: str = "damage") -> Event:
    return Event(
        event_id=event_id,
        type="player.health_critical",
        severity=EventSeverity.HIGH,
        source="test",
        payload={"detail": detail},
    )


def test_journal_persists_events_and_monotonic_cursors(tmp_path) -> None:
    path = tmp_path / "events.sqlite3"
    journal = EventJournal(path)
    first_event = make_event("event-1")
    first = journal.append(first_event)
    journal.close()

    reopened = EventJournal(path)
    second_event = make_event("event-2")
    second = reopened.append(second_event)
    assert second > first
    records = reopened.get_events("consumer-a")
    assert records[0].event == first_event
    assert records[1].event == second_event
    reopened.close()


def test_duplicate_event_id_is_idempotent_and_conflicts_fail(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3") as journal:
        event = make_event("event-1")
        first = journal.append(event)
        assert journal.append(event) == first
        with pytest.raises(EventIdConflictError):
            journal.append(make_event("event-1", detail="different"))


def test_pruned_event_tombstone_keeps_only_fixed_size_fingerprint(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3", max_events=1) as journal:
        old_event = make_event("event-secret", detail="private historical payload")
        old_cursor = journal.append(old_event)
        journal.append(make_event("event-current"))

        fingerprint = journal._conn.execute(
            "SELECT fingerprint FROM event_ids WHERE event_id = ?", ("event-secret",)
        ).fetchone()[0]
        assert len(fingerprint) == 64
        assert "private historical payload" not in fingerprint
        assert journal.retained_event_count == 1
        assert journal.append(old_event) == old_cursor


def test_append_revalidates_detached_event_envelope_before_commit(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3") as journal:
        invalid = make_event("mutated-event")
        invalid.source = ""

        with pytest.raises(ValidationError):
            journal.append(invalid)
        assert journal.last_cursor() == 0
        assert journal.retained_event_count == 0

        valid = make_event("valid-event")
        invalid_batch = make_event("invalid-batch-event")
        invalid_batch.payload = {"nested": object()}
        with pytest.raises(ValidationError):
            journal.append_many([valid, invalid_batch])
        assert journal.last_cursor() == 0
        assert journal.retained_event_count == 0


def test_consumer_poll_replays_until_acknowledged(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3") as journal:
        first = journal.append(make_event("event-1"))
        second = journal.append(make_event("event-2"))
        assert [record.cursor for record in journal.get_events("consumer-a", limit=1)] == [first]
        assert [record.cursor for record in journal.get_events("consumer-a", limit=1)] == [first]
        journal.ack_event("consumer-a", first)
        assert [record.cursor for record in journal.get_events("consumer-a")] == [second]
        journal.ack_event("consumer-a", second)
        assert journal.get_events("consumer-a") == []


def test_retention_expires_lagging_consumer_and_can_be_reset(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3", max_events=2) as journal:
        pruned_event = make_event("event-1")
        pruned_cursor = journal.append(pruned_event)
        journal.get_events("lagging-consumer")
        journal.append(make_event("event-2"))
        journal.append(make_event("event-3"))

        assert journal.retained_event_count == 2
        assert journal.append(pruned_event) == pruned_cursor
        assert journal.retained_event_count == 2
        with pytest.raises(EventIdConflictError):
            journal.append(make_event("event-1", detail="changed after pruning"))

        with pytest.raises(EventCursorExpiredError):
            journal.get_events("lagging-consumer")
        journal.reset_consumer("lagging-consumer")
        assert [record.event.event_id for record in journal.get_events("lagging-consumer")] == [
            "event-2",
            "event-3",
        ]


def test_acknowledged_consumer_is_not_expired_by_retention(tmp_path) -> None:
    with EventJournal(tmp_path / "events.sqlite3", max_events=1) as journal:
        first = journal.append(make_event("event-1"))
        journal.get_events("consumer-a")
        journal.ack_event("consumer-a", first)
        journal.append(make_event("event-2"))

        assert [record.event.event_id for record in journal.get_events("consumer-a")] == ["event-2"]


def test_journal_validates_limits_and_ack_cursors(tmp_path) -> None:
    with pytest.raises(ValueError):
        EventJournal(tmp_path / "bad.sqlite3", max_events=0)
    with EventJournal(tmp_path / "events.sqlite3") as journal:
        with pytest.raises(ValueError):
            journal.get_events("", limit=1)
        with pytest.raises(ValueError):
            journal.get_events("consumer-a", limit=0)
        with pytest.raises(ValueError):
            journal.ack_event("consumer-a", 1)
        with pytest.raises(ValueError):
            journal.ack_event("consumer-a", -1)
