"""Persona dataclass + YAML loader.

A persona represents a class of market participants (not a single trader). At
sim time the sandbox spawns N stochastic agent instances per persona, each
sampled from the persona's `capital_distribution` and `initial_position_distribution`.
The LLM is called once per persona class per round to produce an action
distribution; the sandbox then samples N agents from that distribution and
aggregates the resulting order flow.

Each persona has:
    - identity (id, archetype, display_name, voice_prompt)
    - market_share (multi-dim weight: by_volume / by_holdings / by_account_count)
    - decision_mode + role (taxonomy across markets)
    - sandbox config (capital + holdings + risk + action_space distributions)
    - data source citations linking back to the file-level data_sources block

The schema is intentionally flat YAML so a future open-source persona pack
contributor can drop in a new yaml without learning the codebase.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = 3


# Phase I — what social role this persona plays in the information ecosystem.
# Drives the OASIS agent setup (which actions are available, whether the persona
# needs a sandbox config, etc.)
VALID_ENTITY_ROLES = {
    "trader",       # places orders, may also publish
    "media",        # news wires, financial press — publishes news_brief
    "analyst",      # sell-side or buy-side research — publishes research_note
    "regulator",    # CSRC etc — publishes regulatory_inquiry, rare
    "policy",       # central bank, NDRC, ministries — publishes policy_statement
    "kol",          # social media KOLs — publishes social_post frequently
    "news_wire",    # alias for `media`, kept for clarity in YAML
    "company_ir",   # corporate investor relations — publishes company_announcement
}


# Whitelist of allowed publication content types. New types added here also
# need a corresponding prompt template in the publishing component (Phase I plan I7).
VALID_CONTENT_TYPES = {
    "news_brief",
    "research_note",
    "policy_statement",
    "regulatory_inquiry",
    "social_post",
    "company_announcement",
}


class PersonaSchemaError(ValueError):
    """Raised when a persona YAML file is malformed."""


# ─────────────────────── Nested dataclasses ───────────────────────


@dataclass
class MarketShare:
    """Multi-dimensional weight for a persona class.

    Each dimension represents a different "what does this persona class
    contribute" axis. The aggregation engine picks one dimension per
    sim based on the question being asked:

      - by_volume: short-term event reaction (high-frequency classes
        dominate; quants and short-term retail get extra weight)
      - by_holdings: long-term anchor (large stable holders dominate;
        strategic capital and pension funds get extra weight)
      - by_account_count: democratic / sentiment-of-the-street (massive
        retail tail dominates; institutions barely register)

    At least one dimension MUST be set. `citations` is a list of
    {source_id, page, figure, note} dicts that point back to the
    file-level data_sources block.
    """

    by_volume: float | None = None
    by_holdings: float | None = None
    by_account_count: float | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)

    def get(self, dimension: str) -> float | None:
        """Look up a dimension by name. Returns None if dimension is unset."""
        return getattr(self, dimension, None)

    def has_any(self) -> bool:
        return any(
            v is not None
            for v in (self.by_volume, self.by_holdings, self.by_account_count)
        )


# Sub-population decision styles. MVP uses 5 stable archetypes.
# Fail HARD on unknown styles at YAML load time so the research layer
# can't silently drift — matches the plan-agent review #8 decision.
_VALID_DECISION_STYLES: frozenset[str] = frozenset({
    "momentum",
    "contrarian",
    "fundamental",
    "panic",
    "conviction",
})


@dataclass(frozen=True)
class SubPopulation:
    """One behavioral sub-population within a persona class.

    A persona's ``sub_populations`` list partitions its ``instance_count``
    agents into groups that respond to the same class-level LLM decision
    differently. Example: ``retail_short_term_chaser`` might split into
    40% momentum chasers (amplify bull signals) + 30% swing traders +
    20% meme speculators (panic on bad news) + 10% burnt veterans
    (contrarian, fade hype).

    The sub-pop mechanism adds STRUCTURAL heterogeneity beyond the
    existing Gaussian dispersion noise in ``apply_distribution_to_agent_pop``.
    Different sub-pops get different event-type conviction offsets via
    ``STYLE_TILT`` + ``event_conviction_offset``, so a class with mixed
    sub-pops produces a non-degenerate action histogram even when the
    LLM class decision is a single scalar.

    Fractions within a persona must sum to 1.0 (±1e-6 at load time).
    Unknown ``decision_style`` raises at load time. Everything else is
    optional and falls through to persona-class defaults.

    ``exit_rules`` is a Phase 2 placeholder — schema forward-compat for
    continuous target tracking ("hold until NAV +20%") — not evaluated
    by the runtime yet.
    """

    id: str                                # "momentum_chaser"
    label_zh: str                          # "动量追涨客"
    fraction: float                        # 0.0-1.0, sum to 1.0 per persona
    decision_style: str                    # one of _VALID_DECISION_STYLES
    rationale: str = ""                    # free text / research citation

    # Per-agent spawn-time correlations — applied inside spawn_agents
    capital_multiplier: float = 1.0        # multiplies sampled capital
    prob_holding_override: float | None = None  # override initial_position_distribution

    # Per-agent risk overrides — fall through to persona.sandbox.risk if None
    max_position_pct_override: float | None = None
    stop_loss_threshold_override: float | None = None

    # Merged OVER persona.biases at decision time
    bias_overrides: dict[str, float] = field(default_factory=dict)

    # Added ON TOP OF STYLE_TILT during conviction computation
    # Example: {"policy": +0.3, "regulatory": -0.1}
    event_conviction_offset: dict[str, float] = field(default_factory=dict)

    # Phase 2 placeholder — schema forward-compat only
    exit_rules: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SandboxConfig:
    """Per-persona sandbox configuration for agent-based market mode.

    The persona class spawns N agent instances at sim time. Each instance
    samples capital from `capital_distribution`, initial holdings from
    `initial_position_distribution`, applies `risk` constraints, and at
    each round executes one of the `action_space` entries (sampled from
    the LLM-produced action distribution for that class).

    Field shapes are loose dicts on purpose — the sandbox engine validates
    them at use time, not at YAML load time. This keeps the schema
    extensible (new distribution types, new action variants) without
    requiring code changes per yaml edit.

    capital_distribution example:
        {type: lognormal, median_cny: 120000, sigma: 0.8,
         floor_cny: 10000, ceiling_cny: 1000000}

    initial_position_distribution example:
        {type: bernoulli, prob_holding: 0.60,
         position_size_pct_when_holding: {type: uniform, min: 0.05, max: 0.30},
         avg_entry_price_offset: {type: normal, mean: -0.05, sigma: 0.15}}

    risk example:
        {max_position_pct: 0.95, margin_account_pct: 0.10,
         leverage_max: 2.0, stop_loss_threshold: -0.15,
         stop_loss_discipline: 0.30}

    action_space example (Gotcha 1 lock-in: dicts not bare strings):
        [{name: panic_sell_50pct, side: sell,
          pool: holdings_in_target, fraction: 0.5},
         {name: hold, side: none, pool: none, fraction: 0.0},
         {name: average_down_10pct, side: buy, pool: cash, fraction: 0.10}]

    reaction_lag_rounds example:
        {type: discrete, values: [0, 0, 0, 1, 1, 2]}
        # 50% of agents react in R0, 33% in R1, 17% in R2
    """

    instance_count: int
    capital_distribution: dict[str, Any]
    initial_position_distribution: dict[str, Any]
    risk: dict[str, Any]
    action_space: list[dict[str, Any]]
    reaction_lag_rounds: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishConfig:
    """Per-content-type publishing config for a persona.

    Phase I — drives the OASIS social action loop. A persona with one or more
    `PublishConfig` entries can produce that content type during the simulation;
    `trigger_prob` is the base probability of emitting per round (the LLM can
    still decide to skip), `authority_weight` is how much the published content
    dominates downstream readers' feeds (high authority = analyst note, low =
    KOL post). `style_hint` is a short phrase the prompt builder uses to shape
    the LLM voice (e.g., "中信建投风格, 严谨, 带 DCF").
    """

    content_type: str             # must be in VALID_CONTENT_TYPES
    style_hint: str = ""
    trigger_prob: float = 0.3
    authority_weight: float = 0.5
    max_length_chars: int = 240


@dataclass
class StrategicSignalSchema:
    """Schema for strategic personas (产业资本 / 政府队 / 个人大股东).

    Strategic personas are the largest holders by market cap (~48% in
    A-share) but contribute little to short-term volume. Aggregating
    their sentiment is meaningless — their "reaction" is action
    (减持窗口 / 质押 / 增持考虑) over months, not minutes.

    In sandbox mode they DO submit orders (rare large blocks) but they
    ALSO emit a parallel `strategic_signal` field that the report renders
    in a separate "战略层信号" section.
    """

    direction: list[str] = field(
        default_factory=lambda: ["reduce", "neutral", "accumulate", "defensive"]
    )
    magnitude: list[str] = field(default_factory=lambda: ["low", "medium", "high"])
    time_horizon_days: int = 180


@dataclass
class Persona:
    """A class of market participants.

    All fields after `voice_prompt` are technically optional in the dataclass
    constructor (so unit tests can build minimal Persona objects), but the
    YAML loader enforces a stricter requirement set: market_share + decision_mode
    + role must be present, and a sandbox block is required for sandbox-mode runs.
    """

    # Identity (always required)
    id: str
    archetype: str
    display_name: str
    voice_prompt: str

    # Model assignment (optional; None falls back to settings.default_model)
    model: str | None = None

    # Schema marker
    schema_version: int = SCHEMA_VERSION

    # Free-form metadata (optional dicts surfaced into the system prompt)
    biases: dict[str, float] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)

    # Market structure (required by YAML loader)
    sub_archetype: str | None = None
    market_share: MarketShare | None = None
    decision_mode: str | None = None  # discretionary | systematic | strategic | passive
    time_horizon_days: tuple[int, int] | None = None
    role: str | None = None  # directional_speculator | strategic_holder | ...
    capital_range_cny: tuple[int, int] | None = None
    behavior: dict[str, Any] | None = None
    information: dict[str, Any] | None = None

    # Sandbox config (required for sandbox-mode runs)
    sandbox: SandboxConfig | None = None
    strategic_signal_schema: StrategicSignalSchema | None = None

    # Aggregation flags (default = retail/institutional behavior)
    contributes_to_sentiment_mean: bool = True
    contributes_to_strategic_signal: bool = False

    # ─────────────────────── Phase I — information ecosystem ───────────────────────
    #
    # `entity_role` decides what kind of OASIS agent this persona becomes:
    #   - trader → has a sandbox config + places orders + can optionally publish
    #   - media / analyst / regulator / policy / kol / company_ir → no sandbox,
    #     never trades, only publishes content into the OASIS social stream
    entity_role: str = "trader"

    # Coarse-grained agent type for round schedule filtering.
    # Maps to round_schedule.active_agent_types entries.
    agent_type: str | None = None

    # ─────────────────────── Entity State Sandbox ───────────────────────
    #
    # Links this persona to an Entity in the EntityGraph. When set, the
    # persona's system prompt is augmented per-round with the entity's
    # current state ("处境"). Set by the sandbox generator at Setup time.
    entity_id: str | None = None

    # Who this persona regularly reads. List of persona ids in the same pack.
    # Special values:
    #   - "*"          → follows everyone in the pack (use sparingly: news wires)
    #   - "__market__" → follows the synthetic market-event broadcaster
    #                    (Phase I auto-adds this to all traders, no need to set)
    follows: list[str] = field(default_factory=list)

    # What this persona can publish, and how often. Empty list means
    # "doesn't publish" (typical for retail traders who only consume).
    publishes: list[PublishConfig] = field(default_factory=list)

    # ─────────────────────── Self-model spec ───────────────────────
    #
    # Optional per-persona self-model configuration consumed by the
    # ssflow.self_model subsystem. When None, the runtime applies
    # DEFAULT_SELF_MODEL_DICT (universal financial + trajectory +
    # emotional atoms). When a dict, it's validated + cleaned via
    # ``self_model.schema.validate_self_model_spec`` before use, so
    # unknown atom / component / section names get stripped rather
    # than crashing the sim.
    #
    # Dynamically generated by persona_factory Stage 3 for new packs;
    # existing personas in ashare.yaml leave this None and inherit
    # the default bundle.
    self_model: dict[str, Any] | None = None

    # ─────────────────────── Intra-class sub-populations ───────────────────────
    #
    # Optional list of ``SubPopulation`` that partitions this persona's
    # ``sandbox.instance_count`` agents into behavioral groups. When None,
    # all agents are homogeneous (backward-compatible default for every
    # existing hand-authored persona). When set, the YAML loader validates
    # that fractions sum to 1.0 at load time, ``spawn_agents`` assigns
    # each TraderInstance to a sub-pop via weighted sampling, and
    # ``apply_distribution_to_agent_pop`` applies per-agent event-type
    # conviction offsets keyed off ``decision_style`` + explicit overrides.
    #
    # See ``src/ssflow/sub_population_styles.py`` for the STYLE_TILT library
    # and ``tests/test_persona_sub_populations.py`` for schema-round-trip
    # regression coverage.
    sub_populations: list[SubPopulation] | None = None

    def system_prompt(self) -> str:
        """Render the persona as a system prompt for an LLM call."""
        bias_lines = (
            "\n".join(f"  - {k}: {v}" for k, v in self.biases.items())
            or "  (none specified)"
        )
        knowledge_lines = []
        for key in ("holdings", "information_sources", "ignores"):
            value = self.knowledge.get(key)
            if value:
                if isinstance(value, list):
                    knowledge_lines.append(f"  - {key}: {', '.join(map(str, value))}")
                else:
                    knowledge_lines.append(f"  - {key}: {value}")
        knowledge_block = "\n".join(knowledge_lines) or "  (none specified)"

        return (
            f"You are simulating a Chinese A-share market participant.\n"
            f"\n"
            f"# 身份 / Identity\n"
            f"  - id: {self.id}\n"
            f"  - 类型 (archetype): {self.archetype}\n"
            f"  - 画像 (profile): {self.display_name}\n"
            f"\n"
            f"# 行为偏差 / Behavioral biases\n"
            f"{bias_lines}\n"
            f"\n"
            f"# 知识与信息源 / Knowledge & sources\n"
            f"{knowledge_block}\n"
            f"\n"
            f"# 你的语气 / Your voice\n"
            f"{self.voice_prompt}\n"
            f"\n"
            f"# 重要规则 / Critical rules\n"
            f"  1. Stay strictly in character. Speak like {self.archetype}, not like a neutral analyst.\n"
            f"  2. NEVER output investment recommendations. Avoid these forbidden words in your\n"
            f"     comments (they will be regex-filtered): 建议, 推荐, 应该, 必须, 买入, 卖出,\n"
            f"     减仓, 加仓, 建仓, 清仓, 目标价, 评级, 止损位, 止盈位, BUY, SELL, target price.\n"
            f"  3. Use descriptive language instead. Say '我倾向 / 我看好 / 我担心 / 我会先观望'\n"
            f"     not '我建议你 / 应该买入'. Describe how YOU (this character) would react,\n"
            f"     not what the user should do.\n"
            f"  4. Be specific. Reference actual numbers, prices, or behaviors from the event.\n"
            f"  5. Disagree with other personas when your character would. Do not reach false consensus.\n"
        )


# ─────────────────────── YAML loader ───────────────────────


REQUIRED_PERSONA_FIELDS = {
    "id",
    "archetype",
    "display_name",
    "voice_prompt",
    "market_share",
    "decision_mode",
    "role",
}

VALID_DECISION_MODES = {"discretionary", "systematic", "strategic", "passive"}


def _coerce_market_share(data: dict[str, Any], persona_id: str) -> MarketShare:
    if not isinstance(data, dict):
        raise PersonaSchemaError(
            f"persona '{persona_id}': market_share must be a mapping"
        )
    ms = MarketShare(
        by_volume=_coerce_optional_float(data.get("by_volume"), persona_id, "by_volume"),
        by_holdings=_coerce_optional_float(data.get("by_holdings"), persona_id, "by_holdings"),
        by_account_count=_coerce_optional_float(
            data.get("by_account_count"), persona_id, "by_account_count"
        ),
        citations=data.get("citations", []) or [],
    )
    if not ms.has_any():
        raise PersonaSchemaError(
            f"persona '{persona_id}': market_share must set at least one dimension "
            f"(by_volume, by_holdings, or by_account_count)"
        )
    return ms


def _coerce_optional_float(
    val: Any, persona_id: str, field_name: str
) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise PersonaSchemaError(
            f"persona '{persona_id}': {field_name} must be a number, got {val!r}"
        ) from exc


def _coerce_publishes(data: Any, persona_id: str) -> list[PublishConfig]:
    """Parse the persona's `publishes` list into PublishConfig dataclasses.

    Tolerates the field being missing entirely (returns empty list).
    Raises PersonaSchemaError on malformed entries.
    """
    if data is None:
        return []
    if not isinstance(data, list):
        raise PersonaSchemaError(
            f"persona '{persona_id}': publishes must be a list of mappings"
        )
    out: list[PublishConfig] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise PersonaSchemaError(
                f"persona '{persona_id}': publishes[{i}] must be a mapping"
            )
        ctype = entry.get("content_type")
        if ctype not in VALID_CONTENT_TYPES:
            raise PersonaSchemaError(
                f"persona '{persona_id}': publishes[{i}].content_type must be one of "
                f"{sorted(VALID_CONTENT_TYPES)}, got {ctype!r}"
            )
        out.append(
            PublishConfig(
                content_type=ctype,
                style_hint=str(entry.get("style_hint", "")),
                trigger_prob=float(entry.get("trigger_prob", 0.3)),
                authority_weight=float(entry.get("authority_weight", 0.5)),
                max_length_chars=int(entry.get("max_length_chars", 240)),
            )
        )
    return out


def _coerce_follows(data: Any, persona_id: str) -> list[str]:
    """Parse the persona's `follows` list. Just a list of string ids."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise PersonaSchemaError(
            f"persona '{persona_id}': follows must be a list of persona ids"
        )
    return [str(x) for x in data]


