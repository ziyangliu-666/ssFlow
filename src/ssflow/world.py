"""Unified world model — SimAgent + WorldState + SimGraph.

Phase 1 of the architecture restructure. Provides a single state model
that eliminates the parallel-books problem between Entity state and
trading Agent state.

Key concepts:

  - **SimAgent**: One participant in the simulation. Can be a trader,
    company, regulator, analyst, or market environment. State is a single
    dict[str, float] — no parallel books. Traders carry a population
    of TraderInstance objects (the stochastic agent instances from
    trading_layer.py).

  - **WorldState**: Shared observable snapshot recomputed each round.
    Contains prices, aggregate flows, and each agent's public state.
    Agents read this to make decisions.

  - **SimGraph**: The simulation universe. Contains all SimAgents,
    resource flows, and the WorldState. Replaces EntityGraph as the
    top-level container.

Migration: SimGraph is built from the legacy (Persona[] + EntityGraph)
format via sim_graph_builder.py, so all existing code paths continue
to work unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .policy import Policy

log = logging.getLogger(__name__)


# ─────────────────────── SimAgent ───────────────────────


VALID_AGENT_KINDS = {
    "trader",
    "company",
    "supplier",
    "regulator",
    "analyst",
    "media",
    "kol",
    "market_env",
    "other",
}


@dataclass
class SimAgent:
    """One participant in the simulation universe.

    Replaces Entity for world modeling AND wraps Persona for traders.
    State is ONE dict — when a trader population's holdings change,
    aggregate stats are synced back here by the engine.

    Fields:
        id: unique agent identifier (matches entity slot or persona id)
        kind: agent role tag (trader, company, regulator, ...)
        display_name: human-readable name
        state: ALL state variables in one dict (financial + situational)
        state_labels: display labels for each state var (for UI/prompts)
        persona_id: links to Persona YAML for LLM-driven agents
        public_state_keys: which state vars other agents can observe
        history: per-round state snapshots
    """

    id: str
    kind: str
    display_name: str
    state: dict[str, float] = field(default_factory=dict)
    state_labels: dict[str, str] = field(default_factory=dict)
    persona_id: str | None = None
    policies: list[Policy] = field(default_factory=list)
    public_state_keys: set[str] = field(default_factory=set)
    history: list[dict[str, float]] = field(default_factory=list)

    def get(self, key: str, default: float = 0.0) -> float:
        """Read a state variable."""
        return self.state.get(key, default)

    def set(self, key: str, value: float) -> None:
        """Write a state variable."""
        self.state[key] = value

    def snapshot(self) -> dict[str, float]:
        return dict(self.state)

    def record_round(self) -> None:
        """Snapshot current state and append to history."""
        self.history.append(self.snapshot())

    def public_snapshot(self) -> dict[str, float]:
        """Return only the state vars marked public (observable by others)."""
        if not self.public_state_keys:
            return dict(self.state)
        return {k: self.state[k] for k in self.public_state_keys if k in self.state}


# ─────────────────────── Resource Flow (reused from entity.py) ───────────


@dataclass
class Flow:
    """A typed resource dependency between two SimAgents.

    Executes deterministically each round (no LLM needed). The
    rate_per_round is the base transfer amount.

    source_var / target_var: the state variable to decrement on source and
    increment on target. If empty, the flow is display-only.
    """

    id: str
    source_id: str
    target_id: str
    resource_type: str
    rate_per_round: float
    source_var: str = ""
    target_var: str = ""
    label: str = ""

    def execute(self, source: SimAgent, target: SimAgent) -> float:
        """Execute the flow. Returns actual transfer amount."""
        amount = self.rate_per_round

        if self.source_var:
            available = source.get(self.source_var, 0.0)
            amount = min(amount, max(0.0, available))
            source.set(self.source_var, available - amount)

        if self.target_var:
            current = target.get(self.target_var, 0.0)
            target.set(self.target_var, current + amount)

        return amount


# ─────────────────────── WorldState ───────────────────────


@dataclass
class WorldState:
    """Shared observable snapshot, recomputed each round.

    Every agent can read this. It aggregates public state from all
    agents plus market data (prices, flows, time context).
    """

    prices: dict[str, float] = field(default_factory=dict)
    aggregate_flows: dict[str, float] = field(default_factory=dict)
    agent_public_states: dict[str, dict[str, float]] = field(default_factory=dict)
    recent_events: list[str] = field(default_factory=list)
    round_idx: int = 0
    time_context: str = ""

    def snapshot_for_agent(self, agent_id: str) -> dict[str, Any]:
        """What a specific agent can observe: all public state + prices."""
        return {
            "prices": dict(self.prices),
            "aggregate_flows": dict(self.aggregate_flows),
            "other_agents": {
                k: v for k, v in self.agent_public_states.items()
                if k != agent_id
            },
            "recent_events": list(self.recent_events[-10:]),
            "round_idx": self.round_idx,
            "time_context": self.time_context,
        }


# ─────────────────────── SimGraph ───────────────────────


@dataclass
class SimGraph:
    """The simulation universe. All agents + flows + world state.

    Replaces EntityGraph as the top-level container. The key difference:
    SimGraph contains ALL participants (traders, companies, regulators),
    not just "entities". Every node is a SimAgent with unified state.
    """

    agents: dict[str, SimAgent] = field(default_factory=dict)
    flows: list[Flow] = field(default_factory=list)
    world: WorldState = field(default_factory=WorldState)
    topic: str = ""
    generated_at: str = ""

    def add_agent(self, agent: SimAgent) -> None:
        self.agents[agent.id] = agent

    def add_flow(self, flow: Flow) -> None:
        if flow.source_id not in self.agents:
            raise ValueError(
                f"Flow '{flow.id}': source '{flow.source_id}' not in graph. "
                f"Known agents: {sorted(self.agents.keys())}"
            )
        if flow.target_id not in self.agents:
            raise ValueError(
                f"Flow '{flow.id}': target '{flow.target_id}' not in graph. "
                f"Known agents: {sorted(self.agents.keys())}"
            )
        self.flows.append(flow)

    def trader_agents(self) -> list[SimAgent]:
        """All agents that have a persona_id linking to a trader persona."""
        return [
            a for a in self.agents.values()
            if a.kind == "trader" and a.persona_id is not None
        ]

    def non_trader_agents(self) -> list[SimAgent]:
        return [a for a in self.agents.values() if a.kind != "trader"]

    def agent_by_persona(self, persona_id: str) -> SimAgent | None:
        """Find the SimAgent linked to a given persona_id."""
        for a in self.agents.values():
            if a.persona_id == persona_id:
                return a
        return None

    # ─────────────────── World State Recomputation ───────────────────

    def recompute_world_state(
        self,
        prices: dict[str, float],
        round_idx: int,
        time_context: str = "",
    ) -> None:
        """Recompute WorldState from all agents' public state."""
        self.world.prices = dict(prices)
        self.world.round_idx = round_idx
        self.world.time_context = time_context
        self.world.agent_public_states = {
            aid: agent.public_snapshot()
            for aid, agent in self.agents.items()
        }

    # ─────────────────── Resource Flow Execution ───────────────────

    def execute_flows(self) -> list[tuple[str, float]]:
        """Execute all resource flows. Returns list of (flow_id, amount)."""
        results = []
        for flow in self.flows:
            source = self.agents.get(flow.source_id)
            target = self.agents.get(flow.target_id)
            if source is None or target is None:
                log.warning("Flow '%s' references missing agent, skipping", flow.id)
                continue
            amount = flow.execute(source, target)
            results.append((flow.id, amount))
        return results

    # ─────────────────── State Snapshots ───────────────────

    def record_all_snapshots(self) -> None:
        """Snapshot state for all agents (for history/replay)."""
        for agent in self.agents.values():
            agent.record_round()

    # ─────────────────── Sync Trading State ───────────────────

    def sync_population_state(
        self,
        agent_pops: dict[str, list],
        current_price: float,
        round_idx: int,
    ) -> None:
        """Write aggregate trading stats from populations back to SimAgent.state.

        This is THE FIX for the parallel-books problem: after
        apply_distribution_to_agent_pop mutates the TraderInstance
        population, we compute aggregate stats and write them back.

        Updates: avg_position_pct, avg_cash_pct, total_nav, total_holdings_value
        """
        for agent in self.trader_agents():
            if agent.persona_id is None or agent.persona_id not in agent_pops:
                continue
            pop = agent_pops[agent.persona_id]
            active = [a for a in pop if a.reaction_lag <= round_idx]
            if not active:
                continue

            total_nav = sum(a.nav(current_price) for a in active)
            total_hv = sum(a.holdings_value(current_price) for a in active)
            total_capital = sum(a.capital for a in active)

            if total_nav > 0:
                agent.set("avg_position_pct", total_hv / total_nav)
            if total_capital > 0:
                agent.set("avg_cash_pct", sum(a.cash for a in active) / total_capital)
            agent.set("total_nav", total_nav)
            agent.set("n_active_agents", float(len(active)))

    # ─────────────────── Price-Derived State ───────────────────

    def update_price_derived_state(
        self,
        new_price: float,
        initial_price: float,
    ) -> None:
        """Update price-derived state variables on all agents that track them.

        Operates directly on SimAgent.state — no separate entity engine needed.
        """
        if initial_price <= 0:
            return

        price_change_pct = (new_price / initial_price - 1.0) * 100

        for agent in self.agents.values():
            s = agent.state
            if "stock_price" in s:
                agent.set("stock_price", new_price)
            if "price_change_pct" in s:
                agent.set("price_change_pct", price_change_pct)

            # Margin utilization rises when price drops (collateral shrinks)
            if "margin_utilization" in s:
                base = s.get("margin_utilization", 0.0)
                if base > 0:
                    ratio = 1.0 + price_change_pct / 100.0
                    if ratio > 0:
                        agent.set("margin_utilization", min(1.0, base / ratio))

            # Sentiment tracks cumulative change
            if "sentiment_score" in s:
                agent.set("sentiment_score", price_change_pct)

    # ─────────────────── Serialization ───────────────────

    def to_serializable(self) -> dict[str, Any]:
        """Serialize for API transport."""
        return {
            "topic": self.topic,
            "generated_at": self.generated_at,
            "agents": {
                aid: {
                    "id": a.id,
                    "kind": a.kind,
                    "display_name": a.display_name,
                    "state": dict(a.state),
                    "state_labels": dict(a.state_labels),
                    "persona_id": a.persona_id,
                    "public_state_keys": sorted(a.public_state_keys),
                    "policies": [p.to_serializable() for p in a.policies],
                }
                for aid, a in self.agents.items()
            },
            "flows": [
                {
                    "id": f.id,
                    "source_id": f.source_id,
                    "target_id": f.target_id,
                    "resource_type": f.resource_type,
                    "rate_per_round": f.rate_per_round,
                    "source_var": f.source_var,
                    "target_var": f.target_var,
                    "label": f.label,
                }
                for f in self.flows
            ],
        }

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> SimGraph:
        """Reconstruct from serialized dict."""
        graph = cls(
            topic=data.get("topic", ""),
            generated_at=data.get("generated_at", ""),
        )
        for aid, a_data in data.get("agents", {}).items():
            from .policy import Policy as _Policy
            policies = [
                _Policy.from_serializable(p)
                for p in a_data.get("policies", [])
            ]
            graph.add_agent(SimAgent(
                id=aid,
                kind=a_data.get("kind", "other"),
                display_name=a_data.get("display_name", aid),
                state={k: float(v) for k, v in a_data.get("state", {}).items()},
                state_labels=dict(a_data.get("state_labels", {})),
                persona_id=a_data.get("persona_id"),
                policies=policies,
                public_state_keys=set(a_data.get("public_state_keys", [])),
            ))
        for f_data in data.get("flows", []):
            try:
                graph.add_flow(Flow(
                    id=f_data["id"],
                    source_id=f_data["source_id"],
                    target_id=f_data["target_id"],
                    resource_type=f_data.get("resource_type", ""),
                    rate_per_round=float(f_data.get("rate_per_round", 1.0)),
                    source_var=f_data.get("source_var", ""),
                    target_var=f_data.get("target_var", ""),
                    label=f_data.get("label", ""),
                ))
            except (KeyError, ValueError) as exc:
                log.warning("Skipping flow: %s", exc)
        return graph


    # ─────────────────── Prompt Injection ───────────────────

    def render_situation(self, agent: SimAgent, round_idx: int) -> str:
        """Render an agent's current situation as a prompt block."""
        lines = [f"\n# 你当前的处境 / Your current situation (R{round_idx})"]

        for var_name, value in sorted(agent.state.items()):
            label = agent.state_labels.get(var_name, var_name)
            if value == int(value):
                lines.append(f"  - {label}: {int(value)}")
            else:
                lines.append(f"  - {label}: {value:.2f}")

        # Recent policy fires (last 2 rounds)
        recent = [
            p for p in agent.policies
            if p.last_fired_round >= round_idx - 2 and p.last_fired_round >= 0
        ]
        if recent:
            lines.append("\n  近期触发:")
            for p in recent:
                lines.append(f"  - [R{p.last_fired_round}] {p.description}")

        # Upstream flows
        upstream = [f for f in self.flows if f.target_id == agent.id]
        if upstream:
            lines.append("\n  上游供应:")
            for f in upstream:
                src = self.agents.get(f.source_id)
                src_name = src.display_name if src else f.source_id
                lines.append(f"  - {src_name}: {f.resource_type} @ {f.rate_per_round}/轮")

        # Downstream flows
        downstream = [f for f in self.flows if f.source_id == agent.id]
        if downstream:
            lines.append("\n  下游需求:")
            for f in downstream:
                tgt = self.agents.get(f.target_id)
                tgt_name = tgt.display_name if tgt else f.target_id
                lines.append(f"  - {tgt_name}: {f.resource_type} @ {f.rate_per_round}/轮")

        # Active policies (shows the agent what rules it carries)
        active_policies = [p for p in agent.policies if p.enabled]
        if active_policies:
            lines.append("\n  当前规则:")
            for p in active_policies:
                lines.append(f"  - {p.name}: {p.trigger_expr} → {p.description}")

        return "\n".join(lines)

    _SITUATION_MARKER = "\n# 你当前的处境"

    def inject_state_into_prompts(
        self,
        agent_graph: Any,
        persona_id_to_oasis_id: dict[str, int],
        round_idx: int,
    ) -> None:
        """Inject each agent's situation into its OASIS agent profile."""
        for agent in self.agents.values():
            if agent.persona_id is None:
                continue
            if agent.persona_id not in persona_id_to_oasis_id:
                continue

            oasis_id = persona_id_to_oasis_id[agent.persona_id]
            try:
                oasis_agent = agent_graph.get_agent(oasis_id)
            except Exception:
                log.warning(
                    "Cannot find OASIS agent %d for '%s', skipping",
                    oasis_id, agent.id,
                )
                continue

            situation = self.render_situation(agent, round_idx)

            profile = oasis_agent.user_info.profile
            other_info = profile.get("other_info", {})
            user_profile = other_info.get("user_profile", "")

            marker = self._SITUATION_MARKER
            if marker in user_profile:
                user_profile = user_profile[:user_profile.index(marker)]

            user_profile += situation
            other_info["user_profile"] = user_profile
            profile["other_info"] = other_info


__all__ = [
    "Flow",
    "SimAgent",
    "SimGraph",
    "VALID_AGENT_KINDS",
    "WorldState",
]
