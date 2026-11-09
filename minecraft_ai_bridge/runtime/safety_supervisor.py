"""Independently scheduled safety checks for the built-in runtime."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from ..minecraft.actions import ActionResult, ActionType
from ..minecraft.observer import WorldState
from .condition_events import (
    ATTRIBUTED_EVENT_TYPES,
    ConditionEventDetector,
    damage_source,
)
from .contracts import Event, EventSeverity

if TYPE_CHECKING:
    from ..minecraft.mc_api import LastAttacker

logger = logging.getLogger(__name__)

_SAFETY_EVENT_TYPE = "safety.intervened"
_SAFETY_EVENT_SOURCE = "builtin_safety_supervisor"
_SAFETY_EVENT_ACTIONS = frozenset(
    {ActionType.ATTACK.value, ActionType.EAT.value, ActionType.HEAL.value}
)
# Condition events are rate-limited at the source (edge triggers and a damage
# cooldown), so this only fills if the journal stalls for minutes.
_MAX_PENDING_CONDITION_EVENTS = 64
# Bound on each backend query made for condition events.
_WORLD_EVENT_TIMEOUT_SECONDS = 2.0


class ConditionWorldEvents(Protocol):
    """Backend queries for death events and damage attribution."""

    async def get_last_attacker(self, max_age_ticks: int = 60) -> LastAttacker | None: ...

    async def get_death_messages(self) -> list[str]: ...


class SafetySupervisor:
    """Periodically observe the world and run deterministic safety checks.

    This task is scheduled separately from the strategic LLM loop. It holds
    at most one unconsumed intervention result so slow strategic work cannot
    build an unbounded queue of safety outcomes. The underlying policy and
    action gate remain responsible for deciding whether a reflex mutation is
    allowed.
    """

    def __init__(
        self,
        observe: Callable[[], Awaitable[WorldState]],
        evaluate: Callable[[WorldState], Awaitable[ActionResult | None]],
        *,
        interval: float = 0.25,
        on_intervention_consumed: Callable[[], None] | None = None,
        event_sink: Callable[[Event], int | Awaitable[int]] | None = None,
        steve_id: str | None = None,
        condition_events: ConditionEventDetector | None = None,
        world_events: ConditionWorldEvents | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("safety supervisor interval must be positive")
        self._observe = observe
        self._evaluate = evaluate
        self._interval = interval
        self._on_intervention_consumed = on_intervention_consumed
        self._event_sink = event_sink
        # Attributes events to the Steve, so per-Steve readers (such as
        # ControllerService.get_events) receive them; None leaves them
        # unattributed.
        self._steve_id = steve_id
        self._event_writes: set[asyncio.Task[None]] = set()
        self._coalesced_event: Event | None = None
        self._coalesced_total = 0
        self._coalesced_outcomes: dict[str, dict[str, int]] = {}
        self._coalesced_failed_action: str | None = None
        self._pending: ActionResult | None = None
        self._intervention_ready = asyncio.Event()
        # Health/hunger condition events from each observation, written by
        # one detached task in order. Only used with an event sink.
        self._conditions = condition_events if event_sink is not None else None
        self._condition_queue: deque[Event] = deque()
        self._condition_writer: asyncio.Task[None] | None = None
        self._condition_dropped = 0
        # Death events and damage sources; used only with condition events.
        self._world_events = world_events if self._conditions is not None else None
        if self._world_events is not None and self._conditions is not None:
            self._conditions.expect_death_messages()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll independently until asked to stop; isolate per-tick failures."""

        # Death events are polled by their own task so a slow backend query
        # never delays an observation or a reflex.
        death_poller = (
            asyncio.create_task(self._poll_death_events(stop_event))
            if self._world_events is not None
            else None
        )
        try:
            await self._run_ticks(stop_event)
        finally:
            if death_poller is not None:
                death_poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await death_poller

    async def _run_ticks(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                started = asyncio.get_running_loop().time()
                world = await self._observe()
                self._record_condition_events(world, started)
                if stop_event.is_set():
                    break
                result = await self._evaluate(world)
                if result is not None:
                    # Reflexes keep running while a result is unconsumed; the
                    # single slot holds the newest outcome, except that a
                    # later success never hides an unconsumed failure.
                    if self._pending is None or self._pending.success or not result.success:
                        self._pending = result
                    self._intervention_ready.set()
                    self._record_intervention_event(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Safety supervisor observation/evaluation failed")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    def _record_intervention_event(self, result: ActionResult) -> None:
        """Start one detached durable event write for the intervention.

        The safety path never awaits the journal: the write runs as a
        tracked task so a slow journal cannot delay the reflex result, and
        a failed append is logged without suppressing the intervention.
        """

        if self._event_sink is None:
            return
        action = result.action
        payload: dict[str, object] = {
            "action": action.value if isinstance(action, ActionType) else str(action),
            "success": bool(result.success),
        }
        severity = EventSeverity.HIGH if result.success else EventSeverity.CRITICAL
        event = Event(
            event_id=f"evt-{uuid4()}",
            type=_SAFETY_EVENT_TYPE,
            severity=severity,
            steve_id=self._steve_id,
            source=_SAFETY_EVENT_SOURCE,
            payload=payload,
        )
        if self._event_writes:
            # Keep at most one event write in flight and one bounded summary
            # waiting behind it if the durable sink is slow. Retain failure
            # counts and their action even if a later intervention succeeds.
            self._coalesced_event = event
            action_key = (
                str(payload["action"])
                if str(payload["action"]) in _SAFETY_EVENT_ACTIONS
                else "other"
            )
            outcome_key = "succeeded" if result.success else "failed"
            counts = self._coalesced_outcomes.setdefault(action_key, {"failed": 0, "succeeded": 0})
            counts[outcome_key] += 1
            self._coalesced_total += 1
            if not result.success:
                self._coalesced_failed_action = str(payload["action"])
            return

        write_task = asyncio.create_task(self._write_events(event))
        self._event_writes.add(write_task)
        write_task.add_done_callback(self._event_writes.discard)

    def _record_condition_events(self, world: WorldState, started: float) -> None:
        """Queue this observation's condition events without awaiting the journal."""

        if self._conditions is None:
            return
        try:
            events = self._conditions.observe(world, started)
        except Exception:
            logger.exception("Condition event detection failed")
            return
        self._queue_condition_events(events)

    async def _poll_death_events(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._record_death_events()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    async def _record_death_events(self) -> None:
        """Queue a ``steve.died`` event (and any held respawn) per death message."""

        if self._world_events is None or self._conditions is None:
            return
        try:
            messages = await asyncio.wait_for(
                self._world_events.get_death_messages(), _WORLD_EVENT_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not poll death events", exc_info=True)
            return
        for message in messages:
            received = asyncio.get_running_loop().time()
            self._queue_condition_events(self._conditions.record_death(message, received))

    def _queue_condition_events(self, events: list[Event]) -> None:
        for event in events:
            if len(self._condition_queue) >= _MAX_PENDING_CONDITION_EVENTS:
                self._condition_queue.popleft()
                self._condition_dropped += 1
                logger.warning(
                    "Event journal is not keeping up; dropped %d condition event(s) so far",
                    self._condition_dropped,
                )
            self._condition_queue.append(event)
        if self._condition_queue and (
            self._condition_writer is None or self._condition_writer.done()
        ):
            self._condition_writer = asyncio.create_task(self._write_condition_events())

    async def _write_condition_events(self) -> None:
        while self._condition_queue:
            event = self._condition_queue.popleft()
            if event.type in ATTRIBUTED_EVENT_TYPES:
                event = await self._attribute(event)
            await self._append_event(event)

    async def _attribute(self, event: Event) -> Event:
        """Add the likely damage ``source``; ``{"kind": "unknown"}`` if the query fails."""

        if self._world_events is None:
            return event
        source: dict[str, object]
        try:
            attacker = await asyncio.wait_for(
                self._world_events.get_last_attacker(), _WORLD_EVENT_TIMEOUT_SECONDS
            )
            source = damage_source(attacker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not attribute %s event", event.type, exc_info=True)
            source = {"kind": "unknown"}
        return event.model_copy(update={"payload": {**event.payload, "source": source}})

    async def _write_events(self, event: Event) -> None:
        current: Event | None = event
        while True:
            if current is None:
                task = asyncio.current_task()
                if task is not None:
                    self._event_writes.discard(task)
                return
            await self._append_event(current)
            current = self._coalesced_event
            if current is not None:
                current = self._summarize_coalesced_event(current)
            self._coalesced_event = None

    def _summarize_coalesced_event(self, latest: Event) -> Event:
        """Build one bounded event that retains every coalesced outcome count."""

        outcome_counts = {
            action: dict(counts) for action, counts in sorted(self._coalesced_outcomes.items())
        }
        failed = sum(counts["failed"] for counts in outcome_counts.values())
        payload = dict(latest.payload)
        payload["coalesced_count"] = self._coalesced_total
        payload["outcome_counts"] = outcome_counts
        severity = latest.severity
        if failed:
            # Keep the latest failed action as the representative outcome so
            # an intervening success cannot downgrade a critical incident.
            payload["action"] = self._coalesced_failed_action or payload["action"]
            payload["success"] = False
            severity = EventSeverity.CRITICAL
        event = Event(
            event_id=latest.event_id,
            type=latest.type,
            severity=severity,
            occurred_at=latest.occurred_at,
            steve_id=latest.steve_id,
            task_id=latest.task_id,
            causal_action_id=latest.causal_action_id,
            source=latest.source,
            schema_version=latest.schema_version,
            payload=payload,
        )
        self._coalesced_total = 0
        self._coalesced_outcomes.clear()
        self._coalesced_failed_action = None
        return event

    async def _append_event(self, event: Event) -> None:
        try:
            sink = self._event_sink
            if sink is None:
                return
            if inspect.iscoroutinefunction(sink):
                await sink(event)
                return

            # Synchronous journal implementations must never run on the
            # event loop. A callable may return an awaitable despite not
            # being declared async, so handle that result after dispatch.
            result = await asyncio.to_thread(sink, event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to record %s event in journal", event.type)

    async def close(self) -> None:
        """Wait for outstanding event writes without blocking the safety path."""

        if self._event_writes:
            await asyncio.gather(*list(self._event_writes))
        if self._condition_writer is not None:
            await self._condition_writer

    def take_nowait(self) -> ActionResult | None:
        """Return and clear the pending intervention, if one is available."""

        result = self._pending
        if result is not None:
            self._pending = None
            self._intervention_ready.clear()
            if self._on_intervention_consumed is not None:
                self._on_intervention_consumed()
        return result

    async def wait_for_intervention(self) -> ActionResult:
        """Wait for one intervention and consume its result."""

        while True:
            result = self.take_nowait()
            if result is not None:
                return result
            await self._intervention_ready.wait()
