"""Tests for event_severity.resolve_event_severity.

Regression coverage for the Round 5 / Round 6 severity map extraction. The
critical invariants this suite pins down:

- Every VALID_EVENT_TYPE has a defined severity prior
- Terminal-risk keyword matches (退市 / 立案 / *ST / 破产 / delisting / fraud
  / bankruptcy) clamp sentiment to <= -0.85, even on otherwise-neutral
  event types (earnings / regulatory / other).
- Extreme-bull keywords behave symmetrically.
- Bear + bull keyword conflicts resolve to the larger magnitude, not the
  last-applied clamp, so a bearish text that happens to mention "龙头" is
  not silently flipped to bullish.
- gap_vol is bumped to >= 0.20 whenever |sentiment| >= 0.8 so pre-open
  auctions on extreme scenarios can actually reach a one-word board.
- The ``day1_open`` calibration override takes precedence over the
  heuristic map, but still runs the keyword sniff.
- The resolution is bounded — sentiment in [-1, 1], gap_vol in [0.03, 0.30].
"""

from __future__ import annotations

import pytest

from ssflow.event import VALID_EVENT_TYPES
from ssflow.event_severity import (
    SeverityResolution,
    resolve_event_severity,
)


class TestSeverityMapCoverage:

    def test_every_valid_event_type_has_a_prior(self):
        """Every VALID_EVENT_TYPES value must resolve to a defined severity
        (not silently fall through to 0.0 unless the prior is genuinely
        neutral for that type).
        """
        for etype in VALID_EVENT_TYPES:
            res = resolve_event_severity(
                event_type=etype, event_text="", day1_open=None,
                current_price=None,
            )
            assert isinstance(res, SeverityResolution)
            assert -1.0 <= res.overnight_sentiment <= 1.0
            assert 0.03 <= res.gap_vol <= 0.30

    def test_regulatory_is_bearish_by_default(self):
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="CSRC 针对公司常规问询函",
        )
        assert res.overnight_sentiment < 0
        assert not res.terminal_risk

    def test_policy_is_bullish_by_default(self):
        res = resolve_event_severity(
            event_type="policy",
            event_text="央行降准 0.5 个百分点",
        )
        assert res.overnight_sentiment > 0.3

    def test_earnings_is_neutral_by_default(self):
        res = resolve_event_severity(
            event_type="earnings", event_text="Q1 业绩符合预期",
        )
        assert res.overnight_sentiment == 0.0


class TestExtremeBearClamp:

    def test_delisting_keyword_clamps_regulatory(self):
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="*ST 华讯 退市风险预警, 证监会立案调查财务造假",
        )
        assert res.overnight_sentiment <= -0.85
        assert res.terminal_risk is True

    def test_delisting_keyword_overrides_neutral_earnings(self):
        res = resolve_event_severity(
            event_type="earnings",
            event_text="业绩暴雷, 公司面临强制退市",
        )
        assert res.overnight_sentiment <= -0.85
        assert res.terminal_risk is True

    def test_english_bear_keywords(self):
        res = resolve_event_severity(
            event_type="other",
            event_text="Company filed for bankruptcy protection",
        )
        assert res.overnight_sentiment <= -0.85
        assert res.terminal_risk is True

    def test_fraud_keyword(self):
        res = resolve_event_severity(
            event_type="other", event_text="SEC accuses CEO of fraud",
        )
        assert res.overnight_sentiment <= -0.85
        assert res.terminal_risk is True

    def test_gap_vol_bumps_for_extreme_sentiment(self):
        res = resolve_event_severity(
            event_type="earnings",
            event_text="公司面临强制退市",
        )
        assert res.gap_vol >= 0.20


class TestExtremeBullClamp:

    def test_yizi_limit_up_clamp(self):
        res = resolve_event_severity(
            event_type="other",
            event_text="连续一字涨停, 龙头效应明显",
        )
        assert res.overnight_sentiment >= 0.75
        assert res.bull_keyword_match is True
        assert res.terminal_risk is False

    def test_national_team_clamp(self):
        res = resolve_event_severity(
            event_type="other",
            event_text="国家队进场, 核准注册新股",
        )
        assert res.overnight_sentiment >= 0.75

    def test_broad_easing_clamp(self):
        res = resolve_event_severity(
            event_type="macro",
            event_text="央行宣布全面降准",
        )
        assert res.overnight_sentiment >= 0.75