def _validate_follows_references(personas: list["Persona"], source: str) -> None:
    """After loading all personas, verify every `follows` entry references a known id.

    Allows two special values: "*" (follows all) and "__market__" (auto-added).
    Anything else must be an existing persona id in the same pack.
    """
    known_ids = {p.id for p in personas}
    for p in personas:
        for ref in p.follows:
            if ref in ("*", "__market__"):
                continue
            if ref not in known_ids:
                raise PersonaSchemaError(
                    f"{source}: persona '{p.id}' follows unknown id '{ref}'. "
                    f"Known ids: {sorted(known_ids)}"
                )


def _coerce_sub_populations(
    data: Any,
    persona_id: str,
) -> list[SubPopulation] | None:
    """Parse the optional ``sub_populations:`` YAML block into a list of
    ``SubPopulation`` objects.

    Returns ``None`` when the field is absent (homogeneous persona —
    backward-compatible default). Raises :class:`PersonaSchemaError` on:

    - Non-list top-level
    - Entry missing required fields (``id`` / ``label_zh`` / ``fraction`` /
      ``decision_style``)
    - Unknown ``decision_style`` — fail-hard so the research layer can't
      silently drift
    - Fractions not summing to 1.0 ± 1e-6 — fail at LOAD TIME, not spawn
      time, to avoid 10-minute backtests crashing mid-sim
    - Duplicate sub-pop ids within the same persona

    All trait overrides (``bias_overrides``, ``risk_overrides``,
    ``event_conviction_offset``, etc.) are optional and fall through to
    persona-class defaults when absent.
    """
    if data is None:
        return None
    if not isinstance(data, list):
        raise PersonaSchemaError(
            f"persona '{persona_id}': sub_populations must be a list of mappings, "
            f"got {type(data).__name__}"
        )
    if not data:
        return None  # explicit empty list == no sub-pops

    result: list[SubPopulation] = []
    seen_ids: set[str] = set()
    required = {"id", "label_zh", "fraction", "decision_style"}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] must be a mapping"
            )
        missing = required - entry.keys()
        if missing:
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] missing required "
                f"fields: {sorted(missing)}"
            )

        sp_id = str(entry["id"])
        if sp_id in seen_ids:
            raise PersonaSchemaError(
                f"persona '{persona_id}': duplicate sub_population id '{sp_id}'"
            )
        seen_ids.add(sp_id)

        style = str(entry["decision_style"])
        if style not in _VALID_DECISION_STYLES:
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' has "
                f"unknown decision_style '{style}'. Valid: "
                f"{sorted(_VALID_DECISION_STYLES)}"
            )

        try:
            fraction = float(entry["fraction"])
        except (TypeError, ValueError) as exc:
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' has "
                f"non-numeric fraction {entry['fraction']!r}"
            ) from exc
        if not 0.0 <= fraction <= 1.0:
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' fraction "
                f"{fraction} is outside [0, 1]"
            )

        def _maybe_float(val: Any, field_name: str) -> float | None:
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError) as exc:
                raise PersonaSchemaError(
                    f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' "
                    f"{field_name} must be numeric, got {val!r}"
                ) from exc

        def _coerce_float_dict(val: Any, field_name: str) -> dict[str, float]:
            if val is None:
                return {}
            if not isinstance(val, dict):
                raise PersonaSchemaError(
                    f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' "
                    f"{field_name} must be a dict, got {type(val).__name__}"
                )
            out: dict[str, float] = {}
            for k, v in val.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError) as exc:
                    raise PersonaSchemaError(
                        f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' "
                        f"{field_name}[{k}] must be numeric, got {v!r}"
                    ) from exc
            return out

        exit_rules_raw = entry.get("exit_rules", [])
        if exit_rules_raw and not isinstance(exit_rules_raw, list):
            raise PersonaSchemaError(
                f"persona '{persona_id}': sub_populations[{i}] '{sp_id}' "
                f"exit_rules must be a list, got {type(exit_rules_raw).__name__}"
            )

        result.append(
            SubPopulation(
                id=sp_id,
                label_zh=str(entry["label_zh"]),
                fraction=fraction,
                decision_style=style,
                rationale=str(entry.get("rationale", "")),
                capital_multiplier=_maybe_float(
                    entry.get("capital_multiplier", 1.0), "capital_multiplier"
                ) or 1.0,
                prob_holding_override=_maybe_float(
                    entry.get("prob_holding_override"), "prob_holding_override"
                ),
                max_position_pct_override=_maybe_float(
                    entry.get("max_position_pct_override"),
                    "max_position_pct_override",
                ),
                stop_loss_threshold_override=_maybe_float(
                    entry.get("stop_loss_threshold_override"),
                    "stop_loss_threshold_override",
                ),
                bias_overrides=_coerce_float_dict(
                    entry.get("bias_overrides"), "bias_overrides"
                ),
                event_conviction_offset=_coerce_float_dict(
                    entry.get("event_conviction_offset"),
                    "event_conviction_offset",
                ),
                exit_rules=list(exit_rules_raw or []),
            )
        )

    # Load-time fraction sum validation — fail FAST, not at spawn time.
    total = sum(sp.fraction for sp in result)
    if abs(total - 1.0) > 1e-6:
        raise PersonaSchemaError(
            f"persona '{persona_id}': sub_populations fractions sum to "
            f"{total:.6f}, expected 1.0 ± 1e-6. Check per-entry values: "
            f"{[(sp.id, sp.fraction) for sp in result]}"
        )

    return result


