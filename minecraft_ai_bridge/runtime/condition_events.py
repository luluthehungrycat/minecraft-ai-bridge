"""Steve condition events derived from successive world observations (#18).

``ConditionEventDetector`` turns the snapshots the safety supervisor already
takes into a small number of durable events a controller can poll:

- ``steve.damaged`` — health dropped by at least ``min_damage``. During
  sustained damage at most one event is emitted per ``damage_cooldown``
  seconds; damage taken during the cooldown is added to the next event, so
  ``amount`` and ``hits`` cover every drop since the previous event.
- ``steve.health_low`` — health fell below ``health_low_threshold``.
- ``steve.hunger_low`` — hunger fell below ``hunger_low_threshold``.
- ``steve.respawned`` — the bot is alive (health above 0) again after an
  observation showed it dead or :meth:`ConditionEventDetector.mark_dead`
  reported a death. The bot plugin respawns the bot one tick after death,
  usually before any observation sees health 0.

``steve.died`` comes from the backend's death events rather than from
observations; :func:`death_event` builds it, and the server's death message
names the cause. The supervisor adds a damage ``source`` (see
:func:`damage_source`) to ``steve.damaged`` before journaling it; deaths are
not attributed that way because the respawn clears the attacker record.

The low-value events are edge-triggered: each fires once when the value
first crosses below its threshold (including on the first observation) and
fires again only after the value has recovered to at least
``threshold + hysteresis``. Observations without a health or hunger value
leave that condition's state unchanged.

The detector only reads values; it neither attributes damage to a source nor
acts on the world. It is deterministic given the observation sequence and
the ``now`` timestamps, which makes it independent of wall-clock time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from ..minecraft.observer import WorldState
from .contracts import Event, EventSeverity

if TYPE_CHECKING:
    from ..minecraft.mc_api import LastAttacker

DAMAGED_EVENT_TYPE = "steve.damaged"
HEALTH_LOW_EVENT_TYPE = "steve.health_low"
HUNGER_LOW_EVENT_TYPE = "steve.hunger_low"
DIED_EVENT_TYPE = "steve.died"
RESPAWNED_EVENT_TYPE = "steve.respawned"
CONDITION_EVENT_SOURCE = "condition_monitor"
# Event types that get a damage ``source`` before they are journaled.
ATTRIBUTED_EVENT_TYPES = frozenset({DAMAGED_EVENT_TYPE})


@dataclass(frozen=True)
class ConditionEventConfig:
    """Thresholds in Minecraft units (health and hunger are 0-20)."""

    min_damage: float = 1.5
    damage_cooldown: float = 2.0
    health_low_threshold: float = 4.0
    hunger_low_threshold: float = 6.0
    hysteresis: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "min_damage",
            "damage_cooldown",
            "health_low_threshold",
            "hunger_low_threshold",
            "hysteresis",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative number")
        if self.min_damage == 0:
            raise ValueError("min_damage must be positive")


@dataclass
class _LowValueState:
    """Edge trigger with hysteresis for one value that must not drop too low."""

    threshold: float
    hysteresis: float
    armed: bool = True

    def crossed_below(self, value: float) -> bool:
        if self.armed and value < self.threshold:
            self.armed = False
            return True
        if not self.armed and value >= self.threshold + self.hysteresis:
            self.armed = True
        return False


@dataclass
class _DamageState:
    previous_health: float | None = None
    last_emitted_at: float | None = None
    # Damage seen during the cooldown, reported with the next event.
    pending_amount: float = 0.0
    pending_hits: int = 0
    pending_from_health: float | None = None


@dataclass
class ConditionEventDetector:
    """Derive ``steve.*`` condition events from successive ``WorldState``s."""

    config: ConditionEventConfig = field(default_factory=ConditionEventConfig)
    steve_id: str | None = None

    def __post_init__(self) -> None:
        self._damage = _DamageState()
        self._health_low = _LowValueState(self.config.health_low_threshold, self.config.hysteresis)
        self._hunger_low = _LowValueState(self.config.hunger_low_threshold, self.config.hysteresis)
        self._dead = False

    def mark_dead(self) -> None:
        """Record a backend-reported death; the next live observation is a respawn."""
        self._dead = True

    def observe(self, world: WorldState, now: float) -> list[Event]:
        """Return the events this observation triggers (usually none).

        ``now`` is a monotonic timestamp in seconds used only for the damage
        cooldown.
        """
        events: list[Event] = []
        health = _finite(world.health)
        hunger = _finite(world.hunger)

        if health is not None:
            if health <= 0:
                self._dead = True
            elif self._dead:
                self._dead = False
                position = world.position
                events.append(
                    self._event(
                        RESPAWNED_EVENT_TYPE,
                        EventSeverity.INFO,
                        {
                            "health": health,
                            "position": list(position) if position is not None else None,
                        },
                    )
                )
            damaged = self._observe_damage(health, now)
            if damaged is not None:
                events.append(damaged)
            if self._health_low.crossed_below(health):
                events.append(
                    self._event(
                        HEALTH_LOW_EVENT_TYPE,
                        EventSeverity.HIGH,
                        {"health": health, "threshold": self.config.health_low_threshold},
                    )
                )
        elif self._damage.pending_hits and self._cooldown_elapsed(now):
            # No new health value, but damage from the cooldown is still
            # unreported: report it against the last known health.
            last = self._damage.previous_health
            if last is not None:
                events.append(self._flush_damage(last, now))

        if hunger is not None and self._hunger_low.crossed_below(hunger):
            events.append(
                self._event(
                    HUNGER_LOW_EVENT_TYPE,
                    EventSeverity.MEDIUM,
                    {"hunger": hunger, "threshold": self.config.hunger_low_threshold},
                )
            )
        return events

    def _observe_damage(self, health: float, now: float) -> Event | None:
        state = self._damage
        previous, state.previous_health = state.previous_health, health
        if previous is not None:
            drop = previous - health
            if drop >= self.config.min_damage:
                if state.pending_from_health is None:
                    state.pending_from_health = previous
                state.pending_amount += drop
                state.pending_hits += 1
        if state.pending_hits and self._cooldown_elapsed(now):
            return self._flush_damage(health, now)
        return None

    def _cooldown_elapsed(self, now: float) -> bool:
        last = self._damage.last_emitted_at
        return last is None or now - last >= self.config.damage_cooldown

    def _flush_damage(self, health: float, now: float) -> Event:
        state = self._damage
        payload: dict[str, object] = {
            "health": health,
            "previous_health": state.pending_from_health,
            "amount": round(state.pending_amount, 3),
            "hits": state.pending_hits,
        }
        state.last_emitted_at = now
        state.pending_amount = 0.0
        state.pending_hits = 0
        state.pending_from_health = None
        severity = (
            EventSeverity.HIGH
            if health < self.config.health_low_threshold
            else EventSeverity.MEDIUM
        )
        return self._event(DAMAGED_EVENT_TYPE, severity, payload)

    def _event(self, event_type: str, severity: EventSeverity, payload: dict[str, object]) -> Event:
        return Event(
            event_id=f"evt-{uuid4()}",
            type=event_type,
            severity=severity,
            steve_id=self.steve_id,
            source=CONDITION_EVENT_SOURCE,
            payload=payload,
        )


def death_event(message: str, steve_id: str | None) -> Event:
    """Build a ``steve.died`` event from a backend death message.

    ``message`` is the server's death message, for example
    ``"AIBot was blown up by Creeper"``.
    """
    return Event(
        event_id=f"evt-{uuid4()}",
        type=DIED_EVENT_TYPE,
        severity=EventSeverity.CRITICAL,
        steve_id=steve_id,
        source=CONDITION_EVENT_SOURCE,
        payload={"message": message[:500]},
    )


def damage_source(attacker: LastAttacker | None) -> dict[str, object]:
    """Describe the likely damage source for an event payload.

    ``{"kind": "entity", "entity_type": "minecraft:zombie"}`` when an entity
    hurt the bot recently (``entity_type`` is None if it is no longer
    loaded or the server does not record it), otherwise
    ``{"kind": "environment"}``. This is an inference: an entity hit within
    the last few seconds is credited even if a later fall did the damage.
    """
    if attacker is None:
        return {"kind": "environment"}
    return {"kind": "entity", "entity_type": attacker.entity_type}


def _finite(value: float | int | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "ATTRIBUTED_EVENT_TYPES",
    "CONDITION_EVENT_SOURCE",
    "DAMAGED_EVENT_TYPE",
    "DIED_EVENT_TYPE",
    "HEALTH_LOW_EVENT_TYPE",
    "HUNGER_LOW_EVENT_TYPE",
    "RESPAWNED_EVENT_TYPE",
    "ConditionEventConfig",
    "ConditionEventDetector",
    "damage_source",
    "death_event",
]
