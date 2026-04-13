"""Pure-Python trading layer — the math core used by the OASIS engine.

This module is intentionally framework-agnostic. No OASIS, no CAMEL, no
asyncio. It only knows about:

  - `Agent` dataclass: one stochastic trader instance (capital, cash, holdings)
  - `spawn_agents(persona, current_price)`: sample N agents from the persona's
    capital + holdings distributions
  - `apply_action(agent, action_spec, current_price)`: execute one action,
    mutating the agent's cash + holdings
  - `normalize_action_distribution(raw, expected)`: clean up an LLM-returned
    distribution (clip negatives, drop unknowns, rescale to sum 1.0)
  - `apply_distribution_to_agent_pop(persona, agents, distribution, ...)`:
    sample one action per agent from the distribution, apply it, return a
    `ClassFlowResult` with the aggregated net flow + histogram
  - `ClassFlowResult` dataclass: aggregated round result per persona class

The Phase II `oasis_engine` calls `apply_distribution_to_agent_pop` after
each round's OASIS social step, once per trader, using the distributions
collected via `oasis_trading_tool.OrderCollector` from each trader's
`submit_order_distribution` tool call.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any

from .limit_board import LimitBoard, T1Ledger
from .output_filter import sanitize_text
from .persona import Persona, SubPopulation
from .simulation_params import DecisionParams
from .sub_population_styles import style_tilt_for


log = logging.getLogger(__name__)


# Tolerance for action_distribution normalization. Outside this band we still
# normalize but log a warning.
NORMALIZATION_TOLERANCE = 0.05


# ─────────────────────── Stochastic agent state ───────────────────────


# When no explicit ticker is given, we use this key for backward compatibility.
_DEFAULT_TICKER = "_default"


@dataclass
class TraderInstance:
    """One stochastic trader instance, persistent across rounds.

    Capital is immutable, holdings + cash mutate as the agent buys / sells.

    Multi-instrument support: `holdings` is a dict[ticker → shares].
    For backward compatibility, the `holdings_shares` property reads/writes
    the default ticker entry, and methods accept either a float (single
    instrument) or a dict[str, float] (multi-instrument prices).

    Renamed from `Agent` in the architecture restructure. The old name
    is kept as an alias for backward compatibility.
    """

    persona_id: str
    capital: float                  # immutable: NAV at spawn time, in event currency
    cash: float                     # mutable: shrinks on buy, grows on sell
    holdings: dict[str, float] = field(default_factory=dict)  # {ticker: shares}
    max_holdings_value: float = 0.0  # immutable: capital × max_position_pct
    reaction_lag: int = 0           # round index when this agent becomes active
    first_position_round: int = -1  # round when first non-zero position opened (-1 = never)
    agent_id: str = ""              # unique ID for T1Ledger tracking (auto-assigned at spawn)

    # Intra-class sub-population assignment (Phase II heterogeneity).
    # Defaults keep single-bucket homogeneous behavior for every persona
    # without a ``sub_populations`` block. When the persona declares
    # sub-pops, ``spawn_agents`` stamps each instance with the sampled
    # ``SubPopulation`` and the decision path applies per-agent
    # STYLE_TILT + event_conviction_offset before Gaussian dispersion.
    sub_pop_id: str = "default"
    sub_pop_style: str = ""
    sub_pop_traits: SubPopulation | None = None

    @property
    def holdings_shares(self) -> float:
        """Backward compat: total shares of the default ticker."""
        return self.holdings.get(_DEFAULT_TICKER, 0.0)

    @holdings_shares.setter
    def holdings_shares(self, value: float) -> None:
        self.holdings[_DEFAULT_TICKER] = value

    def shares_of(self, ticker: str) -> float:
        return self.holdings.get(ticker, 0.0)

    def _resolve_prices(self, current_price: float | dict[str, float]) -> dict[str, float]:
        if isinstance(current_price, dict):
            return current_price
        return {_DEFAULT_TICKER: current_price}

    def holdings_value(self, current_price: float | dict[str, float]) -> float:
        # Iterate over holdings keys — NOT prices — so positions under
        # tickers the caller didn't supply are still accounted for.
        # Scalar price: apply uniformly to every holdings entry (single-
        # instrument mode, or emergency fallback when the caller has
        # only a global reference price).
        if isinstance(current_price, dict):
            return sum(
                shares * current_price.get(ticker, 0.0)
                for ticker, shares in self.holdings.items()
            )
        return sum(shares * current_price for shares in self.holdings.values())

    def nav(self, current_price: float | dict[str, float]) -> float:
        return self.cash + self.holdings_value(current_price)

    def pnl(self, current_price: float | dict[str, float]) -> float:
        return self.nav(current_price) - self.capital

    def buy_headroom(self, current_price: float | dict[str, float]) -> float:
        """Remaining buy capacity given the persona's max_position_pct cap."""
        return max(0.0, self.max_holdings_value - self.holdings_value(current_price))

    def short_headroom(self, current_price: float | dict[str, float]) -> float:
        """Remaining short capacity — how much more short exposure is allowed.

        Short positions show as negative holdings_value. The absolute value
        of total exposure (long + short) must stay within max_holdings_value.
        """
        current_exposure = abs(self.holdings_value(current_price))
        return max(0.0, self.max_holdings_value - current_exposure)


