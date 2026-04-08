"""Agent-based market sandbox for ssFish.

The sandbox replaces the sentiment-aggregation heuristic with an actual
market microstructure model. Each persona class spawns N stochastic agent
instances at sim time. Each agent has real capital and real holdings (sampled
from the persona's distribution). The LLM is called once per persona class
per round to produce an action distribution. The sandbox samples N agents
from that distribution, aggregates their orders into a net flow, and applies
the square-root price impact formula (Kyle 1985 / Almgren-Chriss 2001) to
derive a price update. The new price feeds into round R+1.

This module owns the pure-Python pieces:
    - compute_price_impact()       — Kyle square-root formula
    - chat_action_distribution()   — LLM wrapper (calls llm_client.chat_json)
    - Agent / spawn_agents()       — agent instance management (B1a, next)
    - OrderBook                    — net flow aggregation (B1b, next)

The orchestration loop (run_sandbox_simulation) lives in simulation.py.

Design decisions locked in plan §9 + persona-pack-spec-v1.md §9:
    - Gotcha 1: action_space items are dicts, not bare strings
    - Gotcha 2: LLM action distributions are normalized (clip + rescale)
    - Gotcha 5: Kyle λ applies once per round to net total flow, not per-class
    - Q9: synchronous parallel rounds (all classes see same current_price)
    - Q7: single ticker only (no cross-asset spillover)
    - λ_ashare = 0.5 from Lillo et al. 2003 / Bouchaud 2010, B7 deferred
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any

from .llm_client import JsonChatResult, chat_json
from .persona import Persona, SandboxConfig


log = logging.getLogger(__name__)


# ─────────────────────── Constants ───────────────────────

# Market-impact coefficients from the literature. These are the Phase B
# default values (B7 deferred — calibration from 30 historical events is a
# separate research project). Override per-call by passing `lambda_market`
# explicitly to compute_price_impact.
LAMBDA_LITERATURE = {
    "ashare": 0.5,        # Bouchaud 2010, Chinese A-share approximations
    "us-equity": 0.3,     # Almgren et al., US large-cap
    "crude-oil-wti": 0.4, # Petroleum derivatives literature
    "default": 0.5,
}

# Tolerance for action_distribution normalization. If the LLM returns a
# distribution that sums to within this band of 1.0, we accept it silently.
# Outside this band, we still normalize but log a warning.
NORMALIZATION_TOLERANCE = 0.05


# ─────────────────────── Pure functions ───────────────────────


def compute_price_impact(
    net_flow_cny: float,
    adv_cny: float,
    lambda_market: float = LAMBDA_LITERATURE["default"],
) -> float:
    """Square-root market impact (Kyle 1985 / Almgren-Chriss 2001).

        ΔP/P = λ × sign(net_flow) × sqrt(|net_flow| / ADV)

    Args:
        net_flow_cny: Net order flow this round in CNY (positive = net buy,
            negative = net sell). Already aggregated across all persona classes
            per Gotcha 5 (single λ application to the summed flow, not per-class).
        adv_cny: Average daily volume in CNY for the event ticker. Trailing
            30 days is the conventional window. Must be > 0.
        lambda_market: Market impact coefficient (dimensionless). See
            LAMBDA_LITERATURE for default values per market.

    Returns:
        Fractional price change as a float (e.g., -0.097 means -9.7%).
        Returns 0.0 if net_flow_cny is exactly 0.

    Raises:
        ValueError: if adv_cny <= 0.

    Example (spec §9.3 BYD case):
        >>> compute_price_impact(net_flow_cny=-3e8, adv_cny=8e9, lambda_market=0.5)
        -0.0968...  # ≈ -9.7%
    """
    if adv_cny <= 0:
        raise ValueError(f"adv_cny must be > 0, got {adv_cny}")
    if net_flow_cny == 0:
        return 0.0

    sign = 1.0 if net_flow_cny > 0 else -1.0
    magnitude = math.sqrt(abs(net_flow_cny) / adv_cny)
    return lambda_market * sign * magnitude


def normalize_action_distribution(
    raw_distribution: dict[str, float],
    expected_actions: list[str],
) -> tuple[dict[str, float], str | None]:
    """Clip negatives, drop unknown actions, normalize to sum=1.0.

    The LLM is asked to return a probability distribution over `expected_actions`,
    but in practice it will:
        - sometimes return values that don't sum to exactly 1.0 (off by 0.01-0.05)
        - sometimes emit small negative numbers (LLM thinks they're "near zero")
        - occasionally hallucinate action names not in expected_actions

    This function fixes all three deterministically.

    Returns:
        (normalized_distribution, warning_message_or_None). The warning is
        non-None if the original distribution sum was outside the tolerance band
        OR if any unknown actions were dropped.
    """
    expected_set = set(expected_actions)
    warnings: list[str] = []

    # Drop unknown actions
    unknown = set(raw_distribution.keys()) - expected_set
    if unknown:
        warnings.append(f"unknown actions dropped: {sorted(unknown)}")
    cleaned = {k: v for k, v in raw_distribution.items() if k in expected_set}

    # Clip negatives to 0
    negatives = {k: v for k, v in cleaned.items() if v < 0}
    if negatives:
        warnings.append(
            f"negative probabilities clipped to 0: "
            f"{ {k: round(v, 4) for k, v in negatives.items()} }"
        )
    cleaned = {k: max(0.0, v) for k, v in cleaned.items()}

    # Fill missing expected actions with 0
    for action in expected_actions:
        cleaned.setdefault(action, 0.0)

    # Normalize
    total = sum(cleaned.values())
    if total <= 0:
        # Pathological: LLM returned all zeros / negatives / unknown.
        # Default to uniform distribution.
        warnings.append("distribution sum was zero, defaulting to uniform")
        n = len(expected_actions)
        normalized = {action: 1.0 / n for action in expected_actions}
    else:
        if abs(total - 1.0) > NORMALIZATION_TOLERANCE:
            warnings.append(f"sum was {total:.4f}, renormalized to 1.0")
        normalized = {k: v / total for k, v in cleaned.items()}

    warning_msg = "; ".join(warnings) if warnings else None
    return normalized, warning_msg


# ─────────────────────── LLM wrapper ───────────────────────


@dataclass
class ActionDistributionResult:
    """Structured result of a chat_action_distribution call.

    Mirrors JsonChatResult's metadata fields so callers can persist
    reproducibility info to the scorecard.
    """

    persona_id: str
    action_distribution: dict[str, float]   # normalized to sum=1.0
    rationale: str
    confidence: float
    model: str
    system_fingerprint: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_distribution: dict[str, float] = field(default_factory=dict)
    normalization_warning: str | None = None
    strategic_signal: dict[str, Any] | None = None  # only for strategic personas


class ActionDistributionParseError(ValueError):
    """Raised when the LLM response cannot be coerced into a valid
    ActionDistributionResult after retry-and-fallback."""


def _coerce_action_distribution_response(
    parsed: Any,
    persona_id: str,
    expected_actions: list[str],
    expects_strategic_signal: bool,
) -> tuple[dict[str, float], str, float, dict[str, Any] | None]:
    """Pull (action_distribution, rationale, confidence, strategic_signal) out
    of a parsed JSON response. Tolerant to missing fields with sensible defaults.
    Raises ActionDistributionParseError if action_distribution is unrecoverable.
    """
    if not isinstance(parsed, dict):
        raise ActionDistributionParseError(
            f"persona '{persona_id}': LLM returned {type(parsed).__name__}, expected dict"
        )

    raw_dist = parsed.get("action_distribution")
    if not isinstance(raw_dist, dict):
        # Some LLMs flatten the dict — treat top-level keys matching expected
        # actions as the distribution
        flat_match = {k: v for k, v in parsed.items() if k in expected_actions}
        if flat_match:
            raw_dist = flat_match
        else:
            raise ActionDistributionParseError(
                f"persona '{persona_id}': LLM response missing 'action_distribution' "
                f"and no expected action names found at top level. Got keys: "
                f"{sorted(parsed.keys())}"
            )

    # Coerce values to floats
    coerced_dist: dict[str, float] = {}
    for k, v in raw_dist.items():
        try:
            coerced_dist[str(k)] = float(v)
        except (TypeError, ValueError):
            log.warning(
                "persona %s: action %r had non-numeric probability %r, treating as 0",
                persona_id, k, v,
            )
            coerced_dist[str(k)] = 0.0

    rationale = str(parsed.get("rationale", "")).strip()
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    strategic_signal: dict[str, Any] | None = None
    if expects_strategic_signal:
        ss = parsed.get("strategic_signal")
        if isinstance(ss, dict):
            strategic_signal = dict(ss)

    return coerced_dist, rationale, confidence, strategic_signal


async def chat_action_distribution(
    messages: list[dict[str, str]],
    persona_id: str,
    expected_actions: list[str],
    model: str | None = None,
    seed: int | None = None,
    max_tokens: int = 800,
    expects_strategic_signal: bool = False,
) -> ActionDistributionResult:
    """LLM wrapper for sandbox-mode action distribution queries.

    Calls llm_client.chat_json with the given messages, then:
        1. Coerces the response into (distribution, rationale, confidence)
        2. Normalizes the distribution to sum=1.0 (Gotcha 2 lock-in)
        3. Returns a structured ActionDistributionResult

    Args:
        messages: pre-built chat messages (system + user). Prompt construction
            is the simulation orchestrator's job, not this layer's.
        persona_id: stable persona class identifier (used for logging + result.persona_id)
        expected_actions: the list of action names this persona class can take.
            The LLM's distribution will be filtered to these names and normalized.
        model: LLM model name (None falls back to settings.default_model)
        seed: deterministic seed (passed through to llm_client.chat_json)
        max_tokens: cap on response length. Default 800 is enough for ~6 actions
            + ~120 token rationale.
        expects_strategic_signal: if True, also parses a `strategic_signal` field
            from the response (for strategic personas like 大股东 / 国家队)

    Raises:
        ActionDistributionParseError: if the LLM response cannot be coerced
            into a valid distribution after fallback parsing.
    """
    json_result: JsonChatResult = await chat_json(
        messages=messages,
        model=model,
        seed=seed,
        max_tokens=max_tokens,
    )

    raw_dist, rationale, confidence, strategic_signal = (
        _coerce_action_distribution_response(
            json_result.parsed,
            persona_id,
            expected_actions,
            expects_strategic_signal,
        )
    )

    normalized, warning = normalize_action_distribution(raw_dist, expected_actions)
    if warning:
        log.warning("persona %s: action distribution normalization: %s", persona_id, warning)

    return ActionDistributionResult(
        persona_id=persona_id,
        action_distribution=normalized,
        rationale=rationale,
        confidence=confidence,
        model=json_result.model,
        system_fingerprint=json_result.system_fingerprint,
        prompt_tokens=json_result.prompt_tokens,
        completion_tokens=json_result.completion_tokens,
        raw_distribution=raw_dist,
        normalization_warning=warning,
        strategic_signal=strategic_signal,
    )


# ─────────────────────── Agents (B1a) ───────────────────────


@dataclass
class Agent:
    """A single simulated trader instance.

    State is mutable across rounds: holdings_shares mutates when the agent
    buys/sells, cash_cny tracks unspent capital. Holdings VALUE in CNY is
    derived from holdings_shares × current_price (computed on demand).

    capital_cny is the immutable initial NAV at spawn time. The agent's
    nav_at(price) method returns the current NAV given an external price.
    """

    persona_id: str
    capital_cny: float                # immutable: NAV at spawn time
    cash_cny: float                   # mutable: shrinks on buy, grows on sell
    holdings_shares: float            # mutable: in shares of the target ticker

    def holdings_value_cny(self, current_price: float) -> float:
        return self.holdings_shares * current_price

    def nav_at(self, current_price: float) -> float:
        return self.cash_cny + self.holdings_value_cny(current_price)

    def pnl_at(self, current_price: float) -> float:
        return self.nav_at(current_price) - self.capital_cny


def _sample_capital(spec: dict[str, Any], rng: random.Random) -> float:
    """Sample a single capital value from a distribution spec.

    Supported types:
        - lognormal: {type: lognormal, median_cny, sigma, floor_cny?, ceiling_cny?}
        - uniform:   {type: uniform, min_cny, max_cny}
        - fixed:     {type: fixed, value_cny}
    """
    dtype = spec.get("type", "fixed")
    if dtype == "lognormal":
        median = float(spec["median_cny"])
        sigma = float(spec["sigma"])
        if median <= 0 or sigma <= 0:
            raise ValueError(
                f"lognormal median_cny and sigma must be > 0, got {median}, {sigma}"
            )
        # Median of lognormal(mu, sigma) is e^mu, so mu = ln(median)
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
    """Sample initial position as a fraction of capital. Returns 0.0 if not holding.

    Supported types:
        - bernoulli: {type: bernoulli, prob_holding,
                      position_size_pct_when_holding: {type: uniform, min, max}}
        - fixed:     {type: fixed, value: 0.0-1.0}
        - none:      missing or {type: none} → always 0.0 (no holdings)
    """
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


def spawn_agents(
    persona: Persona,
    current_price: float,
    rng: random.Random,
) -> list[Agent]:
    """Sample N agent instances from the persona's sandbox distributions.

    Each agent gets:
        - capital_cny sampled from sandbox.capital_distribution
        - cash_cny + holdings_shares derived from
          sandbox.initial_position_distribution and current_price

    The same `rng` instance, seeded identically, will produce identical
    output across runs (reproducibility for the scorecard).

    Raises:
        ValueError: if persona.sandbox is None or current_price <= 0.
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"persona '{persona.id}' has no sandbox config; "
            f"cannot spawn agents for sandbox-mode simulation"
        )
    if current_price <= 0:
        raise ValueError(f"current_price must be > 0, got {current_price}")
    if sandbox.instance_count <= 0:
        raise ValueError(
            f"persona '{persona.id}' sandbox.instance_count must be > 0, "
            f"got {sandbox.instance_count}"
        )

    agents: list[Agent] = []
    for _ in range(sandbox.instance_count):
        capital = _sample_capital(sandbox.capital_distribution, rng)
        position_pct = _sample_position_pct(sandbox.initial_position_distribution, rng)
        position_pct = max(0.0, min(1.0, position_pct))  # clamp to [0,1]
        holdings_value_cny = capital * position_pct
        holdings_shares = holdings_value_cny / current_price
        cash_cny = capital - holdings_value_cny
        agents.append(
            Agent(
                persona_id=persona.id,
                capital_cny=capital,
                cash_cny=cash_cny,
                holdings_shares=holdings_shares,
            )
        )
    return agents


