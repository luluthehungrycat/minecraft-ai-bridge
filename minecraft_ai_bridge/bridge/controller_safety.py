"""Safety supervision for a Steve driven by an external controller (#18).

The built-in agent runs its ``SafetySupervisor`` beside the LLM loop, which
consumes each intervention. An external controller has no such loop, so
``ControllerSafety`` runs the same preservation reflexes (reflex attack, auto
eat, auto heal) against the ControllerService's own ActionService and
consumes each intervention itself:

- While a reflex is waiting for or holding the dispatch lock, controller
  actions are preempted: queued ones are refused with ``SAFETY_PREEMPTED``
  before reaching the backend, and multi-step ones stop between steps.
- The intervention is acknowledged as soon as the evaluation that started it
  returns, so controller actions are blocked only while a reflex runs.
- Each intervention is journaled as a ``safety.intervened`` event attributed
  to the Steve, which the controller can poll.

Flee and find-food advice needs a goal tree, so it is not given; a threat
too large to fight falls back to the reflex attack. This is in-process
supervision: it stops with the process and is not a backend watchdog.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..minecraft.actions import ActionResult
from ..minecraft.observer import Observer, WorldState
from ..runtime.contracts import Event
from ..runtime.controller_service import ControllerService
from ..runtime.safety_supervisor import SafetySupervisor
from .self_preservation import PreservationConfig, SelfPreservationLayer

logger = logging.getLogger(__name__)


class ControllerSafety:
    """Run preservation reflexes for a ``ControllerService``'s Steve."""

    def __init__(
        self,
        controller: ControllerService,
        safety_authority: object,
        *,
        event_sink: Callable[[Event], int] | None = None,
        interval: float = 0.25,
        config: PreservationConfig | None = None,
    ) -> None:
        """``safety_authority`` must be the object the controller was built with.

        Without it, the ActionService treats reflexes as ordinary requests
        and the controller's policy refuses them.
        """
        self._actions = controller.action_service
        world = self._actions.world
        self._observer = Observer(world)
        self._preservation = SelfPreservationLayer(
            mc=world,
            goal_manager=None,
            memory=None,
            config=config or PreservationConfig(),
            action_service=self._actions,
            safety_authority=safety_authority,
        )
        self._supervisor = SafetySupervisor(
            self._observer.observe,
            self._evaluate,
            interval=interval,
            event_sink=event_sink,
            steve_id=controller.steve_id,
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _evaluate(self, world: WorldState) -> ActionResult | None:
        try:
            return await self._preservation.evaluate(world)
        finally:
            # No strategic loop consumes interventions here, so release the
            # preemption once the reflex finished (or none started). The
            # supervisor's single pending slot is simply overwritten.
            self._actions.acknowledge_safety_intervention()

    def start(self) -> None:
        """Enable reflex preemption and start polling."""
        if self._task is not None:
            return
        self._actions.enable_safety_preemption()
        self._stop.clear()
        self._task = asyncio.create_task(
            self._supervisor.run(self._stop), name="minecraft-ai-bridge-controller-safety"
        )

    async def aclose(self) -> None:
        """Stop polling, flush pending safety events, and disable preemption."""
        self._stop.set()
        if self._task is not None:
            task, self._task = self._task, None
            try:
                await task
            finally:
                await self._supervisor.close()
                self._actions.disable_safety_preemption()


__all__ = ["ControllerSafety"]