# Backward-compat alias: old code uses `Agent`, new code uses `TraderInstance`
Agent = TraderInstance


def _sample_capital(spec: dict[str, Any], rng: random.Random) -> float:
    dtype = spec.get("type", "fixed")
    if dtype == "lognormal":
        median = float(spec["median_cny"])
        sigma = float(spec["sigma"])
        if median <= 0 or sigma <= 0:
            raise ValueError(
                f"lognormal median_cny and sigma must be > 0, got {median}, {sigma}"
            )
        mu = math.log(median)
        val = rng.lognormvariate(mu, sigma)
        floor = float(spec.get("floor_cny", 0.0))
        ceiling = float(spec.get("ceiling_cny", float("inf")))
        return max(floor, min(ceiling, val))
    if dtype == "uniform":
        return rng.uniform(float(spec["min_cny"]), float(spec["max_cny"]))
    if dtype == "fixed":
        return float(spec["value_cny"])
    raise ValueError(f"Unknown capital_distribution type: {dtype!r}")


def _sample_position_pct(spec: dict[str, Any], rng: random.Random) -> float:
    if not spec:
        return 0.0
    dtype = spec.get("type", "none")
    if dtype == "none":
        return 0.0
    if dtype == "fixed":
        return float(spec.get("value", 0.0))
    if dtype == "bernoulli":
        prob = float(spec.get("prob_holding", 0.5))
        if rng.random() > prob:
            return 0.0
        size_spec = spec.get(
            "position_size_pct_when_holding",
            {"type": "uniform", "min": 0.05, "max": 0.30},
        )
        size_type = size_spec.get("type", "uniform")
        if size_type == "uniform":
            return rng.uniform(float(size_spec["min"]), float(size_spec["max"]))
        if size_type == "fixed":
            return float(size_spec["value"])
        raise ValueError(f"Unknown position size spec type: {size_type!r}")
    raise ValueError(f"Unknown initial_position_distribution type: {dtype!r}")


def _sample_reaction_lag(spec: dict[str, Any], rng: random.Random) -> int:
    """Sample a reaction lag from the persona's reaction_lag_rounds spec.

    Returns the round index at which the agent becomes active (0 = immediate).
    """
    if not spec:
        return 0
    dtype = spec.get("type", "none")
    if dtype == "discrete":
        values = spec.get("values", [0])
        return int(rng.choice(values)) if values else 0
    if dtype == "fixed":
        return int(spec.get("value", 0))
    return 0


