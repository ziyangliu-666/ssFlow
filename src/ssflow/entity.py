"""Entity State Sandbox — core data structures.

The Entity State Sandbox models an economic ecosystem around a market event.
Each simulation has an EntityGraph containing:

  - **Entities**: companies, suppliers, dealers, regulators, trader classes,
    and an "other" catch-all for minor participants. Each entity has typed
    float state variables (inventory, cash, capacity, etc.).

  - **ResourceFlows**: directed dependencies between entities. Each flow
    transfers a resource from source to target at a fixed rate per round.
    Flows execute deterministically (no LLM needed).

  - **Thresholds**: when an entity's state crosses a boundary, a forced
    effect fires (event injection, state mutation, or forced trading action).

The EntityGraph is generated per-simulation by the sandbox_generator
(LLM + templates + real data), not hardcoded.

Design principle: 主角单独建模，配角合并。A typical graph has 5 protagonist
entities + 1 aggregated "other" entity, never more than 8 nodes total.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ─────────────────────── Entity State ───────────────────────


@dataclass
class EntityState:
    """Typed float state variables for one entity.

    Keys are variable names (e.g. 'inventory_months', 'cash_bn').
    Values are floats. The schema is dynamic — generated per-simulation
    by the sandbox generator.
    """

    variables: dict[str, float] = field(default_factory=dict)

    def get(self, key: str, default: float = 0.0) -> float:
        return self.variables.get(key, default)

    def set(self, key: str, value: float) -> None:
        self.variables[key] = value

    def snapshot(self) -> dict[str, float]:
        return dict(self.variables)


# ─────────────────────── Entity ───────────────────────


VALID_ENTITY_TYPES = {
    "company",
    "supplier",
    "dealer",
    "regulator",
    "trader_class",
    "government",
    "other",
    "custom",
}


@dataclass
class Entity:
    """One entity in the simulation ecosystem.

    An entity is a company, supplier, dealer, regulator, trader class,
    or the aggregated "other" catch-all. It has structured state, optional
    resource dependencies, and an optional Persona for LLM-driven decisions.
    """

    id: str
    display_name: str
    entity_type: str
    state: EntityState = field(default_factory=EntityState)

    # Link to a Persona for LLM-driven decisions. None means this entity
    # is purely mechanical (resource flows + thresholds only, no LLM).
    persona_id: str | None = None

    # State variable metadata for display and prompt injection.
    # e.g. {"inventory_months": "库存(月)", "cash_bn": "现金(亿元)"}
    state_labels: dict[str, str] = field(default_factory=dict)

    # History of state snapshots per round (for charts and debugging).
    history: list[dict[str, float]] = field(default_factory=list)

    def record_round(self) -> None:
        """Snapshot current state and append to history."""
        self.history.append(self.state.snapshot())


# ─────────────────────── Resource Flow ───────────────────────


@dataclass
class ResourceFlow:
    """A typed resource dependency between two entities.

    Executes deterministically each round (no LLM needed). The rate_per_round
    is the base transfer amount.

    source_var / target_var: the state variable to decrement on source and
    increment on target. If empty, the flow is display-only (no state mutation).

    gate: optional Python callable (source, target) -> bool. If provided and
    returns False, the flow is skipped this round.
    """

    id: str
    source_id: str
    target_id: str
    resource_type: str
    rate_per_round: float
    source_var: str = ""
    target_var: str = ""
    gate: Callable[[Entity, Entity], bool] | None = None
    label: str = ""

    def execute(self, source: Entity, target: Entity) -> float:
        """Execute the flow: decrement source, increment target.

        Returns the actual transfer amount (may be less than rate if source
        doesn't have enough). Returns 0.0 if gated off.
        """
        if self.gate is not None and not self.gate(source, target):
            return 0.0

        amount = self.rate_per_round

        if self.source_var:
            available = source.state.get(self.source_var, 0.0)
            amount = min(amount, max(0.0, available))
            source.state.set(self.source_var, available - amount)

        if self.target_var:
            current = target.state.get(self.target_var, 0.0)
            target.state.set(self.target_var, current + amount)

        return amount


# ─────────────────────── Threshold ───────────────────────


@dataclass
class ThresholdEffect:
    """What happens when a threshold fires."""

    effect_type: str  # "inject_event" | "mutate_state" | "force_action"

    # For inject_event: text injected as an ExternalEvent into the social feed.
    event_text: str = ""
    event_author_id: str = ""
    event_content_type: str = "company_announcement"

    # For mutate_state: dict of {var_name: new_value} applied to the entity.
    state_mutations: dict[str, float] = field(default_factory=dict)

    # For force_action: override the entity's next trading action.
    forced_side: str = ""        # "buy" | "sell"
    forced_quantity_pct: float = 0.0


@dataclass
class Threshold:
    """When an entity's state crosses a boundary, fire an effect.

    condition is a Python callable (entity: Entity) -> bool. It is
    validated at registration time via TriggerProbe to catch typos.
    """

    id: str
    entity_id: str
    description: str
    condition: Callable[[Entity], bool]
    effect: ThresholdEffect
    cooldown_rounds: int = 2
    last_fired_round: int = -999

    def should_fire(self, entity: Entity, current_round: int) -> bool:
        if current_round - self.last_fired_round < self.cooldown_rounds:
            return False
        try:
            return self.condition(entity)
        except Exception:
            log.warning(
                "Threshold '%s' condition raised during evaluation, treating as False",
                self.id,
            )
            return False


# ─────────────────────── TriggerProbe ───────────────────────


class TriggerProbe:
    """Register-time validation for threshold conditions.

    Dry-runs the condition against a PROBE entity where accessing an
    unknown state variable raises an immediate error with a helpful
    message listing known variables + a close-match suggestion.

    EntityState.get() normally returns 0.0 for unknown keys (by design),
    which would let typos silently pass as False. The probe intercepts
    this by wrapping the entity's state in a _ProbeState that checks
    membership before returning.
    """

    @staticmethod
    def validate(condition: Callable[[Entity], bool], entity: Entity) -> None:
        known_vars = set(entity.state.variables.keys())
        probe_state = _ProbeState(entity.state.variables, known_vars)
        probe_entity = Entity(
            id=entity.id,
            display_name=entity.display_name,
            entity_type=entity.entity_type,
            state=probe_state,  # type: ignore[arg-type]
        )
        try:
            condition(probe_entity)
        except _TriggerFieldError as exc:
            raise ValueError(str(exc)) from exc
        except (KeyError, AttributeError) as exc:
            raise ValueError(
                f"Threshold condition references invalid state: {exc}. "
                f"Available variables: {sorted(known_vars)}"
            ) from exc


class _TriggerFieldError(Exception):
    pass


class _ProbeState(EntityState):
    """EntityState subclass that raises on access to unknown variables."""

    def __init__(
        self, variables: dict[str, float], known: set[str]
    ) -> None:
        super().__init__(variables=dict(variables))
        self._known = known

    def get(self, key: str, default: float = 0.0) -> float:
        if key not in self._known:
            import difflib

            close = difflib.get_close_matches(key, sorted(self._known), n=1)
            suggestion = f" Did you mean '{close[0]}'?" if close else ""
            raise _TriggerFieldError(
                f"Threshold condition accessed unknown state variable "
                f"'{key}'. Known variables: {sorted(self._known)}.{suggestion}"
            )
        return self.variables.get(key, default)


# ─────────────────────── Condition Compiler ───────────────────────

_COMPARISON_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}

_CONDITION_RE = re.compile(r"^(\w+)\s*(>=|<=|==|>|<)\s*(-?[0-9.]+)$")


def compile_condition(expr: str) -> Callable[[Entity], bool]:
    """Compile a simple 'var op value' string into a callable.

    Only supports: variable_name {>, <, >=, <=, ==} float_literal.
    Rejects anything else (no arbitrary code execution).

    Example: compile_condition("inventory_months > 3.0")
    """
    m = _CONDITION_RE.match(expr.strip())
    if not m:
        raise ValueError(
            f"Cannot compile condition: {expr!r}. "
            f"Expected format: 'variable_name > 3.0'"
        )
    var_name = m.group(1)
    op = _COMPARISON_OPS[m.group(2)]
    threshold_val = float(m.group(3))
    return lambda e, _var=var_name, _op=op, _val=threshold_val: _op(
        e.state.get(_var), _val
    )


# ─────────────────────── EntityGraph ───────────────────────


@dataclass
class EntityGraph:
    """The full entity ecosystem for one simulation.

    Contains all entities, resource flows, and thresholds. Generated by
    sandbox_generator.py from LLM + template + real data.
    """

    entities: dict[str, Entity] = field(default_factory=dict)
    flows: list[ResourceFlow] = field(default_factory=list)
    thresholds: list[Threshold] = field(default_factory=list)
    topic: str = ""
    generated_at: str = ""
    template_used: str = ""

    def add_entity(self, entity: Entity) -> None:
        if entity.entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"Entity '{entity.id}': entity_type must be one of "
                f"{sorted(VALID_ENTITY_TYPES)}, got {entity.entity_type!r}"
            )
        self.entities[entity.id] = entity

    def add_flow(self, flow: ResourceFlow) -> None:
        if flow.source_id not in self.entities:
            raise ValueError(
                f"Flow '{flow.id}': source '{flow.source_id}' not in graph. "
                f"Known entities: {sorted(self.entities.keys())}"
            )
        if flow.target_id not in self.entities:
            raise ValueError(
                f"Flow '{flow.id}': target '{flow.target_id}' not in graph. "
                f"Known entities: {sorted(self.entities.keys())}"
            )
        self.flows.append(flow)

    def register_threshold(self, threshold: Threshold) -> None:
        if threshold.entity_id not in self.entities:
            raise ValueError(
                f"Threshold '{threshold.id}': entity '{threshold.entity_id}' "
                f"not in graph. Known: {sorted(self.entities.keys())}"
            )
        TriggerProbe.validate(
            threshold.condition, self.entities[threshold.entity_id]
        )
        self.thresholds.append(threshold)

    def to_serializable(self) -> dict[str, Any]:
        """Serialize for API transport (strips callables)."""
        return {
            "topic": self.topic,
            "generated_at": self.generated_at,
            "template_used": self.template_used,
            "entities": {
                eid: {
                    "id": e.id,
                    "display_name": e.display_name,
                    "entity_type": e.entity_type,
                    "state": e.state.snapshot(),
                    "state_labels": e.state_labels,
                    "persona_id": e.persona_id,
                }
                for eid, e in self.entities.items()
            },
            "flows": [
                {
                    "id": f.id,
                    "source_id": f.source_id,
                    "target_id": f.target_id,
                    "resource_type": f.resource_type,
                    "rate_per_round": f.rate_per_round,
                    "label": f.label,
                    "source_var": f.source_var,
                    "target_var": f.target_var,
                }
                for f in self.flows
            ],
            "thresholds": [
                {
                    "id": t.id,
                    "entity_id": t.entity_id,
                    "description": t.description,
                    "effect_type": t.effect.effect_type,
                    "cooldown_rounds": t.cooldown_rounds,
                }
                for t in self.thresholds
            ],
        }


__all__ = [
    "Entity",
    "EntityGraph",
    "EntityState",
    "ResourceFlow",
    "Threshold",
    "ThresholdEffect",
    "TriggerProbe",
    "VALID_ENTITY_TYPES",
    "compile_condition",
]