def _coerce_sandbox(data: dict[str, Any], persona_id: str) -> SandboxConfig | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise PersonaSchemaError(
            f"persona '{persona_id}': sandbox must be a mapping"
        )
    required = {
        "instance_count",
        "capital_distribution",
        "initial_position_distribution",
        "risk",
        "action_space",
    }
    missing = required - data.keys()
    if missing:
        raise PersonaSchemaError(
            f"persona '{persona_id}': sandbox missing required fields: {sorted(missing)}"
        )
    if not isinstance(data["action_space"], list) or not data["action_space"]:
        raise PersonaSchemaError(
            f"persona '{persona_id}': sandbox.action_space must be a non-empty list"
        )
    for i, action in enumerate(data["action_space"]):
        if not isinstance(action, dict) or "name" not in action:
            raise PersonaSchemaError(
                f"persona '{persona_id}': sandbox.action_space[{i}] must be a "
                f"dict with at least a 'name' field"
            )
    return SandboxConfig(
        instance_count=int(data["instance_count"]),
        capital_distribution=dict(data["capital_distribution"]),
        initial_position_distribution=dict(data["initial_position_distribution"]),
        risk=dict(data["risk"]),
        action_space=[dict(a) for a in data["action_space"]],
        reaction_lag_rounds=dict(data.get("reaction_lag_rounds", {})),
    )


