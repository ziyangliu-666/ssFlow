"""Decision-style library for intra-class persona heterogeneity.

Phase II — backs the ``SubPopulation.decision_style`` field declared in
``src/ssflow/persona.py``. The engine passes each agent's style +
``event.event_type`` through ``style_tilt_for`` and adds the result to
the class-level conviction inside
``trading_layer.apply_distribution_to_agent_pop``.

Why a lookup table and not a prompt:
    The LLM produces ONE decision per persona class per round. That
    collapses all structural dissent inside a class into a single
    scalar. Gaussian dispersion alone only smears the scalar. To make
    a "momentum chaser" and a "burnt veteran" inside the same retail
    class take OPPOSITE directions on the same policy event, we need
    a structural post-processing step that is driven by each agent's
    sub-pop, not by the LLM call. STYLE_TILT is that step.

Design rules:
    - STYLE_TILT[style][event_type] is an ADDITIVE conviction offset in
      the same units as ``class_conviction`` (range roughly [-1, +1]).
    - Positive = more bullish / buy-leaning; negative = more bearish.
    - Magnitudes are small (±0.05–0.30). The goal is to tip the
      histogram, not to override the LLM decision.
    - Unknown (style, event_type) pairs return 0.0. The research layer
      can introduce new event_types without crashing existing
      backtests.
    - Unknown styles also return 0.0. Load-time schema validation in
      ``persona._coerce_sub_populations`` rejects unknown styles at
      YAML parse time (fail fast at config, not at hot path).

Tuning notes for each style:

    momentum — chases strong directional moves. Policy / supply shock
        / demand shock all amplify the bull side. Fades
        regulatory + lawsuit modestly (they're typically bear-biased).

    contrarian — fades the consensus. Hardest to get right: if the
        class decision is bullish, contrarians push toward neutral
        (not bearish), so the tilt is a small negative number not a
        big one.

    fundamental — cares about cash flow and balance sheet. Heavily
        punishes regulatory / lawsuit events (impairs earnings power)
        and modestly rewards earnings beats. Unmoved by pure policy
        vibes.

    panic — sells first, asks later. Overweights bear events
        (regulatory / lawsuit / earnings miss) and ignores bull
        setups. A panic sub-pop within a retail class is the reason
        some retail agents dump even on a policy bull event.

    conviction — long-only high-conviction stock pickers. Modest
        positive tilt to everything they see as validating the thesis
        (earnings, policy, supply), small negative to regulatory.

These numbers are a starting point for Phase D hand-tuning. If the
backtest shows overfitting to one event type, prefer reducing the
magnitudes over adding special cases.
"""

from __future__ import annotations


STYLE_TILT: dict[str, dict[str, float]] = {
    "momentum": {
        "policy": +0.20,
        "supply_disruption": +0.15,
        "demand_shock": +0.15,
        "regulatory": -0.10,
        "lawsuit": -0.05,
        "earnings": 0.0,
    },
    "contrarian": {
        "policy": -0.15,
        "supply_disruption": -0.10,
        "demand_shock": -0.10,
        "regulatory": +0.10,
        "lawsuit": +0.05,
        "earnings": 0.0,
    },
    "fundamental": {
        "policy": +0.05,
        "supply_disruption": +0.05,
        "demand_shock": +0.05,
        "regulatory": -0.30,
        "lawsuit": -0.25,
        # Earnings direction depends on beat-vs-miss polarity, which the
        # style tilt cannot know. A fixed positive tilt here flipped 五粮液
        # (earnings miss) from correct -19% to wrong +5% in backtest v4.
        # Leave at 0.0 so the LLM class decision drives direction alone.
        "earnings": 0.0,
    },
    "panic": {
        "policy": 0.0,
        "supply_disruption": 0.0,
        "demand_shock": 0.0,
        "regulatory": -0.30,
        "lawsuit": -0.20,
        # Same reasoning as fundamental: panic's negative tilt here was
        # correct for misses but wrong for beats. Zeroed to avoid
        # polarity-ignorant interference.
        "earnings": 0.0,
    },
    "conviction": {
        "policy": +0.15,
        "supply_disruption": +0.10,
        "demand_shock": +0.10,
        "regulatory": -0.15,
        "lawsuit": -0.10,
        "earnings": 0.0,
    },
}


def style_tilt_for(style: str, event_type: str) -> float:
    """Return the additive conviction offset for (style, event_type).

    Returns 0.0 on any unknown key — never raises — so the engine can
    call this in a hot loop without defensive try/except and so new
    event_types added by the research layer don't crash existing
    backtests (plan-agent fatal issue #3).
    """
    return STYLE_TILT.get(style, {}).get(event_type, 0.0)


def flat_action_histogram(histogram: dict[str, object]) -> dict[str, int]:
    """Collapse a per-sub-pop bucketed action histogram into the legacy
    flat ``{"buy": N, "sell": N, "hold": N}`` shape.

    The freeform decision path now buckets the histogram by
    ``sub_pop_id`` so the frontend + report can show which sub-pops
    dissented. Legacy callers (tests, older frontend components) still
    expect the flat shape, and rather than branching everywhere we hand
    them this helper.

    Accepts the legacy flat shape too (returns a copy) so callers can
    be uniform without first checking the shape.
    """
    out: dict[str, int] = {"buy": 0, "sell": 0, "hold": 0}
    if not histogram:
        return out
    # Detect legacy flat shape: values are ints.
    sample = next(iter(histogram.values()))
    if isinstance(sample, int):
        for action, count in histogram.items():  # type: ignore[assignment]
            out[action] = out.get(action, 0) + int(count)
        return out
    # Bucketed shape: values are dicts.
    for sub_counts in histogram.values():
        if not isinstance(sub_counts, dict):
            continue
        for action, count in sub_counts.items():
            out[action] = out.get(action, 0) + int(count)
    return out


__all__ = ["STYLE_TILT", "style_tilt_for", "flat_action_histogram"]
