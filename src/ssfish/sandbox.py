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

import asyncio
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .event import Event
from .llm_client import BudgetExceeded, JsonChatResult, chat_json, cost_tracker
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

# Per-round price impact cap. Models A-share daily 涨停/跌停 limit (±10%).
# Without this cap, the unbounded Kyle formula produces nonsense at very
# large net flows (e.g., ¥3000億 flow into a stock with ¥80億 ADV would
# yield ΔP = +325% per round, which is physically impossible). The cap is
# applied AFTER the Kyle formula computes the raw delta, so the formula's
# sign + magnitude ranking is preserved up to the cap.
MAX_DELTA_PCT_PER_ROUND = 0.10


# ─────────────────────── Pure functions ───────────────────────


def compute_price_impact(
    net_flow_value: float,
    adv_value: float,
    lambda_market: float = LAMBDA_LITERATURE["default"],
    max_delta_pct: float = MAX_DELTA_PCT_PER_ROUND,
) -> float:
    """Square-root market impact (Kyle 1985 / Almgren-Chriss 2001).

        ΔP/P = clip(λ × sign(net_flow) × sqrt(|net_flow| / ADV), ±max_delta_pct)

    The result is clipped to ±max_delta_pct (default ±10%, modeling A-share
    daily 涨停板 limit; override for markets with different rules). Without
    the cap, unbounded Kyle output produces nonsense at extreme net flows.

    Currency-agnostic: net_flow_value and adv_value must be in the SAME unit
    (CNY for A-share, USD for US equity, USD for crude oil futures, etc.).

    Args:
        net_flow_value: Net order flow this round (positive = net buy,
            negative = net sell). Already aggregated across all persona classes
            per Gotcha 5 (single λ application to the summed flow, not per-class).
        adv_value: Average daily volume in the same currency as net_flow.
            Trailing 30 days is the conventional window. Must be > 0.
        lambda_market: Market impact coefficient (dimensionless). See
            LAMBDA_LITERATURE for default values per market.
        max_delta_pct: Per-round absolute price-change cap (default ±0.10).

    Returns:
        Fractional price change as a float (e.g., -0.097 means -9.7%).
        Returns 0.0 if net_flow_value is exactly 0.

    Raises:
        ValueError: if adv_value <= 0.

    Example (spec §9.3 BYD case):
        >>> compute_price_impact(net_flow_value=-3e8, adv_value=8e9, lambda_market=0.5)
        -0.0968...  # ≈ -9.7% (within the ±10% cap)
    """
    if adv_value <= 0:
        raise ValueError(f"adv_value must be > 0, got {adv_value}")
    if net_flow_value == 0:
        return 0.0

    sign = 1.0 if net_flow_value > 0 else -1.0
    magnitude = math.sqrt(abs(net_flow_value) / adv_value)
    raw = lambda_market * sign * magnitude
    return max(-max_delta_pct, min(max_delta_pct, raw))


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

    capital_cny is the immutable initial NAV at spawn time.
    max_holdings_value_cny is the immutable hard cap on this agent's
    exposure to the target ticker (= capital × persona.sandbox.risk.max_position_pct).
    Buy actions are automatically clamped to respect this cap — this is
    how we model the constraint that an institutional fund won't put 100%
    of its AUM into one stock even though it has the cash to do so.
    """

    persona_id: str
    capital_cny: float                # immutable: NAV at spawn time
    cash_cny: float                   # mutable: shrinks on buy, grows on sell
    holdings_shares: float            # mutable: in shares of the target ticker
    max_holdings_value_cny: float     # immutable: capital × max_position_pct

    def holdings_value_cny(self, current_price: float) -> float:
        return self.holdings_shares * current_price

    def nav_at(self, current_price: float) -> float:
        return self.cash_cny + self.holdings_value_cny(current_price)

    def pnl_at(self, current_price: float) -> float:
        return self.nav_at(current_price) - self.capital_cny

    def buy_headroom_cny(self, current_price: float) -> float:
        """How much MORE the agent can put into the target stock given the
        max_position_pct cap. Returns 0 if already at or above the cap.
        """
        return max(0.0, self.max_holdings_value_cny - self.holdings_value_cny(current_price))


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
        - max_holdings_value_cny = capital × sandbox.risk.max_position_pct

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

    max_position_pct = float(sandbox.risk.get("max_position_pct", 1.0))
    max_position_pct = max(0.0, min(1.0, max_position_pct))

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
                max_holdings_value_cny=capital * max_position_pct,
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
        # Enforce max_holdings_value_cny: cap the buy at the agent's headroom.
        # This is how we model "an institutional fund can't put 100% of AUM
        # in one stock" — even though it has the cash, max_position_pct
        # limits its exposure.
        headroom = agent.buy_headroom_cny(current_price)
        if headroom <= 0:
            return 0.0
        if cny_amount > headroom:
            cny_amount = headroom
            shares_to_act = cny_amount / current_price
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


# ─────────────────────── Prompt builders (B3) ───────────────────────


def _format_biases(biases: dict[str, float]) -> str:
    if not biases:
        return "  (none specified)"
    return "\n".join(f"  - {k}: {v}" for k, v in biases.items())


def _format_action_space(action_space: list[dict[str, Any]]) -> str:
    lines = []
    for a in action_space:
        side = a.get("side", "none")
        pool = a.get("pool", "none")
        fraction = a.get("fraction", 0)
        lines.append(
            f"  - {a['name']}: side={side}, pool={pool}, fraction={fraction}"
        )
    return "\n".join(lines)


def _build_sandbox_system_prompt(persona: Persona) -> str:
    """Build the system message for sandbox-mode action distribution queries.

    The persona is asked to output a probability distribution over its
    `sandbox.action_space`, NOT a sentiment score. Strategic personas
    (decision_mode=strategic) are also asked to emit a `strategic_signal`
    field that gets routed to the report's separate "战略层信号" section.
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"persona '{persona.id}' has no sandbox config; "
            f"cannot build sandbox prompt"
        )

    action_descriptions = _format_action_space(sandbox.action_space)
    bias_block = _format_biases(persona.biases)
    expected_actions_json = ", ".join(f'"{a["name"]}": <0-1>' for a in sandbox.action_space)

    is_strategic = persona.decision_mode == "strategic"
    strategic_field = ""
    if is_strategic:
        strategic_field = (
            ',\n'
            '  "strategic_signal": {\n'
            '    "direction": "reduce|neutral|accumulate|defensive",\n'
            '    "magnitude": "low|medium|high",\n'
            '    "time_horizon_days": <integer>\n'
            '  }'
        )

    return (
        f"你代表 A 股市场中的一类参与者: {persona.archetype} ({persona.id})\n"
        f"画像: {persona.display_name}\n"
        f"\n"
        f"# 你的语气和风格\n"
        f"{persona.voice_prompt}\n"
        f"\n"
        f"# 你的行为偏差\n"
        f"{bias_block}\n"
        f"\n"
        f"# 这一类参与者的特征\n"
        f"  - 决策模式: {persona.decision_mode}\n"
        f"  - 角色: {persona.role}\n"
        f"  - 市场份额 (by_volume): {persona.market_share.by_volume if persona.market_share else 'unknown'}\n"
        f"\n"
        f"# 你这一类的可选动作\n"
        f"{action_descriptions}\n"
        f"\n"
        f"# 任务\n"
        f"给定下面的事件和当前市场状态, 输出你这一类参与者的动作概率分布.\n"
        f"概率应该反映你这一类的真实分歧——不需要全部押注同一个动作.\n"
        f"\n"
        f"必须返回 JSON 格式 (所有概率加起来约等于 1.0):\n"
        f'{{\n'
        f'  "action_distribution": {{{expected_actions_json}}},\n'
        f'  "rationale": "用 50-150 字解释这一类参与者会怎么想",\n'
        f'  "confidence": 0.0-1.0{strategic_field}\n'
        f'}}\n'
        f"\n"
        f"# 重要规则\n"
        f"  1. 严格按 JSON 格式输出, 不要任何额外文字\n"
        f"  2. action_distribution 的 keys 必须只包含上面列出的动作名\n"
        f"  3. 不要输出投资建议. 你描述的是这一类人会**做什么**, 不是建议读者做什么\n"
        f"  4. 你的角色是这一类的代表, 不是单一个体\n"
    )


