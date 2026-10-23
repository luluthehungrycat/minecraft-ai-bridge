"""Independently scheduled safety checks for the built-in runtime."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from ..minecraft.actions import ActionResult, ActionType
from ..minecraft.observer import WorldState
from .contracts import Event, EventSeverity

logger = logging.getLogger(__name__)

_SAFETY_EVENT_TYPE = "safety.intervened"
_SAFETY_EVENT_SOURCE = "builtin_safety_supervisor"


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
    ) -> None:
        if interval <= 0:
            raise ValueError("safety supervisor interval must be positive")
        self._observe = observe
        self._evaluate = evaluate
        self._interval = interval
        self._on_intervention_consumed = on_intervention_consumed
        self._event_sink = event_sink
        self._event_writes: set[asyncio.Task[None]] = set()
        self._pending: ActionResult | None = None
        self._intervention_ready = asyncio.Event()
        self._pending_consumed = asyncio.Event()
        self._pending_consumed.set()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll independently until asked to stop; isolate per-tick failures."""

        while not stop_event.is_set():
            if self._pending is not None:
                consumed_task = asyncio.create_task(self._pending_consumed.wait())
                stopped_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    (consumed_task, stopped_task),
                    timeout=self._interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if stopped_task in done and stop_event.is_set():
                    break
                continue
            try:
                world = await self._observe()
                if stop_event.is_set():
                    break
                result = await self._evaluate(world)
                if result is not None and self._pending is None:
                    self._pending = result
                    self._pending_consumed.clear()
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
            source=_SAFETY_EVENT_SOURCE,
            payload=payload,
        )
        write_task = asyncio.create_task(self._append_event(event))
        self._event_writes.add(write_task)
        write_task.add_done_callback(self._event_writes.discard)

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
            logger.exception("Failed to record safety intervention event in journal")

    async def close(self) -> None:
        """Wait for outstanding event writes without blocking the safety path."""

        if self._event_writes:
            await asyncio.gather(*list(self._event_writes))

    def take_nowait(self) -> ActionResult | None:
        """Return and clear the pending intervention, if one is available."""

        result = self._pending
        if result is not None:
            self._pending = None
            self._intervention_ready.clear()
            self._pending_consumed.set()
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