# ─────────────────────── OrderBook (B1b) ───────────────────────


def apply_action_to_agent(
    agent: Agent,
    action_spec: dict[str, Any],
    current_price: float,
) -> float:
    """Apply one action to one agent. Mutates agent state.

    Returns the order amount in CNY (positive = buy, negative = sell).

    action_spec shape (Gotcha 1 lock-in: dicts not strings):
        {name: str, side: 'none'|'buy'|'sell', pool: 'none'|'cash'|'holdings_in_target',
         fraction: 0.0-1.0}
    """
    side = action_spec.get("side", "none")
    pool = action_spec.get("pool", "none")
    try:
        fraction = float(action_spec.get("fraction", 0.0))
    except (TypeError, ValueError):
        fraction = 0.0

    if side == "none" or fraction <= 0:
        return 0.0

    if pool == "holdings_in_target":
        if agent.holdings_shares <= 0:
            return 0.0
        shares_to_act = agent.holdings_shares * fraction
        cny_amount = shares_to_act * current_price
    elif pool == "cash":
        if agent.cash_cny <= 0:
            return 0.0
        cny_amount = agent.cash_cny * fraction
        shares_to_act = cny_amount / current_price
    else:
        return 0.0

    if side == "sell":
        agent.holdings_shares -= shares_to_act
        agent.cash_cny += cny_amount
        return -cny_amount
    if side == "buy":
        agent.holdings_shares += shares_to_act
        agent.cash_cny -= cny_amount
        return +cny_amount
    return 0.0