def _currency_format(currency: str) -> tuple[str, str, float]:
    """Pick (symbol, big_unit, big_divisor) for a currency code.

    Mirrors report.CURRENCY_FORMATS but kept here to avoid circular imports.
    """
    table = {
        "CNY": ("¥",   "億", 1e8),
        "USD": ("$",   "B",  1e9),
        "EUR": ("€",   "B",  1e9),
        "JPY": ("¥",   "B",  1e9),
        "HKD": ("HK$", "B",  1e9),
        "BTC": ("₿",   "K",  1e3),
    }
    return table.get(currency, table["USD"])


def _build_sandbox_round_zero_user_message(
    event: Event,
    current_price: float,
    adv_value: float,
) -> str:
    sym, big_unit, big_div = _currency_format(event.price_currency)
    return (
        f"# Event\n"
        f"{event.to_simulation_input()}\n"
        f"\n"
        f"# Current market state\n"
        f"  - Current price: {sym}{current_price:.2f}\n"
        f"  - Average daily volume (ADV): {sym}{adv_value / big_div:.1f}{big_unit}\n"
        f"  - This is t=0 (event just hit, no price reaction yet)\n"
        f"\n"
        f"Output your class's action_distribution + rationale + confidence."
    )


def _build_sandbox_round_n_user_message(
    event: Event,
    current_price: float,
    adv_value: float,
    initial_price: float,
    round_idx: int,
    history: list["SandboxRoundRecord"],
) -> str:
    sym, big_unit, big_div = _currency_format(event.price_currency)
    history_lines = []
    for r in history:
        history_lines.append(
            f"  R{r.round_idx}: {sym}{r.price_before:.2f} → {sym}{r.price_after:.2f} "
            f"(Δ {r.delta_pct * 100:+.2f}%, net flow {sym}{r.net_flow_cny / big_div:+.2f}{big_unit})"
        )
    history_block = "\n".join(history_lines) if history_lines else "  (no prior rounds)"
    cumulative_delta = (current_price / initial_price - 1.0) * 100 if initial_price else 0.0

    return (
        f"# Event\n"
        f"{event.to_simulation_input()}\n"
        f"\n"
        f"# Round history (what you've already seen happen)\n"
        f"{history_block}\n"
        f"\n"
        f"# Current market state (R{round_idx})\n"
        f"  - Current price: {sym}{current_price:.2f}\n"
        f"  - Cumulative change since R0: {cumulative_delta:+.2f}%\n"
        f"  - ADV: {sym}{adv_value / big_div:.1f}{big_unit}\n"
        f"\n"
        f"Considering the prior rounds, output your class's action_distribution for this round.\n"
        f"Holders who've seen {cumulative_delta:+.2f}% cumulative move may feel differently now.\n"
    )


