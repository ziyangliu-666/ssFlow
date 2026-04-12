"""Regression coverage for ``persona.SubPopulation`` schema + YAML loader.

Covers Phase A of the intra-class heterogeneity plan:
    - Round-trip: YAML → Persona.sub_populations with all fields parsed
    - Fraction sum validation (fail at load time, not spawn time)
    - Unknown decision_style fails hard
    - Duplicate sub-pop id within one persona fails
    - Missing required fields fails with a clear error
    - Backward compat: ashare.yaml loads with existing personas unchanged
    - Empty / absent ``sub_populations`` → None (homogeneous default)
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from ssflow.persona import (
    PersonaSchemaError,
    SubPopulation,
    load_personas,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────── Fixtures ───────────────────────


def _base_persona() -> dict[str, Any]:
    """Return a minimal valid persona dict that satisfies the loader."""
    return {
        "id": "retail_x",
        "archetype": "retail",
        "display_name": "test retail",
        "voice_prompt": "test",
        "decision_mode": "discretionary",
        "role": "directional_speculator",
        "market_share": {
            "by_account_count": 0.1,
            "citations": [{"source_id": "test-src", "note": "test"}],
        },
        "capital_range_cny": [10000, 100000],
        "time_horizon_days": [1, 14],
        "behavior": {"avg_position_pct": 0.5, "annual_turnover": 1},
        "sandbox": {
            "instance_count": 100,
            "capital_distribution": {"type": "fixed", "value_cny": 100000},
            "initial_position_distribution": {"type": "fixed", "value": 0.5},
            "risk": {"max_position_pct": 0.9},
            "action_space": [
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
            ],
        },
    }


def _write_pack(
    tmp_path: Path,
    persona_override: dict[str, Any] | None = None,
    sub_populations: Any = "__unset__",
) -> Path:
    """Write a one-persona pack to tmp_path and return the file path."""
    persona = _base_persona()
    if persona_override:
        persona.update(copy.deepcopy(persona_override))
    if sub_populations != "__unset__":
        persona["sub_populations"] = sub_populations
    pack = {
        "pack_id": "test_pack",
        "pack_name": "Test Pack",
        "market": "A",
        "schema_version": 3,
        "data_sources": [
            {"id": "test-src", "kind": "internal", "description": "test fixture"},
        ],
        "personas": [persona],
    }
    path = tmp_path / "pack.yaml"
    path.write_text(
        yaml.safe_dump(pack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# ─────────────────────── Round-trip ───────────────────────


def test_yaml_roundtrip_all_fields(tmp_path):
    sub_pops = [
        {
            "id": "momentum_chaser",
            "label_zh": "动量追涨客",
            "fraction": 0.4,
            "decision_style": "momentum",
            "rationale": "test citation",
            "capital_multiplier": 0.8,
            "prob_holding_override": 0.3,
            "max_position_pct_override": 0.85,
            "stop_loss_threshold_override": -0.10,
            "bias_overrides": {"momentum_chasing": 0.95, "loss_aversion": 0.5},
            "event_conviction_offset": {"policy": 0.15, "regulatory": -0.05},
            "exit_rules": [{"type": "nav_target", "target_pct": 0.2}],
        },
        {
            "id": "burnt_veteran",
            "label_zh": "老油条",
            "fraction": 0.6,
            "decision_style": "contrarian",
        },
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    personas = load_personas(path)
    assert len(personas) == 1
    p = personas[0]
    assert p.sub_populations is not None
    assert len(p.sub_populations) == 2

    mom = p.sub_populations[0]
    assert isinstance(mom, SubPopulation)
    assert mom.id == "momentum_chaser"
    assert mom.label_zh == "动量追涨客"
    assert mom.fraction == pytest.approx(0.4)
    assert mom.decision_style == "momentum"
    assert mom.rationale == "test citation"
    assert mom.capital_multiplier == pytest.approx(0.8)
    assert mom.prob_holding_override == pytest.approx(0.3)
    assert mom.max_position_pct_override == pytest.approx(0.85)
    assert mom.stop_loss_threshold_override == pytest.approx(-0.10)
    assert mom.bias_overrides == {
        "momentum_chasing": 0.95,
        "loss_aversion": 0.5,
    }
    assert mom.event_conviction_offset == {"policy": 0.15, "regulatory": -0.05}
    assert mom.exit_rules == [{"type": "nav_target", "target_pct": 0.2}]

    vet = p.sub_populations[1]
    assert vet.id == "burnt_veteran"
    assert vet.fraction == pytest.approx(0.6)
    assert vet.capital_multiplier == 1.0  # default
    assert vet.prob_holding_override is None
    assert vet.bias_overrides == {}
    assert vet.event_conviction_offset == {}
    assert vet.exit_rules == []


# ─────────────────────── Validation ───────────────────────


def test_fraction_sum_validation_fails_at_load(tmp_path):
    sub_pops = [
        {"id": "a", "label_zh": "A", "fraction": 0.5, "decision_style": "momentum"},
        {"id": "b", "label_zh": "B", "fraction": 0.4, "decision_style": "panic"},
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    with pytest.raises(PersonaSchemaError, match="fractions sum to"):
        load_personas(path)


def test_fraction_sum_near_one_tolerance(tmp_path):
    sub_pops = [
        {"id": "a", "label_zh": "A", "fraction": 0.1, "decision_style": "momentum"},
        {"id": "b", "label_zh": "B", "fraction": 0.2, "decision_style": "contrarian"},
        {"id": "c", "label_zh": "C", "fraction": 0.3, "decision_style": "fundamental"},
        {"id": "d", "label_zh": "D", "fraction": 0.4, "decision_style": "panic"},
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    personas = load_personas(path)
    assert len(personas[0].sub_populations) == 4


def test_unknown_decision_style_fails(tmp_path):
    sub_pops = [
        {"id": "a", "label_zh": "A", "fraction": 1.0, "decision_style": "mysterious"},
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    with pytest.raises(PersonaSchemaError, match="unknown decision_style"):
        load_personas(path)


def test_duplicate_sub_pop_id_fails(tmp_path):
    sub_pops = [
        {"id": "dup", "label_zh": "A", "fraction": 0.5, "decision_style": "momentum"},
        {"id": "dup", "label_zh": "B", "fraction": 0.5, "decision_style": "panic"},
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    with pytest.raises(PersonaSchemaError, match="duplicate sub_population id"):
        load_personas(path)


def test_missing_required_field_fails(tmp_path):
    sub_pops = [{"id": "a", "label_zh": "A", "fraction": 1.0}]  # no decision_style
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    with pytest.raises(PersonaSchemaError, match="missing required"):
        load_personas(path)


def test_fraction_out_of_range_fails(tmp_path):
    sub_pops = [
        {"id": "a", "label_zh": "A", "fraction": 1.5, "decision_style": "momentum"},
    ]
    path = _write_pack(tmp_path, sub_populations=sub_pops)
    with pytest.raises(PersonaSchemaError, match=r"outside \[0, 1\]"):
        load_personas(path)


def test_non_list_sub_populations_fails(tmp_path):
    path = _write_pack(tmp_path, sub_populations={"id": "a", "fraction": 1.0})
    with pytest.raises(PersonaSchemaError, match="must be a list"):
        load_personas(path)


# ─────────────────────── Backward compat ───────────────────────


def test_no_sub_populations_returns_none(tmp_path):
    path = _write_pack(tmp_path)  # field not set at all
    personas = load_personas(path)
    assert personas[0].sub_populations is None


def test_empty_sub_populations_list_returns_none(tmp_path):
    path = _write_pack(tmp_path, sub_populations=[])
    personas = load_personas(path)
    assert personas[0].sub_populations is None


def test_ashare_yaml_loads_with_sub_populations():
    """The real production catalogue must load cleanly — verifies that
    Phase D hand-authored sub_populations blocks and all personas
    without sub_populations coexist under the same loader.
    """
    path = _REPO_ROOT / "personas" / "ashare.yaml"
    personas = load_personas(path)
    assert len(personas) >= 20

    with_sp = [p for p in personas if p.sub_populations is not None]
    without_sp = [p for p in personas if p.sub_populations is None]
    assert len(with_sp) >= 1, "expected at least one persona with sub_populations"
    assert len(without_sp) >= 1, "expected at least one persona without sub_populations"

    target_ids = {
        "retail_short_term_chaser",
        "mutual_fund_active_pm",
        "private_equity_active",
    }
    found = {p.id for p in with_sp} & target_ids
    assert found == target_ids, f"missing sub_populations on: {target_ids - found}"

    for p in with_sp:
        total = sum(sp.fraction for sp in p.sub_populations)
        assert abs(total - 1.0) < 1e-6, f"{p.id} sub-pop fractions = {total}"
