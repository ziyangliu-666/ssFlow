"""Policy evaluation engine — compile triggers and evaluate policies.

Replaces entity_engine.py's evaluate_thresholds + collect_forced_actions
+ collect_threshold_events with a single unified pass.

The trigger DSL extends the existing compile_condition format:
  - "margin_utilization > 0.72"      → reads from agent.state
  - "price_change_pct < -5.0"        → reads from agent.state
  - "avg_position_pct > 0.50"        → reads from agent.state (synced from population)

Compound triggers (Phase 5):
  - "cumulative_delta < -0.07 AND round_idx >= 2"
  - "limit_down_days >= 2 OR (delta_pct < -0.05 AND stress_level == 'crisis')"
  - "NOT (national_team_active) AND delta_pct < -0.03"

String comparison:
  - "stress_level == 'crisis'"       → reads from agent.state, compares as string

All triggers evaluate against the owning agent's state dict.
WorldState-scoped triggers (Phase 4) will extend the evaluator to
also read from sim_graph.world.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .policy import (
    Action,
    AnnounceAction,
    MutateAction,
    Policy,
    PolicyFire,
    TradeAction,
)
from .world import SimAgent, SimGraph

log = logging.getLogger(__name__)


# ─────────────────────── Trigger Compilation ───────────────────────

_COMPARISON_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}

_TRIGGER_RE = re.compile(r"^(\w+)\s*(>=|<=|==|>|<)\s*(-?[0-9.]+)$")

# String comparison: var == 'literal'
_STRING_CMP_RE = re.compile(r"^(\w+)\s*==\s*'([^']*)'$")


def compile_trigger(expr: str) -> Callable[[SimAgent], bool]:
    """Compile a trigger expression into a callable.

    Supports simple comparisons:
        variable_name {>, <, >=, <=, ==} float_literal
        variable_name == 'string_literal'

    And compound expressions:
        expr AND expr
        expr OR expr
        NOT (expr)
        Parenthesised grouping: (expr AND expr) OR expr

    The variable is read from agent.state.
    Rejects anything else (no arbitrary code execution).
    """
    stripped = expr.strip()

    # Try compound expression first
    compound = _try_compile_compound(stripped)
    if compound is not None:
        return compound

    # Fall back to simple comparison
    return _compile_simple_trigger(stripped)


def _compile_simple_trigger(expr: str) -> Callable[[SimAgent], bool]:
    """Compile a single atomic comparison expression."""
    expr = expr.strip()

    # Try string comparison first: var == 'literal'
    sm = _STRING_CMP_RE.match(expr)
    if sm:
        var_name = sm.group(1)
        str_val = sm.group(2)
        return lambda agent, _var=var_name, _val=str_val: (
            agent.state.get(_var) == _val
            if isinstance(agent.state.get(_var), str)
            else str(agent.get(_var)) == _val
        )

    # Numeric comparison
    m = _TRIGGER_RE.match(expr)
    if not m:
        raise ValueError(
            f"Cannot compile trigger: {expr!r}. "
            f"Expected format: 'variable_name > 3.0' or compound with AND/OR/NOT"
        )
    var_name = m.group(1)
    op = _COMPARISON_OPS[m.group(2)]
    threshold_val = float(m.group(3))
    return lambda agent, _var=var_name, _op=op, _val=threshold_val: _op(
        agent.get(_var), _val
    )


# ─────────────────── Compound Trigger Parsing ────────────────────


def _try_compile_compound(expr: str) -> Callable[[SimAgent], bool] | None:
    """Try to parse a compound expression. Returns None if not compound."""
    stripped = expr.strip()

    # Split on top-level AND / OR FIRST (respecting parentheses).
    # This must come before NOT handling so that
    # "NOT (x > 0.5) AND y < 0.3" is parsed as AND(NOT(...), y<0.3)
    # rather than NOT("(x > 0.5) AND y < 0.3").
    for connective in ("OR", "AND"):
        parts = _split_on_connective(stripped, connective)
        if parts is not None and len(parts) >= 2:
            fns = [compile_trigger(p) for p in parts]
            if connective == "AND":
                return lambda agent, _fns=fns: all(fn(agent) for fn in _fns)
            else:
                return lambda agent, _fns=fns: any(fn(agent) for fn in _fns)

    # NOT (...) prefix — only reached if no top-level AND/OR found
    if stripped.startswith("NOT "):
        inner = stripped[4:].strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        inner_fn = compile_trigger(inner)
        return lambda agent, _fn=inner_fn: not _fn(agent)

    # Parenthesised single expression: "(expr)"
    if stripped.startswith("(") and stripped.endswith(")"):
        inner = stripped[1:-1].strip()
        # Verify balanced — only unwrap if the outer parens match
        if _parens_balanced(inner):
            return compile_trigger(inner)

    return None


def _split_on_connective(expr: str, connective: str) -> list[str] | None:
    """Split expression on a top-level connective (AND / OR).

    Only splits when the connective is outside all parentheses.
    Returns None if the connective does not appear at the top level.
    """
    token = f" {connective} "
    depth = 0
    positions: list[int] = []

    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and expr[i:i + len(token)] == token:
            positions.append(i)
            i += len(token)
            continue
        i += 1

    if not positions:
        return None

    parts: list[str] = []
    prev = 0
    for pos in positions:
        parts.append(expr[prev:pos].strip())
        prev = pos + len(token)
    parts.append(expr[prev:].strip())

    return [p for p in parts if p]


def _parens_balanced(expr: str) -> bool:
    """Check if parentheses are balanced in an expression."""
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


# ─────────────────── Staged Policy System ────────────────────


@dataclass
class PolicyStage:
    """One escalation stage within a StagedPolicy.

    Attributes:
        level: escalation level (1 = verbal/warning, 2 = operational, 3 = emergency)
        trigger: compound trigger expression
        actions: list of PolicyAction objects to execute at this stage
        description: human-readable description for timeline/logs
    """

    level: int
    trigger: str
    actions: list[Action]
    description: str


@dataclass
class StagedPolicy:
    """A multi-stage escalation policy.

    Each stage has its own trigger and actions. Stages are evaluated in order
    (lowest level first). Once a stage fires, it cannot re-fire until
    cooldown_rounds have passed. Higher stages require lower stages to have
    fired first (or at least ``min_rounds_between_stages`` rounds gap).

    Example: national_team_etf_buy
      Stage 1 (verbal): cumulative_delta < -0.03 → AnnounceAction "汇金表态..."
      Stage 2 (operational): cumulative_delta < -0.05 → TradeAction buy 5%
      Stage 3 (emergency): cumulative_delta < -0.08 → TradeAction buy 15%
    """

    name: str
    stages: list[PolicyStage]
    cooldown_rounds: int = 1
    current_stage: int = 0
    _stage_fired_rounds: dict[int, int] = field(default_factory=dict)

    def evaluate(self, agent: SimAgent, round_idx: int) -> list[Action] | None:
        """Evaluate the staged policy and return actions if a new stage fires.

        Returns the actions for the newly triggered stage, or None if no
        stage fires this round.
        """
        # Check each stage in order from current+1 upward
        for stage in self.stages:
            if stage.level <= self.current_stage:
                continue

            # Enforce cooldown between stage escalations
            if self.current_stage > 0:
                last_fire = self._stage_fired_rounds.get(self.current_stage, -999)
                if round_idx - last_fire < self.cooldown_rounds:
                    break  # Too soon to escalate

            try:
                trigger_fn = compile_trigger(stage.trigger)
                if trigger_fn(agent):
                    self.current_stage = stage.level
                    self._stage_fired_rounds[stage.level] = round_idx
                    log.info(
                        "StagedPolicy '%s' escalated to stage %d at R%d: %s",
                        self.name, stage.level, round_idx, stage.description,
                    )
                    return stage.actions
            except Exception:
                log.warning(
                    "StagedPolicy '%s' stage %d trigger raised, skipping",
                    self.name, stage.level,
                )
                continue

        return None

    def reset(self) -> None:
        """Reset staged policy to initial state."""
        self.current_stage = 0
        self._stage_fired_rounds.clear()


# ─────────────────── A-Share Policy Templates ────────────────────


def create_exchange_inquiry_policy() -> StagedPolicy:
    """Exchange inquiry letter: stock rises >50% in 10 days.

    Dampens retail FOMO by signaling regulatory attention. In real A-share
    markets, the exchange issues a 问询函 asking the company to explain the
    price move, which typically cools speculative trading.

    Stage 1: Verbal warning when cumulative rise > 30%
    Stage 2: Formal inquiry letter when cumulative rise > 50%
    """
    return StagedPolicy(
        name="exchange_inquiry",
        stages=[
            PolicyStage(
                level=1,
                trigger="cumulative_delta >= 0.30",
                actions=[
                    AnnounceAction(
                        text="交易所关注函: 贵公司股价近期涨幅较大，请核实是否存在应披露未披露的重大信息。",
                        content_type="exchange_notice",
                        authority_weight=0.90,
                    ),
                ],
                description="交易所发出关注函 (涨幅>30%)",
            ),
            PolicyStage(
                level=2,
                trigger="cumulative_delta >= 0.50",
                actions=[
                    AnnounceAction(
                        text="交易所问询函: 贵公司股价累计涨幅超过50%，要求公司停牌自查并在5个交易日内回复。",
                        content_type="exchange_notice",
                        authority_weight=0.95,
                    ),
                    MutateAction(mutations={"conviction_damper": 0.5}),
                ],
                description="交易所发出问询函 (涨幅>50%)",
            ),
        ],
        cooldown_rounds=2,
    )


def create_short_sell_restriction_policy() -> StagedPolicy:
    """Short-sell restriction: consecutive limit-down triggers restriction.

    When a stock hits limit-down 2+ consecutive days, the exchange restricts
    short selling to prevent panic cascades. Models the real A-share融券限制.

    Stage 1: Warning after first limit-down
    Stage 2: Restriction after 2 consecutive limit-down days
    """
    return StagedPolicy(
        name="short_sell_restriction",
        stages=[
            PolicyStage(
                level=1,
                trigger="limit_down_days >= 1",
                actions=[
                    AnnounceAction(
                        text="风险提示: 该股票已触及跌停，请投资者注意风险。",
                        content_type="exchange_notice",
                        authority_weight=0.85,
                    ),
                ],
                description="跌停风险提示 (连续跌停>=1日)",
            ),
            PolicyStage(
                level=2,
                trigger="limit_down_days >= 2",
                actions=[
                    AnnounceAction(
                        text="交易所公告: 因连续跌停，即日起暂停该股票融券卖出。",
                        content_type="exchange_notice",
                        authority_weight=0.95,
                    ),
                    MutateAction(mutations={"short_sell_allowed": 0.0}),
                ],
                description="暂停融券 (连续跌停>=2日)",
            ),
        ],
        cooldown_rounds=1,
    )


def create_national_team_etf_buy_policy() -> StagedPolicy:
    """National team ETF buy: staged intervention during market crash.

    Models 汇金/证金's real-world behavior during A-share crashes:
      Stage 1: Verbal support ("充分信心")
      Stage 2: Small ETF buy (5% of capital)
      Stage 3: Large ETF buy (15% of capital)
    """
    return StagedPolicy(
        name="national_team_etf_buy",
        stages=[
            PolicyStage(
                level=1,
                trigger="cumulative_delta < -0.03",
                actions=[
                    AnnounceAction(
                        text="中央汇金: 对A股市场长期投资价值充满信心，将适时增持ETF。",
                        content_type="policy_statement",
                        authority_weight=0.95,
                    ),
                ],
                description="汇金口头表态 (跌幅>3%)",
            ),
            PolicyStage(
                level=2,
                trigger="cumulative_delta < -0.05",
                actions=[
                    AnnounceAction(
                        text="中央汇金: 已于近日增持部分宽基ETF，并将继续增持。",
                        content_type="policy_statement",
                        authority_weight=0.95,
                    ),
                    TradeAction(side="buy", quantity_pct=0.05, pool="cash"),
                ],
                description="汇金小额增持ETF (跌幅>5%)",
            ),
            PolicyStage(
                level=3,
                trigger="cumulative_delta < -0.08",
                actions=[
                    AnnounceAction(
                        text="中央汇金: 坚决维护资本市场稳定运行，已大幅增持宽基ETF和蓝筹股。",
                        content_type="policy_statement",
                        authority_weight=0.98,
                    ),
                    TradeAction(side="buy", quantity_pct=0.15, pool="cash"),
                ],
                description="汇金大额增持 (跌幅>8%)",
            ),
        ],
        cooldown_rounds=2,
    )


def create_suspension_risk_policy() -> StagedPolicy:
    """Suspension risk: ST + continued losses may trigger trading suspension.

    Models the A-share *ST and suspension mechanism. Companies with
    consecutive annual losses get ST-flagged, and further deterioration
    can lead to trading suspension or delisting warning.

    Stage 1: *ST warning when losses continue
    Stage 2: Trading suspension on severe deterioration
    """
    return StagedPolicy(
        name="suspension_risk",
        stages=[
            PolicyStage(
                level=1,
                trigger="st_flagged >= 1.0 AND consecutive_loss_quarters >= 2.0",
                actions=[
                    AnnounceAction(
                        text="深交所公告: 该公司已被实施退市风险警示(*ST)，请投资者注意风险。",
                        content_type="exchange_notice",
                        authority_weight=0.95,
                    ),
                ],
                description="退市风险警示 (ST+连续亏损)",
            ),
            PolicyStage(
                level=2,
                trigger="st_flagged >= 1.0 AND consecutive_loss_quarters >= 4.0",
                actions=[
                    AnnounceAction(
                        text="深交所公告: 因触发退市条件，该股票将于下一交易日起停牌。",
                        content_type="exchange_notice",
                        authority_weight=0.98,
                    ),
                    MutateAction(mutations={"conviction_damper": 0.0}),
                ],
                description="暂停上市 (ST+连续4季度亏损)",
            ),
        ],
        cooldown_rounds=3,
    )


def get_ashare_policy_templates() -> dict[str, StagedPolicy]:
    """Return all pre-built A-share specific policy templates.

    Returns a dict keyed by template name for easy lookup.
    """
    return {
        "exchange_inquiry": create_exchange_inquiry_policy(),
        "short_sell_restriction": create_short_sell_restriction_policy(),
        "national_team_etf_buy": create_national_team_etf_buy_policy(),
        "suspension_risk": create_suspension_risk_policy(),
    }


def evaluate_staged_policies(
    staged_policies: list[tuple[SimAgent, StagedPolicy]],
    round_idx: int,
) -> list[PolicyFire]:
    """Evaluate staged policies and return fires for any newly triggered stages.

    Args:
        staged_policies: list of (agent, staged_policy) tuples
        round_idx: current simulation round

    Returns:
        list of PolicyFire for each action in each newly triggered stage.
    """
    fires: list[PolicyFire] = []
    for agent, sp in staged_policies:
        actions = sp.evaluate(agent, round_idx)
        if actions is None:
            continue
        for action in actions:
            fires.append(PolicyFire(
                agent_id=agent.id,
                agent_display_name=agent.display_name,
                policy=Policy(
                    id=f"staged_{sp.name}_L{sp.current_stage}",
                    name=f"{sp.name} (stage {sp.current_stage})",
                    description=sp.stages[sp.current_stage - 1].description,
                    trigger_expr=sp.stages[sp.current_stage - 1].trigger,
                    action=action,
                    source=f"staged_{sp.name}",
                    priority=20 + sp.current_stage * 5,
                ),
                action=action,
                round_idx=round_idx,
            ))
            # Apply MutateAction immediately
            if isinstance(action, MutateAction):
                for var_name, new_val in action.mutations.items():
                    old_val = agent.get(var_name)
                    agent.set(var_name, new_val)
                    log.info(
                        "  staged mutate '%s'.%s: %.2f → %.2f",
                        agent.id, var_name, old_val, new_val,
                    )
    return fires


# ─────────────────────── Policy Evaluation ───────────────────────


def evaluate_policies(
    sim_graph: SimGraph,
    round_idx: int,
) -> list[PolicyFire]:
    """Evaluate all policies on all agents in one pass.

    Returns a list of PolicyFire results sorted by priority (highest first).
    The caller dispatches each fire by action type.

    This single function replaces:
      - entity_engine.evaluate_thresholds()
      - entity_engine.collect_forced_actions()
      - entity_engine.collect_threshold_events()
    """
    fires: list[PolicyFire] = []

    for agent in sim_graph.agents.values():
        for policy in agent.policies:
            if not policy.should_evaluate(round_idx):
                continue

            try:
                triggered = _evaluate_trigger(policy, agent)
            except Exception:
                log.warning(
                    "Policy '%s' on agent '%s' raised during evaluation, skipping",
                    policy.id, agent.id,
                )
                continue

            if not triggered:
                continue

            policy.last_fired_round = round_idx
            fires.append(PolicyFire(
                agent_id=agent.id,
                agent_display_name=agent.display_name,
                policy=policy,
                action=policy.action,
                round_idx=round_idx,
            ))

            log.info(
                "Policy '%s' fired on '%s' at R%d: %s [%s]",
                policy.id, agent.display_name, round_idx,
                policy.description, type(policy.action).__name__,
            )

            # Apply MutateAction immediately (same as old evaluate_thresholds)
            if isinstance(policy.action, MutateAction):
                for var_name, new_val in policy.action.mutations.items():
                    old_val = agent.get(var_name)
                    agent.set(var_name, new_val)
                    log.info(
                        "  mutate '%s'.%s: %.2f → %.2f",
                        agent.id, var_name, old_val, new_val,
                    )

    # Sort by priority (highest first)
    fires.sort(key=lambda f: f.policy.priority, reverse=True)
    return fires


def _evaluate_trigger(policy: Policy, agent: SimAgent) -> bool:
    """Evaluate a policy's trigger against the agent's state."""
    # Compile on first use (cache on the policy object)
    cache_attr = "_compiled_trigger"
    compiled = getattr(policy, cache_attr, None)
    if compiled is None:
        compiled = compile_trigger(policy.trigger_expr)
        object.__setattr__(policy, cache_attr, compiled)
    return compiled(agent)


# ─────────────────────── Dispatch Helpers ───────────────────────


def collect_trade_overrides(
    fires: list[PolicyFire],
    sim_graph: SimGraph,
) -> dict[str, PolicyFire]:
    """Extract TradeAction fires, keyed by persona_id.

    Returns dict[persona_id → PolicyFire] for the engine to override
    LLM trading decisions. If multiple trade policies fire for the same
    persona, highest priority wins (fires are pre-sorted).
    """
    overrides: dict[str, PolicyFire] = {}
    for fire in fires:
        if not isinstance(fire.action, TradeAction):
            continue
        agent = sim_graph.agents.get(fire.agent_id)
        if agent is None or agent.persona_id is None:
            continue
        # First match wins (fires sorted by priority desc)
        if agent.persona_id not in overrides:
            overrides[agent.persona_id] = fire
    return overrides


def collect_announcements(
    fires: list[PolicyFire],
) -> list[PolicyFire]:
    """Extract AnnounceAction fires for social feed injection."""
    return [f for f in fires if isinstance(f.action, AnnounceAction)]


# ─────────────────────── Sentiment Scoring ───────────────────────

_NEGATIVE_KEYWORDS: list[tuple[str, float]] = [
    ("降价", -0.15), ("促销", -0.10), ("亏损", -0.20), ("减持", -0.15),
    ("处罚", -0.20), ("下调", -0.15), ("库存积压", -0.15), ("裁员", -0.15),
    ("下滑", -0.10), ("下降", -0.10), ("诉讼", -0.15), ("违规", -0.20),
    ("暂停", -0.10), ("推迟", -0.10), ("召回", -0.15),
]

_POSITIVE_KEYWORDS: list[tuple[str, float]] = [
    ("增持", 0.15), ("回购", 0.15), ("超预期", 0.15), ("上调", 0.15),
    ("突破", 0.10), ("创新高", 0.15), ("签约", 0.10), ("中标", 0.10),
    ("扩产", 0.10), ("增长", 0.10), ("盈利", 0.10), ("分红", 0.10),
]


def score_announcement_sentiment(text: str) -> float:
    """Score announcement text for market sentiment impact.

    Returns float in [-1.0, 1.0]. Positive = bullish, negative = bearish.
    Uses keyword matching (fast, deterministic, no LLM call).
    """
    score = 0.0
    for keyword, weight in _NEGATIVE_KEYWORDS:
        if keyword in text:
            score += weight
    for keyword, weight in _POSITIVE_KEYWORDS:
        if keyword in text:
            score += weight
    return max(-1.0, min(1.0, score))


# ─────────────────────── Regulatory Dispatch ───────────────────────


def dispatch_regulatory_action(
    payload: dict[str, Any],
    sim_graph: SimGraph,
    round_idx: int,
) -> list[Policy]:
    """Convert a regulatory PendingAction payload into mechanical policies.

    Supported action_types:
      - window_guidance → conviction_damper=0.35 on all traders (3 rounds)
      - restriction → halve max_position_pct_override on traders
      - trading_halt → RejectAction blocking both buy/sell (2 rounds)
      - inquiry / statement → informational only, no mechanical effect

    Returns newly-created policies that were attached to SimAgents.
    """
    action_type = payload.get("action_type", "")
    new_policies: list[Policy] = []

    if action_type == "window_guidance":
        for agent in sim_graph.agents.values():
            if agent.persona_id is None:
                continue
            # Only apply to trader agents (those with persona_id set)
            agent.set("conviction_damper", 0.35)
            p = Policy(
                name=f"窗口指导_R{round_idx}",
                description=payload.get("detail", "窗口指导"),
                trigger_expr="conviction_damper > 0.0",
                action=MutateAction(mutations={"conviction_damper": 0.35}),
                priority=15,
                cooldown_rounds=3,
                source=f"regulate_round_{round_idx}",
            )
            p.last_fired_round = round_idx
            agent.policies.append(p)
            new_policies.append(p)

    elif action_type == "restriction":
        for agent in sim_graph.agents.values():
            if agent.persona_id is None:
                continue
            current_max = agent.get("max_position_pct_override")
            new_max = (current_max * 0.5) if current_max > 0 else 0.5
            agent.set("max_position_pct_override", new_max)
            new_policies.append(Policy(
                name=f"限制措施_R{round_idx}",
                description=payload.get("detail", "交易限制"),
                trigger_expr="max_position_pct_override > 0.0",
                action=MutateAction(mutations={"max_position_pct_override": new_max}),
                priority=15,
                cooldown_rounds=5,
                source=f"regulate_round_{round_idx}",
            ))

    elif action_type == "trading_halt":
        for agent in sim_graph.agents.values():
            if agent.persona_id is None:
                continue
            # Block both buy and sell via conviction_damper = 0
            agent.set("conviction_damper", 0.0)
            halt_policy = Policy(
                name=f"停牌_R{round_idx}",
                description=payload.get("detail", "临时停牌"),
                trigger_expr="stock_price > 0.0",
                action=MutateAction(mutations={"conviction_damper": 0.0}),
                priority=20,
                cooldown_rounds=2,
                source=f"regulate_round_{round_idx}",
            )
            halt_policy.last_fired_round = round_idx
            agent.policies.append(halt_policy)
            new_policies.append(halt_policy)

    # inquiry / statement → no mechanical effect, just informational
    return new_policies


__all__ = [
    "PolicyStage",
    "StagedPolicy",
    "collect_announcements",
    "collect_trade_overrides",
    "compile_trigger",
    "create_exchange_inquiry_policy",
    "create_national_team_etf_buy_policy",
    "create_short_sell_restriction_policy",
    "create_suspension_risk_policy",
    "dispatch_regulatory_action",
    "evaluate_policies",
    "evaluate_staged_policies",
    "get_ashare_policy_templates",
    "score_announcement_sentiment",
]
