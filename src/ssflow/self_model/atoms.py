"""State atom library for the self-model subsystem.

Each atom is a ``StateAtomDef`` with an init rule (called once at sim
start from sandbox + event context) and an update rule (called every
round from state + round context + trade result). Atoms are pure —
they can only see what's in the state dict + the narrow RoundCtx /
TradeResult structs.

## Atom categories

- **universal_financial**: cash, holdings_value, nav, initial_nav,
  unrealized_pnl_pct, realized_pnl_cum, max_drawdown_pct, peak_nav
- **universal_trajectory**: last_action_side, last_action_size_pct,
  last_action_price, last_action_outcome_pct, rounds_since_last_trade
- **universal_emotional**: conviction, stress, regret, consecutive_loss_rounds
- **accountability**: client_net_redemption, career_risk_pressure,
  reputation_score
- **benchmark**: benchmark_gap_pct, peer_rank_percentile
- **mandate**: risk_budget_used_pct, compliance_breaches_this_sim
- **company_side**: cash_runway_months, board_pressure_index,
  customer_concentration_risk, media_negative_mention_count

## Adding a new atom

1. Write init_rule and update_rule as tiny pure functions
2. Register in STATE_ATOMS dict at module bottom
3. Add unit test in ``tests/test_self_model_atoms.py``
4. If you want the LLM to use it via ``persona_factory`` Stage 3,
   make sure the key appears in the catalog prompt fragment in
   ``persona_factory.py``.

Atom update rules are written defensively: they read from the state
dict via ``.get(key, default)`` so a partial spec (persona selects
some atoms but not others) doesn't KeyError at runtime.
"""

from __future__ import annotations

from typing import Any

from .schema import RoundCtx, StateAtomDef, TradeResult


# ─────────────────────── Universal financial ───────────────────────


def _init_cash(sandbox_state: dict[str, Any], event: Any, _ctx: Any) -> float:
    """Initial cash from the sandbox capital distribution.

    Falls back to 1e6 CNY if sandbox is absent (strategic personas /
    info-only personas that got accidentally wired into the evaluator).
    """
    cap_dist = (sandbox_state or {}).get("capital_distribution", {})
    if "median_cny" in cap_dist:
        return float(cap_dist["median_cny"])
    if "value_cny" in cap_dist:
        return float(cap_dist["value_cny"])
    return 1_000_000.0


