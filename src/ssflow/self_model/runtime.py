"""Self-model runtime — one evaluator per trader persona per simulation.

The runtime is the glue layer: takes a validated ``SelfModelSpec``,
constructs initial state from the persona's sandbox + event, runs
per-round ``update`` calls fed by ``TradeResult`` structs, computes
utility on demand, and produces the prompt render block the engine
splices into each agent's conviction_ctx.

Intended call pattern from ``oasis_engine.py``::

    evaluators = build_evaluators_for_personas(personas, event)

    for round_idx in range(n_rounds):
        ...
        # After trading + sync_population_state:
        world_public_state = snapshot_world_for_self_models(
            evaluators, sim_graph,
        )
        for p in personas:
            if p.sandbox is None:
                continue
            ev = evaluators[p.id]
            ctx = RoundCtx(
                round_idx=round_idx,
                round_hours=rd.hours_since_event,
                n_rounds=n_rounds,
                current_price=current_price,
                initial_price=initial_price,
                cumulative_delta_pct=cumulative_delta_pct,
                prev_round_delta_pct=prev_round_delta_pct,
                event_type=event.event_type or "",
                world_public_state=world_public_state,
            )
            tr = build_trade_result(p.id, sim_graph, ...)
            ev.update(ctx, tr)
            peers = ev.select_peers(evaluators)
            self_ctx = ev.render_prompt(peers)
            set_round_context(
                oasis_agent, conviction_ctx=existing + "\\n\\n" + self_ctx,
            )
            # Emit persona_state_updated event for frontend:
            safe_emit(event_sink, EVENT_PERSONA_STATE_UPDATED,
                      simulation_id=simulation_id,
                      round_idx=round_idx,
                      persona_id=p.id,
                      archetype=p.archetype,
                      display_name=p.display_name,
                      state=ev.state_public_snapshot(),
                      state_labels=ev.state_labels(),
                      utility=ev.last_utility,
                      utility_delta=ev.last_utility_delta,
                      utility_breakdown=ev.last_utility_breakdown)

"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, TYPE_CHECKING

from .atoms import STATE_ATOMS
from .default_bundle import DEFAULT_SELF_MODEL_DICT
from .render import RENDER_SECTIONS
from .schema import (
    RoundCtx,
    SelfModelSpec,
    TradeResult,
    validate_self_model_spec,
)
from .utility import compute_utility


if TYPE_CHECKING:
    from ..persona import Persona


log = logging.getLogger(__name__)


# ─────────────────────── Helpers ───────────────────────


def _compact_money(v: float) -> str:
    """Short money format for prompt injection (no spaces, no separators).

    Used by ``SelfModelEvaluator.render_prompt`` to keep the self-state
    block terse — longer strings inflate OASIS feed posts and slow the
    twhin recsys cosine-similarity path.
    """
    if v is None or v == 0:
        return "—"
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v >= 1e8:
        return f"{sign}¥{abs_v/1e8:.1f}亿"
    if abs_v >= 1e4:
        return f"{sign}¥{abs_v/1e4:.0f}万"
    return f"{sign}¥{abs_v:,.0f}"


# ─────────────────────── Evaluator ───────────────────────


class SelfModelEvaluator:
    """Per-persona runtime instance.

    Holds the mutable state dict + a frozen spec. One method to update
    each round, one to render into a prompt block. Attributes like
    ``last_utility`` are exposed for the engine's event-emission step.
    """

    def __init__(
        self,
        persona: "Persona",
        spec: SelfModelSpec,
        event: Any,
    ) -> None:
        self.persona = persona
        self.spec = spec
        self.event = event

        # Pull the sandbox dict out of the persona for atom init rules
        sandbox_dict: dict[str, Any] = {}
        if persona.sandbox is not None:
            sandbox_dict = {
                "capital_distribution": dict(persona.sandbox.capital_distribution),
                "initial_position_distribution": dict(
                    persona.sandbox.initial_position_distribution
                ),
                "risk": dict(persona.sandbox.risk),
            }

        self.state: dict[str, float] = {}
        for key in spec.state_atoms:
            atom = STATE_ATOMS.get(key)
            if atom is None:
                continue  # Already stripped by validate, but defensive
            try:
                self.state[key] = float(
                    atom.init_rule(sandbox_dict, event, None)
                )
            except Exception as exc:
                log.warning(
                    "Atom %s init_rule raised %s; defaulting to 0",
                    key, exc,
                )
                self.state[key] = 0.0

        self.prev_state: dict[str, float] = dict(self.state)
        self.last_utility: float = 0.0
        self.last_utility_delta: float = 0.0
        self.last_utility_breakdown: dict[str, float] = {}
        # Fingerprint for step_utility's per-round idempotence guard
        self._last_stepped_fingerprint: tuple = ()

    # ── Update ──

    def update(self, round_ctx: RoundCtx, trade_result: TradeResult) -> None:
        """Recompute every atom in this persona's spec.

        Order matters where atoms depend on each other:
          - cash / holdings_value are written first (read from TradeResult)
          - nav depends on cash + holdings_value
          - unrealized_pnl_pct depends on nav + initial_nav
          - peak_nav depends on nav
          - max_drawdown_pct depends on peak_nav + nav
          - conviction / stress depend on unrealized_pnl_pct + max_drawdown_pct
          - consecutive_loss_rounds depends on last_action_outcome_pct
          - last_action_outcome_pct depends on last_action_price (stable if held)
          - regret depends on prev_round_delta_pct + tr.side

        The ordering is enforced by the PREFERRED_UPDATE_ORDER below; atoms
        not in that list run at the end in registration order.
        """
        self.prev_state = dict(self.state)

        # Update in a dependency-aware order
        ordered_keys = _order_atoms_for_update(self.spec.state_atoms)

        for key in ordered_keys:
            atom = STATE_ATOMS.get(key)
            if atom is None:
                continue
            try:
                self.state[key] = float(
                    atom.update_rule(self.state, round_ctx, trade_result)
                )
            except Exception as exc:
                log.warning(
                    "Atom %s update_rule raised %s; retaining prior value",
                    key, exc,
                )

    # ── Utility ──

    def compute_utility(self) -> tuple[float, dict[str, float]]:
        """Pure computation — does NOT mutate state. Safe to call repeatedly."""
        total, breakdown = compute_utility(self.state, self.spec.utility_weights)
        return total, breakdown

    def step_utility(self) -> tuple[float, float, dict[str, float]]:
        """Compute + record the utility — ONE call per round, idempotent.

        Writes ``last_utility`` / ``last_utility_delta`` /
        ``last_utility_breakdown`` as a snapshot for the engine to read
        when emitting events. Must be called ONCE per round, AFTER
        ``update()`` and BEFORE ``render_prompt()`` (which reads the
        cached values, not recomputes them).

        Calling step_utility twice in the same round is guarded by a
        round_version counter — the second call returns the cached tuple.
        Without this guard, a caller that calls step_utility explicitly
        for event emission and also relies on render_prompt would
        double-mutate ``last_utility`` and zero out the delta.
        """
        # Cache key = (round_idx as implicit via state snapshot id). We
        # use the state dict identity since update() rebuilds the state
        # each round. If the state dict reference + its hash of key vals
        # hasn't changed, step_utility was already called for this round.
        state_fingerprint = tuple(sorted(self.state.items()))
        if state_fingerprint == self._last_stepped_fingerprint:
            return (
                self.last_utility,
                self.last_utility_delta,
                self.last_utility_breakdown,
            )
        total, breakdown = self.compute_utility()
        delta = total - self.last_utility
        self.last_utility = total
        self.last_utility_delta = delta
        self.last_utility_breakdown = breakdown
        self._last_stepped_fingerprint = state_fingerprint
        return total, delta, breakdown

    # ── Peer selection ──

    def select_peers(
        self,
        all_evaluators: dict[str, "SelfModelEvaluator"],
    ) -> list[dict[str, Any]]:
        """Return up to ``top_k`` peers matching ``role_match`` globs.

        Returns list of flat dicts (not evaluators) so the render layer
        can treat peers as read-only snapshots.
        """
        filt = self.spec.peer_watchlist_filter
        if not filt:
            return []
        role_patterns = filt.get("role_match", ["*"])
        top_k = int(filt.get("top_k", 3))
        sort_by = filt.get("sort_by", "nav_growth_desc")

        candidates: list[tuple[float, dict[str, Any]]] = []
        for pid, ev in all_evaluators.items():
            if pid == self.persona.id:
                continue
            if not any(
                fnmatch.fnmatch(pid, pat) for pat in role_patterns
            ):
                continue
            snap = {
                "persona_id": pid,
                "display_name": ev.persona.display_name,
                "archetype": ev.persona.archetype,
                "unrealized_pnl_pct": ev.state.get("unrealized_pnl_pct", 0),
                "last_action_side": ev.state.get("last_action_side", 0),
                "last_action_size_pct": ev.state.get("last_action_size_pct", 0),
                "nav": ev.state.get("nav", 0),
            }
            # Sort key
            if sort_by == "nav_growth_desc":
                sort_key = -snap["unrealized_pnl_pct"]
            elif sort_by == "nav_growth_asc":
                sort_key = snap["unrealized_pnl_pct"]
            else:
                sort_key = 0.0
            candidates.append((sort_key, snap))

        candidates.sort(key=lambda t: t[0])
        return [snap for _, snap in candidates[:top_k]]

    # ── Render ──

    def render_prompt(self, peers: list[dict[str, Any]]) -> str:
        """Produce a COMPACT self-state block to inject into conviction_ctx.

        Optimized for token budget + OASIS recommender throughput:
        the render is a single dense line of ~100 chars, NOT multiple
        markdown sections. Long multi-line blocks (the earlier version
        produced ~450 chars with headings, blanks, and unicode bars)
        were triggering OASIS's twhin BERT cosine-similarity path into
        O(n²) pathological execution because LLMs tended to echo the
        verbose state into their posts, inflating post length 2-3×.

        The compact format still gives the agent the essentials:
          - Financial state: nav / pnl%  / drawdown
          - Last action + outcome
          - Emotional state: conviction / stress
          - Utility snapshot + top driver

        Peer watchlist (if configured) gets one terse line appended.

        If a persona's spec requests verbose render sections via
        ``render_sections``, they are IGNORED at runtime in favour of
        this compact format. The ``render_sections`` enum stays in the
        Library for future UI-only rendering; prompts always get the
        compact path.
        """
        s = self.state
        parts: list[str] = []

        # Financial quick-take
        nav = s.get("nav", 0)
        pnl = s.get("unrealized_pnl_pct", 0)
        dd = s.get("max_drawdown_pct", 0)
        nav_str = _compact_money(nav) if nav else "—"
        parts.append(f"持仓净值 {nav_str} Δ{pnl:+.1f}% 回撤 {dd:.1f}%")

        # Last trade outcome (terse)
        side_code = s.get("last_action_side", 0)
        size = s.get("last_action_size_pct", 0)
        outcome = s.get("last_action_outcome_pct", 0)
        if side_code > 0.5:
            side = "买"
        elif side_code < -0.5:
            side = "卖"
        else:
            side = "观望"
        if side != "观望" and size > 0:
            parts.append(f"上轮{side}{int(size*100)}% → {outcome:+.1f}%")
        else:
            parts.append(f"上轮{side}")

        # Emotional snapshot if tracked
        if "conviction" in s or "stress" in s:
            parts.append(
                f"信心{s.get('conviction', 0.5):.1f} 压力{s.get('stress', 0):.1f}"
            )

        # Utility summary
        total, delta, breakdown = self.step_utility()
        if breakdown:
            top = max(breakdown.items(), key=lambda kv: abs(kv[1]))
            top_str = f" 驱动:{top[0]}({top[1]:+.0f})"
        else:
            top_str = ""
        parts.append(f"U={total:+.1f} Δ{delta:+.1f}{top_str}")

        # Peer watchlist — 1 line, top 2 peers only
        if peers:
            peer_strs = []
            for p in peers[:2]:
                name = p.get("archetype") or p.get("persona_id", "?")[:8]
                peer_strs.append(f"{name}{p.get('unrealized_pnl_pct', 0):+.1f}%")
            if peer_strs:
                parts.append(f"同业:{'/'.join(peer_strs)}")

        # Join into a single hash-prefixed line so it stands out in the
        # user instruction but stays compact.
        return "# 你的处境: " + " | ".join(parts)

    # ── Snapshots for events / frontend ──

    def state_public_snapshot(self) -> dict[str, float]:
        """Subset of state keys marked observable_by_self=True.

        This is what goes into persona_state_updated events for the
        frontend panel. Internal anchors like initial_nav / peak_nav
        are filtered out so the UI only shows meaningful cards.
        """
        return {
            k: v
            for k, v in self.state.items()
            if k in STATE_ATOMS and STATE_ATOMS[k].observable_by_self
        }

    def state_labels(self) -> dict[str, str]:
        """Chinese labels for every key in the public snapshot.

        Reuses the pattern from EntityStatePanel: frontend gets a
        {key: label} dict alongside {key: value} and renders them as
        table rows.
        """
        return {
            k: STATE_ATOMS[k].label_zh
            for k in self.state_public_snapshot().keys()
        }


# ─────────────────────── Factory ───────────────────────


# Dependency-aware update order — atoms earlier in this list have their
# update rules called first so later atoms can read them via state.get().
_PREFERRED_UPDATE_ORDER: list[str] = [
    # Financial layer (cash/holdings from trade, nav derived)
    "cash",
    "holdings_value",
    "nav",
    "initial_nav",
    "unrealized_pnl_pct",
    "realized_pnl_cum",
    "peak_nav",
    "max_drawdown_pct",
    # Trajectory layer (reads price + trade result)
    "last_action_price",
    "last_action_outcome_pct",
    "last_action_side",
    "last_action_size_pct",
    "rounds_since_last_trade",
    # Emotional layer (reads financial + trajectory)
    "consecutive_loss_rounds",
    "stress",
    "conviction",
    "regret",
    # Accountability layer (reads emotional)
    "client_net_redemption",
    "career_risk_pressure",
    "reputation_score",
    # Benchmark layer (reads peers)
    "benchmark_gap_pct",
    "peer_rank_percentile",
    # Mandate layer
    "risk_budget_used_pct",
    "compliance_breaches_this_sim",
    # Company side
    "cash_runway_months",
    "board_pressure_index",
    "customer_concentration_risk",
    "media_negative_mention_count",
]


def _order_atoms_for_update(spec_atoms: list[str]) -> list[str]:
    """Return the persona's atoms in dependency-aware order.

    Atoms in ``_PREFERRED_UPDATE_ORDER`` come first in their canonical
    order; anything not in that list tacks onto the end in the order
    the persona declared it.
    """
    spec_set = set(spec_atoms)
    ordered = [k for k in _PREFERRED_UPDATE_ORDER if k in spec_set]
    extras = [k for k in spec_atoms if k not in _PREFERRED_UPDATE_ORDER]
    return ordered + extras


def build_evaluators_for_personas(
    personas: list["Persona"],
    event: Any,
) -> dict[str, SelfModelEvaluator]:
    """Factory called once at sim start from oasis_engine.

    Walks all trader personas (those with sandbox), validates each
    persona's ``self_model`` (or applies the default bundle), builds
    one ``SelfModelEvaluator`` per persona, and returns a dict keyed
    by persona id.

    Info-only personas (analysts, media, regulators, KOLs) are not
    given evaluators — they don't trade and don't have nav to track.
    """
    out: dict[str, SelfModelEvaluator] = {}
    for p in personas:
        if p.sandbox is None:
            continue
        raw_spec = getattr(p, "self_model", None)
        if raw_spec is None:
            raw_spec = DEFAULT_SELF_MODEL_DICT
        cleaned, warnings = validate_self_model_spec(raw_spec)
        for w in warnings:
            log.info(
                "persona %s self_model validation: %s",
                p.id, w,
            )
        # Fallback: if validation stripped everything, apply default
        if not cleaned["state_atoms"]:
            log.warning(
                "persona %s: self_model spec was empty after validation, "
                "falling back to DEFAULT_SELF_MODEL",
                p.id,
            )
            cleaned = dict(DEFAULT_SELF_MODEL_DICT)
        spec = SelfModelSpec.from_dict(cleaned)
        out[p.id] = SelfModelEvaluator(p, spec, event)
    log.info(
        "Built %d self_model evaluators (of %d total personas)",
        len(out), len(personas),
    )
    return out


__all__ = [
    "SelfModelEvaluator",
    "build_evaluators_for_personas",
]
