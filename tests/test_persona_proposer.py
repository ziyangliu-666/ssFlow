"""Tests for `ssflow.persona_proposer`."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from ssflow.persona import (
    MarketShare,
    Persona,
    PersonaSchemaError,
    SandboxConfig,
    load_personas,
)
from ssflow.persona_proposer import (
    blank_partial,
    persona_from_partial,
    persona_to_partial,
    propose_personas,
)


# ─────────────────────── Fixtures ───────────────────────


def _make_trader_persona(pid: str = "retail_test") -> Persona:
    return Persona(
        id=pid,
        archetype="散户(测试)",
        display_name="测试散户 50 岁",
        voice_prompt="你是一个测试散户 ...",
        decision_mode="discretionary",
        role="directional_speculator",
        entity_role="trader",
        market_share=MarketShare(by_volume=0.1),
        sandbox=SandboxConfig(
            instance_count=5000,
            capital_distribution={
                "type": "lognormal",
                "median_cny": 120_000,
                "sigma": 0.8,
            },
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": 0.6,
            },
            risk={"max_position_pct": 0.95},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
                {"name": "buy_20pct", "side": "buy", "pool": "cash", "fraction": 0.2},
                {
                    "name": "sell_30pct",
                    "side": "sell",
                    "pool": "holdings_in_target",
                    "fraction": 0.3,
                },
            ],
        ),
    )


def _make_media_persona(pid: str = "news_wire_test") -> Persona:
    return Persona(
        id=pid,
        archetype="新闻通讯社",
        display_name="测试新闻源",
        voice_prompt="你是中立的财经新闻通讯社 ...",
        decision_mode="discretionary",
        role="information_intermediary",
        entity_role="media",
        market_share=MarketShare(by_volume=0.0),
    )


# ─────────────────────── persona_to_partial ───────────────────────


def test_trader_partial_projects_editable_fields():
    p = _make_trader_persona()
    partial = persona_to_partial(p)

    assert partial["id"] == "retail_test"
    assert partial["archetype"] == "散户(测试)"
    assert partial["display_name"] == "测试散户 50 岁"
    assert partial["voice_prompt"].startswith("你是一个测试散户")
    assert partial["entity_role"] == "trader"
    assert partial["decision_mode"] == "discretionary"
    assert partial["instance_count"] == 5000
    assert partial["capital_median_cny"] == 120_000
    assert partial["prob_holding"] == 0.6
    assert partial["_base_template"] == "retail_test"


def test_media_partial_has_null_sandbox_fields():
    p = _make_media_persona()
    partial = persona_to_partial(p)

    assert partial["entity_role"] == "media"
    assert partial["instance_count"] is None
    assert partial["capital_median_cny"] is None
    assert partial["prob_holding"] is None


def test_fixed_capital_distribution_projects_value_cny():
    p = _make_trader_persona()
    p.sandbox.capital_distribution = {"type": "fixed", "value_cny": 50_000}
    partial = persona_to_partial(p)
    assert partial["capital_median_cny"] == 50_000


# ─────────────────────── propose_personas ───────────────────────


def test_propose_personas_loads_base_pack():
    """Smoke test against the real ashare pack."""
    path = Path(__file__).resolve().parents[1] / "personas" / "ashare.yaml"
    if not path.exists():
        pytest.skip(f"ashare pack not found at {path}")
    partials = propose_personas(base_pack_path=path)
    assert len(partials) >= 10
    # Every entry is a dict with the editable keys.
    for partial in partials:
        assert "id" in partial
        assert "archetype" in partial
        assert "voice_prompt" in partial
        assert "entity_role" in partial
        assert "_base_template" in partial


# ─────────────────────── persona_from_partial (overlay path) ───────────────────────


def test_persona_from_partial_overlays_on_base():
    base = _make_trader_persona()
    partial = persona_to_partial(base)

    # User edits 3 of the 6 fields
    partial["archetype"] = "散户(激进)"
    partial["instance_count"] = 9999
    partial["prob_holding"] = 0.9
    partial["voice_prompt"] = "你现在变得非常激进 ..."

    rebuilt = persona_from_partial(partial, [base])

    assert rebuilt.archetype == "散户(激进)"
    assert rebuilt.voice_prompt.startswith("你现在变得非常激进")
    assert rebuilt.sandbox is not None
    assert rebuilt.sandbox.instance_count == 9999
    assert rebuilt.sandbox.initial_position_distribution["prob_holding"] == 0.9

    # Untouched fields survive.
    assert rebuilt.sandbox.action_space == base.sandbox.action_space
    assert rebuilt.sandbox.risk == base.sandbox.risk
    assert rebuilt.role == base.role
    assert rebuilt.decision_mode == base.decision_mode


def test_persona_from_partial_does_not_mutate_base():
    base = _make_trader_persona()
    original_copy = copy.deepcopy(base)

    partial = persona_to_partial(base)
    partial["instance_count"] = 1
    persona_from_partial(partial, [base])

    assert base.sandbox.instance_count == original_copy.sandbox.instance_count
    assert base.archetype == original_copy.archetype


def test_persona_from_partial_preserves_fixed_capital_type():
    base = _make_trader_persona()
    base.sandbox.capital_distribution = {"type": "fixed", "value_cny": 50_000}
    partial = persona_to_partial(base)
    partial["capital_median_cny"] = 75_000

    rebuilt = persona_from_partial(partial, [base])

    assert rebuilt.sandbox.capital_distribution["type"] == "fixed"
    assert rebuilt.sandbox.capital_distribution["value_cny"] == 75_000


def test_persona_from_partial_updates_lognormal_median():
    base = _make_trader_persona()
    partial = persona_to_partial(base)
    partial["capital_median_cny"] = 500_000

    rebuilt = persona_from_partial(partial, [base])

    assert rebuilt.sandbox.capital_distribution["type"] == "lognormal"
    assert rebuilt.sandbox.capital_distribution["median_cny"] == 500_000
    # sigma from base is preserved.
    assert rebuilt.sandbox.capital_distribution["sigma"] == 0.8


def test_persona_from_partial_rejects_bad_instance_count():
    base = _make_trader_persona()
    partial = persona_to_partial(base)
    partial["instance_count"] = "not a number"

    with pytest.raises(PersonaSchemaError):
        persona_from_partial(partial, [base])


# ─────────────────────── persona_from_partial (scratch path) ───────────────────────


def test_build_from_scratch_requires_fields():
    partial = {
        "id": "",
        "archetype": "",
        "display_name": "",
        "voice_prompt": "",
        "decision_mode": "discretionary",
    }
    with pytest.raises(PersonaSchemaError):
        persona_from_partial(partial, base_pack=[])


def test_build_from_scratch_creates_trader_with_defaults():
    partial = {
        "id": "new_class",
        "archetype": "新的一类散户",
        "display_name": "新一代",
        "voice_prompt": "你是全新的散户类型",
        "decision_mode": "discretionary",
        "entity_role": "trader",
        "instance_count": 1234,
        "capital_median_cny": 200_000,
        "prob_holding": 0.4,
    }
    p = persona_from_partial(partial, base_pack=[])
    assert p.id == "new_class"
    assert p.sandbox is not None
    assert p.sandbox.instance_count == 1234
    assert p.sandbox.capital_distribution["median_cny"] == 200_000
    assert p.sandbox.initial_position_distribution["prob_holding"] == 0.4
    # Action space came from DEFAULT_ACTION_TEMPLATES["discretionary"]
    assert len(p.sandbox.action_space) >= 3
    assert any(a["name"] == "hold" for a in p.sandbox.action_space)


def test_build_from_scratch_media_has_no_sandbox():
    partial = {
        "id": "new_media",
        "archetype": "新的一家媒体",
        "display_name": "新媒体",
        "voice_prompt": "你是全新媒体类型",
        "decision_mode": "discretionary",
        "entity_role": "media",
    }
    p = persona_from_partial(partial, base_pack=[])
    assert p.entity_role == "media"
    assert p.sandbox is None


def test_build_from_scratch_rejects_bad_decision_mode():
    partial = {
        "id": "bad",
        "archetype": "bad",
        "display_name": "bad",
        "voice_prompt": "bad",
        "decision_mode": "tarot",
    }
    with pytest.raises(PersonaSchemaError):
        persona_from_partial(partial, base_pack=[])


# ─────────────────────── blank_partial ───────────────────────


def test_blank_partial_returns_empty_fields_for_add_button():
    b = blank_partial(decision_mode="discretionary")
    assert b["id"] == ""
    assert b["archetype"] == ""
    assert b["voice_prompt"] == ""
    assert b["entity_role"] == "trader"
    assert b["decision_mode"] == "discretionary"
    assert b["instance_count"] == 500
    assert b["prob_holding"] == 0.5
    assert b["_base_template"] is None
    assert isinstance(b["_default_actions"], list)
    assert "hold" in b["_default_actions"]


def test_blank_partial_unknown_mode_falls_back_to_discretionary():
    b = blank_partial(decision_mode="invalid_mode")
    assert b["decision_mode"] == "discretionary"


# ─────────────────────── Round-trip with real YAML ───────────────────────


def test_round_trip_with_ashare_pack():
    """Load ashare.yaml, project every persona to a partial, rebuild,
    and confirm the rebuild still passes the simulation-ready checks."""
    path = Path(__file__).resolve().parents[1] / "personas" / "ashare.yaml"
    if not path.exists():
        pytest.skip(f"ashare pack not found at {path}")
    base_pack = load_personas(path)
    assert len(base_pack) > 0

    for p in base_pack:
        partial = persona_to_partial(p)
        rebuilt = persona_from_partial(partial, base_pack=base_pack)
        # Id / archetype preserved
        assert rebuilt.id == p.id
        assert rebuilt.archetype == p.archetype
        # Sandbox preserved (with deep-merged capital + init_pos, which
        # in a clean round-trip should be identical).
        if p.sandbox is not None:
            assert rebuilt.sandbox is not None
            assert rebuilt.sandbox.instance_count == p.sandbox.instance_count
            assert rebuilt.sandbox.action_space == p.sandbox.action_space
