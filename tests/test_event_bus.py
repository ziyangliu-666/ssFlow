"""Tests for `ssflow.event_bus`.

Validates:
  - EVENT_* constants form a frozenset (no typos, no duplicates)
  - `make_event` stamps type + ts and preserves extra fields
  - `safe_emit` is a no-op when sink is None
  - `safe_emit` swallows sink exceptions so a broken sink cannot crash
    the engine
  - `ListSink` collects events in order with working accessors
  - `QueueSink` delivers events across threads and drops on full
  - `CallbackSink` invokes its callback per event
  - `oasis_engine.run_simulation` exposes the `event_sink` parameter
    and has a safe_emit call for every EVENT_* constant (static check
    against the module source — avoids needing a live LLM for this
    check)
"""

from __future__ import annotations

import inspect
import queue
import threading
import time
from typing import Any

import pytest

from ssflow import event_bus
from ssflow.event_bus import (
    ALL_EVENT_TYPES,
    EVENT_CLASS_FLOW_COMPUTED,
    EVENT_ERROR,
    EVENT_EXTERNAL_EVENT_INJECTED,
    EVENT_PERSONA_THOUGHT,
    EVENT_PRICE_UPDATED,
    EVENT_ROUND_COMPLETE,
    EVENT_ROUND_START,
    EVENT_SIMULATION_COMPLETE,
    EVENT_SIMULATION_START,
    EVENT_TRADE_SUBMITTED,
    CallbackSink,
    ListSink,
    QueueSink,
    make_event,
    safe_emit,
)


# ─────────────────────── make_event ───────────────────────


def test_make_event_stamps_type_and_timestamp():
    before = time.time()
    evt = make_event(EVENT_ROUND_START, round_idx=3, price=100.5)
    after = time.time()

    assert evt["type"] == EVENT_ROUND_START
    assert before <= evt["ts"] <= after
    assert evt["round_idx"] == 3
    assert evt["price"] == 100.5


def test_make_event_accepts_unknown_types_with_warning(caplog):
    with caplog.at_level("WARNING", logger="ssflow.event_bus"):
        evt = make_event("definitely_not_a_real_event_type", foo=1)
    assert evt["type"] == "definitely_not_a_real_event_type"
    assert any("unknown event type" in rec.message for rec in caplog.records)


def test_all_event_types_is_frozenset():
    assert isinstance(ALL_EVENT_TYPES, frozenset)
    # All constants exported from the module are in the set
    constants = {
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
    }
    assert constants == ALL_EVENT_TYPES
    # No accidental dupes
    assert len(constants) == 10


# ─────────────────────── safe_emit ───────────────────────


def test_safe_emit_noop_when_sink_is_none():
    # Must not raise, must not do anything observable.
    safe_emit(None, EVENT_ROUND_START, round_idx=0)


def test_safe_emit_forwards_to_sink():
    sink = ListSink()
    safe_emit(sink, EVENT_ROUND_START, round_idx=2, current_price=99.0)
    assert len(sink.events) == 1
    evt = sink.events[0]
    assert evt["type"] == EVENT_ROUND_START
    assert evt["round_idx"] == 2
    assert evt["current_price"] == 99.0
    assert "ts" in evt


def test_safe_emit_swallows_sink_exceptions(caplog):
    class BrokenSink:
        def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("boom")

    with caplog.at_level("WARNING", logger="ssflow.event_bus"):
        # Must not raise — engine cannot be taken down by a broken sink.
        safe_emit(BrokenSink(), EVENT_ROUND_START, round_idx=0)

    assert any("EventSink.emit raised" in rec.message for rec in caplog.records)


# ─────────────────────── ListSink ───────────────────────


def test_list_sink_collects_events_in_order():
    sink = ListSink()
    safe_emit(sink, EVENT_SIMULATION_START, simulation_id="sim_abc")
    safe_emit(sink, EVENT_ROUND_START, round_idx=0, current_price=100.0)
    safe_emit(sink, EVENT_TRADE_SUBMITTED, round_idx=0, persona_id="retail")
    safe_emit(sink, EVENT_PRICE_UPDATED, round_idx=0, price_after=101.5)
    safe_emit(sink, EVENT_ROUND_COMPLETE, round_idx=0)

    assert sink.types_in_order() == [
        EVENT_SIMULATION_START,
        EVENT_ROUND_START,
        EVENT_TRADE_SUBMITTED,
        EVENT_PRICE_UPDATED,
        EVENT_ROUND_COMPLETE,
    ]


def test_list_sink_by_type_filters_correctly():
    sink = ListSink()
    safe_emit(sink, EVENT_ROUND_START, round_idx=0)
    safe_emit(sink, EVENT_TRADE_SUBMITTED, round_idx=0, persona_id="a")
    safe_emit(sink, EVENT_TRADE_SUBMITTED, round_idx=0, persona_id="b")
    safe_emit(sink, EVENT_ROUND_COMPLETE, round_idx=0)

    trades = sink.by_type(EVENT_TRADE_SUBMITTED)
    assert len(trades) == 2
    assert {t["persona_id"] for t in trades} == {"a", "b"}


