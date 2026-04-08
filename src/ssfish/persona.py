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


SCHEMA_VERSION = 2


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


def load_personas(path: str | Path) -> list[Persona]:
    """Load + validate persona pack YAML.

    Expected file structure:

        schema_version: 2
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
            f"v1 is no longer supported; rewrite the pack as schema_version 2."
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
            )
        )

    return personas


def persona_set_hash(personas: list[Persona]) -> str:
    """Stable hash of a persona set, for reproducibility tracking in scorecard.

    Hash inputs: id + model + voice_prompt + market_share. Other fields are
    excluded so a v2 schema bump that adds metadata fields doesn't invalidate
    the hash chain.
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
    return h.hexdigest()[:16]


__all__ = [
    "Persona",
    "PersonaSchemaError",
    "MarketShare",
    "SandboxConfig",
    "StrategicSignalSchema",
    "SCHEMA_VERSION",
    "VALID_DECISION_MODES",
    "load_personas",
    "persona_set_hash",
]