def _coerce_tuple_pair(
    val: Any, persona_id: str, field_name: str
) -> tuple[int, int] | None:
    if val is None:
        return None
    if not (isinstance(val, (list, tuple)) and len(val) == 2):
        raise PersonaSchemaError(
            f"persona '{persona_id}': {field_name} must be a [min, max] pair, got {val!r}"
        )
    try:
        return (int(val[0]), int(val[1]))
    except (TypeError, ValueError) as exc:
        raise PersonaSchemaError(
            f"persona '{persona_id}': {field_name} pair must be numeric, got {val!r}"
        ) from exc


_SUBARCH_TO_AGENT_TYPE: dict[str, str] = {
    "retail_active": "retail",
    "retail_passive": "retail",
    "retail_pro_am": "retail",
    "short_term_momentum": "retail",
    "institution_long_only": "institutional",
    "institution_passive": "institutional",
    "institution_active": "institutional",
    "institution_long_horizon": "institutional",
    "foreign_long_short": "institutional",
    "quant": "institutional",
    "strategic_industrial": "strategic",
    "strategic_cross_holding": "strategic",
    "strategic_government": "strategic",
    "strategic_national_team": "strategic",
    "strategic_individual": "strategic",
    "news_wire": "news_wire",
    "news_wire_state": "news_wire",
    "news_wire_foreign": "news_wire",
    "news_wire_mainstream": "news_wire",
    "sellside_research_conservative": "analyst",
    "sellside_research_contrarian": "analyst",
    "sellside_research_growth": "analyst",
    "sellside_research_bearish": "analyst",
    "regulator_securities": "regulator",
    "policy_central_bank": "policy",
    "policy_industrial": "policy",
    "policy_industry_ministry": "policy",
    "retail_kol_momentum": "kol",
    "retail_kol_broad": "kol",
    "retail_kol_longform": "kol",
    "corporate_ir": "company_ir",
    "industry_association": "media",
}


