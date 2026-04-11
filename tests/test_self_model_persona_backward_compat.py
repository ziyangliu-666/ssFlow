"""Backward-compat tests: existing personas/ashare.yaml must load fine
with the new ``self_model`` field added to Persona dataclass, and
must fall back to DEFAULT_SELF_MODEL when they lack an explicit spec.

Also covers persona_factory Stage 3 merge behavior:
  - When LLM output has self_model → merge passes it through
  - When LLM output omits self_model → persona.self_model stays None
  - Unknown atom/component/section names are stripped with warnings
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ssflow.event import Event
from ssflow.persona import load_personas
from ssflow.persona_factory import _merge_creative_into_structural, ParticipantClass
from ssflow.self_model import build_evaluators_for_personas


class TestAshareYamlLoadsWithoutSelfModel:
    def test_existing_pack_loads(self):
        """personas/ashare.yaml has 39 personas, none with self_model.
        The loader must accept them and default self_model to None."""
        yaml_path = Path("personas/ashare.yaml")
        if not yaml_path.exists():
            pytest.skip("personas/ashare.yaml not present")
        personas = load_personas(yaml_path)
        assert len(personas) >= 30
        # Every persona should have self_model = None (attribute exists, value None)
        for p in personas:
            assert hasattr(p, "self_model")
            assert p.self_model is None

    def test_default_bundle_applied_to_legacy_personas(self):
        yaml_path = Path("personas/ashare.yaml")
        if not yaml_path.exists():
            pytest.skip("personas/ashare.yaml not present")
        personas = load_personas(yaml_path)
        event = Event(
            ticker="300750",
            event_text="test",
            event_type="policy",
            event_date="2024-09-24",
        )
        evaluators = build_evaluators_for_personas(personas, event)
        # Every trader gets an evaluator with the default bundle
        trader_ids = [p.id for p in personas if p.sandbox is not None]
        assert len(evaluators) == len(trader_ids)
        for ev in evaluators.values():
            # Default bundle has nav_growth weight
            assert "nav_growth" in ev.spec.utility_weights
            # Default bundle has financial_snapshot section
            assert "financial_snapshot" in ev.spec.render_sections
            # And the state has the universal atoms
            assert "cash" in ev.state
            assert "nav" in ev.state
            assert "unrealized_pnl_pct" in ev.state


class TestPersonaFactoryMergePassesSelfModel:
    def _make_class(self) -> ParticipantClass:
        return ParticipantClass(
            id="test_persona",
            archetype="Test Archetype",
            description="desc",
            decision_mode="discretionary",
            role="directional_speculator",
            by_volume=0.05,
        )

    def test_merge_passes_self_model_through(self):
        creative = {
            "display_name": "test",
            "voice_prompt": "test voice",
            "biases": {},
            "knowledge": {},
            "behavior": None,
            "information": None,
            "sandbox": {
                "instance_count": 10,
                "action_space": [
                    {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                ],
            },
            "self_model": {
                "state_atoms": ["cash", "nav", "unrealized_pnl_pct"],
                "utility_weights": {"nav_growth": 1.0, "drawdown_penalty": 1.5},
                "render_sections": ["financial_snapshot"],
                "peer_watchlist_filter": {"role_match": ["*"], "top_k": 2},
            },
        }
        merged = _merge_creative_into_structural(self._make_class(), creative)
        assert "self_model" in merged
        assert merged["self_model"]["state_atoms"] == ["cash", "nav", "unrealized_pnl_pct"]
        assert merged["self_model"]["utility_weights"]["nav_growth"] == 1.0

    def test_merge_absent_self_model_stays_absent(self):
        creative = {
            "display_name": "test",
            "voice_prompt": "test",
            "sandbox": {
                "instance_count": 10,
                "action_space": [
                    {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                ],
            },
        }
        merged = _merge_creative_into_structural(self._make_class(), creative)
        assert "self_model" not in merged

    def test_merge_strips_unknown_atoms_from_self_model(self):
        creative = {
            "display_name": "test",
            "voice_prompt": "test",
            "sandbox": {
                "instance_count": 10,
                "action_space": [
                    {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                ],
            },
            "self_model": {
                "state_atoms": ["cash", "FAKE_ATOM", "nav"],
                "utility_weights": {"nav_growth": 1.0, "BOGUS": 2.0},
                "render_sections": ["financial_snapshot", "FAKE_SECTION"],
            },
        }
        merged = _merge_creative_into_structural(self._make_class(), creative)
        assert merged["self_model"]["state_atoms"] == ["cash", "nav"]
        assert merged["self_model"]["utility_weights"] == {"nav_growth": 1.0}
        assert merged["self_model"]["render_sections"] == ["financial_snapshot"]