def sample_actions(
    distribution: dict[str, float],
    n: int,
    rng: random.Random,
) -> list[str]:
    """Sample n action names from a probability distribution.

    The distribution is assumed to be normalized (sum to 1.0). Use
    normalize_action_distribution() upstream if you got it from an LLM.
    """
    if not distribution:
        raise ValueError("cannot sample from empty distribution")
    if n <= 0:
        return []
    actions = list(distribution.keys())
    weights = list(distribution.values())
    return rng.choices(actions, weights=weights, k=n)


@dataclass
class ClassFlowResult:
    """Aggregated order flow for one persona class in one round."""

    persona_id: str
    net_flow_cny: float
    action_histogram: dict[str, int]
    n_agents: int
    rationale: str = ""
    strategic_signal: dict[str, Any] | None = None


def aggregate_class_flow(
    persona_id: str,
    agents: list[Agent],
    action_distribution: dict[str, float],
    action_specs: list[dict[str, Any]],
    current_price: float,
    rng: random.Random,
    rationale: str = "",
    strategic_signal: dict[str, Any] | None = None,
) -> ClassFlowResult:
    """Sample one action per agent, apply each, return the net CNY flow.

    Mutates agent state in place. Each agent gets exactly one action drawn
    from `action_distribution`. The flow is summed across all agents in
    this class. Per Gotcha 5, the SUMMED total flow (across all classes)
    feeds into compute_price_impact() — not per-class flows individually.
    """
    spec_by_name = {s["name"]: s for s in action_specs}
    sampled_actions = sample_actions(action_distribution, len(agents), rng)

    net_flow = 0.0
    histogram: dict[str, int] = {}
    for agent, action_name in zip(agents, sampled_actions):
        spec = spec_by_name.get(action_name)
        if spec is None:
            # Action sampled but no spec found — skip silently (this shouldn't
            # happen if the upstream normalize_action_distribution dropped
            # unknown names, but defend in depth)
            histogram[action_name] = histogram.get(action_name, 0) + 1
            continue
        order = apply_action_to_agent(agent, spec, current_price)
        net_flow += order
        histogram[action_name] = histogram.get(action_name, 0) + 1

    return ClassFlowResult(
        persona_id=persona_id,
        net_flow_cny=net_flow,
        action_histogram=histogram,
        n_agents=len(agents),
        rationale=rationale,
        strategic_signal=strategic_signal,
    )


def update_holdings_for_price(agents: list[Agent], price_delta_pct: float) -> None:
    """No-op: agent holdings track shares (not value), so no update needed
    when price moves. This function exists as documentation of the design
    choice — call it after each round if you want to be explicit, but it's
    a noop.

    Why: holdings_value_cny is derived from holdings_shares × current_price.
    A price move changes the derived value but not the underlying shares.
    Cash is also unaffected by price moves (only by buy/sell actions).

    The only state that DOES change between rounds is what the agent did
    in apply_action_to_agent() — that's where mutation happens.
    """
    return


__all__ = [
    "Agent",
    "ClassFlowResult",
    "LAMBDA_LITERATURE",
    "NORMALIZATION_TOLERANCE",
    "ActionDistributionParseError",
    "ActionDistributionResult",
    "aggregate_class_flow",
    "apply_action_to_agent",
    "chat_action_distribution",
    "compute_price_impact",
    "normalize_action_distribution",
    "sample_actions",
    "spawn_agents",
    "update_holdings_for_price",
]
