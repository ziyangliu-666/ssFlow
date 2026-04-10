"""Policy engine — composable rules that replace Threshold + constraint systems.

A Policy is an IF-THEN rule attached to a SimAgent. When the trigger
condition evaluates to True against the agent's state (and optionally
the WorldState), the policy's Action fires.

Policies unify three previously separate rule systems:
  1. Entity Thresholds  (condition → inject_event / mutate_state / force_action)
  2. Persona constraints (max_position_pct → reject_buy)
  3. Prompt-based rules  (LLM instructions, now expressible as soft policies)

Policies are composable: an agent can carry many policies. They're
evaluated in priority order. Policies can be created at setup time
(from templates) or dynamically during simulation (Phase 4: LLM
creates a stop-loss rule on itself).

Action types:
  - TradeAction:    force a buy/sell on the agent's trading population
  - AnnounceAction: inject text into the OASIS social feed
  - MutateAction:   directly set state variables on the agent
  - RejectAction:   block a specific trading direction (e.g., no more buying)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─────────────────────── Actions ───────────────────────


@dataclass
class TradeAction:
    """Force a trade on the agent's population.

    Replaces ThresholdEffect(effect_type="force_action").
    """

    side: str = ""              # "buy" | "sell"
    quantity_pct: float = 0.0   # 0.0 ~ 1.0
    pool: str = ""              # "cash" | "holdings_in_target" (auto-inferred if empty)


@dataclass
class AnnounceAction:
    """Inject text into the OASIS social feed.

    Replaces ThresholdEffect(effect_type="inject_event").
    """

    text: str = ""
    content_type: str = "company_announcement"
    authority_weight: float = 0.85


@dataclass
class MutateAction:
    """Directly set state variables on the owning agent.

    Replaces ThresholdEffect(effect_type="mutate_state").
    """

    mutations: dict[str, float] = field(default_factory=dict)


@dataclass
class RejectAction:
    """Block a specific trading direction.

    Replaces SandboxConfig.risk.max_position_pct enforcement.
    When this fires, the engine caps or rejects the LLM's order.
    """

    reject_side: str = ""  # "buy" | "sell" — which direction to block


# Union type for dispatch
Action = TradeAction | AnnounceAction | MutateAction | RejectAction


# ─────────────────────── Policy ───────────────────────


@dataclass
class Policy:
    """A composable rule attached to a SimAgent.

    Fields:
        id: unique identifier
        name: human-readable name (shown in timeline)
        description: explanation (injected into agent prompt + timeline)
        trigger_expr: condition DSL string, e.g. "margin_utilization > 0.72"
        trigger_scope: "self" (agent state) | "world" (world state)
        action: what to do when triggered (typed Action)
        source: who created this ("template" | "persona_config" | "llm_round_5")
        cooldown_rounds: minimum rounds between activations
        priority: higher = evaluated first, wins conflicts
        enabled: can be disabled without removing
    """

    id: str
    name: str
    description: str
    trigger_expr: str
    action: Action
    trigger_scope: str = "self"
    source: str = "template"
    cooldown_rounds: int = 2
    priority: int = 0
    last_fired_round: int = -999
    enabled: bool = True

    def should_evaluate(self, round_idx: int) -> bool:
        """Check if this policy is eligible to fire this round."""
        if not self.enabled:
            return False
        if round_idx - self.last_fired_round < self.cooldown_rounds:
            return False
        return True

    def to_serializable(self) -> dict[str, Any]:
        action_data: dict[str, Any] = {}
        if isinstance(self.action, TradeAction):
            action_data = {
                "action_type": "trade",
                "side": self.action.side,
                "quantity_pct": self.action.quantity_pct,
                "pool": self.action.pool,
            }
        elif isinstance(self.action, AnnounceAction):
            action_data = {
                "action_type": "announce",
                "text": self.action.text,
                "content_type": self.action.content_type,
                "authority_weight": self.action.authority_weight,
            }
        elif isinstance(self.action, MutateAction):
            action_data = {
                "action_type": "mutate",
                "mutations": dict(self.action.mutations),
            }
        elif isinstance(self.action, RejectAction):
            action_data = {
                "action_type": "reject",
                "reject_side": self.action.reject_side,
            }
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger_expr": self.trigger_expr,
            "trigger_scope": self.trigger_scope,
            "action": action_data,
            "source": self.source,
            "cooldown_rounds": self.cooldown_rounds,
            "priority": self.priority,
            "enabled": self.enabled,
        }

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> Policy:
        a = data.get("action", {})
        atype = a.get("action_type", "")
        action: Action
        if atype == "trade":
            action = TradeAction(
                side=a.get("side", ""),
                quantity_pct=float(a.get("quantity_pct", 0)),
                pool=a.get("pool", ""),
            )
        elif atype == "announce":
            action = AnnounceAction(
                text=a.get("text", ""),
                content_type=a.get("content_type", "company_announcement"),
                authority_weight=float(a.get("authority_weight", 0.85)),
            )
        elif atype == "mutate":
            action = MutateAction(
                mutations={k: float(v) for k, v in a.get("mutations", {}).items()},
            )
        elif atype == "reject":
            action = RejectAction(reject_side=a.get("reject_side", ""))
        else:
            action = MutateAction()  # fallback
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            trigger_expr=data.get("trigger_expr", ""),
            trigger_scope=data.get("trigger_scope", "self"),
            action=action,
            source=data.get("source", "template"),
            cooldown_rounds=int(data.get("cooldown_rounds", 2)),
            priority=int(data.get("priority", 0)),
            enabled=data.get("enabled", True),
        )


    @classmethod
    def from_llm_spec(
        cls,
        spec: dict[str, Any],
        source: str = "llm",
        owner_id: str = "",
    ) -> Policy:
        """Create a Policy from an LLM-generated spec dict.

        The LLM provides a loose dict; we validate and compile it.
        Raises ValueError if the spec is invalid.

        Expected spec shape:
            {
                "name": "stop loss at -20%",
                "trigger": "drawdown < -20.0",        # or any compile_trigger-able expr
                "action_type": "trade",                # "trade" | "announce" | "mutate"
                "side": "sell",                        # for trade
                "quantity_pct": 1.0,                   # for trade
                "text": "...",                         # for announce
                "mutations": {"var": val},             # for mutate
                "cooldown": 2,                         # optional
                "duration_rounds": null,               # optional, for future use
            }
        """
        name = str(spec.get("name", "unnamed"))
        trigger_expr = str(spec.get("trigger", ""))
        if not trigger_expr:
            raise ValueError("Policy spec must have a 'trigger' expression")

        # Validate the trigger compiles
        from .policy_engine import compile_trigger
        try:
            compile_trigger(trigger_expr)
        except ValueError as exc:
            raise ValueError(f"Invalid trigger expression: {exc}") from exc

        action_type = str(spec.get("action_type", ""))
        action: Action
        if action_type == "trade":
            side = str(spec.get("side", ""))
            if side not in ("buy", "sell"):
                raise ValueError(f"Trade action side must be 'buy' or 'sell', got {side!r}")
            action = TradeAction(
                side=side,
                quantity_pct=float(spec.get("quantity_pct", 0)),
            )
        elif action_type == "announce":
            action = AnnounceAction(text=str(spec.get("text", "")))
        elif action_type == "mutate":
            mutations = spec.get("mutations", {})
            if not isinstance(mutations, dict):
                raise ValueError("mutate action requires a mutations dict")
            action = MutateAction(mutations={k: float(v) for k, v in mutations.items()})
        else:
            raise ValueError(f"Unknown action_type: {action_type!r}")

        import uuid
        return cls(
            id=f"llm_{uuid.uuid4().hex[:8]}",
            name=name,
            description=name,
            trigger_expr=trigger_expr,
            action=action,
            source=source,
            cooldown_rounds=int(spec.get("cooldown", 2)),
            priority=5,  # LLM-created policies are medium priority
        )


# ─────────────────────── PolicyFire ───────────────────────


@dataclass
class PolicyFire:
    """Record of one policy activation in a round."""

    agent_id: str
    agent_display_name: str
    policy: Policy
    action: Action
    round_idx: int


__all__ = [
    "Action",
    "AnnounceAction",
    "MutateAction",
    "Policy",
    "PolicyFire",
    "RejectAction",
    "TradeAction",
]
