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
        graph.add_agent(agent)

    n_traders = len(graph.trader_agents())
    n_policies = sum(len(a.policies) for a in graph.agents.values())
    log.info(
        "SimGraph built: %d agents (%d traders), %d flows, %d policies, topic=%r",
        len(graph.agents), n_traders, len(graph.flows), n_policies,
        graph.topic[:60],
    )

    return graph


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