def _infer_agent_type(sub_archetype: str | None, entity_role: str) -> str:
    """Derive a coarse agent_type from sub_archetype or entity_role.

    The returned value matches round_schedule.active_agent_types categories:
    retail, institutional, strategic, kol, analyst, media, news_wire,
    regulator, policy, company_ir.
    """
    if sub_archetype and sub_archetype in _SUBARCH_TO_AGENT_TYPE:
        return _SUBARCH_TO_AGENT_TYPE[sub_archetype]
    if entity_role in ("media", "analyst", "kol", "news_wire", "regulator",
                       "policy", "company_ir"):
        return entity_role
    return "retail"


def _validate_persona_dict(data: dict[str, Any], idx: int, source: str) -> None:
    pid = data.get("id", f"#{idx}")
    missing = REQUIRED_PERSONA_FIELDS - data.keys()
    if missing:
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source} missing required fields: {sorted(missing)}"
        )
    if not isinstance(data.get("biases", {}), dict):
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: biases must be a dict"
        )
    if not isinstance(data.get("knowledge", {}), dict):
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: knowledge must be a dict"
        )
    decision_mode = data.get("decision_mode")
    if decision_mode not in VALID_DECISION_MODES:
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: decision_mode must be one of "
            f"{sorted(VALID_DECISION_MODES)}, got {decision_mode!r}"
        )
    # Phase I — entity_role validation
    entity_role = data.get("entity_role", "trader")
    if entity_role not in VALID_ENTITY_ROLES:
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: entity_role must be one of "
            f"{sorted(VALID_ENTITY_ROLES)}, got {entity_role!r}"
        )
    # Trader requires sandbox; non-trader must NOT have sandbox
    has_sandbox = data.get("sandbox") is not None
    if entity_role == "trader" and not has_sandbox:
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: entity_role=trader requires a sandbox block"
        )
    if entity_role != "trader" and has_sandbox:
        raise PersonaSchemaError(
            f"Persona '{pid}' in {source}: entity_role={entity_role} must NOT have "
            f"a sandbox block (non-traders don't place orders)"
        )


