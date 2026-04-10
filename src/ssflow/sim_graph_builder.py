"""Build a SimGraph from legacy inputs (Persona[] + EntityGraph).

Converts EntityGraph entities → SimAgents, Thresholds → Policies,
and creates SimAgents for trader personas not covered by entities.

Usage:
    from ssflow.sim_graph_builder import build_sim_graph
    sim_graph = build_sim_graph(personas, entity_graph, event)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .policy import AnnounceAction, MutateAction, Policy, TradeAction
from .world import Flow, SimAgent, SimGraph

if TYPE_CHECKING:
    from .entity import EntityGraph, Threshold
    from .event import Event
    from .persona import Persona

log = logging.getLogger(__name__)


# Entity type → SimAgent kind mapping
_ENTITY_TYPE_TO_KIND = {
    "company": "company",
    "supplier": "supplier",
    "dealer": "supplier",
    "regulator": "regulator",
    "trader_class": "trader",
    "government": "regulator",
    "other": "market_env",
    "custom": "other",
}


def build_sim_graph(
    personas: list[Persona],
    entity_graph: EntityGraph | None,
    event: Event,
) -> SimGraph:
    """Build a SimGraph from legacy Persona list + optional EntityGraph.

    Strategy:
      1. Convert each Entity → SimAgent (preserving state + persona_id)
      2. Convert each Threshold → Policy on the owning SimAgent
      3. For uncovered trader personas → create minimal SimAgent
      4. Copy flows from EntityGraph
    """
    graph = SimGraph(
        topic=event.event_text or f"{event.ticker} {event.event_type}",
        generated_at="",
    )

    covered_persona_ids: set[str] = set()

    # ── Step 1: Convert entities → SimAgents ──
    if entity_graph is not None:
        for eid, entity in entity_graph.entities.items():
            kind = _ENTITY_TYPE_TO_KIND.get(entity.entity_type, "other")
            agent = SimAgent(
                id=eid,
                kind=kind,
                display_name=entity.display_name,
                state=dict(entity.state.variables),
                state_labels=dict(entity.state_labels),
                persona_id=entity.persona_id,
                public_state_keys=set(entity.state.variables.keys()),
            )
            # Auto-generate risk policies for covered trader personas
            if entity.persona_id and kind == "trader":
                persona_match = next(
                    (p for p in personas if p.id == entity.persona_id and p.sandbox),
                    None,
                )
                if persona_match:
                    agent.policies.extend(
                        _risk_config_to_policies(persona_match.id, persona_match.sandbox.risk)
                    )
            graph.add_agent(agent)
            if entity.persona_id:
                covered_persona_ids.add(entity.persona_id)

        # ── Step 2: Convert thresholds → policies ──
        for threshold in entity_graph.thresholds:
            agent = graph.agents.get(threshold.entity_id)
            if agent is None:
                continue
            policy = _threshold_to_policy(threshold)
            if policy is not None:
                agent.policies.append(policy)

        # ── Step 3: Copy flows ──
        for flow in entity_graph.flows:
            try:
                graph.add_flow(Flow(
                    id=flow.id,
                    source_id=flow.source_id,
                    target_id=flow.target_id,
                    resource_type=flow.resource_type,
                    rate_per_round=flow.rate_per_round,
                    source_var=flow.source_var,
                    target_var=flow.target_var,
                    label=flow.label,
                ))
            except ValueError as exc:
                log.warning("Skipping flow during SimGraph build: %s", exc)

    # ── Step 4: Create SimAgents for uncovered trader personas ──
    for persona in personas:
        if persona.id in covered_persona_ids:
            continue
        if persona.sandbox is None:
            continue

        agent = SimAgent(
            id=f"trader_{persona.id}",
            kind="trader",
            display_name=persona.display_name or persona.archetype or persona.id,
            state={
                "avg_position_pct": 0.0,
                "avg_cash_pct": 1.0,
                "total_nav": 0.0,
                "n_active_agents": 0.0,
            },
            state_labels={
                "avg_position_pct": "平均仓位(%)",
                "avg_cash_pct": "平均现金占比(%)",
                "total_nav": "总净值",
                "n_active_agents": "活跃实例数",
            },
            persona_id=persona.id,
            public_state_keys={"avg_position_pct"},
        )
        # Auto-generate mechanical policies from persona risk config
        risk_policies = _risk_config_to_policies(persona.id, persona.sandbox.risk)
        agent.policies.extend(risk_policies)
        graph.add_agent(agent)

    n_traders = len(graph.trader_agents())
    n_policies = sum(len(a.policies) for a in graph.agents.values())
    log.info(
        "SimGraph built: %d agents (%d traders), %d flows, %d policies, topic=%r",
        len(graph.agents), n_traders, len(graph.flows), n_policies,
        graph.topic[:60],
    )

    return graph


def _risk_config_to_policies(
    persona_id: str,
    risk: dict,
) -> list[Policy]:
    """Auto-generate mechanical trading policies from persona risk config.

    Reads optional keys from the risk dict:
      - stop_loss_threshold + stop_loss_discipline → sell on drawdown
      - profit_take_threshold + profit_take_fraction → sell on gain
      - max_hold_rounds → sell after N rounds held

    Returns an empty list when none of the keys are present, so existing
    personas without these keys are unaffected.
    """
    from typing import Any
    policies: list[Policy] = []

    # Stop-loss: sell when unrealized P&L drops below threshold
    sl_threshold = risk.get("stop_loss_threshold")
    sl_discipline = risk.get("stop_loss_discipline", 0.0)
    if sl_threshold is not None and sl_discipline > 0:
        policies.append(Policy(
            id=f"auto_stop_loss_{persona_id}",
            name=f"止损 {sl_threshold * 100:.0f}%",
            description=(
                f"Mechanical stop-loss: sell {sl_discipline * 100:.0f}% of holdings "
                f"when unrealized P&L drops below {sl_threshold * 100:.0f}%"
            ),
            trigger_expr=f"unrealized_pnl_pct < {sl_threshold * 100:.1f}",
            action=TradeAction(
                side="sell",
                quantity_pct=sl_discipline,
                pool="holdings_in_target",
            ),
            source="risk_config",
            cooldown_rounds=3,
            priority=20,
        ))
        log.info(
            "  Auto-policy: %s stop-loss at %.0f%% (discipline %.0f%%)",
            persona_id, sl_threshold * 100, sl_discipline * 100,
        )

    # Profit-taking: sell when unrealized gain exceeds threshold
    pt_threshold = risk.get("profit_take_threshold")
    pt_fraction = risk.get("profit_take_fraction", 0.30)
    if pt_threshold is not None:
        policies.append(Policy(
            id=f"auto_profit_take_{persona_id}",
            name=f"止盈 {pt_threshold * 100:.0f}%",
            description=(
                f"Mechanical profit-take: sell {pt_fraction * 100:.0f}% of holdings "
                f"when unrealized gain exceeds +{pt_threshold * 100:.0f}%"
            ),
            trigger_expr=f"unrealized_pnl_pct > {pt_threshold * 100:.1f}",
            action=TradeAction(
                side="sell",
                quantity_pct=pt_fraction,
                pool="holdings_in_target",
            ),
            source="risk_config",
            cooldown_rounds=2,
            priority=15,
        ))
        log.info(
            "  Auto-policy: %s profit-take at +%.0f%% (sell %.0f%%)",
            persona_id, pt_threshold * 100, pt_fraction * 100,
        )

    # Time-exit: sell after holding for too many rounds
    max_hold = risk.get("max_hold_rounds")
    if max_hold is not None:
        policies.append(Policy(
            id=f"auto_time_exit_{persona_id}",
            name=f"持仓超时 {max_hold}轮",
            description=(
                f"Mechanical time-exit: sell 50% of holdings "
                f"after holding for {max_hold} rounds"
            ),
            trigger_expr=f"avg_rounds_held > {float(max_hold)}",
            action=TradeAction(
                side="sell",
                quantity_pct=0.50,
                pool="holdings_in_target",
            ),
            source="risk_config",
            cooldown_rounds=max_hold,
            priority=10,
        ))
        log.info(
            "  Auto-policy: %s time-exit after %d rounds",
            persona_id, max_hold,
        )

    return policies


def _threshold_to_policy(threshold: Threshold) -> Policy | None:
    """Convert a legacy Threshold → Policy."""
    cond_expr = threshold.condition_expr
    if not cond_expr:
        log.warning("Threshold '%s' has no condition_expr, skipping", threshold.id)
        return None

    effect = threshold.effect
    action = None

    if effect.effect_type == "force_action":
        action = TradeAction(
            side=effect.forced_side,
            quantity_pct=effect.forced_quantity_pct,
        )
    elif effect.effect_type == "inject_event":
        action = AnnounceAction(
            text=effect.event_text,
            content_type=effect.event_content_type,
            authority_weight=0.85,
        )
    elif effect.effect_type == "mutate_state":
        action = MutateAction(
            mutations=dict(effect.state_mutations),
        )

    if action is None:
        return None

    return Policy(
        id=threshold.id,
        name=threshold.description,
        description=threshold.description,
        trigger_expr=cond_expr,
        action=action,
        source="template",
        cooldown_rounds=threshold.cooldown_rounds,
        priority=10 if isinstance(action, TradeAction) else 0,
    )


__all__ = ["build_sim_graph"]
