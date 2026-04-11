"""Default self-model bundle for legacy personas without an explicit spec.

Any persona whose YAML doesn't carry a ``self_model:`` block (which is
all 39 of the current ``personas/ashare.yaml`` pack) falls back to this
bundle. It's deliberately minimal:

- Just the universal financial + trajectory + emotional atoms
- A simple 4-term utility (nav + drawdown + stress + conviction)
- 3 render sections so token footprint stays low
- Peer watchlist is permissive (all traders in scope)

This lets us turn self-model ON by default in the engine without having
to regenerate the persona pack. The results for the first backtest run
will be mostly down to:
  1. The 4 P0 bug fixes
  2. Agents getting self-visibility via 3 render sections
  3. Agents getting peer-visibility via the watchlist

If that doesn't move direction accuracy enough, Phase 4 (factory Stage 3
extension) generates richer per-persona specs.
"""

from __future__ import annotations

from .schema import SelfModelSpec


# The dict form is what's stored on ``Persona.self_model`` — serialized,
# survives deep-copy by persona_proposer._overlay_on_base.
DEFAULT_SELF_MODEL_DICT: dict = {
    "state_atoms": [
        # Universal financial
        "cash",
        "holdings_value",
        "nav",
        "initial_nav",
        "unrealized_pnl_pct",
        "max_drawdown_pct",
        "peak_nav",
        # Universal trajectory
        "last_action_side",
        "last_action_size_pct",
        "last_action_price",
        "last_action_outcome_pct",
        "rounds_since_last_trade",
        # Universal emotional
        "conviction",
        "stress",
        "consecutive_loss_rounds",
    ],
    "utility_weights": {
        "nav_growth": 1.0,
        "drawdown_penalty": 1.0,
        "stress_penalty": 0.5,
        "conviction_reward": 0.5,
    },
    "render_sections": [
        "financial_snapshot",
        "last_trade_outcome",
        "emotional_state",
    ],
    "peer_watchlist_filter": {
        "role_match": ["*"],
        "top_k": 2,
        "sort_by": "nav_growth_desc",
    },
    "custom_atoms": [],
}


# Convenience object form for Python callers that want a SelfModelSpec.
DEFAULT_SELF_MODEL = SelfModelSpec.from_dict(DEFAULT_SELF_MODEL_DICT)


__all__ = ["DEFAULT_SELF_MODEL", "DEFAULT_SELF_MODEL_DICT"]