def _update_cash(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    """Cash is authoritative from trade_result (sync_population_state wrote it)."""
    if tr.cash > 0:
        return tr.cash
    return state.get("cash", 1_000_000.0)


def _init_holdings_value(sandbox_state: dict[str, Any], event: Any, ctx: Any) -> float:
    """Initial holdings value = prob_holding × position_size × cash.

    Rough estimate for R0 before sync_population_state has run once.
    Good enough — as soon as R0 trading completes, the real value
    flows in from TradeResult.
    """
    cap_dist = (sandbox_state or {}).get("capital_distribution", {})
    init_pos = (sandbox_state or {}).get("initial_position_distribution", {})
    cash_est = cap_dist.get("median_cny") or cap_dist.get("value_cny") or 1e6
    prob = init_pos.get("prob_holding", 0.5)
    size = 0.15  # reasonable default when position_size_pct_when_holding absent
    pos_spec = init_pos.get("position_size_pct_when_holding")
    if isinstance(pos_spec, dict):
        lo = pos_spec.get("min", 0.05)
        hi = pos_spec.get("max", 0.30)
        size = (float(lo) + float(hi)) / 2
    return float(cash_est) * float(prob) * float(size)


def _update_holdings_value(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    if tr.holdings_value > 0:
        return tr.holdings_value
    # If nothing was reported, mark-to-market the prior holdings at the
    # current price ratio so nav tracking stays sane.
    prev = state.get("holdings_value", 0.0)
    if ctx.current_price > 0 and ctx.initial_price > 0:
        ratio = ctx.current_price / max(1e-9, state.get("_last_mtm_price", ctx.initial_price))
        return prev * ratio
    return prev


def _init_initial_nav(sandbox_state: dict[str, Any], event: Any, ctx: Any) -> float:
    """Frozen snapshot of R0 NAV for P&L denominator."""
    return _init_cash(sandbox_state, event, ctx) + _init_holdings_value(
        sandbox_state, event, ctx
    )


def _update_initial_nav(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    """Never changes — it's the R0 anchor."""
    return state.get("initial_nav", 0.0) or (state.get("cash", 0) + state.get("holdings_value", 0))


def _update_nav(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    if tr.nav > 0:
        return tr.nav
    return state.get("cash", 0) + state.get("holdings_value", 0)


def _update_unrealized_pnl_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    initial = state.get("initial_nav", 0)
    if initial <= 0:
        return 0.0
    current = state.get("nav", initial)
    return (current / initial - 1.0) * 100.0


def _update_peak_nav(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    nav = state.get("nav", 0)
    peak = state.get("peak_nav", 0)
    return max(peak, nav)


def _update_max_drawdown_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    peak = state.get("peak_nav", 0)
    nav = state.get("nav", 0)
    if peak <= 0:
        return state.get("max_drawdown_pct", 0.0)
    current_dd = (peak - nav) / peak * 100.0
    return max(state.get("max_drawdown_pct", 0.0), current_dd)


def _update_realized_pnl_cum(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Rough: when the persona net-sells, we mark the sell as realized gain
    against the weighted average cost of the prior holdings.

    This is a proxy — the authoritative realized P&L is inside
    ``trading_layer.compute_class_pnl`` but that's not easily callable
    per-round per-persona in the atom context. The approximation is good
    enough for the agent's self-narrative; the Report view still uses
    the real number.
    """
    prev = state.get("realized_pnl_cum", 0.0)
    # We can only observe net_flow sign; a proper realized P&L needs
    # cost-basis tracking which lives in trading_layer. Leave as-is for
    # now and document: atom is best-effort, backed by authoritative
    # trading_layer numbers for Report display.
    return prev


# ─────────────────────── Universal trajectory ───────────────────────


def _noop_init_zero(*_: Any) -> float:
    return 0.0


def _noop_init_neg_one(*_: Any) -> float:
    return -1.0


def _update_last_action_side_numeric(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Encoded numerically so it fits the float state dict.

    0.0 = hold, +1.0 = buy, -1.0 = sell.
    """
    return {"buy": 1.0, "sell": -1.0, "hold": 0.0}.get(tr.side, 0.0)


def _update_last_action_size_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    return tr.quantity_pct


def _update_last_action_price(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Price at the moment of the action — for P&L narration."""
    if tr.side in ("buy", "sell") and tr.quantity_pct > 0:
        return ctx.current_price
    # Preserve prior entry price if we held
    return state.get("last_action_price", ctx.initial_price)


def _update_last_action_outcome_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """What happened to the price between last action and now.

    This is rendered in the ``last_trade_outcome`` section so the agent
    can explicitly see "you bought at 73.29 and price is now 39.89,
    outcome -45.5%".
    """
    entry = state.get("last_action_price", 0)
    if entry <= 0 or ctx.current_price <= 0:
        return 0.0
    return (ctx.current_price / entry - 1.0) * 100.0


def _update_rounds_since_last_trade(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    if tr.side in ("buy", "sell") and tr.quantity_pct > 0:
        return 0.0
    return state.get("rounds_since_last_trade", 0.0) + 1.0


# ─────────────────────── Universal emotional ───────────────────────


def _init_conviction(*_: Any) -> float:
    return 0.5


def _update_conviction(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Conviction tracks recent decision outcomes.

    - Winning a bet (positive outcome after buy, or negative after sell)
      pushes conviction up by 0.15
    - Losing pushes down by 0.10
    - Stress always drags it down slightly
    - Clamped to [0, 1]
    """
    prev = state.get("conviction", 0.5)
    side = state.get("last_action_side", 0)  # numeric encoding
    outcome = state.get("last_action_outcome_pct", 0) / 100
    signal = 0.0
    if side > 0:  # was long
        signal = 0.15 if outcome > 0.01 else (-0.10 if outcome < -0.01 else 0)
    elif side < 0:  # was short / sold
        signal = 0.15 if outcome < -0.01 else (-0.10 if outcome > 0.01 else 0)
    stress = state.get("stress", 0)
    new = prev + signal - 0.05 * stress
    return max(0.0, min(1.0, new))


def _update_stress(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Drawdown-driven anxiety, clamped to [0, 1].

    - Every 10pp of unrealized loss adds 0.1 stress
    - Decays by 0.1 per round when pnl is non-negative
    """
    prev = state.get("stress", 0)
    upnl = state.get("unrealized_pnl_pct", 0)
    dd = state.get("max_drawdown_pct", 0)
    if upnl >= 0:
        new = max(0.0, prev - 0.1)
    else:
        new = prev + max(0, -upnl) / 100.0 + dd / 200.0
    return max(0.0, min(1.0, new))


def _update_regret(state: dict[str, float], ctx: RoundCtx, tr: TradeResult) -> float:
    """FOMO / missed-move pain.

    If the persona held (or went the wrong way) while the price made a
    big favorable move they missed, regret climbs. Simple proxy: sum
    |price move| in rounds where the persona's action magnitude was
    near zero.
    """
    prev = state.get("regret", 0)
    if tr.side == "hold" or tr.quantity_pct < 0.02:
        moved = abs(ctx.prev_round_delta_pct or 0) * 100
        if moved > 2.0:  # only count meaningful moves
            return min(1.0, prev + moved / 50.0)
    # Decay
    return max(0.0, prev - 0.05)


def _update_consecutive_loss_rounds(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    prev = state.get("consecutive_loss_rounds", 0)
    outcome = state.get("last_action_outcome_pct", 0)
    if outcome < 0:
        return prev + 1
    return 0.0


# ─────────────────────── Accountability ───────────────────────


def _update_client_net_redemption(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Clients start withdrawing once drawdown exceeds 10%.

    Simple linear model: redemption fraction = max(0, (drawdown - 10) / 100).
    """
    dd = state.get("max_drawdown_pct", 0)
    return max(0.0, (dd - 10.0) / 100.0)


def _update_career_risk_pressure(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Builds up with consecutive losses + client redemption."""
    losses = state.get("consecutive_loss_rounds", 0)
    redemption = state.get("client_net_redemption", 0)
    return min(1.0, losses * 0.15 + redemption * 2.0)


def _update_reputation_score(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Bounded around 0.5, drifts toward unrealized pnl direction."""
    prev = state.get("reputation_score", 0.5)
    upnl = state.get("unrealized_pnl_pct", 0)
    drift = 0.01 * (1 if upnl > 0 else -1) * min(abs(upnl) / 10, 1.0)
    return max(0.0, min(1.0, prev + drift))


# ─────────────────────── Benchmark ───────────────────────


def _update_benchmark_gap_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Persona NAV return minus benchmark return.

    Benchmark is modeled as a geometric mean of all trader personas'
    NAV growth — we don't have a real sector ETF track. Populated from
    ``ctx.world_public_state`` by the runtime's peer selector.
    """
    my_return = state.get("unrealized_pnl_pct", 0)
    peers = ctx.world_public_state or {}
    if not peers:
        return 0.0
    peer_returns = [
        float(p.get("unrealized_pnl_pct", 0))
        for p in peers.values()
        if isinstance(p, dict)
    ]
    if not peer_returns:
        return 0.0
    avg = sum(peer_returns) / len(peer_returns)
    return my_return - avg


def _update_peer_rank_percentile(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Rank this persona's unrealized_pnl_pct among all observable peers.

    Returns 0.0 = best, 1.0 = worst.
    """
    my_return = state.get("unrealized_pnl_pct", 0)
    peers = ctx.world_public_state or {}
    peer_returns = [
        float(p.get("unrealized_pnl_pct", 0))
        for p in peers.values()
        if isinstance(p, dict)
    ]
    if not peer_returns:
        return 0.5
    worse_or_equal = sum(1 for r in peer_returns if r <= my_return)
    return 1.0 - worse_or_equal / max(1, len(peer_returns))


# ─────────────────────── Mandate ───────────────────────


def _update_risk_budget_used_pct(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Current position size divided by the persona's mandated cap.

    Falls back to 0.3 (30% of cap) if sandbox risk.max_position_pct is
    absent — that's the neutral "mid-mandate" starting point.
    """
    position = state.get("holdings_value", 0)
    nav = state.get("nav", 0) or state.get("initial_nav", 1)
    return min(2.0, position / max(nav, 1))  # clamped a bit above 1 for "over-limit" signal


def _update_compliance_breaches_this_sim(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    prev = state.get("compliance_breaches_this_sim", 0)
    if state.get("risk_budget_used_pct", 0) > 1.0:
        return prev + 1
    return prev


# ─────────────────────── Company side ───────────────────────


def _init_cash_runway_months(
    sandbox_state: dict[str, Any], event: Any, ctx: Any
) -> float:
    """Start at 12 months — typical runway for a healthy listed company."""
    return 12.0


def _update_cash_runway_months(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Burns down linearly with time + accelerates under regulatory stress.

    Simplest useful model: decrement by round_hours/720 (one month =
    720 hours) + penalty if cumulative_delta is deeply negative.
    """
    prev = state.get("cash_runway_months", 12.0)
    burn_rate_months = ctx.round_hours / 720.0
    stress_penalty = 0.0
    if ctx.cumulative_delta_pct < -0.15:
        stress_penalty = 0.5  # regulatory / sentiment stress drains liquidity
    return max(0.0, prev - burn_rate_months - stress_penalty)


def _update_board_pressure_index(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Builds up from stock drop + time."""
    drop = max(0, -ctx.cumulative_delta_pct)
    return min(1.0, drop * 3.0 + ctx.round_idx * 0.05)


def _init_customer_concentration_risk(*_: Any) -> float:
    return 0.3  # 30% baseline


def _update_customer_concentration_risk(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Regulatory events push it up, recoveries pull it down."""
    prev = state.get("customer_concentration_risk", 0.3)
    if ctx.event_type in ("regulatory", "lawsuit", "geopolitical"):
        return min(1.0, prev + 0.03 * (1 + ctx.round_idx / 5))
    return max(0.0, prev - 0.02)


def _update_media_negative_mention_count(
    state: dict[str, float], ctx: RoundCtx, tr: TradeResult
) -> float:
    """Monotonic counter, climbs with drawdown."""
    prev = state.get("media_negative_mention_count", 0)
    if ctx.cumulative_delta_pct < -0.10:
        return prev + 1
    return prev


# ─────────────────────── Library registry ───────────────────────


STATE_ATOMS: dict[str, StateAtomDef] = {
    # Universal financial
    "cash": StateAtomDef(
        key="cash",
        init_rule=_init_cash,
        update_rule=_update_cash,
        label_zh="可用现金",
        category="financial",
    ),
    "holdings_value": StateAtomDef(
        key="holdings_value",
        init_rule=_init_holdings_value,
        update_rule=_update_holdings_value,
        label_zh="持仓市值",
        category="financial",
    ),
    "nav": StateAtomDef(
        key="nav",
        init_rule=_init_initial_nav,  # R0 nav == initial_nav
        update_rule=_update_nav,
        observable_by_peers=True,
        label_zh="账户净值",
        category="financial",
    ),
    "initial_nav": StateAtomDef(
        key="initial_nav",
        init_rule=_init_initial_nav,
        update_rule=_update_initial_nav,
        observable_by_self=False,  # internal anchor, not shown
        label_zh="初始净值",
        category="financial",
    ),
    "unrealized_pnl_pct": StateAtomDef(
        key="unrealized_pnl_pct",
        init_rule=_noop_init_zero,
        update_rule=_update_unrealized_pnl_pct,
        observable_by_peers=True,
        label_zh="未实现盈亏(%)",
        category="financial",
    ),
    "realized_pnl_cum": StateAtomDef(
        key="realized_pnl_cum",
        init_rule=_noop_init_zero,
        update_rule=_update_realized_pnl_cum,
        label_zh="累计已实现",
        category="financial",
    ),
    "peak_nav": StateAtomDef(
        key="peak_nav",
        init_rule=_init_initial_nav,
        update_rule=_update_peak_nav,
        observable_by_self=False,
        label_zh="历史峰值",
        category="financial",
    ),
    "max_drawdown_pct": StateAtomDef(
        key="max_drawdown_pct",
        init_rule=_noop_init_zero,
        update_rule=_update_max_drawdown_pct,
        observable_by_peers=True,
        label_zh="最大回撤(%)",
        category="financial",
    ),

    # Universal trajectory
    "last_action_side": StateAtomDef(
        key="last_action_side",
        init_rule=_noop_init_zero,  # 0=hold, +1=buy, -1=sell
        update_rule=_update_last_action_side_numeric,
        label_zh="上轮方向",
        category="trajectory",
    ),
    "last_action_size_pct": StateAtomDef(
        key="last_action_size_pct",
        init_rule=_noop_init_zero,
        update_rule=_update_last_action_size_pct,
        label_zh="上轮规模",
        category="trajectory",
    ),
    "last_action_price": StateAtomDef(
        key="last_action_price",
        init_rule=lambda sandbox, event, ctx: 0.0,
        update_rule=_update_last_action_price,
        label_zh="上轮入场价",
        category="trajectory",
    ),
    "last_action_outcome_pct": StateAtomDef(
        key="last_action_outcome_pct",
        init_rule=_noop_init_zero,
        update_rule=_update_last_action_outcome_pct,
        label_zh="上轮效果(%)",
        category="trajectory",
    ),
    "rounds_since_last_trade": StateAtomDef(
        key="rounds_since_last_trade",
        init_rule=_noop_init_zero,
        update_rule=_update_rounds_since_last_trade,
        label_zh="闲置轮数",
        category="trajectory",
    ),

    # Universal emotional
    "conviction": StateAtomDef(
        key="conviction",
        init_rule=_init_conviction,
        update_rule=_update_conviction,
        label_zh="信心",
        category="emotional",
    ),
    "stress": StateAtomDef(
        key="stress",
        init_rule=_noop_init_zero,
        update_rule=_update_stress,
        label_zh="压力",
        category="emotional",
    ),
    "regret": StateAtomDef(
        key="regret",
        init_rule=_noop_init_zero,
        update_rule=_update_regret,
        label_zh="遗憾",
        category="emotional",
    ),
    "consecutive_loss_rounds": StateAtomDef(
        key="consecutive_loss_rounds",
        init_rule=_noop_init_zero,
        update_rule=_update_consecutive_loss_rounds,
        label_zh="连亏轮数",
        category="emotional",
    ),

    # Accountability
    "client_net_redemption": StateAtomDef(
        key="client_net_redemption",
        init_rule=_noop_init_zero,
        update_rule=_update_client_net_redemption,
        label_zh="客户申赎",
        category="accountability",
    ),
    "career_risk_pressure": StateAtomDef(
        key="career_risk_pressure",
        init_rule=_noop_init_zero,
        update_rule=_update_career_risk_pressure,
        label_zh="职业风险",
        category="accountability",
    ),
    "reputation_score": StateAtomDef(
        key="reputation_score",
        init_rule=_init_conviction,  # 0.5 baseline
        update_rule=_update_reputation_score,
        observable_by_peers=True,
        label_zh="声誉",
        category="accountability",
    ),

    # Benchmark
    "benchmark_gap_pct": StateAtomDef(
        key="benchmark_gap_pct",
        init_rule=_noop_init_zero,
        update_rule=_update_benchmark_gap_pct,
        label_zh="vs基准(pp)",
        category="benchmark",
    ),
    "peer_rank_percentile": StateAtomDef(
        key="peer_rank_percentile",
        init_rule=_init_conviction,  # 0.5 midpoint
        update_rule=_update_peer_rank_percentile,
        label_zh="同业排名",
        category="benchmark",
    ),

    # Mandate
    "risk_budget_used_pct": StateAtomDef(
        key="risk_budget_used_pct",
        init_rule=lambda *args: 0.3,  # 30% baseline
        update_rule=_update_risk_budget_used_pct,
        label_zh="风险预算",
        category="mandate",
    ),
    "compliance_breaches_this_sim": StateAtomDef(
        key="compliance_breaches_this_sim",
        init_rule=_noop_init_zero,
        update_rule=_update_compliance_breaches_this_sim,
        label_zh="合规违规次",
        category="mandate",
    ),

    # Company side
    "cash_runway_months": StateAtomDef(
        key="cash_runway_months",
        init_rule=_init_cash_runway_months,
        update_rule=_update_cash_runway_months,
        label_zh="现金支撑(月)",
        category="company",
    ),
    "board_pressure_index": StateAtomDef(
        key="board_pressure_index",
        init_rule=_noop_init_zero,
        update_rule=_update_board_pressure_index,
        label_zh="董事会压力",
        category="company",
    ),
    "customer_concentration_risk": StateAtomDef(
        key="customer_concentration_risk",
        init_rule=_init_customer_concentration_risk,
        update_rule=_update_customer_concentration_risk,
        label_zh="客户集中风险",
        category="company",
    ),
    "media_negative_mention_count": StateAtomDef(
        key="media_negative_mention_count",
        init_rule=_noop_init_zero,
        update_rule=_update_media_negative_mention_count,
        label_zh="负面媒体提及",
        category="company",
    ),
}


def atom_keys_by_category() -> dict[str, list[str]]:
    """Group atom keys by category — used by persona_factory Stage 3 prompt."""
    out: dict[str, list[str]] = {}
    for k, a in STATE_ATOMS.items():
        out.setdefault(a.category, []).append(k)
    return out


__all__ = ["STATE_ATOMS", "atom_keys_by_category"]