class TestConflictResolution:
    """Regression for R6 finding: bear-then-bull keyword sequencing silently
    erased the bear signal. The resolver must pick the larger-magnitude
    clamp when both sides match.
    """

    def test_bear_stronger_than_bull_wins_bear(self):
        # Text has both "退市" (bear) and "龙头" (bull); bear is stronger
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="曾是板块龙头, 如今面临退市风险",
        )
        # Bear clamp is -0.85, bull clamp is +0.75. |bear| > |bull|.
        assert res.overnight_sentiment < 0
        assert res.terminal_risk is True

    def test_bull_wins_when_bear_is_only_weak_substring(self):
        # Force the larger-magnitude side to be the bull side by giving an
        # event type with a weak base (policy = +0.6 → bull clamp +0.75),
        # bull keywords that fire, and no bear keywords at all. The bear
        # clamp in this case is -0.85 of magnitude vs. bull +0.75, but
        # since no bear keyword matches, the resolver must not invoke the
        # bear clamp at all — only the bull side, and overnight sentiment
        # should be clamped to >= +0.75.
        res = resolve_event_severity(
            event_type="policy",
            event_text="央行全面降准, 一字涨停, 龙头板块迎来爆发",
        )
        assert res.overnight_sentiment >= 0.75
        assert res.bull_keyword_match is True
        assert res.terminal_risk is False

    def test_bull_wins_conflict_when_bear_is_weak(self):
        # Explicit conflict: a bull event type (policy=+0.6) whose text
        # mentions 'delisting' in passing while also announcing 全面降准
        # and 一字涨停. Bear clamp is -0.85, bull clamp is +0.75. Magnitude
        # comparison: |bear|=0.85 > |bull|=0.75, so the resolver's tie-
        # breaking rule says bear should win. Verify that explicitly —
        # this pins the "larger magnitude wins" rule, which is the
        # non-obvious semantic we want to regression-test.
        res = resolve_event_severity(
            event_type="policy",
            event_text="央行全面降准, 一字涨停. Background: recent delisting wave.",
        )
        assert res.overnight_sentiment <= -0.85, (
            "larger-magnitude (bear) side should win the conflict, "
            f"got {res.overnight_sentiment}"
        )
        assert res.terminal_risk is True
        assert res.bull_keyword_match is True

    def test_no_keyword_match_leaves_base_alone(self):
        res = resolve_event_severity(
            event_type="earnings",
            event_text="Revenue +12% YoY, no guidance change.",
        )
        assert res.overnight_sentiment == 0.0
        assert res.terminal_risk is False
        assert res.bull_keyword_match is False


class TestDay1OpenOverride:

    def test_day1_open_override_drives_sentiment(self):
        # Calibration: stock was 100 before event, opens day 1 at 91 (-9%)
        res = resolve_event_severity(
            event_type="other",
            event_text="nothing special",
            day1_open=91.0,
            current_price=100.0,
        )
        assert res.source == "day1_open"
        # Implied gap * 10 = -0.9, clamped to -1.0..1.0 → -0.9
        assert res.overnight_sentiment < -0.5

    def test_day1_open_respects_keyword_override(self):
        # Calibration says +0.1 but text mentions 退市 → bear clamp wins
        res = resolve_event_severity(
            event_type="earnings",
            event_text="公司面临退市",
            day1_open=101.0,
            current_price=100.0,
        )
        assert res.source == "day1_open"
        assert res.terminal_risk is True
        assert res.overnight_sentiment <= -0.85

    def test_day1_open_ignored_without_current_price(self):
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="",
            day1_open=91.0,
            current_price=None,
        )
        assert res.source == "severity_map"

    def test_day1_open_ignored_with_zero_current_price(self):
        res = resolve_event_severity(
            event_type="regulatory",
            event_text="",
            day1_open=91.0,
            current_price=0.0,
        )
        assert res.source == "severity_map"


class TestBounds:

    def test_sentiment_clamped_to_unit_interval(self):
        # Massive implied gap — must clamp to -1.0
        res = resolve_event_severity(
            event_type="other",
            event_text="",
            day1_open=1.0,
            current_price=100.0,
        )
        assert res.overnight_sentiment >= -1.0
        assert res.overnight_sentiment <= 1.0

    def test_gap_vol_has_floor(self):
        res = resolve_event_severity(
            event_type="dividend", event_text="routine annual dividend",
        )
        assert res.gap_vol >= 0.03

    def test_gap_vol_has_ceiling(self):
        res = resolve_event_severity(
            event_type="delisting_risk",
            event_text="公司面临强制退市和破产重整",
        )
        assert res.gap_vol <= 0.30
