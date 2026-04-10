"""Policy evaluation engine — compile triggers and evaluate policies.

Replaces entity_engine.py's evaluate_thresholds + collect_forced_actions
+ collect_threshold_events with a single unified pass.

The trigger DSL extends the existing compile_condition format:
  - "margin_utilization > 0.72"      → reads from agent.state
  - "price_change_pct < -5.0"        → reads from agent.state
  - "avg_position_pct > 0.50"        → reads from agent.state (synced from population)

All triggers evaluate against the owning agent's state dict.
WorldState-scoped triggers (Phase 4) will extend the evaluator to
also read from sim_graph.world.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .policy import (
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


def compile_trigger(expr: str) -> Callable[[SimAgent], bool]:
    """Compile a trigger expression into a callable.

    Supports: variable_name {>, <, >=, <=, ==} float_literal
    The variable is read from agent.state.

    Rejects anything else (no arbitrary code execution).
    """
    m = _TRIGGER_RE.match(expr.strip())
    if not m:
        raise ValueError(
            f"Cannot compile trigger: {expr!r}. "
            f"Expected format: 'variable_name > 3.0'"
        )
    var_name = m.group(1)
    op = _COMPARISON_OPS[m.group(2)]
    threshold_val = float(m.group(3))
    return lambda agent, _var=var_name, _op=op, _val=threshold_val: _op(
        agent.get(_var), _val
    )


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


__all__ = [
    "collect_announcements",
    "collect_trade_overrides",
    "compile_trigger",
    "evaluate_policies",
]
