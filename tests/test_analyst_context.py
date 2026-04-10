"""Tests for analyst counter-narrative generation (Fix 6)."""

import pytest
from unittest.mock import MagicMock

from ssflow.analyst_context import (
    AnalystRoundContext,
    compute_analyst_context,
    detect_mixed_signals,
)


def _make_persona(sub_archetype="sellside_research_contrarian"):
    """Create a minimal mock Persona for testing."""
    p = MagicMock()
    p.id = f"analyst_{sub_archetype}"
    p.sub_archetype = sub_archetype
    p.entity_role = "analyst"
    return p


def _make_event(text="BYD Q1 earnings beat expectations"):
    e = MagicMock()
    e.event_text = text
    return e


class TestDetectMixedSignals:
    def test_positive_only(self):
        pos, neg = detect_mixed_signals("营收增长超预期，创新高")
        assert len(pos) >= 2
        assert len(neg) == 0

    def test_negative_only(self):
        pos, neg = detect_mixed_signals("毛利率下降，成本上升")
        assert len(pos) == 0
        assert len(neg) >= 2

    def test_mixed(self):
        pos, neg = detect_mixed_signals(
            "营收增长超预期，但毛利率下滑，竞争加剧"
        )
        assert len(pos) >= 1
        assert len(neg) >= 1

    def test_no_signals(self):
        pos, neg = detect_mixed_signals("公司发布公告")
        assert len(pos) == 0
        assert len(neg) == 0


class TestComputeAnalystContext:
    def test_no_injection_at_round_0(self):
        persona = _make_persona("sellside_research_contrarian")
        event = _make_event("营收增长超预期")
        ctx = compute_analyst_context(persona, 110.0, 100.0, 0, event)
        assert not ctx.should_inject

    def test_no_injection_when_small_move(self):
        persona = _make_persona("sellside_research_contrarian")
        event = _make_event("BYD Q1 results")
        ctx = compute_analyst_context(persona, 102.0, 100.0, 3, event)
        # 2% move is not significant, and no mixed signals
        assert not ctx.should_inject

    def test_injection_on_big_move_late_round(self):
        """Contrarian analyst should get nudge at R3 with 8% move."""
        persona = _make_persona("sellside_research_contrarian")
        event = _make_event("BYD Q1 beat")
        # Try multiple round indices to find one where the deterministic hash passes
        injected = False
        for r in range(2, 10):
            ctx = compute_analyst_context(persona, 108.0, 100.0, r, event)
            if ctx.should_inject:
                injected = True
                assert "分析师深度思考" in ctx.contrarian_prompt
                assert ctx.price_move_since_event_pct == pytest.approx(8.0, rel=0.01)
                break
        assert injected, "Contrarian analyst (90% prob) should inject in at least one round"

    def test_mixed_signal_triggers_injection(self):
        """Mixed signals + round >= 2 should trigger injection."""
        persona = _make_persona("sellside_research_contrarian")
        event = _make_event("营收增长超预期，但毛利率下滑严重")
        injected = False
        for r in range(2, 10):
            ctx = compute_analyst_context(persona, 106.0, 100.0, r, event)
            if ctx.should_inject:
                injected = True
                assert "正面信号" in ctx.contrarian_prompt
                assert "负面信号" in ctx.contrarian_prompt
                break
        assert injected

    def test_growth_analyst_lower_probability(self):
        """Growth analyst should have lower injection rate than contrarian."""
        persona_growth = _make_persona("sellside_research_growth")
        persona_contra = _make_persona("sellside_research_contrarian")
        event = _make_event("BYD Q1 beat")

        growth_count = 0
        contra_count = 0
        for r in range(2, 50):
            ctx_g = compute_analyst_context(persona_growth, 110.0, 100.0, r, event)
            ctx_c = compute_analyst_context(persona_contra, 110.0, 100.0, r, event)
            if ctx_g.should_inject:
                growth_count += 1
            if ctx_c.should_inject:
                contra_count += 1

        # Contrarian should inject more frequently than growth
        assert contra_count > growth_count

    def test_context_has_price_info(self):
        persona = _make_persona("sellside_research_contrarian")
        event = _make_event("BYD Q1 beat")
        for r in range(2, 10):
            ctx = compute_analyst_context(persona, 110.0, 100.0, r, event)
            if ctx.should_inject:
                assert "+10.0%" in ctx.contrarian_prompt or "10.0%" in ctx.contrarian_prompt
                break
