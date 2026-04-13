"""ssFlow event bus — stream simulation events as they happen.

Used by `oasis_engine.run_simulation` to emit per-round progress events
(round_start, persona_thought, trade_submitted, price_updated, …) so a
Flask SSE route can forward them to the browser in near-real time.

Design notes
------------
* Sinks are **synchronous**: `emit()` MUST return immediately without
  blocking. The engine runs in an async context and emit is called from
  the hot loop — a slow sink would stall the whole simulation.
* Events are plain JSON-serializable dicts. The `type` key is always one
  of the ``EVENT_*`` constants defined below.
* `safe_emit()` swallows exceptions from broken sinks so a bad subscriber
  can never crash a running simulation.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Any, Callable, Protocol


log = logging.getLogger(__name__)


# ─────────────────────── Event type constants ───────────────────────

EVENT_SIMULATION_START = "simulation_start"
EVENT_ROUND_START = "round_start"
EVENT_EXTERNAL_EVENT_INJECTED = "external_event_injected"
EVENT_PERSONA_THOUGHT = "persona_thought"
EVENT_TRADE_SUBMITTED = "trade_submitted"
EVENT_CLASS_FLOW_COMPUTED = "class_flow_computed"
EVENT_PRICE_UPDATED = "price_updated"
EVENT_ROUND_COMPLETE = "round_complete"
EVENT_SIMULATION_COMPLETE = "simulation_complete"
EVENT_ERROR = "error"

# Entity State Sandbox events (legacy, kept for frontend compat)
EVENT_ENTITY_STATE_UPDATED = "entity_state_updated"
EVENT_RESOURCE_FLOW_EXECUTED = "resource_flow_executed"
EVENT_THRESHOLD_FIRED = "threshold_fired"
EVENT_FORCE_ACTION_OVERRIDE = "force_action_override"

# Policy engine events (Phase 2 unified model)
EVENT_POLICY_FIRED = "policy_fired"

# Non-trader agent actions (Phase 3)
EVENT_AGENT_ACTION = "agent_action"

# Dynamic policy creation (Phase 4)
EVENT_POLICY_CREATED = "policy_created"

# Per-persona self-model state (Phase 5 — self_model subsystem).
# Emitted once per trader persona per round with state dict, utility,
# and utility_delta. Frontend's PersonaStatePanel reads this into the
# session.personaStates store — separate from entity_state_updated
# (which is for EntityGraph entities) so the two domains never collide.
EVENT_PERSONA_STATE_UPDATED = "persona_state_updated"

# Phase-based execution events (round_phase subsystem)
EVENT_PHASE_COMPLETE = "phase_complete"
EVENT_PLAN_CREATED = "plan_created"
EVENT_PLAN_SLICE_EXECUTED = "plan_slice_executed"
EVENT_PLAN_CANCELLED = "plan_cancelled"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_SIMULATION_START,
        EVENT_ROUND_START,
        EVENT_EXTERNAL_EVENT_INJECTED,
        EVENT_PERSONA_THOUGHT,
        EVENT_TRADE_SUBMITTED,
        EVENT_CLASS_FLOW_COMPUTED,
        EVENT_PRICE_UPDATED,
        EVENT_ROUND_COMPLETE,
        EVENT_SIMULATION_COMPLETE,
        EVENT_ERROR,
        EVENT_ENTITY_STATE_UPDATED,
        EVENT_RESOURCE_FLOW_EXECUTED,
        EVENT_THRESHOLD_FIRED,
        EVENT_FORCE_ACTION_OVERRIDE,
        EVENT_POLICY_FIRED,
        EVENT_AGENT_ACTION,
        EVENT_POLICY_CREATED,
        EVENT_PERSONA_STATE_UPDATED,
        EVENT_PHASE_COMPLETE,
        EVENT_PLAN_CREATED,
        EVENT_PLAN_SLICE_EXECUTED,
        EVENT_PLAN_CANCELLED,
    }
)


# ─────────────────────── Sink protocol ───────────────────────


class EventSink(Protocol):
    """A subscriber to engine progress events.

    Implementations must be sync and non-blocking. The `emit` method
    receives a plain dict with a ``type`` key and a ``ts`` (wall-clock
    float) key, plus event-specific fields.
    """

    def emit(self, event: dict[str, Any]) -> None:  # pragma: no cover — protocol
        ...


# ─────────────────────── Concrete sinks ───────────────────────


class ListSink:
    """Collects events into an in-memory list. Used by unit tests and CLIs."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    # ── Convenience accessors for tests ──

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == event_type]

    def types_in_order(self) -> list[str]:
        return [str(e.get("type", "")) for e in self.events]

    def clear(self) -> None:
        self.events.clear()