def load_personas(path: str | Path) -> list[Persona]:
    """Load + validate persona pack YAML.

    Expected file structure:

        schema_version: 3
        market: ashare
        last_updated: 2026-04-08
        data_sources:
          - id: cdc-2025
            name: 中登公司投资者数量月报 2025-12
            url: https://...
            accessed: 2026-04-08
        personas:
          - id: retail_short_term_chaser
            archetype: retail_active
            sub_archetype: short_term_momentum
            display_name: 短线追涨散户 (5-30 万, 25-40 岁)
            decision_mode: discretionary
            role: directional_speculator
            market_share:
              by_volume: 0.30
              by_holdings: 0.10
              citations:
                - source_id: cdc-2025
            voice_prompt: |
              ...
            sandbox:
              instance_count: 10000
              capital_distribution: {type: lognormal, median_cny: 120000, sigma: 0.8}
              initial_position_distribution: {type: bernoulli, prob_holding: 0.60}
              risk: {max_position_pct: 0.95}
              action_space:
                - {name: hold, side: none, pool: none, fraction: 0.0}
                - {name: panic_sell_50pct, side: sell, pool: holdings_in_target, fraction: 0.5}
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Persona file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PersonaSchemaError(f"{path}: top-level must be a YAML mapping")

    file_schema = raw.get("schema_version")
    if file_schema != SCHEMA_VERSION:
        raise PersonaSchemaError(
            f"{path}: schema_version {file_schema} != expected {SCHEMA_VERSION}. "
            f"v1/v2 are no longer supported; rewrite the pack as schema_version "
            f"{SCHEMA_VERSION}."
        )

    personas_raw = raw.get("personas")
    if not isinstance(personas_raw, list) or not personas_raw:
        raise PersonaSchemaError(f"{path}: 'personas' must be a non-empty list")

    personas: list[Persona] = []
    seen_ids: set[str] = set()
    for i, p in enumerate(personas_raw):
        if not isinstance(p, dict):
            raise PersonaSchemaError(f"Persona #{i} in {path} is not a mapping")
        _validate_persona_dict(p, i, str(path))
        if p["id"] in seen_ids:
            raise PersonaSchemaError(f"{path}: duplicate persona id '{p['id']}'")
        seen_ids.add(p["id"])

        market_share = _coerce_market_share(p["market_share"], p["id"])
        sandbox = _coerce_sandbox(p.get("sandbox"), p["id"])
        time_horizon_days = _coerce_tuple_pair(
            p.get("time_horizon_days"), p["id"], "time_horizon_days"
        )
        capital_range_cny = _coerce_tuple_pair(
            p.get("capital_range_cny"), p["id"], "capital_range_cny"
        )

        decision_mode = p["decision_mode"]
        # Strategic personas default to NOT contributing to sentiment_mean
        # and DO contribute to strategic_signal, unless explicitly overridden.
        is_strategic = decision_mode == "strategic"
        contributes_to_sentiment_mean = bool(
            p.get("contributes_to_sentiment_mean", not is_strategic)
        )
        contributes_to_strategic_signal = bool(
            p.get("contributes_to_strategic_signal", is_strategic)
        )

        strategic_signal_schema = None
        sss_raw = p.get("strategic_signal_schema")
        if sss_raw is not None:
            strategic_signal_schema = StrategicSignalSchema(
                direction=list(sss_raw.get("direction", []))
                or StrategicSignalSchema().direction,
                magnitude=list(sss_raw.get("magnitude", []))
                or StrategicSignalSchema().magnitude,
                time_horizon_days=int(sss_raw.get("time_horizon_days", 180)),
            )
        elif is_strategic:
            strategic_signal_schema = StrategicSignalSchema()

        personas.append(
            Persona(
                id=p["id"],
                archetype=p["archetype"],
                display_name=p["display_name"],
                voice_prompt=p["voice_prompt"].strip(),
                model=p.get("model"),
                schema_version=file_schema,
                biases=p.get("biases", {}),
                knowledge=p.get("knowledge", {}),
                sub_archetype=p.get("sub_archetype"),
                market_share=market_share,
                decision_mode=decision_mode,
                time_horizon_days=time_horizon_days,
                role=p["role"],
                capital_range_cny=capital_range_cny,
                behavior=p.get("behavior"),
                information=p.get("information"),
                sandbox=sandbox,
                strategic_signal_schema=strategic_signal_schema,
                contributes_to_sentiment_mean=contributes_to_sentiment_mean,
                contributes_to_strategic_signal=contributes_to_strategic_signal,
                entity_role=p.get("entity_role", "trader"),
                agent_type=p.get("agent_type") or _infer_agent_type(
                    p.get("sub_archetype"), p.get("entity_role", "trader"),
                ),
                follows=_coerce_follows(p.get("follows"), p["id"]),
                publishes=_coerce_publishes(p.get("publishes"), p["id"]),
                self_model=p.get("self_model"),  # dict or None; validated at runtime
                sub_populations=_coerce_sub_populations(
                    p.get("sub_populations"), p["id"],
                ),
            )
        )

    # Phase I — second-pass validation now that we know all persona ids
    _validate_follows_references(personas, str(path))

    return personas


def persona_set_hash(personas: list[Persona]) -> str:
    """Stable hash of a persona set, for reproducibility tracking in scorecard.

    Hash inputs cover all fields that affect simulation behavior:
    id, model, voice_prompt, market_share, sandbox config (instance_count,
    action_space, risk), sub_populations, and follows.
    """
    h = hashlib.sha256()
    for p in sorted(personas, key=lambda x: x.id):
        h.update(p.id.encode())
        h.update((p.model or "").encode())
        h.update(p.voice_prompt.encode())
        if p.market_share is not None:
            for dim in ("by_volume", "by_holdings", "by_account_count"):
                val = p.market_share.get(dim)
                h.update(f"{dim}={val}".encode())
        if p.sandbox:
            h.update(f"instance_count={p.sandbox.instance_count}".encode())
            for action in sorted(p.sandbox.action_space, key=lambda a: a.get("id", a.get("name", ""))):
                h.update(f"action={action.get('id', action.get('name', ''))}".encode())
            if p.sandbox.risk:
                h.update(f"stop_loss={p.sandbox.risk.get('stop_loss_threshold')}".encode())
                h.update(f"profit_take={p.sandbox.risk.get('profit_take_threshold')}".encode())
        if p.sub_populations:
            for sp in sorted(p.sub_populations, key=lambda x: x.id):
                h.update(f"subpop={sp.id}:{sp.fraction}:{sp.decision_style}".encode())
        if p.follows:
            for f in sorted(p.follows):
                h.update(f"follows={f}".encode())
    return h.hexdigest()[:16]


__all__ = [
    "Persona",
    "PersonaSchemaError",
    "MarketShare",
    "PublishConfig",
    "SandboxConfig",
    "StrategicSignalSchema",
    "SubPopulation",
    "SCHEMA_VERSION",
    "VALID_CONTENT_TYPES",
    "VALID_DECISION_MODES",
    "VALID_ENTITY_ROLES",
    "load_personas",
    "persona_set_hash",
]