# ─────────────────────── Round + Result dataclasses ───────────────────────


@dataclass
class SandboxRoundRecord:
    """Snapshot of one sandbox round.

    `class_flows` is keyed by persona_id and carries the per-class flow result
    (net_flow + action histogram + LLM rationale + optional strategic signal).
    `fingerprints` is parallel to the persona list and stores each LLM call's
    system_fingerprint for reproducibility tracking in the scorecard.
    """

    round_idx: int
    price_before: float
    price_after: float
    delta_pct: float
    net_flow_cny: float
    class_flows: dict[str, ClassFlowResult]
    fingerprints: list[str | None] = field(default_factory=list)


@dataclass
class SandboxResult:
    """Top-level result of a sandbox-mode simulation."""

    simulation_id: str
    event: Event
    personas: list[Persona]
    initial_price: float
    final_price: float
    rounds: list[SandboxRoundRecord]
    elapsed_seconds: float
    cost_usd_at_start: float
    cost_usd_at_end: float
    llm_seed: int | None
    lambda_used: float
    adv_value_used: float
    final_agents_by_class: dict[str, list[Agent]] = field(default_factory=dict)

    @property
    def n_personas(self) -> int:
        return len(self.personas)

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def cost_usd(self) -> float:
        return max(0.0, self.cost_usd_at_end - self.cost_usd_at_start)

    @property
    def price_trajectory(self) -> list[float]:
        if not self.rounds:
            return [self.initial_price]
        return [self.initial_price] + [r.price_after for r in self.rounds]

    def compute_class_pnl(self) -> dict[str, float]:
        return {
            class_id: sum(a.pnl_at(self.final_price) for a in agents)
            for class_id, agents in self.final_agents_by_class.items()
        }

    @property
    def cumulative_delta_pct(self) -> float:
        if not self.initial_price:
            return 0.0
        return (self.final_price / self.initial_price - 1.0)