def spawn_agents(
    persona: Persona,
    current_price: float,
    rng: random.Random,
    *,
    primary_ticker: str = _DEFAULT_TICKER,
    multi_prices: dict[str, float] | None = None,
    holdings_by_persona: dict[str, dict[str, float]] | None = None,
) -> list[Agent]:
    """Sample N agent instances from a persona's sandbox config.

    Args:
        primary_ticker: which ticker to assign initial holdings to in
            single-instrument mode.
        multi_prices: when provided (multi-instrument mode), initial holdings
            are distributed across tickers.
        holdings_by_persona: {ticker: {persona_id: pct}} from real holder data.
            When provided, initial positions are allocated proportionally to
            the persona's real ownership stake in each instrument.
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"persona '{persona.id}' has no sandbox config; cannot spawn agents"
        )
    if current_price <= 0:
        raise ValueError(f"current_price must be > 0, got {current_price}")
    if sandbox.instance_count <= 0:
        raise ValueError(
            f"persona '{persona.id}' sandbox.instance_count must be > 0, "
            f"got {sandbox.instance_count}"
        )

    default_max_position_pct = float(sandbox.risk.get("max_position_pct", 1.0))
    default_max_position_pct = max(0.0, min(1.0, default_max_position_pct))

    # ── Sub-population assignment (deterministic under ``rng``) ──
    #
    # When the persona declares ``sub_populations``, we draw instance_count
    # sub-pop labels *before* the per-agent loop using the same ``rng`` that
    # already seeds capital / position / lag sampling. This keeps
    # spawn_agents fully deterministic under a shared seed — critical for
    # backtest reproducibility (plan-agent fatal issue #2).
    sub_pops = persona.sub_populations
    if sub_pops:
        sub_pop_assignments: list[SubPopulation | None] = rng.choices(
            sub_pops,
            weights=[sp.fraction for sp in sub_pops],
            k=sandbox.instance_count,
        )
    else:
        sub_pop_assignments = [None] * sandbox.instance_count

    agents: list[Agent] = []
    for agent_idx in range(sandbox.instance_count):
        sp = sub_pop_assignments[agent_idx]
        capital = _sample_capital(sandbox.capital_distribution, rng)
        if sp is not None and sp.capital_multiplier != 1.0:
            capital = capital * sp.capital_multiplier
        # Sub-pop may override the class-level prob_holding so e.g. a
        # "burnt_veteran" sub-pop of a retail class can start mostly flat
        # even when the base class runs a bernoulli(0.60).
        pos_spec = sandbox.initial_position_distribution
        if sp is not None and sp.prob_holding_override is not None and pos_spec:
            pos_spec = {**pos_spec, "prob_holding": sp.prob_holding_override}
        position_pct = _sample_position_pct(pos_spec, rng)
        position_pct = max(0.0, min(1.0, position_pct))
        holdings_value = capital * position_pct
        cash = capital - holdings_value
        reaction_lag = _sample_reaction_lag(sandbox.reaction_lag_rounds, rng)

        # Per-agent risk cap: sub-pop override first, else the class default.
        if sp is not None and sp.max_position_pct_override is not None:
            agent_max_pct = max(0.0, min(1.0, float(sp.max_position_pct_override)))
        else:
            agent_max_pct = default_max_position_pct
        agent_max_holdings = capital * agent_max_pct

        sub_pop_id = sp.id if sp is not None else "default"
        sub_pop_style = sp.decision_style if sp is not None else ""

        if multi_prices and len(multi_prices) > 0:
            # Multi-instrument: allocate initial holdings based on real
            # holder structure data when available.
            holdings = {}
            if holdings_by_persona and persona.id in holdings_by_persona:
                # Data-driven: this persona type owns X% of each instrument.
                # Scale by holdings_value (not capital) to get the right NAV.
                total_weight = 0.0
                weights: dict[str, float] = {}
                for tk in multi_prices:
                    pct = holdings_by_persona.get(tk, {}).get(persona.id, 0)
                    weights[tk] = pct
                    total_weight += pct
                if total_weight > 0:
                    for tk, price in multi_prices.items():
                        if price > 0 and weights.get(tk, 0) > 0:
                            alloc = holdings_value * (weights[tk] / total_weight)
                            holdings[tk] = alloc / price
                else:
                    # Persona type not found in any instrument's holders — equal split
                    per_ticker_value = holdings_value / len(multi_prices)
                    for tk, price in multi_prices.items():
                        if price > 0:
                            holdings[tk] = per_ticker_value / price
            else:
                # No holder data: spread equally
                per_ticker_value = holdings_value / len(multi_prices)
                for tk, price in multi_prices.items():
                    if price > 0:
                        holdings[tk] = per_ticker_value / price
            agents.append(
                Agent(
                    persona_id=persona.id,
                    capital=capital,
                    cash=cash,
                    holdings=holdings,
                    max_holdings_value=agent_max_holdings,
                    reaction_lag=reaction_lag,
                    first_position_round=0 if holdings_value > 0 else -1,
                    agent_id=f"{persona.id}_{agent_idx}",
                    sub_pop_id=sub_pop_id,
                    sub_pop_style=sub_pop_style,
                    sub_pop_traits=sp,
                )
            )
        else:
            # Single-instrument
            holdings_shares = holdings_value / current_price
            agents.append(
                Agent(
                    persona_id=persona.id,
                    capital=capital,
                    cash=cash,
                    holdings={primary_ticker: holdings_shares},
                    max_holdings_value=agent_max_holdings,
                    reaction_lag=reaction_lag,
                    first_position_round=0 if holdings_value > 0 else -1,
                    agent_id=f"{persona.id}_{agent_idx}",
                    sub_pop_id=sub_pop_id,
                    sub_pop_style=sub_pop_style,
                    sub_pop_traits=sp,
                )
            )
    return agents


# ─────────────────────── Action application ───────────────────────


def apply_action(
    agent: Agent,
    action_spec: dict[str, Any],
    current_price: float,
    *,
    instrument: str = _DEFAULT_TICKER,
    round_idx: int = 0,
    t1_ledger: T1Ledger | None = None,
) -> float:
    """Apply one action_spec to one agent. Mutates state. Returns order amount.

    Order amount is in event currency, positive for buy, negative for sell,
    zero for hold or insufficient capacity. The buy path is capped by
    `agent.buy_headroom(current_price)` so the persona's max_position_pct
    constraint is enforced.

    instrument: which ticker to trade. Defaults to _DEFAULT_TICKER for
    backward compat with single-instrument simulations.
    round_idx: current round index, used to track first_position_round.
    t1_ledger: optional T1Ledger for enforcing T+1 sell constraints.
        When provided, sell amounts are capped to sellable shares (excluding
        shares bought in the current round) and buys are recorded.
    """
    side = action_spec.get("side", "none")
    pool = action_spec.get("pool", "none")
    try:
        fraction = float(action_spec.get("fraction", 0.0))
    except (TypeError, ValueError):
        fraction = 0.0

    if side == "none" or fraction <= 0:
        return 0.0

    current_shares = agent.holdings.get(instrument, 0.0)

    if pool == "holdings_in_target":
        if current_shares <= 0:
            return 0.0
        shares_to_act = current_shares * fraction
        amount = shares_to_act * current_price
    elif pool == "cash":
        if agent.cash <= 0:
            return 0.0
        amount = agent.cash * fraction
        shares_to_act = amount / current_price
    elif pool == "margin":
        # Short-sell: use cash as collateral to borrow shares and sell them.
        # Creates negative holdings (short position). Cash increases by proceeds.
        if side != "sell":
            return 0.0  # margin pool only valid for selling
        if agent.cash <= 0:
            return 0.0
        amount = agent.cash * fraction
        shares_to_act = amount / current_price
        # Cap by short headroom (total exposure <= max_holdings_value)
        headroom = agent.short_headroom(current_price)
        if headroom <= 0:
            return 0.0
        if amount > headroom:
            amount = headroom
            shares_to_act = amount / current_price
        agent.holdings[instrument] = current_shares - shares_to_act
        agent.cash += amount
        return -amount
    else:
        return 0.0

    if side == "sell":
        # T+1 enforcement: cap sell to shares that are actually sellable
        if t1_ledger is not None:
            agent_id = agent.agent_id or agent.persona_id
            sellable = t1_ledger.sellable_shares(
                agent_id, instrument, round_idx,
                total_holdings=current_shares,
            )
            if sellable <= 0:
                return 0.0
            if shares_to_act > sellable:
                shares_to_act = sellable
                amount = shares_to_act * current_price
        agent.holdings[instrument] = current_shares - shares_to_act
        agent.cash += amount
        return -amount
    if side == "buy":
        headroom = agent.buy_headroom(current_price)
        if headroom <= 0:
            return 0.0
        if amount > headroom:
            amount = headroom
            shares_to_act = amount / current_price
        # Track first position opening
        had_no_position = current_shares <= 0
        agent.holdings[instrument] = current_shares + shares_to_act
        agent.cash -= amount
        if had_no_position and shares_to_act > 0 and agent.first_position_round < 0:
            agent.first_position_round = round_idx
        # T+1 recording: mark these shares as locked for this round
        if t1_ledger is not None:
            agent_id = agent.agent_id or agent.persona_id
            t1_ledger.record_buy(agent_id, instrument, shares_to_act, round_idx)
        return +amount
    return 0.0


def normalize_action_distribution(
    raw: dict[str, float], expected_actions: list[str]
) -> tuple[dict[str, float], str | None]:
    """Clip negatives, drop unknown keys, normalize to sum 1.0.

    Returns (clean_distribution, optional_warning_string).
    """
    expected_set = set(expected_actions)
    warnings: list[str] = []

    unknown = set(raw.keys()) - expected_set
    if unknown:
        warnings.append(f"unknown actions dropped: {sorted(unknown)}")
    cleaned = {k: v for k, v in raw.items() if k in expected_set}

    negatives = {k: v for k, v in cleaned.items() if v < 0}
    if negatives:
        warnings.append(f"negative probabilities clipped: {sorted(negatives)}")
    cleaned = {k: max(0.0, v) for k, v in cleaned.items()}

    for action in expected_actions:
        cleaned.setdefault(action, 0.0)

    total = sum(cleaned.values())
    if total <= 0:
        warnings.append("distribution sum was zero, defaulting to uniform")
        n = len(expected_actions)
        normalized = {action: 1.0 / n for action in expected_actions}
    else:
        if abs(total - 1.0) > NORMALIZATION_TOLERANCE:
            warnings.append(f"sum was {total:.4f}, renormalized to 1.0")
        normalized = {k: v / total for k, v in cleaned.items()}

    return normalized, "; ".join(warnings) if warnings else None


# ─────────────────────── Round result dataclass ───────────────────────


@dataclass
class ClassFlowResult:
    """One persona class's contribution to a round.

    The Phase I `oasis_engine` calls `decide_orders(persona, ...)` once per
    trading persona per round and collects these into a list to feed Kyle.
    """

    persona_id: str
    net_flow: float
    action_histogram: dict[str, int]
    n_agents: int
    instrument: str = "_default"
    rationale: str = ""
    raw_distribution: dict[str, float] = field(default_factory=dict)
    normalized_distribution: dict[str, float] = field(default_factory=dict)
    normalization_warning: str | None = None
    confidence: float = 0.5
    system_fingerprint: str | None = None
    fill_rate: float = 1.0          # Fraction of intended orders that filled [0, 1]
    unfilled_volume: float = 0.0    # Volume that could not fill due to limit board
    t1_blocked_sells: int = 0       # Number of sell attempts blocked by T+1
    # Per-sub-pop breakdown of ``action_histogram`` for classes that declare
    # ``sub_populations``. Keys are sub_pop_id; values are per-action counts.
    # For personas without sub_populations this is {"default": {...}} and
    # summing across keys gives back ``action_histogram``.
    action_histogram_by_sub_pop: dict[str, dict[str, int]] = field(default_factory=dict)
    # Phase-based execution metadata (backward-compat: defaults to empty/llm)
    phase: str = ""                  # "fast_react" | "slow_react" | ""
    source: str = "llm"             # "llm" | "plan" | "policy" | "hold"


# ─────────────────────── Distribution application (pure math) ───────────────────────


def _compute_fill_rate(
    limit_board: LimitBoard | None,
    buy_volume: float,
    sell_volume: float,
) -> tuple[float, float, float, float]:
    """Compute per-side fill rates from a limit board's state.

    Returns:
        (buy_fill_rate, sell_fill_rate, unfilled_buy, unfilled_sell)
    """
    if limit_board is None or not limit_board.at_limit:
        return 1.0, 1.0, 0.0, 0.0

    if limit_board.at_limit_up:
        # LIMIT_UP / ONE_WORD_UP: sell orders fill fully; buy orders partially
        # fill based on contra-side (sell) volume available.
        sell_fill = 1.0
        if buy_volume > 0 and sell_volume < buy_volume:
            buy_fill = sell_volume / buy_volume
        elif buy_volume > 0:
            buy_fill = 1.0
        else:
            buy_fill = 1.0
        unfilled_buy = max(0.0, buy_volume - sell_volume) if buy_volume > 0 else 0.0
        return buy_fill, sell_fill, unfilled_buy, 0.0

    if limit_board.at_limit_down:
        # LIMIT_DOWN / ONE_WORD_DOWN: buy orders fill fully; sell orders partially
        # fill based on contra-side (buy) volume available.
        buy_fill = 1.0
        if sell_volume > 0 and buy_volume < sell_volume:
            sell_fill = buy_volume / sell_volume
        elif sell_volume > 0:
            sell_fill = 1.0
        else:
            sell_fill = 1.0
        unfilled_sell = max(0.0, sell_volume - buy_volume) if sell_volume > 0 else 0.0
        return buy_fill, sell_fill, 0.0, unfilled_sell

    return 1.0, 1.0, 0.0, 0.0


def apply_distribution_to_agent_pop(
    persona: Persona,
    agents: list[Agent],
    distribution: dict[str, float],
    current_price: float,
    rng: random.Random,
    *,
    instrument: str = _DEFAULT_TICKER,
    rationale: str = "",
    confidence: float = 0.5,
    raw_distribution: dict[str, float] | None = None,
    system_fingerprint: str | None = None,
    round_idx: int = 0,
    participation_rate: float = 1.0,
    conviction_damper: float = 1.0,
    limit_board: LimitBoard | None = None,
    t1_ledger: T1Ledger | None = None,
    event_type: str = "",
    decision_params: DecisionParams | None = None,
) -> ClassFlowResult:
    """Pure-math: sample one action per agent from `distribution`, apply it,
    return aggregated ClassFlowResult. Does NOT make any LLM calls.

    This is the function both `decide_orders` (LLM path) and the Phase II
    unified-decision path (OASIS tool-call path) call to turn a normalized
    distribution into actual per-agent mutations + net flow.

    Mutates `agents` in place: their cash + holdings reflect the round's trades.
    Only agents whose `reaction_lag <= round_idx` participate; the rest hold.

    Args:
        persona: trading persona (must have sandbox + action_space)
        agents: pre-spawned agent population; mutated in place
        distribution: dict of action_name → probability. Will be normalized
            via `normalize_action_distribution` before sampling.
        current_price: instrument price, in event currency
        rng: shared sampling RNG
        rationale: optional LLM rationale string (attached to the result)
        confidence: optional LLM confidence (attached to the result)
        raw_distribution: optional raw pre-normalization dict for audit
        system_fingerprint: optional LLM provider fingerprint for reproducibility
        round_idx: current simulation round (0-based). Agents with
            reaction_lag > round_idx are filtered out (they hold).
        limit_board: optional LimitBoard for fill-rate constraints.
            When at limit, one side's orders are partially filled.
        t1_ledger: optional T1Ledger for T+1 sell constraints.
            When provided, sell amounts are capped to sellable shares and
            buys are recorded for lockup tracking.
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"apply_distribution_to_agent_pop: persona '{persona.id}' has no sandbox"
        )

    # Filter to agents whose reaction_lag has been reached
    active_agents = [a for a in agents if a.reaction_lag <= round_idx]

    # Participation rate: subsample active agents (urgency decay / exhaustion)
    if participation_rate < 1.0 and active_agents:
        n_keep = max(1, int(len(active_agents) * participation_rate))
        active_agents = rng.sample(active_agents, n_keep)

    if not active_agents:
        return ClassFlowResult(
            persona_id=persona.id,
            instrument=instrument,
            net_flow=0.0,
            action_histogram={},
            n_agents=0,
            rationale=sanitize_text(rationale),
            raw_distribution=raw_distribution or dict(distribution),
            normalized_distribution={},
            normalization_warning="no active agents this round (reaction_lag)",
            confidence=confidence,
            system_fingerprint=system_fingerprint,
        )

    # ── Build per-agent action specs (first pass — no mutations yet) ──
    #
    # Both the freeform and legacy paths first collect (agent, spec) pairs,
    # then we compute limit-board fill rates from the aggregate intended
    # volume, and finally apply the fill-rate-scaled orders.

    agent_specs: list[tuple[Agent, dict[str, Any]]] = []
    histogram: dict[str, int] = {}
    # Per-sub-pop bucketed view of the histogram. Populated in both the
    # freeform and legacy paths so ClassFlowResult always carries the
    # breakdown — personas without sub_populations land everything under
    # a single "default" key, which sums back to ``histogram``.
    histogram_by_sub_pop: dict[str, dict[str, int]] = {}
    normalized: dict[str, float] = {}
    warning: str | None = None

    # ── Free-form path: per-agent stochastic dispersion ──
    #
    # The LLM decides once for the whole class (side + qty_pct). But real
    # participants within a class don't all act identically — some chase
    # momentum, others take profits, others sit out. We model this by
    # converting the class decision into a conviction score and adding
    # per-agent Gaussian noise. The dispersion is derived from the
    # persona's behavioral biases (herd_following → low noise, contrarian
    # → high noise). No hardcoded thresholds — heterogeneity emerges
    # from the persona's own documented characteristics.
    if "__freeform__" in distribution and raw_distribution:
        side = raw_distribution.get("side", "hold")
        qty_pct = float(raw_distribution.get("quantity_pct", 0.0))
        pool = raw_distribution.get("pool", "")

        # Conviction damper: mechanically reduce order magnitude
        if conviction_damper < 1.0:
            qty_pct = qty_pct * conviction_damper

        # Convert the class decision to a conviction score:
        #   buy  → +qty_pct
        #   sell → -qty_pct
        #   hold → 0
        if side == "buy":
            class_conviction = qty_pct
        elif side == "sell":
            class_conviction = -qty_pct
        else:
            class_conviction = 0.0

        # When the LLM explicitly chose "hold", ALL agents hold — no noise,
        # no sub-pop sampling. Sub-pop dispersion is meant to create
        # heterogeneity around a *directional* signal; when class conviction
        # is zero, noise produces a random directional bias that looks like
        # "hold but net buy ¥0.9億" — a credibility-killing contradiction.
        if class_conviction == 0.0:
            histogram = {"buy": 0, "sell": 0, "hold": len(active_agents)}
            for agent_inst in active_agents:
                spec = {"side": "none", "pool": "none", "fraction": 0.0}
                agent_specs.append((agent_inst, spec))
                bucket = histogram_by_sub_pop.setdefault(
                    agent_inst.sub_pop_id,
                    {"buy": 0, "sell": 0, "hold": 0},
                )
                bucket["hold"] += 1
            normalized = {"__freeform__": 1.0}
        else:
            # Compute per-agent dispersion from persona biases.
            # Weights and floor are now driven by DecisionParams so each
            # simulation can have different sensitivity.
            dp = decision_params or DecisionParams()
            biases = persona.biases if persona.biases else {}
            herd = biases.get("herd_following", biases.get("herd_mentality", 0.5))
            contrarian = biases.get("contrarian_tendency", 0.0)
            dispersion = max(
                dp.min_dispersion,
                dp.herd_weight * (1.0 - herd) + dp.contrarian_weight * contrarian,
            )
            conv_thresh = dp.conviction_threshold
            tilt_table = dp.style_tilt if dp.style_tilt else None

            histogram = {"buy": 0, "sell": 0, "hold": 0}
            for agent_inst in active_agents:
                style_tilt = style_tilt_for(
                    agent_inst.sub_pop_style, event_type, tilt_table=tilt_table,
                )
                explicit_offset = 0.0
                traits = agent_inst.sub_pop_traits
                if traits is not None and traits.event_conviction_offset:
                    explicit_offset = float(
                        traits.event_conviction_offset.get(event_type, 0.0)
                    )
                agent_conviction = (
                    class_conviction
                    + style_tilt
                    + explicit_offset
                    + rng.gauss(0, dispersion)
                )

                if agent_conviction > conv_thresh:
                    agent_frac = min(abs(agent_conviction), 1.0)
                    buy_pool = pool if pool and side == "buy" else "cash"
                    spec = {"side": "buy", "pool": buy_pool, "fraction": agent_frac}
                    action_name = "buy"
                elif agent_conviction < -conv_thresh:
                    agent_frac = min(abs(agent_conviction), 1.0)
                    sell_pool = pool if pool and side == "sell" else "holdings_in_target"
                    spec = {"side": "sell", "pool": sell_pool, "fraction": agent_frac}
                    action_name = "sell"
                else:
                    spec = {"side": "none", "pool": "none", "fraction": 0.0}
                    action_name = "hold"

                histogram[action_name] += 1
                bucket = histogram_by_sub_pop.setdefault(
                    agent_inst.sub_pop_id,
                    {"buy": 0, "sell": 0, "hold": 0},
                )
                bucket[action_name] += 1

                agent_specs.append((agent_inst, spec))

            normalized = {"__freeform__": 1.0}

    else:
        # ── Legacy distribution path ──
        expected_names = [a["name"] for a in sandbox.action_space]
        normalized, warning = normalize_action_distribution(distribution, expected_names)
        if warning:
            log.info(
                "persona %s: distribution normalization: %s",
                persona.id, warning,
            )

        spec_by_name = {a["name"]: a for a in sandbox.action_space}
        # Conviction damper: scale action fractions for legacy distribution path
        if conviction_damper < 1.0:
            spec_by_name = {
                name: {**s, "fraction": s.get("fraction", 0.0) * conviction_damper}
                for name, s in spec_by_name.items()
            }
        actions = list(normalized.keys())
        weights = list(normalized.values())
        sampled_names = rng.choices(actions, weights=weights, k=len(active_agents))

        for agent_inst, action_name in zip(active_agents, sampled_names):
            spec = spec_by_name.get(action_name)
            histogram[action_name] = histogram.get(action_name, 0) + 1
            bucket = histogram_by_sub_pop.setdefault(agent_inst.sub_pop_id, {})
            bucket[action_name] = bucket.get(action_name, 0) + 1
            if spec is None:
                agent_specs.append((agent_inst, {"side": "none", "pool": "none", "fraction": 0.0}))
            else:
                agent_specs.append((agent_inst, dict(spec)))

    # ── Estimate intended buy/sell volume for fill-rate computation ──
    intended_buy_volume = 0.0
    intended_sell_volume = 0.0
    for agent_inst, spec in agent_specs:
        spec_side = spec.get("side", "none")
        spec_pool = spec.get("pool", "none")
        try:
            spec_frac = float(spec.get("fraction", 0.0))
        except (TypeError, ValueError):
            spec_frac = 0.0
        if spec_side == "none" or spec_frac <= 0:
            continue
        if spec_side == "buy":
            if spec_pool == "cash" and agent_inst.cash > 0:
                intended_buy_volume += agent_inst.cash * spec_frac
        elif spec_side == "sell":
            current_sh = agent_inst.holdings.get(instrument, 0.0)
            if spec_pool == "holdings_in_target" and current_sh > 0:
                intended_sell_volume += current_sh * spec_frac * current_price
            elif spec_pool == "margin" and agent_inst.cash > 0:
                intended_sell_volume += agent_inst.cash * spec_frac

    # ── Compute fill rates from limit board ──
    buy_fill_rate, sell_fill_rate, unfilled_buy, unfilled_sell = _compute_fill_rate(
        limit_board, intended_buy_volume, intended_sell_volume,
    )
    total_unfilled = unfilled_buy + unfilled_sell

    # Weighted average fill rate across sides (for the result summary)
    total_intended = intended_buy_volume + intended_sell_volume
    if total_intended > 0:
        avg_fill_rate = (
            buy_fill_rate * intended_buy_volume + sell_fill_rate * intended_sell_volume
        ) / total_intended
    else:
        avg_fill_rate = 1.0

    # ── Second pass: apply fill-rate-scaled orders ──
    net_flow = 0.0
    t1_blocked = 0
    for agent_inst, spec in agent_specs:
        spec_side = spec.get("side", "none")

        # Apply per-side fill rate by scaling the fraction
        if spec_side == "buy" and buy_fill_rate < 1.0:
            try:
                orig_frac = float(spec.get("fraction", 0.0))
            except (TypeError, ValueError):
                orig_frac = 0.0
            spec = {**spec, "fraction": orig_frac * buy_fill_rate}
        elif spec_side == "sell" and sell_fill_rate < 1.0:
            try:
                orig_frac = float(spec.get("fraction", 0.0))
            except (TypeError, ValueError):
                orig_frac = 0.0
            spec = {**spec, "fraction": orig_frac * sell_fill_rate}

        # Track T+1 blocked sells: check before apply_action mutates state
        if spec_side == "sell" and t1_ledger is not None:
            agent_id = agent_inst.agent_id or agent_inst.persona_id
            current_sh = agent_inst.holdings.get(instrument, 0.0)
            sellable = t1_ledger.sellable_shares(
                agent_id, instrument, round_idx, total_holdings=current_sh,
            )
            if current_sh > 0 and sellable <= 0:
                t1_blocked += 1

        order = apply_action(
            agent_inst, spec, current_price,
            instrument=instrument, round_idx=round_idx, t1_ledger=t1_ledger,
        )
        net_flow += order

    return ClassFlowResult(
        persona_id=persona.id,
        instrument=instrument,
        net_flow=net_flow,
        action_histogram=histogram,
        n_agents=len(active_agents),
        rationale=sanitize_text(rationale),
        raw_distribution=raw_distribution or dict(distribution),
        normalized_distribution=normalized,
        normalization_warning=warning,
        confidence=max(0.0, min(1.0, confidence)),
        system_fingerprint=system_fingerprint,
        fill_rate=avg_fill_rate,
        unfilled_volume=total_unfilled,
        t1_blocked_sells=t1_blocked,
        action_histogram_by_sub_pop=histogram_by_sub_pop,
    )


__all__ = [
    "Agent",
    "ClassFlowResult",
    "NORMALIZATION_TOLERANCE",
    "TraderInstance",
    "_DEFAULT_TICKER",
    "apply_action",
    "apply_distribution_to_agent_pop",
    "normalize_action_distribution",
    "spawn_agents",
]
