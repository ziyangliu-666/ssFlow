"""Entity engine — round-loop integration for the Entity State Sandbox.

Provides the three functions that oasis_engine.py calls each round when
an EntityGraph is present:

  1. execute_resource_flows() — deterministic resource transfers
  2. evaluate_thresholds() — fire effects when state crosses boundaries
  3. inject_entity_state_into_prompts() — inject "处境" into agent prompts

Plus helpers for rendering entity situation blocks and updating
price-derived state variables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .entity import Entity, EntityGraph, ThresholdEffect
from .event_bus import (
    EVENT_ENTITY_STATE_UPDATED,
    EVENT_RESOURCE_FLOW_EXECUTED,
    EVENT_THRESHOLD_FIRED,
    EventSink,
    safe_emit,
)
from .information.external_events import ExternalEvent

log = logging.getLogger(__name__)


# ─────────────────────── Resource Flow Execution ───────────────────────


@dataclass
class FlowResult:
    """Result of one resource flow execution."""

    flow_id: str
    source_id: str
    target_id: str
    resource_type: str
    amount: float
    skipped: bool


def execute_resource_flows(
    graph: EntityGraph,
    round_idx: int,
    event_sink: EventSink | None = None,
    simulation_id: str = "",
) -> list[FlowResult]:
    """Execute all resource flows in the graph. Deterministic, no LLM.

    Each flow transfers resources from source to target at the declared
    rate_per_round. If the source doesn't have enough, the transfer is
    capped at what's available. If gated off, the flow is skipped.

    Returns a list of FlowResults for the round.
    """
    results: list[FlowResult] = []

    for flow in graph.flows:
        source = graph.entities.get(flow.source_id)
        target = graph.entities.get(flow.target_id)
        if source is None or target is None:
            log.warning(
                "Flow '%s' references missing entity, skipping", flow.id
            )
            continue

        amount = flow.execute(source, target)
        skipped = amount == 0.0 and flow.rate_per_round > 0.0

        results.append(
            FlowResult(
                flow_id=flow.id,
                source_id=flow.source_id,
                target_id=flow.target_id,
                resource_type=flow.resource_type,
                amount=amount,
                skipped=skipped,
            )
        )

        if amount > 0.0:
            safe_emit(
                event_sink,
                EVENT_RESOURCE_FLOW_EXECUTED,
                simulation_id=simulation_id,
                round_idx=round_idx,
                flow_id=flow.id,
                source_id=flow.source_id,
                source_name=source.display_name,
                target_id=flow.target_id,
                target_name=target.display_name,
                resource_type=flow.resource_type,
                amount=float(amount),
                label=flow.label,
            )

    return results


# ─────────────────────── Threshold Evaluation ───────────────────────


@dataclass
class ThresholdFire:
    """Record of one threshold firing."""

    threshold_id: str
    entity_id: str
    description: str
    effect: ThresholdEffect
    round_idx: int


def evaluate_thresholds(
    graph: EntityGraph,
    round_idx: int,
    event_sink: EventSink | None = None,
    simulation_id: str = "",
) -> list[ThresholdFire]:
    """Evaluate all thresholds. Fire effects for those whose conditions are met.

    Returns a list of ThresholdFires (the events that fired this round).
    The caller is responsible for actually injecting external events or
    applying forced actions based on the ThresholdFire.effect.
    """
    fires: list[ThresholdFire] = []

    for threshold in graph.thresholds:
        entity = graph.entities.get(threshold.entity_id)
        if entity is None:
            continue

        if not threshold.should_fire(entity, round_idx):
            continue

        threshold.last_fired_round = round_idx
        fire = ThresholdFire(
            threshold_id=threshold.id,
            entity_id=threshold.entity_id,
            description=threshold.description,
            effect=threshold.effect,
            round_idx=round_idx,
        )
        fires.append(fire)

        log.info(
            "Threshold '%s' fired on entity '%s' at R%d: %s",
            threshold.id,
            entity.display_name,
            round_idx,
            threshold.description,
        )

        # Apply state mutations immediately if effect_type is mutate_state.
        if threshold.effect.effect_type == "mutate_state":
            for var_name, new_val in threshold.effect.state_mutations.items():
                old_val = entity.state.get(var_name)
                entity.state.set(var_name, new_val)
                log.info(
                    "  mutate '%s'.%s: %.2f → %.2f",
                    entity.id,
                    var_name,
                    old_val,
                    new_val,
                )

        safe_emit(
            event_sink,
            EVENT_THRESHOLD_FIRED,
            simulation_id=simulation_id,
            round_idx=round_idx,
            threshold_id=threshold.id,
            entity_id=threshold.entity_id,
            entity_name=entity.display_name,
            description=threshold.description,
            effect_type=threshold.effect.effect_type,
            effect_event_text=threshold.effect.event_text,
        )

    return fires


@dataclass
class ForcedAction:
    """A hard trading override triggered by a threshold fire.

    When a threshold with effect_type='force_action' fires, the linked
    persona's LLM trading decision is overridden by this mechanical
    constraint. The engine replaces whatever the LLM submitted with
    this forced side/quantity.
    """

    persona_id: str
    side: str                # "buy" | "sell"
    quantity_pct: float      # 0.0 ~ 1.0
    reason: str              # threshold description (shown in timeline)
    threshold_id: str


def collect_forced_actions(
    fires: list[ThresholdFire],
    graph: "EntityGraph",
) -> dict[str, ForcedAction]:
    """Extract force_action effects from threshold fires.

    Returns a dict mapping persona_id → ForcedAction. If multiple
    force_action thresholds fire for the same persona, the last one wins
    (thresholds are evaluated in registration order, so later = higher priority).
    """
    forced: dict[str, ForcedAction] = {}
    for fire in fires:
        if fire.effect.effect_type != "force_action":
            continue
        if not fire.effect.forced_side:
            continue
        entity = graph.entities.get(fire.entity_id)
        if entity is None or entity.persona_id is None:
            continue
        forced[entity.persona_id] = ForcedAction(
            persona_id=entity.persona_id,
            side=fire.effect.forced_side,
            quantity_pct=fire.effect.forced_quantity_pct,
            reason=fire.description,
            threshold_id=fire.threshold_id,
        )
    return forced


def collect_threshold_events(
    fires: list[ThresholdFire],
    round_idx: int,
) -> list[ExternalEvent]:
    """Convert threshold fires with effect_type='inject_event' into
    ExternalEvent objects ready for injection into the OASIS social feed.

    Called by oasis_engine.py after evaluate_thresholds().
    """
    events: list[ExternalEvent] = []
    for fire in fires:
        if fire.effect.effect_type != "inject_event":
            continue
        if not fire.effect.event_text:
            continue
        events.append(
            ExternalEvent(
                round_idx=round_idx,
                content_type=fire.effect.event_content_type,
                author_persona_id=fire.effect.event_author_id or fire.entity_id,
                text=fire.effect.event_text,
                authority_weight=0.85,
            )
        )
    return events


# ─────────────────────── State Snapshots ───────────────────────


def record_entity_snapshots(
    graph: EntityGraph,
    round_idx: int,
    event_sink: EventSink | None = None,
    simulation_id: str = "",
) -> None:
    """Record state snapshots for all entities and emit state events."""
    for entity in graph.entities.values():
        entity.record_round()
        safe_emit(
            event_sink,
            EVENT_ENTITY_STATE_UPDATED,
            simulation_id=simulation_id,
            round_idx=round_idx,
            entity_id=entity.id,
            entity_name=entity.display_name,
            entity_type=entity.entity_type,
            state=entity.state.snapshot(),
            state_labels=dict(entity.state_labels),
        )


# ─────────────────────── Situation Rendering ───────────────────────

_SITUATION_MARKER = "\n# 你当前的处境"


def render_situation(
    entity: Entity,
    graph: EntityGraph,
    round_idx: int,
) -> str:
    """Render an entity's current "处境" as a prompt block.

    Includes: current state variables, recent threshold fires,
    upstream supply status, downstream demand status.
    """
    lines = [f"{_SITUATION_MARKER} / Your current situation (R{round_idx})"]

    # State snapshot with labels
    for var_name, value in sorted(entity.state.variables.items()):
        label = entity.state_labels.get(var_name, var_name)
        if value == int(value):
            lines.append(f"  - {label}: {int(value)}")
        else:
            lines.append(f"  - {label}: {value:.2f}")

    # Recent threshold fires (last 2 rounds)
    recent = [
        t
        for t in graph.thresholds
        if t.entity_id == entity.id and t.last_fired_round >= round_idx - 2
    ]
    if recent:
        lines.append("\n  近期触发:")
        for t in recent:
            lines.append(f"  - [R{t.last_fired_round}] {t.description}")

    # Upstream flows (resources coming IN)
    upstream = [f for f in graph.flows if f.target_id == entity.id]
    if upstream:
        lines.append("\n  上游供应:")
        for f in upstream:
            src = graph.entities.get(f.source_id)
            src_name = src.display_name if src else f.source_id
            lines.append(
                f"  - {src_name}: {f.resource_type} @ {f.rate_per_round}/轮"
            )

    # Downstream flows (resources going OUT)
    downstream = [f for f in graph.flows if f.source_id == entity.id]
    if downstream:
        lines.append("\n  下游需求:")
        for f in downstream:
            tgt = graph.entities.get(f.target_id)
            tgt_name = tgt.display_name if tgt else f.target_id
            lines.append(
                f"  - {tgt_name}: {f.resource_type} @ {f.rate_per_round}/轮"
            )

    return "\n".join(lines)


def inject_entity_state_into_prompts(
    graph: EntityGraph,
    agent_graph: Any,  # oasis AgentGraph — avoid hard import
    persona_id_to_oasis_id: dict[str, int],
    round_idx: int,
) -> None:
    """Mutate each LLM-driven entity's agent profile to include current state.

    Uses a sentinel marker so the situation block can be cleanly replaced
    each round without accumulating. OASIS reads UserInfo.profile fresh
    at each step, so the mutation takes effect immediately.
    """
    for entity in graph.entities.values():
        if entity.persona_id is None:
            continue
        if entity.persona_id not in persona_id_to_oasis_id:
            continue

        oasis_id = persona_id_to_oasis_id[entity.persona_id]
        try:
            agent = agent_graph.get_agent(oasis_id)
        except Exception:
            log.warning(
                "Cannot find OASIS agent %d for entity '%s', skipping injection",
                oasis_id,
                entity.id,
            )
            continue

        situation_block = render_situation(entity, graph, round_idx)

        profile = agent.user_info.profile
        other_info = profile.get("other_info", {})
        user_profile = other_info.get("user_profile", "")

        # Replace previous situation block (truncate at sentinel)
        if _SITUATION_MARKER in user_profile:
            user_profile = user_profile[: user_profile.index(_SITUATION_MARKER)]

        user_profile += situation_block

        other_info["user_profile"] = user_profile
        profile["other_info"] = other_info


def update_price_derived_state(
    graph: EntityGraph,
    new_price: float,
    initial_price: float,
) -> None:
    """Update price-derived state variables on entities that track them.

    Convention: entities with a state variable named 'stock_price' get it
    updated. Entities with 'price_change_pct' get the cumulative change.

    Financially-derived updates:
    - margin_utilization: when price drops, collateral shrinks but loan stays
      the same, so utilization ratio rises. Formula:
      new_margin = base_margin / (1 + price_change_pct/100)
      This is accurate: if price drops 10%, a 60% margin becomes ~67%.
    - sentiment_score: tracks cumulative price_change_pct (simple proxy).
    """
    if initial_price <= 0:
        return

    price_change_pct = (new_price / initial_price - 1.0) * 100

    for entity in graph.entities.values():
        if "stock_price" in entity.state.variables:
            entity.state.set("stock_price", new_price)
        if "price_change_pct" in entity.state.variables:
            entity.state.set("price_change_pct", price_change_pct)

        # Margin utilization rises when price drops (collateral shrinks)
        if "margin_utilization" in entity.state.variables:
            base = entity.state.variables.get("margin_utilization", 0.0)
            # Only adjust if there's an initial non-zero base
            if base > 0:
                ratio = 1.0 + price_change_pct / 100.0
                if ratio > 0:
                    new_margin = min(1.0, base / ratio)
                    entity.state.set("margin_utilization", new_margin)

        # Sentiment tracks cumulative change (simple proxy)
        if "sentiment_score" in entity.state.variables:
            entity.state.set("sentiment_score", price_change_pct)


__all__ = [
    "FlowResult",
    "ForcedAction",
    "ThresholdFire",
    "collect_forced_actions",
    "collect_threshold_events",
    "evaluate_thresholds",
    "execute_resource_flows",
    "inject_entity_state_into_prompts",
    "record_entity_snapshots",
    "render_situation",
    "update_price_derived_state",
]
