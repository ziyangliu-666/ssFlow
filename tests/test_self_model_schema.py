"""Unit tests for ssflow.self_model.schema — spec + validation guardrail.

The validator is load-bearing: persona_factory Stage 3 and the runtime
both call it to strip unknown atom / component / section names before
they reach the engine. These tests pin down that:

1. Unknown atoms are dropped with warnings (not crashes)
2. Unknown utility components are dropped
3. Unknown render sections are dropped
4. render_sections is hard-capped at RENDER_SECTION_CAP = 5
5. Valid specs pass through unchanged
6. None / non-dict input returns an empty-but-valid spec
7. peer_watchlist_filter keys are validated
8. Dedup happens silently
9. ``SelfModelSpec.from_dict`` + ``to_dict`` round-trips
"""

from __future__ import annotations

import pytest

from ssflow.self_model.schema import (
    SelfModelSpec,
    validate_self_model_spec,
)


class TestValidateSelfModelSpecDropsUnknownKeys:
    def test_unknown_atom_dropped_with_warning(self):
        raw = {
            "state_atoms": ["cash", "TOTALLY_FAKE_ATOM", "nav"],
            "utility_weights": {},
            "render_sections": [],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["state_atoms"] == ["cash", "nav"]
        assert any("TOTALLY_FAKE_ATOM" in w for w in warns)

    def test_unknown_utility_component_dropped(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {"nav_growth": 1.0, "not_a_real_component": 2.5},
            "render_sections": [],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["utility_weights"] == {"nav_growth": 1.0}
        assert any("not_a_real_component" in w for w in warns)

    def test_unknown_render_section_dropped(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {},
            "render_sections": ["financial_snapshot", "fake_section", "emotional_state"],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["render_sections"] == ["financial_snapshot", "emotional_state"]
        assert any("fake_section" in w for w in warns)


class TestRenderSectionCap:
    def test_more_than_cap_sections_truncated(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {},
            "render_sections": [
                "financial_snapshot",
                "last_trade_outcome",
                "benchmark_gap",
                "mandate_headroom",
                "emotional_state",
                "peer_watchlist",
                "career_pressure",
                "runway_status",
                "board_pressure",
            ],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert len(cleaned["render_sections"]) == SelfModelSpec.RENDER_SECTION_CAP
        assert any("capped" in w for w in warns)

    def test_exactly_cap_sections_unchanged(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {},
            "render_sections": ["financial_snapshot"] * 1 + [
                "last_trade_outcome",
                "benchmark_gap",
                "mandate_headroom",
                "emotional_state",
            ],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert len(cleaned["render_sections"]) == 5
        assert not any("capped" in w for w in warns)


class TestValidateSpecEdgeCases:
    def test_none_input_returns_empty_valid_spec(self):
        cleaned, warns = validate_self_model_spec(None)
        assert cleaned["state_atoms"] == []
        assert cleaned["utility_weights"] == {}
        assert cleaned["render_sections"] == []
        assert cleaned["peer_watchlist_filter"] == {}
        assert cleaned["custom_atoms"] == []
        assert warns == []

    def test_non_dict_input_returns_empty_valid_spec(self):
        cleaned, warns = validate_self_model_spec("this is a string, not a spec")
        assert cleaned["state_atoms"] == []
        assert any("dict" in w for w in warns)

    def test_non_list_state_atoms_drops(self):
        raw = {"state_atoms": "cash,nav", "utility_weights": {}, "render_sections": []}
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["state_atoms"] == []
        assert any("state_atoms must be a list" in w for w in warns)

    def test_non_numeric_weight_dropped(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {"nav_growth": "one"},
            "render_sections": [],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert "nav_growth" not in cleaned["utility_weights"]
        assert any("non-numeric" in w for w in warns)

    def test_dedup_atoms_silently(self):
        raw = {
            "state_atoms": ["cash", "nav", "cash", "nav"],
            "utility_weights": {},
            "render_sections": [],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["state_atoms"] == ["cash", "nav"]
        # Dedup is silent — no warning

    def test_custom_atoms_dropped_with_warning(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {},
            "render_sections": [],
            "custom_atoms": [{"key": "foo", "init": 0, "update_rule": {"type": "decay"}}],
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["custom_atoms"] == []
        assert any("DSL executor" in w for w in warns)

    def test_peer_filter_top_k_coerced(self):
        raw = {
            "state_atoms": [],
            "utility_weights": {},
            "render_sections": [],
            "peer_watchlist_filter": {"top_k": "3", "role_match": ["mutual_fund_*"]},
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["peer_watchlist_filter"]["top_k"] == 3
        assert cleaned["peer_watchlist_filter"]["role_match"] == ["mutual_fund_*"]


class TestSelfModelSpecRoundtrip:
    def test_from_dict_to_dict_stable(self):
        d = {
            "state_atoms": ["cash", "nav", "unrealized_pnl_pct"],
            "utility_weights": {"nav_growth": 1.0, "drawdown_penalty": 2.0},
            "render_sections": ["financial_snapshot", "emotional_state"],
            "peer_watchlist_filter": {"role_match": ["*"], "top_k": 3},
            "custom_atoms": [],
        }
        spec = SelfModelSpec.from_dict(d)
        out = spec.to_dict()
        assert out == d


class TestValidateSpecNoWarnsOnGoodInput:
    def test_clean_pack_passes_through(self):
        raw = {
            "state_atoms": ["cash", "nav", "unrealized_pnl_pct", "stress", "conviction"],
            "utility_weights": {"nav_growth": 1.0, "drawdown_penalty": 1.5},
            "render_sections": ["financial_snapshot", "emotional_state"],
            "peer_watchlist_filter": {"role_match": ["*"], "top_k": 3, "sort_by": "nav_growth_desc"},
        }
        cleaned, warns = validate_self_model_spec(raw)
        assert cleaned["state_atoms"] == raw["state_atoms"]
        assert cleaned["utility_weights"] == raw["utility_weights"]
        assert cleaned["render_sections"] == raw["render_sections"]
        assert cleaned["peer_watchlist_filter"] == raw["peer_watchlist_filter"]
        assert warns == []
