"""Tests for dynamic event injection (Fix 1: DynamicEventStream)."""

import pytest

from ssflow.information.external_events import (
    ConditionalEvent,
    DynamicEventStream,
    ExternalEvent,
    ExternalEventSchedule,
    SimSnapshot,
)


def _make_snapshot(round_idx=0, cum_delta=0.0, price=100.0, **kwargs):
    return SimSnapshot(
        round_idx=round_idx,
        current_price=price,
        initial_price=100.0,
        cumulative_delta_pct=cum_delta,
        net_flow_last_round=0.0,
        round_count=6,
        agent_states={},
        **kwargs,
    )


class TestSimSnapshot:
    def test_frozen(self):
        snap = _make_snapshot()
        with pytest.raises(AttributeError):
            snap.current_price = 200.0

    def test_values(self):
        snap = _make_snapshot(round_idx=3, cum_delta=-0.08, price=92.0)
        assert snap.round_idx == 3
        assert snap.cumulative_delta_pct == -0.08
        assert snap.current_price == 92.0


class TestStaticScheduleCompat:
    def test_with_snapshot_param(self):
        """ExternalEventSchedule.events_for_round accepts snapshot kwarg."""
        sched = ExternalEventSchedule()
        sched.add(ExternalEvent(
            round_idx=1, content_type="news_brief",
            author_persona_id="__market__", text="test event",
        ))
        snap = _make_snapshot(round_idx=1)
        events = sched.events_for_round(1, snapshot=snap)
        assert len(events) == 1
        assert events[0].text == "test event"

    def test_without_snapshot(self):
        sched = ExternalEventSchedule()
        sched.add(ExternalEvent(
            round_idx=0, content_type="news_brief",
            author_persona_id="__market__", text="r0 event",
        ))
        events = sched.events_for_round(0)
        assert len(events) == 1


class TestConditionalEvent:
    def test_fires_on_condition(self):
        cond = ConditionalEvent(
            condition=lambda s: s.cumulative_delta_pct < -0.10,
            event_factory=lambda s: ExternalEvent(
                round_idx=s.round_idx, content_type="policy_statement",
                author_persona_id="csrc", text="stabilization",
            ),
            max_fires=1,
        )
        # Not triggered at -5%
        snap_mild = _make_snapshot(cum_delta=-0.05)
        assert not cond.should_fire(snap_mild)

        # Triggered at -12%
        snap_crisis = _make_snapshot(round_idx=2, cum_delta=-0.12)
        assert cond.should_fire(snap_crisis)

        ev = cond.fire(snap_crisis)
        assert ev.text == "stabilization"
        assert ev.content_type == "policy_statement"

    def test_max_fires_respected(self):
        cond = ConditionalEvent(
            condition=lambda s: True,
            event_factory=lambda s: ExternalEvent(
                round_idx=s.round_idx, content_type="news_brief",
                author_persona_id="__market__", text="test",
            ),
            max_fires=2,
        )
        snap = _make_snapshot()
        assert cond.should_fire(snap)
        cond.fire(snap)
        assert cond.should_fire(snap)
        cond.fire(snap)
        assert not cond.should_fire(snap)  # exhausted

    def test_cooldown_respected(self):
        cond = ConditionalEvent(
            condition=lambda s: True,
            event_factory=lambda s: ExternalEvent(
                round_idx=s.round_idx, content_type="news_brief",
                author_persona_id="__market__", text="test",
            ),
            max_fires=-1,
            cooldown_rounds=3,
        )
        snap_r0 = _make_snapshot(round_idx=0)
        assert cond.should_fire(snap_r0)
        cond.fire(snap_r0)

        snap_r1 = _make_snapshot(round_idx=1)
        assert not cond.should_fire(snap_r1)  # cooldown

        snap_r3 = _make_snapshot(round_idx=3)
        assert cond.should_fire(snap_r3)  # cooldown expired

    def test_condition_exception_returns_false(self):
        cond = ConditionalEvent(
            condition=lambda s: 1 / 0,  # will raise
            event_factory=lambda s: ExternalEvent(
                round_idx=0, content_type="news_brief",
                author_persona_id="__market__", text="test",
            ),
        )
        snap = _make_snapshot()
        assert not cond.should_fire(snap)


class TestDynamicEventStream:
    def test_combines_static_and_conditional(self):
        static = ExternalEventSchedule()
        static.add(ExternalEvent(
            round_idx=2, content_type="news_brief",
            author_persona_id="__market__", text="scheduled event",
        ))
        cond = ConditionalEvent(
            condition=lambda s: s.cumulative_delta_pct < -0.05,
            event_factory=lambda s: ExternalEvent(
                round_idx=s.round_idx, content_type="policy_statement",
                author_persona_id="csrc", text="reactive event",
            ),
        )
        stream = DynamicEventStream(
            static_schedule=static,
            conditionals=[cond],
        )

        snap = _make_snapshot(round_idx=2, cum_delta=-0.08)
        events = stream.events_for_round(2, snapshot=snap)
        assert len(events) == 2
        texts = {e.text for e in events}
        assert "scheduled event" in texts
        assert "reactive event" in texts

    def test_generator_callback(self):
        def gen(snap):
            if snap.round_idx >= 3:
                return [ExternalEvent(
                    round_idx=snap.round_idx, content_type="news_brief",
                    author_persona_id="__market__",
                    text=f"gen event at R{snap.round_idx}",
                )]
            return []

        stream = DynamicEventStream(generator=gen)
        snap_r1 = _make_snapshot(round_idx=1)
        assert len(stream.events_for_round(1, snapshot=snap_r1)) == 0

        snap_r3 = _make_snapshot(round_idx=3)
        events = stream.events_for_round(3, snapshot=snap_r3)
        assert len(events) == 1
        assert "gen event at R3" in events[0].text

    def test_empty_stream(self):
        stream = DynamicEventStream()
        snap = _make_snapshot()
        assert len(stream.events_for_round(0, snapshot=snap)) == 0

    def test_without_snapshot(self):
        """Static events still work even without snapshot."""
        static = ExternalEventSchedule()
        static.add(ExternalEvent(
            round_idx=0, content_type="news_brief",
            author_persona_id="__market__", text="r0",
        ))
        stream = DynamicEventStream(static_schedule=static)
        events = stream.events_for_round(0)
        assert len(events) == 1

    def test_add_delegates(self):
        stream = DynamicEventStream()
        stream.add(ExternalEvent(
            round_idx=1, content_type="news_brief",
            author_persona_id="__market__", text="added",
        ))
        events = stream.events_for_round(1)
        assert len(events) == 1

    def test_bool_with_conditionals(self):
        stream = DynamicEventStream(conditionals=[
            ConditionalEvent(
                condition=lambda s: True,
                event_factory=lambda s: ExternalEvent(
                    round_idx=0, content_type="news_brief",
                    author_persona_id="__market__", text="t",
                ),
            ),
        ])
        assert bool(stream)

    def test_bool_empty(self):
        stream = DynamicEventStream()
        assert not bool(stream)