class QueueSink:
    """Drops events onto a stdlib ``queue.Queue`` for cross-thread consumption.

    Used by the Flask SSE route: the engine runs in a background thread
    with its own asyncio loop, emits events into the queue, and a sync
    generator on the request thread drains the queue and yields SSE
    frames.

    If the queue is full the event is dropped with a warning rather than
    blocking — we never want to stall the engine because a client is slow
    to read.
    """

    def __init__(self, q: "queue.Queue[dict[str, Any] | None]") -> None:
        self.q = q

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self.q.put_nowait(dict(event))
        except queue.Full:
            log.warning(
                "QueueSink full, dropping event type=%s", event.get("type")
            )


class CallbackSink:
    """Invokes a user-supplied callback for each event."""

    def __init__(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._cb = callback

    def emit(self, event: dict[str, Any]) -> None:
        self._cb(dict(event))


class TeeSink:
    """Fans every event out to multiple downstream sinks.

    Used by the SSE route to emit the live stream to the browser
    (`QueueSink`) while also capturing the full event log for replay
    (`ListSink`). A failure in one downstream sink never prevents the
    others from receiving the event.
    """

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = tuple(s for s in sinks if s is not None)

    def emit(self, event: dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception as exc:
                log.warning(
                    "TeeSink downstream failed (ignored): %s %s",
                    type(sink).__name__,
                    exc,
                )


# ─────────────────────── Helpers used by the engine ───────────────────────


def make_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build an event dict with the given type + a wall-clock timestamp.

    Unknown event types are still passed through (with a warning) so that
    adding a new instrumentation point in the engine does not require
    simultaneously updating this module.
    """
    if event_type not in ALL_EVENT_TYPES:
        log.warning("unknown event type emitted: %s", event_type)
    return {"type": event_type, "ts": time.time(), **fields}


def safe_emit(
    sink: EventSink | None,
    event_type: str,
    **fields: Any,
) -> None:
    """Emit an event, swallowing sink errors.

    This is the canonical call site used inside `run_simulation`. If the
    sink is None this is a no-op (zero overhead). If the sink raises,
    the exception is logged but NOT propagated — a broken subscriber
    must never crash a simulation.
    """
    if sink is None:
        return
    try:
        sink.emit(make_event(event_type, **fields))
    except Exception as exc:  # pragma: no cover — sink bugs are logged, not raised
        log.warning("EventSink.emit raised (ignored): %s", exc)


__all__ = [
    "EVENT_SIMULATION_START",
    "EVENT_ROUND_START",
    "EVENT_EXTERNAL_EVENT_INJECTED",
    "EVENT_PERSONA_THOUGHT",
    "EVENT_TRADE_SUBMITTED",
    "EVENT_CLASS_FLOW_COMPUTED",
    "EVENT_PRICE_UPDATED",
    "EVENT_ROUND_COMPLETE",
    "EVENT_SIMULATION_COMPLETE",
    "EVENT_ERROR",
    "EVENT_FORCE_ACTION_OVERRIDE",
    "EVENT_PHASE_COMPLETE",
    "EVENT_PLAN_CREATED",
    "EVENT_PLAN_SLICE_EXECUTED",
    "EVENT_PLAN_CANCELLED",
    "ALL_EVENT_TYPES",
    "EventSink",
    "ListSink",
    "QueueSink",
    "CallbackSink",
    "TeeSink",
    "make_event",
    "safe_emit",
]