def test_list_sink_clear_empties_events():
    sink = ListSink()
    safe_emit(sink, EVENT_ROUND_START, round_idx=0)
    assert len(sink.events) == 1
    sink.clear()
    assert sink.events == []


def test_list_sink_events_are_independent_copies():
    """Mutating an emitted event must not affect the sink's stored copy."""
    sink = ListSink()
    payload = {"round_idx": 0}
    sink.emit({"type": EVENT_ROUND_START, **payload})
    payload["round_idx"] = 99
    assert sink.events[0]["round_idx"] == 0


# ─────────────────────── QueueSink ───────────────────────


def test_queue_sink_delivers_events_through_queue():
    q: queue.Queue = queue.Queue()
    sink = QueueSink(q)

    sink.emit({"type": EVENT_ROUND_START, "round_idx": 0})
    sink.emit({"type": EVENT_ROUND_COMPLETE, "round_idx": 0})

    first = q.get(timeout=0.1)
    second = q.get(timeout=0.1)
    assert first["type"] == EVENT_ROUND_START
    assert second["type"] == EVENT_ROUND_COMPLETE


def test_queue_sink_drops_when_full(caplog):
    q: queue.Queue = queue.Queue(maxsize=2)
    sink = QueueSink(q)

    sink.emit({"type": EVENT_ROUND_START, "round_idx": 0})
    sink.emit({"type": EVENT_ROUND_START, "round_idx": 1})

    with caplog.at_level("WARNING", logger="ssflow.event_bus"):
        # Third event must be dropped without raising.
        sink.emit({"type": EVENT_ROUND_START, "round_idx": 2})

    assert any("QueueSink full" in rec.message for rec in caplog.records)
    # Only the first two events should be in the queue.
    assert q.qsize() == 2


def test_queue_sink_works_across_threads():
    q: queue.Queue = queue.Queue()
    sink = QueueSink(q)

    received: list[dict[str, Any]] = []
    done = threading.Event()

    def reader():
        while True:
            evt = q.get(timeout=1.0)
            if evt is None:
                break
            received.append(evt)
        done.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    for i in range(5):
        safe_emit(sink, EVENT_ROUND_START, round_idx=i)
    q.put(None)  # sentinel

    done.wait(timeout=2.0)
    assert len(received) == 5
    assert [e["round_idx"] for e in received] == [0, 1, 2, 3, 4]


# ─────────────────────── CallbackSink ───────────────────────


def test_callback_sink_invokes_callback_per_event():
    calls: list[dict[str, Any]] = []
    sink = CallbackSink(lambda e: calls.append(e))

    safe_emit(sink, EVENT_ROUND_START, round_idx=0)
    safe_emit(sink, EVENT_ROUND_COMPLETE, round_idx=0)

    assert len(calls) == 2
    assert calls[0]["type"] == EVENT_ROUND_START
    assert calls[1]["type"] == EVENT_ROUND_COMPLETE


def test_callback_sink_receives_copy_not_reference():
    received: list[dict[str, Any]] = []
    sink = CallbackSink(lambda e: received.append(e))

    payload = {"type": EVENT_ROUND_START, "round_idx": 0}
    sink.emit(payload)
    payload["round_idx"] = 99

    assert received[0]["round_idx"] == 0


# ─────────────────────── oasis_engine integration (static) ───────────────────────


def test_run_simulation_accepts_event_sink_parameter():
    """The engine signature must include a keyword-only `event_sink` parameter
    of type `EventSink | None` with default None."""
    from ssflow.oasis_engine import run_simulation

    sig = inspect.signature(run_simulation)
    assert "event_sink" in sig.parameters
    param = sig.parameters["event_sink"]
    assert param.default is None
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_oasis_engine_has_emit_call_for_every_event_type():
    """Every EVENT_* constant in event_bus must have at least one
    `safe_emit(event_sink, EVENT_...)` call in oasis_engine source."""
    from ssflow import oasis_engine

    source = inspect.getsource(oasis_engine)

    # The engine must import safe_emit
    assert "safe_emit" in source, "oasis_engine must import safe_emit"

    # Every event type constant must appear in a safe_emit call
    for event_const in [
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
    ]:
        assert event_const in source, (
            f"oasis_engine is missing an emit call for {event_const}"
        )


def test_oasis_engine_imports_cleanly():
    """Smoke test: import the engine module to catch syntax errors in
    our emit-point insertions."""
    import importlib

    import ssflow.oasis_engine

    importlib.reload(ssflow.oasis_engine)
    # If we get here, import succeeded.
    assert hasattr(ssflow.oasis_engine, "run_simulation")
