"""Tests for aggregation math (no LLM calls)."""

from __future__ import annotations

import pytest

from ssfish.aggregation import aggregate
from ssfish.event import Event
from ssfish.persona import MarketShare, Persona
from ssfish.simulation import Reaction, SimulationResult


def _persona(pid: str, by_volume: float = 1.0) -> Persona:
    """Construct a minimal v2 Persona for aggregation tests.

    `by_volume` is the persona's market_share[by_volume] weight, which is the
    default dimension used by `_weighted_sentiment`. Schema-required fields
    (decision_mode, role) get sensible defaults so the dataclass instantiates
    cleanly.
    """
    return Persona(
        id=pid,
        archetype=f"archetype_{pid}",
        display_name=f"display {pid}",
        voice_prompt="x",
        model="gpt-4o-mini",
        biases={},
        knowledge={},
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=by_volume),
    )


def _event() -> Event:
    return Event(
        ticker="002594",
        event_text="测试公告: 新能源车销量同比 +18%",
        event_type="earnings",
        event_date="2026-04-09",
        prior_consensus="市场预期 +15%",
        recent_price_action="过去 30 天上涨 8%",
        sector_context="新能源板块情绪平稳",
    )


def _result_with_reactions(reactions_per_round: list[list[Reaction]]) -> SimulationResult:
    personas = list({r.persona_id for round_ in reactions_per_round for r in round_})
    persona_objs = [_persona(pid) for pid in sorted(personas)]
    return SimulationResult(
        simulation_id="test-sim",
        event=_event(),
        personas=persona_objs,
        rounds=reactions_per_round,
        elapsed_seconds=1.0,
        cost_usd_at_start=0.0,
        cost_usd_at_end=0.01,
    )


def _r(pid: str, sentiment: float, intent: str = "observe", comment: str = "test") -> Reaction:
    return Reaction(
        persona_id=pid,
        sentiment=sentiment,
        action_intent=intent,
        comment=comment,
        confidence=0.7,
    )


# ─────────────────────── happy path ───────────────────────


def test_aggregate_basic_distribution() -> None:
    final = [
        _r("a", -0.8, "panic_observe", "完了完了"),
        _r("b", -0.3, "observe", "再看看"),
        _r("c", 0.0, "observe", "中性"),
        _r("d", 0.3, "lean_in", "可以"),
        _r("e", 0.7, "lean_in", "看好"),
    ]
    result = _result_with_reactions([final])
    report = aggregate(result)

    assert -0.1 < report.sentiment_mean < 0.1  # roughly balanced
    assert report.sentiment_std > 0  # there's spread
    assert sum(report.sentiment_histogram.values()) == 5
    assert report.implied_move_low < report.implied_move_high
    assert 0.0 <= report.implied_move_confidence <= 1.0
    assert len(report.persona_summary) == 5


def test_aggregate_strongly_negative() -> None:
    final = [_r(f"p{i}", -0.9, "panic_observe", "崩了") for i in range(5)]
    report = aggregate(_result_with_reactions([final]))

    assert report.sentiment_mean < -0.5
    assert report.implied_move_high < 0  # range is below zero
    # All same sentiment -> low dispersion -> confidence stays moderate-high
    assert report.dispersion < 0.1
    assert report.implied_move_confidence > 0.4


def test_aggregate_high_dispersion_lowers_confidence() -> None:
    final_low_disp = [_r(f"p{i}", 0.5) for i in range(5)]
    final_high_disp = [
        _r("p0", -0.9), _r("p1", -0.5), _r("p2", 0.0), _r("p3", 0.5), _r("p4", 0.9),
    ]

    r_low = aggregate(_result_with_reactions([final_low_disp]))
    r_high = aggregate(_result_with_reactions([final_high_disp]))

    assert r_low.dispersion < r_high.dispersion
    assert r_low.implied_move_confidence > r_high.implied_move_confidence


def test_aggregate_blind_spots_picks_outliers() -> None:
    # 4 personas agree negative, 1 contrarian positive
    final = [
        _r("a", -0.7, "panic_observe", "卖光"),
        _r("b", -0.6, "panic_observe", "完了"),
        _r("c", -0.7, "observe", "等等"),
        _r("d", -0.6, "observe", "再看"),
        _r("contrarian", 0.8, "lean_in", "这是个机会, 大家都没看到这一点"),
    ]
    report = aggregate(_result_with_reactions([final]))

    # The contrarian should be in the blind spots (largest deviation)
    contrarian_in_bs = any("contrarian" in bs or "机会" in bs for bs in report.blind_spots)
    assert contrarian_in_bs, f"Contrarian persona missing from blind spots: {report.blind_spots}"


def test_aggregate_intent_breakdown_sums_to_one() -> None:
    final = [
        _r("a", 0.0, "observe"),
        _r("b", 0.0, "observe"),
        _r("c", 0.0, "lean_in"),
        _r("d", 0.0, "lean_out"),
    ]
    report = aggregate(_result_with_reactions([final]))
    total = sum(report.action_intent_breakdown.values())
    assert abs(total - 1.0) < 0.01


def test_aggregate_uses_only_final_round() -> None:
    """If multiple rounds exist, aggregation uses the LAST round."""
    round_0 = [_r("a", -0.9), _r("b", -0.9)]
    round_1 = [_r("a", 0.5), _r("b", 0.5)]  # users switched stance
    report = aggregate(_result_with_reactions([round_0, round_1]))
    assert report.sentiment_mean > 0  # should reflect round 1


def test_aggregate_empty_raises() -> None:
    result = SimulationResult(
        simulation_id="empty",
        event=_event(),
        personas=[],
        rounds=[[]],
        elapsed_seconds=0.0,
        cost_usd_at_start=0.0,
        cost_usd_at_end=0.0,
    )
    with pytest.raises(ValueError):
        aggregate(result)


def test_persona_weight_affects_implied_move() -> None:
    """Higher-weight personas should pull the implied move toward their stance."""
    # Setup: 4 neutral personas + 1 strongly bearish persona with high market_share.by_volume
    personas = [
        _persona("a", by_volume=1.0),
        _persona("b", by_volume=1.0),
        _persona("c", by_volume=1.0),
        _persona("d", by_volume=1.0),
        _persona("heavyweight", by_volume=5.0),
    ]
    final = [
        _r("a", 0.0),
        _r("b", 0.0),
        _r("c", 0.0),
        _r("d", 0.0),
        _r("heavyweight", -0.9),
    ]
    result = SimulationResult(
        simulation_id="weighted",
        event=_event(),
        personas=personas,
        rounds=[final],
        elapsed_seconds=1.0,
        cost_usd_at_start=0.0,
        cost_usd_at_end=0.01,
    )
    report = aggregate(result)
    # Weighted mean: (0+0+0+0 + 5*-0.9) / 9 = -0.5
    # Center should be ~ -4%
    assert report.implied_move_low < -2.0
