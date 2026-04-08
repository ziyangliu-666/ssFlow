"""Tests for the external event schedule."""

from __future__ import annotations

import pytest

from ssfish.information.external_events import ExternalEvent, ExternalEventSchedule


class TestExternalEventSchedule:

    def test_empty_schedule(self):
        s = ExternalEventSchedule()
        assert len(s) == 0
        assert not s
        assert s.events_for_round(0) == []

    def test_add_and_retrieve(self):
        s = ExternalEventSchedule()
        ev = ExternalEvent(
            round_idx=3,
            content_type="policy_statement",
            author_persona_id="__market__",
            text="发改委发布新政策",
        )
        s.add(ev)
        assert len(s) == 1
        assert bool(s)
        assert s.events_for_round(3) == [ev]
        assert s.events_for_round(0) == []
        assert s.events_for_round(2) == []

    def test_multiple_events_same_round(self):
        s = ExternalEventSchedule()
        ev1 = ExternalEvent(
            round_idx=2,
            content_type="policy_statement",
            author_persona_id="__market__",
            text="政策 A",
        )
        ev2 = ExternalEvent(
            round_idx=2,
            content_type="news_brief",
            author_persona_id="__market__",
            text="新闻 B",
        )
        s.events.append(ev1)
        s.events.append(ev2)
        result = s.events_for_round(2)
        assert len(result) == 2
        assert ev1 in result
        assert ev2 in result

    def test_default_authority_high(self):
        ev = ExternalEvent(
            round_idx=0,
            content_type="policy_statement",
            author_persona_id="__market__",
            text="x",
        )
        # External events default to high authority
        assert ev.authority_weight == 0.85

    def test_topic_tags_default_empty_list(self):
        ev = ExternalEvent(
            round_idx=0,
            content_type="news_brief",
            author_persona_id="__market__",
            text="x",
        )
        assert ev.topic_tags == []
        # Confirm separate instances don't share state
        ev.topic_tags.append("auto")
        ev2 = ExternalEvent(
            round_idx=0,
            content_type="news_brief",
            author_persona_id="__market__",
            text="x",
        )
        assert ev2.topic_tags == []