# ─────────────────────── Main orchestration loop (B3) ───────────────────────


async def _query_class_action_distribution(
    persona: Persona,
    event: Event,
    current_price: float,
    adv_value: float,
    round_idx: int,
    history: list[SandboxRoundRecord],
    initial_price: float,
    seed: int | None,
) -> ActionDistributionResult:
    """Build messages + call chat_action_distribution for one persona class.

    Round 0 uses the no-history prompt, round R>=1 uses the with-history prompt.
    """
    sandbox = persona.sandbox
    if sandbox is None:
        raise ValueError(
            f"persona '{persona.id}' has no sandbox config; "
            f"cannot run sandbox-mode simulation"
        )

    system_msg = _build_sandbox_system_prompt(persona)
    if round_idx == 0:
        user_msg = _build_sandbox_round_zero_user_message(event, current_price, adv_value)
    else:
        user_msg = _build_sandbox_round_n_user_message(
            event, current_price, adv_value, initial_price, round_idx, history
        )

    expected_actions = [a["name"] for a in sandbox.action_space]
    expects_strategic = bool(persona.contributes_to_strategic_signal)

    return await chat_action_distribution(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        persona_id=persona.id,
        expected_actions=expected_actions,
        model=persona.model,
        seed=seed,
        expects_strategic_signal=expects_strategic,
    )


