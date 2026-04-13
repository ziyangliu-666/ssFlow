"""Regression coverage for spawn + decision-path sub-population mechanics.

Covers Phase B (spawn) + Phase C (decision path) of the intra-class
heterogeneity plan:

    Phase B — ``spawn_agents``:
      - Assignment honors declared fractions within a 5% band
      - Seeded determinism: same RNG seed → identical assignments
      - Backward compat: no sub_populations → every agent lands in
        the "default" bucket
      - capital_multiplier / max_position_pct_override / prob_holding_override
        are applied per agent

    Phase C — ``apply_distribution_to_agent_pop``:
      - Structural dissent: a mixed-sub-pop class with the same neutral
        LLM decision produces opposite-direction actions across sub-pops
      - ``action_histogram_by_sub_pop`` buckets the histogram by sub_pop_id
      - ``action_histogram`` (flat) sums back to the same totals
      - ``flat_action_histogram`` helper works on both shapes
      - ``style_tilt_for`` returns 0.0 on unknown keys (no raise)
      - A persona without sub_populations uses a single "default" bucket
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from ssflow.persona import (
    MarketShare,
    Persona,
    SandboxConfig,
    SubPopulation,
)
from ssflow.sub_population_styles import (
    STYLE_TILT,
    flat_action_histogram,
    style_tilt_for,
)
from ssflow.trading_layer import (
    apply_distribution_to_agent_pop,
    spawn_agents,
)


# ─────────────────────── Helpers ───────────────────────


def _make_retail_persona(
    sub_populations: list[SubPopulation] | None = None,
    *,
    instance_count: int = 10_000,
    prob_holding: float = 0.8,
) -> Persona:
    return Persona(
        id="retail_test",
        archetype="retail",
        display_name="test",
        voice_prompt="test",
        market_share=MarketShare(by_account_count=0.1),
        decision_mode="discretionary",
        role="directional_speculator",
        biases={"herd_following": 0.5},
        sandbox=SandboxConfig(
            instance_count=instance_count,
            capital_distribution={"type": "fixed", "value_cny": 100_000},
            initial_position_distribution={
                "type": "bernoulli",
                "prob_holding": prob_holding,
                "position_size_pct_when_holding": {"type": "fixed", "value": 0.5},
            },
            risk={"max_position_pct": 0.9},
            action_space=[
                {"name": "hold", "side": "none", "pool": "none", "fraction": 0.0},
            ],
        ),
        sub_populations=sub_populations,
    )


def _four_way_retail_sub_pops() -> list[SubPopulation]:
    return [
        SubPopulation(
            id="momentum_chaser",
            label_zh="动量",
            fraction=0.40,
            decision_style="momentum",
        ),
        SubPopulation(
            id="swing_trader",
            label_zh="波段",
            fraction=0.30,
            decision_style="conviction",
        ),
        SubPopulation(
            id="meme_speculator",
            label_zh="梗",
            fraction=0.20,
            decision_style="panic",
            capital_multiplier=0.6,
            max_position_pct_override=0.95,
        ),
        SubPopulation(
            id="burnt_veteran",
            label_zh="老油条",
            fraction=0.10,
            decision_style="contrarian",
            prob_holding_override=0.15,
        ),
    ]


# ─────────────────────── Phase B: spawn_agents ───────────────────────


def test_spawn_assignment_within_fraction_band():
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
    counts = Counter(a.sub_pop_id for a in agents)

    expected = {
        "momentum_chaser": 4000,
        "swing_trader": 3000,
        "meme_speculator": 2000,
        "burnt_veteran": 1000,
    }
    for sub_pop_id, exp in expected.items():
        got = counts[sub_pop_id]
        err_pct = abs(got - exp) / exp * 100
        assert err_pct < 5.0, (
            f"{sub_pop_id}: got {got}, expected {exp}, "
            f"err {err_pct:.1f}% outside 5% band"
        )


def test_spawn_assignment_deterministic_under_seed():
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents_a = spawn_agents(persona, current_price=100.0, rng=random.Random(7))
    agents_b = spawn_agents(persona, current_price=100.0, rng=random.Random(7))
    assert [a.sub_pop_id for a in agents_a] == [a.sub_pop_id for a in agents_b]
    assert [a.capital for a in agents_a] == [a.capital for a in agents_b]


def test_spawn_backward_compat_no_sub_populations():
    persona = _make_retail_persona(sub_populations=None, instance_count=500)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(1))
    assert len(agents) == 500
    assert all(a.sub_pop_id == "default" for a in agents)
    assert all(a.sub_pop_style == "" for a in agents)
    assert all(a.sub_pop_traits is None for a in agents)


def test_spawn_applies_capital_multiplier():
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))

    # meme_speculator has capital_multiplier=0.6 over a fixed 100_000 base
    meme = [a for a in agents if a.sub_pop_id == "meme_speculator"]
    swing = [a for a in agents if a.sub_pop_id == "swing_trader"]
    assert meme and swing
    assert all(a.capital == pytest.approx(60_000) for a in meme)
    assert all(a.capital == pytest.approx(100_000) for a in swing)


def test_spawn_applies_max_position_pct_override():
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))

    # meme has 0.95 override; swing falls through to class default 0.9
    meme = [a for a in agents if a.sub_pop_id == "meme_speculator"]
    swing = [a for a in agents if a.sub_pop_id == "swing_trader"]
    assert all(
        a.max_holdings_value / a.capital == pytest.approx(0.95) for a in meme
    )
    assert all(
        a.max_holdings_value / a.capital == pytest.approx(0.9) for a in swing
    )


def test_spawn_applies_prob_holding_override():
    # burnt_veteran has prob_holding_override=0.15 — dramatically lower
    # than the class default 0.8, so its proportion of non-zero holdings
    # should be dramatically lower.
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))

    def held_ratio(sub_pop_id: str) -> float:
        same = [a for a in agents if a.sub_pop_id == sub_pop_id]
        held = [a for a in same if sum(a.holdings.values()) > 0]
        return len(held) / max(1, len(same))

    # Class default prob_holding=0.8, so non-override sub-pops hold ~80%
    # and burnt_veteran holds ~15% — huge gap.
    assert held_ratio("burnt_veteran") < 0.30
    assert held_ratio("momentum_chaser") > 0.70


def test_spawn_stamps_sub_pop_traits_on_instances():
    persona = _make_retail_persona(_four_way_retail_sub_pops())
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(42))
    for a in agents:
        assert a.sub_pop_traits is not None
        assert a.sub_pop_traits.id == a.sub_pop_id
        assert a.sub_pop_style == a.sub_pop_traits.decision_style


# ─────────────────────── Phase C: STYLE_TILT + decision path ───────────────────────


def test_style_tilt_known_keys_nonzero():
    # Momentum + policy should be positive; contrarian + policy negative
    assert style_tilt_for("momentum", "policy") > 0
    assert style_tilt_for("contrarian", "policy") < 0
    assert style_tilt_for("fundamental", "regulatory") < 0
    assert style_tilt_for("panic", "regulatory") < 0


def test_style_tilt_unknown_keys_return_zero():
    assert style_tilt_for("momentum", "totally_unknown_event") == 0.0
    assert style_tilt_for("unknown_style", "policy") == 0.0
    assert style_tilt_for("", "") == 0.0
    assert style_tilt_for("unknown", "unknown") == 0.0


def test_style_tilt_library_shape():
    # Every declared style must cover the Phase I canonical event types.
    expected_events = {
        "policy",
        "supply_disruption",
        "demand_shock",
        "regulatory",
        "lawsuit",
        "earnings",
    }
    for style, mapping in STYLE_TILT.items():
        missing = expected_events - mapping.keys()
        assert not missing, f"style {style!r} missing event types {missing}"


def test_structural_dissent_neutral_class_policy_event():
    """When the LLM chooses hold (class_conviction=0), ALL agents hold —
    sub-pop dissent does NOT override a hold decision.

    This was changed to prevent "hold but net buy" contradictions in
    reports. Sub-pop dissent still applies when the class has a
    directional conviction (buy/sell), where momentum agents amplify
    and contrarian agents dampen the signal.
    """
    sub_pops = [
        SubPopulation(
            id="momentum_chaser",
            label_zh="动量",
            fraction=0.5,
            decision_style="momentum",
            event_conviction_offset={"policy": +0.10},
        ),
        SubPopulation(
            id="burnt_veteran",
            label_zh="老油条",
            fraction=0.5,
            decision_style="contrarian",
            event_conviction_offset={"policy": -0.20},
        ),
    ]
    persona = _make_retail_persona(sub_pops, instance_count=2000)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(1))

    result = apply_distribution_to_agent_pop(
        persona=persona,
        agents=agents,
        distribution={"__freeform__": 1.0},
        current_price=100.0,
        rng=random.Random(2),
        raw_distribution={"side": "hold", "quantity_pct": 0.0, "pool": ""},
        round_idx=0,
        event_type="policy",
    )

    buckets = result.action_histogram_by_sub_pop
    assert set(buckets.keys()) == {"momentum_chaser", "burnt_veteran"}

    # When class=hold, ALL agents hold regardless of sub-pop traits
    mom = buckets["momentum_chaser"]
    vet = buckets["burnt_veteran"]
    mom_total = sum(mom.values())
    vet_total = sum(vet.values())
    assert mom["hold"] == mom_total, f"all momentum agents should hold, got {mom}"
    assert vet["hold"] == vet_total, f"all veteran agents should hold, got {vet}"
    assert result.net_flow == 0.0, "hold should produce zero net flow"


def test_action_histogram_buckets_by_sub_pop_id():
    persona = _make_retail_persona(_four_way_retail_sub_pops(), instance_count=2000)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(1))
    result = apply_distribution_to_agent_pop(
        persona=persona,
        agents=agents,
        distribution={"__freeform__": 1.0},
        current_price=100.0,
        rng=random.Random(2),
        raw_distribution={"side": "buy", "quantity_pct": 0.3, "pool": "cash"},
        round_idx=0,
        event_type="policy",
    )
    buckets = result.action_histogram_by_sub_pop
    assert set(buckets.keys()) == {
        "momentum_chaser",
        "swing_trader",
        "meme_speculator",
        "burnt_veteran",
    }


def test_flat_histogram_sums_match_bucketed():
    persona = _make_retail_persona(_four_way_retail_sub_pops(), instance_count=1500)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(3))
    result = apply_distribution_to_agent_pop(
        persona=persona,
        agents=agents,
        distribution={"__freeform__": 1.0},
        current_price=100.0,
        rng=random.Random(4),
        raw_distribution={"side": "buy", "quantity_pct": 0.2, "pool": "cash"},
        round_idx=0,
        event_type="supply_disruption",
    )
    totals = {"buy": 0, "sell": 0, "hold": 0}
    for bucket in result.action_histogram_by_sub_pop.values():
        for action, count in bucket.items():
            totals[action] = totals.get(action, 0) + count
    for key, value in totals.items():
        assert result.action_histogram.get(key, 0) == value


def test_flat_action_histogram_helper_both_shapes():
    bucketed = {
        "momentum_chaser": {"buy": 400, "sell": 10, "hold": 5},
        "burnt_veteran": {"buy": 20, "sell": 150, "hold": 30},
    }
    flat = flat_action_histogram(bucketed)
    assert flat == {"buy": 420, "sell": 160, "hold": 35}

    legacy_flat = {"buy": 100, "sell": 50, "hold": 25}
    passthrough = flat_action_histogram(legacy_flat)
    assert passthrough == legacy_flat

    assert flat_action_histogram({}) == {"buy": 0, "sell": 0, "hold": 0}


def test_bare_persona_single_default_bucket():
    persona = _make_retail_persona(sub_populations=None, instance_count=200)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(5))
    result = apply_distribution_to_agent_pop(
        persona=persona,
        agents=agents,
        distribution={"__freeform__": 1.0},
        current_price=100.0,
        rng=random.Random(6),
        raw_distribution={"side": "buy", "quantity_pct": 0.4, "pool": "cash"},
        round_idx=0,
        event_type="policy",
    )
    assert list(result.action_histogram_by_sub_pop.keys()) == ["default"]
    default_bucket = result.action_histogram_by_sub_pop["default"]
    # All agents accounted for under the single bucket
    assert sum(default_bucket.values()) == result.n_agents


def test_event_type_default_is_zero_tilt():
    """Calls to apply_distribution_to_agent_pop without an event_type
    should behave as before Phase C — no STYLE_TILT contribution. This
    protects existing tests that call the function with its old sig.
    """
    persona = _make_retail_persona(_four_way_retail_sub_pops(), instance_count=1000)
    agents = spawn_agents(persona, current_price=100.0, rng=random.Random(11))
    result = apply_distribution_to_agent_pop(
        persona=persona,
        agents=agents,
        distribution={"__freeform__": 1.0},
        current_price=100.0,
        rng=random.Random(12),
        raw_distribution={"side": "hold", "quantity_pct": 0.0, "pool": ""},
        round_idx=0,
        # event_type omitted — default "" → no tilt
    )
    # When class=hold, ALL agents hold — no noise, no sub-pop sampling.
    # This prevents "hold but net buy" contradictions in reports.
    hist = result.action_histogram
    assert hist.get("buy", 0) == 0, f"hold should produce no buys, got {hist}"
    assert hist.get("sell", 0) == 0, f"hold should produce no sells, got {hist}"
    assert hist.get("hold", 0) == len(agents), f"all agents should hold, got {hist}"
