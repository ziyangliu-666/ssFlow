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

from .output_filter import sanitize_text
from .persona import Persona


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
        prices = self._resolve_prices(current_price)
        return sum(
            self.holdings.get(t, 0.0) * p
            for t, p in prices.items()
        )

    def nav(self, current_price: float | dict[str, float]) -> float:
        return self.cash + self.holdings_value(current_price)

    def pnl(self, current_price: float | dict[str, float]) -> float:
        return self.nav(current_price) - self.capital

    def buy_headroom(self, current_price: float | dict[str, float]) -> float:
        """Remaining buy capacity given the persona's max_position_pct cap."""
        return max(0.0, self.max_holdings_value - self.holdings_value(current_price))


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

    max_position_pct = float(sandbox.risk.get("max_position_pct", 1.0))
    max_position_pct = max(0.0, min(1.0, max_position_pct))

    agents: list[Agent] = []
    for _ in range(sandbox.instance_count):
        capital = _sample_capital(sandbox.capital_distribution, rng)
        position_pct = _sample_position_pct(sandbox.initial_position_distribution, rng)
        position_pct = max(0.0, min(1.0, position_pct))
        holdings_value = capital * position_pct
        cash = capital - holdings_value
        reaction_lag = _sample_reaction_lag(sandbox.reaction_lag_rounds, rng)

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
                    max_holdings_value=capital * max_position_pct,
                    reaction_lag=reaction_lag,
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
                    max_holdings_value=capital * max_position_pct,
                    reaction_lag=reaction_lag,
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
) -> float:
    """Apply one action_spec to one agent. Mutates state. Returns order amount.

    Order amount is in event currency, positive for buy, negative for sell,
    zero for hold or insufficient capacity. The buy path is capped by
    `agent.buy_headroom(current_price)` so the persona's max_position_pct
    constraint is enforced.

    instrument: which ticker to trade. Defaults to _DEFAULT_TICKER for
    backward compat with single-instrument simulations.
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
    else:
        return 0.0

    if side == "sell":
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
        agent.holdings[instrument] = current_shares + shares_to_act
        agent.cash -= amount
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


# ─────────────────────── Distribution application (pure math) ───────────────────────


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
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"apply_distribution_to_agent_pop: persona '{persona.id}' has no sandbox"
        )

    # Filter to agents whose reaction_lag has been reached
    active_agents = [a for a in agents if a.reaction_lag <= round_idx]
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

    # ── Free-form path: entire class does the same action ──
    if "__freeform__" in distribution and raw_distribution:
        side = raw_distribution.get("side", "hold")
        qty_pct = float(raw_distribution.get("quantity_pct", 0.0))
        pool = raw_distribution.get("pool", "")

        if side == "hold" or qty_pct <= 0:
            freeform_spec = {"side": "none", "pool": "none", "fraction": 0.0}
        else:
            if not pool:
                pool = "cash" if side == "buy" else "holdings_in_target"
            freeform_spec = {"side": side, "pool": pool, "fraction": qty_pct}

        net_flow = 0.0
        histogram: dict[str, int] = {"__freeform__": len(active_agents)}
        for agent_inst in active_agents:
            order = apply_action(agent_inst, freeform_spec, current_price, instrument=instrument)
            net_flow += order

        return ClassFlowResult(
            persona_id=persona.id,
            instrument=instrument,
            net_flow=net_flow,
            action_histogram=histogram,
            n_agents=len(active_agents),
            rationale=sanitize_text(rationale),
            raw_distribution=raw_distribution or dict(distribution),
            normalized_distribution={"__freeform__": 1.0},
            normalization_warning=None,
            confidence=max(0.0, min(1.0, confidence)),
            system_fingerprint=system_fingerprint,
        )

    # ── Legacy distribution path ──
    expected_names = [a["name"] for a in sandbox.action_space]
    normalized, warning = normalize_action_distribution(distribution, expected_names)
    if warning:
        log.info(
            "persona %s: distribution normalization: %s",
            persona.id, warning,
        )

    spec_by_name = {a["name"]: a for a in sandbox.action_space}
    actions = list(normalized.keys())
    weights = list(normalized.values())
    sampled_names = rng.choices(actions, weights=weights, k=len(active_agents))

    net_flow = 0.0
    histogram: dict[str, int] = {}
    for agent, action_name in zip(active_agents, sampled_names):
        spec = spec_by_name.get(action_name)
        histogram[action_name] = histogram.get(action_name, 0) + 1
        if spec is None:
            continue
        order = apply_action(agent, spec, current_price, instrument=instrument)
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