async def run_sandbox_simulation(
    event: Event,
    personas: list[Persona],
    n_rounds: int | None = None,
    *,
    simulation_id: str | None = None,
    lambda_market: float | None = None,
) -> SandboxResult:
    """Run an agent-based market simulation in sandbox mode.

    For each round R in [0, n_rounds):
        1. Build per-class messages (R0 prompt for first round, Rn for rest)
        2. Call chat_action_distribution for each persona class IN PARALLEL
           (asyncio.gather across classes — Q9 synchronous-parallel rounds)
        3. Sample agents from each class's action distribution and aggregate
           into a per-class net flow (aggregate_class_flow)
        4. Sum all class flows into a single net total flow (Gotcha 5)
        5. Apply Kyle square-root price impact: ΔP/P = λ × sign(flow) × sqrt(|flow|/ADV)
        6. Update price for next round
        7. Record SandboxRoundRecord

    Args:
        event: must have current_price and adv_value set (Event.is_sandbox_ready)
        personas: list of v2 Personas with sandbox config
        n_rounds: number of rounds (default = settings.default_rounds)
        simulation_id: stable id for scorecard tracking (auto-generated if None)
        lambda_market: market impact coefficient (defaults to LAMBDA_LITERATURE['ashare'])

    Returns:
        SandboxResult with the full price trajectory + final agent state.

    Raises:
        ValueError: if event is not sandbox-ready or personas list is empty
        BudgetExceeded: if the LLM cost guard trips mid-run (caller should
            persist the partial result if useful)
    """
    if not personas:
        raise ValueError("run_sandbox_simulation requires at least one persona")
    if not event.is_sandbox_ready:
        raise ValueError(
            f"event {event.ticker} is not sandbox-ready: "
            f"current_price={event.current_price}, adv_value={event.adv_value}. "
            f"Sandbox mode requires both fields."
        )

    n_rounds = n_rounds or settings.n_rounds
    simulation_id = simulation_id or f"sandbox_{uuid.uuid4().hex[:12]}"
    # Default to A-share λ since ssFish's primary market is A-share. Override
    # via the lambda_market kwarg for cross-market sims.
    lambda_used = (
        lambda_market
        if lambda_market is not None
        else LAMBDA_LITERATURE["ashare"]
    )
    seed = settings.seed

    log.info(
        "Starting sandbox simulation: id=%s ticker=%s personas=%d rounds=%d "
        "initial_price=%.2f adv=%.0f lambda=%.3f seed=%s",
        simulation_id,
        event.ticker,
        len(personas),
        n_rounds,
        event.current_price,
        event.adv_value,
        lambda_used,
        seed,
    )

    cost_at_start = cost_tracker.total_cost_usd
    t0 = time.time()

    # Spawn all agents fresh at t=0
    spawn_rng = random.Random(seed if seed is not None else 0)
    agents_by_class: dict[str, list[Agent]] = {}
    for persona in personas:
        agents_by_class[persona.id] = spawn_agents(
            persona, current_price=event.current_price, rng=spawn_rng
        )

    rounds: list[SandboxRoundRecord] = []
    current_price = float(event.current_price)
    initial_price = current_price

    for round_idx in range(n_rounds):
        history_for_this_round = list(rounds)

        # Step 1: query all persona classes IN PARALLEL (Q9)
        # asyncio.gather respects the order of personas → keeps zipping safe
        tasks = [
            _query_class_action_distribution(
                persona=p,
                event=event,
                current_price=current_price,
                adv_value=event.adv_value,
                round_idx=round_idx,
                history=history_for_this_round,
                initial_price=initial_price,
                seed=seed,
            )
            for p in personas
        ]
        try:
            distribution_results: list[ActionDistributionResult] = await asyncio.gather(*tasks)
        except BudgetExceeded:
            log.warning(
                "Sandbox simulation %s hit budget guard at round %d", simulation_id, round_idx
            )
            raise

        # Step 2: aggregate per-class flow
        class_flows: dict[str, ClassFlowResult] = {}
        fingerprints: list[str | None] = []
        flow_rng = random.Random(
            (seed if seed is not None else 0) + 1000 + round_idx
        )

        for persona, dist_result in zip(personas, distribution_results):
            agents = agents_by_class[persona.id]
            class_flow = aggregate_class_flow(
                persona_id=persona.id,
                agents=agents,
                action_distribution=dist_result.action_distribution,
                action_specs=persona.sandbox.action_space,
                current_price=current_price,
                rng=flow_rng,
                rationale=dist_result.rationale,
                strategic_signal=dist_result.strategic_signal,
            )
            class_flows[persona.id] = class_flow
            fingerprints.append(dist_result.system_fingerprint)

        # Step 3: sum total net flow (Gotcha 5: single λ application)
        net_flow_total = sum(cf.net_flow_cny for cf in class_flows.values())

        # Step 4: Kyle price impact
        delta_pct = compute_price_impact(
            net_flow_value=net_flow_total,
            adv_value=event.adv_value,
            lambda_market=lambda_used,
        )
        price_after = current_price * (1.0 + delta_pct)

        rounds.append(
            SandboxRoundRecord(
                round_idx=round_idx,
                price_before=current_price,
                price_after=price_after,
                delta_pct=delta_pct,
                net_flow_cny=net_flow_total,
                class_flows=class_flows,
                fingerprints=fingerprints,
            )
        )

        log.info(
            "  R%d: %.2f → %.2f (%+.2f%%), net_flow=¥%+.2f億",
            round_idx,
            current_price,
            price_after,
            delta_pct * 100,
            net_flow_total / 1e8,
        )

        current_price = price_after

    cost_at_end = cost_tracker.total_cost_usd
    elapsed = time.time() - t0
    final_price = current_price

    log.info(
        "Sandbox sim %s done in %.1fs ($%.4f). Cumulative price delta: %+.2f%%",
        simulation_id,
        elapsed,
        cost_at_end - cost_at_start,
        (final_price / initial_price - 1.0) * 100,
    )

    return SandboxResult(
        simulation_id=simulation_id,
        event=event,
        personas=personas,
        initial_price=initial_price,
        final_price=final_price,
        rounds=rounds,
        elapsed_seconds=elapsed,
        cost_usd_at_start=cost_at_start,
        cost_usd_at_end=cost_at_end,
        llm_seed=seed,
        lambda_used=lambda_used,
        adv_value_used=event.adv_value,
        final_agents_by_class=agents_by_class,
    )


__all__ = [
    "Agent",
    "ClassFlowResult",
    "LAMBDA_LITERATURE",
    "NORMALIZATION_TOLERANCE",
    "ActionDistributionParseError",
    "ActionDistributionResult",
    "SandboxResult",
    "SandboxRoundRecord",
    "aggregate_class_flow",
    "apply_action_to_agent",
    "chat_action_distribution",
    "compute_price_impact",
    "normalize_action_distribution",
    "run_sandbox_simulation",
    "sample_actions",
    "spawn_agents",
    "update_holdings_for_price",
]
