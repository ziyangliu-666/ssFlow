"""Self-model subsystem schema — core dataclasses.

Three dataclasses form the API surface:

- ``SelfModelSpec``   — declarative per-persona configuration (what atoms,
                        what utility weights, what render sections, what
                        peer filter). Serializable to/from JSON.
- ``RoundCtx``        — per-round context handed to atom update rules.
- ``TradeResult``     — per-persona trade outcome summary from the round.

Plus the ``StateAtomDef`` type that ``atoms.py`` instantiates — each atom
is a (init_rule, update_rule, metadata) triple with narrow, well-typed
callables so the runtime can compose them without introspection.

Validation lives here (``validate_self_model_spec``) so both the
``persona_factory`` Stage 3 merge step and the runtime can call it to
strip unknown atom/component/section names before they reach the engine.
Stripping (with a warning) rather than raising is deliberate: an
LLM-proposed spec that picked one bad atom should still run on the good
ones — total failure would force a re-roll of the whole persona pack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable


log = logging.getLogger(__name__)


# ─────────────────────── Round context ───────────────────────


@dataclass(frozen=True)
class RoundCtx:
    """Read-only context handed to every atom update rule each round.

    Intentionally narrow: atoms should only reach into this struct, not
    into the engine's global state. That keeps atoms unit-testable.

    ``round_hours`` is schedule-aware so decay rates calibrate correctly
    across daily (hours=0/3.5/24/…), monthly (hours=720/1440/…), and
    weekly schedules. Atoms that use exponential decay should normalise
    against round_hours rather than round_idx.
    """

    round_idx: int
    round_hours: float              # hours_since_event, from round_schedule
    n_rounds: int
    current_price: float
    initial_price: float
    cumulative_delta_pct: float     # ratio, e.g. -0.05 = -5%
    prev_round_delta_pct: float = 0.0
    event_type: str = ""
    # Public snapshot of other agents' observable state. Keyed by
    # persona_id, value is dict of atom_key → float. Populated by the
    # runtime's peer selector each round before calling update(). Atoms
    # that care about peers (e.g. peer_rank_percentile) read this;
    # atoms that don't ignore it.
    world_public_state: dict[str, dict[str, float]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class TradeResult:
    """What happened to this persona's trade request this round.

    Populated after the engine runs ``trading_layer.apply_distribution_to_agent_pop``
    and ``sync_population_state``. Atom update rules read ``nav /
    holdings_value / cash`` from here (authoritative post-trade values)
    rather than doing their own accounting.
    """

    persona_id: str
    side: str = "hold"              # "buy" | "sell" | "hold"
    quantity_pct: float = 0.0
    rationale: str = ""
    # Aggregate population stats written by sync_population_state
    nav: float = 0.0
    cash: float = 0.0
    holdings_value: float = 0.0
    unrealized_pnl_pct: float = 0.0
    avg_position_pct: float = 0.0
    net_flow: float = 0.0           # signed: positive buy, negative sell


# ─────────────────────── Atom definition ───────────────────────


# Callable type aliases — named so mypy / IDEs surface them in hints
InitRule = Callable[[dict[str, Any], Any, Any], float]
# init_rule signature: (sandbox_state, event, round_ctx) -> float
UpdateRule = Callable[[dict[str, float], RoundCtx, TradeResult], float]
# update_rule signature: (current_state_dict, round_ctx, trade_result) -> new_value


@dataclass(frozen=True)
class StateAtomDef:
    """One unit of persona state with rules for how it evolves.

    Atoms are the leaves of the self-model — each one is a scalar float
    with explicit init + update logic. Compose them into a persona by
    listing atom keys in ``SelfModelSpec.state_atoms``.

    Design rules:
      - Atoms may ONLY read from the state dict passed to update_rule;
        no global state, no engine hooks. This makes them trivially
        unit-testable.
      - ``observable_by_peers=True`` atoms get exposed into
        ``RoundCtx.world_public_state`` so other personas can compare.
      - ``label_zh`` is the display label for the frontend state panel
        (reused via the same state_labels dict that EntityStatePanel
        already renders).
    """

    key: str
    init_rule: InitRule
    update_rule: UpdateRule
    observable_by_self: bool = True
    observable_by_peers: bool = False
    label_zh: str = ""
    # Optional category for grouping in the UI / for the atom library index
    category: str = "universal"


# ─────────────────────── Self-model spec ───────────────────────


@dataclass
class SelfModelSpec:
    """Per-persona self-model configuration.

    One instance per persona. Either hand-authored in the persona YAML
    via a ``self_model:`` block, or generated by ``persona_factory``
    Stage 3 from the atom/component/section catalogs.

    Fields:
      state_atoms: list of atom keys this persona maintains. Must all
          appear in STATE_ATOMS after validation.
      utility_weights: dict of component_key → weight. Components must
          all appear in UTILITY_COMPONENTS.
      render_sections: ordered list of render section names to inject
          into the agent's prompt each round. Capped at 5 for token
          budget — validation trims extras.
      peer_watchlist_filter: dict used by the runtime's peer selector:
          {"role_match": ["mutual_fund_*"], "top_k": 3,
           "sort_by": "nav_growth_desc"}
      custom_atoms: reserved for Phase 9 DSL. For now these are
          silently dropped by validate_self_model_spec with a log warn.
    """

    state_atoms: list[str]
    utility_weights: dict[str, float]
    render_sections: list[str]
    peer_watchlist_filter: dict[str, Any] = field(default_factory=dict)
    custom_atoms: list[dict[str, Any]] = field(default_factory=list)

    # Hard cap on render sections — token budget protection. See
    # analysis note in the plan file about a naive 9-section render
    # adding ~600 tokens per persona per round.
    RENDER_SECTION_CAP: int = 5

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SelfModelSpec":
        return cls(
            state_atoms=list(d.get("state_atoms", [])),
            utility_weights=dict(d.get("utility_weights", {})),
            render_sections=list(d.get("render_sections", [])),
            peer_watchlist_filter=dict(d.get("peer_watchlist_filter", {})),
            custom_atoms=list(d.get("custom_atoms", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_atoms": list(self.state_atoms),
            "utility_weights": dict(self.utility_weights),
            "render_sections": list(self.render_sections),
            "peer_watchlist_filter": dict(self.peer_watchlist_filter),
            "custom_atoms": list(self.custom_atoms),
        }


# ─────────────────────── Validation guardrail ───────────────────────


def validate_self_model_spec(
    raw: dict[str, Any] | None,
    *,
    known_atoms: set[str] | None = None,
    known_components: set[str] | None = None,
    known_sections: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Strip unknown atom / component / section names from a raw spec dict.

    Accepts ``None`` (returns empty spec, no warnings). Safe to call on
    both hand-authored YAML specs and LLM-generated Stage 3 output.

    Rationale: if an LLM invents ``"career_risk"`` instead of the real
    ``"career_risk_pressure"``, we want the other 14 good choices to
    still take effect. Total-failure validation would force a re-roll
    of the whole persona factory pack, which is expensive.

    Returns:
        (cleaned_dict, warnings_list) — cleaned_dict is always a valid
        shape (possibly empty on all fronts). warnings_list is a list
        of human-readable strings you can log or surface in tests.
    """
    warnings: list[str] = []

    if raw is None:
        return (
            {
                "state_atoms": [],
                "utility_weights": {},
                "render_sections": [],
                "peer_watchlist_filter": {},
                "custom_atoms": [],
            },
            warnings,
        )

    if not isinstance(raw, dict):
        warnings.append(
            f"self_model spec must be a dict, got {type(raw).__name__}; "
            "using empty spec"
        )
        return (
            {
                "state_atoms": [],
                "utility_weights": {},
                "render_sections": [],
                "peer_watchlist_filter": {},
                "custom_atoms": [],
            },
            warnings,
        )

    # Lazy import so this module doesn't force-load the full library
    # (cycle risk and keeps schema.py independently testable).
    if known_atoms is None:
        from .atoms import STATE_ATOMS
        known_atoms = set(STATE_ATOMS.keys())
    if known_components is None:
        from .utility import UTILITY_COMPONENTS
        known_components = set(UTILITY_COMPONENTS.keys())
    if known_sections is None:
        from .render import RENDER_SECTIONS
        known_sections = set(RENDER_SECTIONS.keys())

    # ── state_atoms ──
    raw_atoms = raw.get("state_atoms", [])
    if not isinstance(raw_atoms, list):
        warnings.append(
            f"state_atoms must be a list, got {type(raw_atoms).__name__}"
        )
        raw_atoms = []
    clean_atoms: list[str] = []
    for a in raw_atoms:
        if not isinstance(a, str):
            warnings.append(f"dropped non-string atom entry: {a!r}")
            continue
        if a not in known_atoms:
            warnings.append(f"dropped unknown atom: {a!r}")
            continue
        if a in clean_atoms:
            continue  # dedup silently
        clean_atoms.append(a)

    # ── utility_weights ──
    raw_weights = raw.get("utility_weights", {})
    if not isinstance(raw_weights, dict):
        warnings.append(
            f"utility_weights must be a dict, got {type(raw_weights).__name__}"
        )
        raw_weights = {}
    clean_weights: dict[str, float] = {}
    for comp, w in raw_weights.items():
        if not isinstance(comp, str):
            warnings.append(f"dropped non-string component key: {comp!r}")
            continue
        if comp not in known_components:
            warnings.append(f"dropped unknown utility component: {comp!r}")
            continue
        try:
            clean_weights[comp] = float(w)
        except (TypeError, ValueError):
            warnings.append(
                f"dropped non-numeric weight for {comp!r}: {w!r}"
            )

    # ── render_sections ──
    raw_sections = raw.get("render_sections", [])
    if not isinstance(raw_sections, list):
        warnings.append(
            f"render_sections must be a list, got {type(raw_sections).__name__}"
        )
        raw_sections = []
    clean_sections: list[str] = []
    for s in raw_sections:
        if not isinstance(s, str):
            warnings.append(f"dropped non-string section entry: {s!r}")
            continue
        if s not in known_sections:
            warnings.append(f"dropped unknown render section: {s!r}")
            continue
        if s in clean_sections:
            continue  # dedup
        clean_sections.append(s)
    # Enforce token budget cap
    if len(clean_sections) > SelfModelSpec.RENDER_SECTION_CAP:
        warnings.append(
            f"render_sections capped from {len(clean_sections)} "
            f"to {SelfModelSpec.RENDER_SECTION_CAP} (token budget)"
        )
        clean_sections = clean_sections[: SelfModelSpec.RENDER_SECTION_CAP]

    # ── peer_watchlist_filter ──
    raw_filter = raw.get("peer_watchlist_filter", {})
    if not isinstance(raw_filter, dict):
        warnings.append(
            f"peer_watchlist_filter must be a dict, got {type(raw_filter).__name__}"
        )
        raw_filter = {}
    clean_filter: dict[str, Any] = {}
    if "role_match" in raw_filter:
        rm = raw_filter["role_match"]
        if isinstance(rm, list) and all(isinstance(x, str) for x in rm):
            clean_filter["role_match"] = list(rm)
        else:
            warnings.append("peer_watchlist_filter.role_match must be list[str]")
    if "top_k" in raw_filter:
        try:
            clean_filter["top_k"] = max(0, int(raw_filter["top_k"]))
        except (TypeError, ValueError):
            warnings.append(
                f"peer_watchlist_filter.top_k not an int: {raw_filter['top_k']!r}"
            )
    if "sort_by" in raw_filter and isinstance(raw_filter["sort_by"], str):
        clean_filter["sort_by"] = raw_filter["sort_by"]

    # ── custom_atoms (Phase 9 DSL target) ──
    raw_custom = raw.get("custom_atoms", [])
    if raw_custom:
        warnings.append(
            f"self_model custom_atoms present ({len(raw_custom)} entries) "
            "but DSL executor is not yet implemented — dropping. "
            "(Phase 9 work item.)"
        )

    cleaned: dict[str, Any] = {
        "state_atoms": clean_atoms,
        "utility_weights": clean_weights,
        "render_sections": clean_sections,
        "peer_watchlist_filter": clean_filter,
        "custom_atoms": [],
    }

    if warnings:
        log.info(
            "validate_self_model_spec: %d cleanups (%s)",
            len(warnings),
            warnings[0] if len(warnings) == 1 else f"{warnings[0]}, ...",
        )

    return cleaned, warnings


__all__ = [
    "RoundCtx",
    "TradeResult",
    "StateAtomDef",
    "SelfModelSpec",
    "validate_self_model_spec",
]
