"""Tests for persona schema validation and YAML loading (schema v2)."""

from __future__ import annotations

import pytest

from ssflow.persona import (
    MarketShare,
    Persona,
    PersonaSchemaError,
    SCHEMA_VERSION,
    SandboxConfig,
    StrategicSignalSchema,
    load_personas,
    persona_set_hash,
)


def test_load_valid_personas(sample_personas_yaml) -> None:
    personas = load_personas(sample_personas_yaml)
    assert len(personas) == 2
    assert all(isinstance(p, Persona) for p in personas)
    assert personas[0].id == "test_aunt"
    assert personas[0].archetype == "散户大妈"
    assert personas[0].sub_archetype == "retail_passive"
    assert personas[0].model == "gpt-4o-mini"
    assert personas[0].decision_mode == "discretionary"
    assert personas[0].role == "directional_speculator"
    assert personas[0].biases.get("loss_averse") == 0.8
    assert "银行股" in personas[0].knowledge.get("holdings", [])

    # Market share parsing
    assert isinstance(personas[0].market_share, MarketShare)
    assert personas[0].market_share.by_volume == pytest.approx(0.10)
    assert personas[0].market_share.by_holdings == pytest.approx(0.08)
    assert personas[0].market_share.by_account_count == pytest.approx(0.20)
    assert personas[0].market_share.citations[0]["source_id"] == "test-source"

    # Sandbox config parsing
    assert isinstance(personas[0].sandbox, SandboxConfig)
    assert personas[0].sandbox.instance_count == 1000
    assert personas[0].sandbox.capital_distribution["type"] == "lognormal"
    assert personas[0].sandbox.capital_distribution["median_cny"] == 200000
    assert len(personas[0].sandbox.action_space) == 2
    assert personas[0].sandbox.action_space[0]["name"] == "hold"

    # Second persona
    assert personas[1].id == "test_youzi"
    assert personas[1].market_share.by_volume == pytest.approx(0.18)

    # Default aggregation flags for non-strategic personas
    assert personas[0].contributes_to_sentiment_mean is True
    assert personas[0].contributes_to_strategic_signal is False


def test_persona_system_prompt_includes_critical_rules(sample_personas_yaml) -> None:
    personas = load_personas(sample_personas_yaml)
    prompt = personas[0].system_prompt()
    # Must reference the persona's identity
    assert "散户大妈" in prompt
    assert "test_aunt" in prompt
    # Must contain compliance rules
    assert "NEVER output investment recommendations" in prompt
    assert "investment" in prompt.lower()
    # Must include behavioral biases
    assert "loss_averse" in prompt
    # Must include voice
    assert "套牢" in prompt or "涨" in prompt


def test_load_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_personas(tmp_path / "nonexistent.yaml")


def test_load_malformed_top_level(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- just a list\n- not a mapping")
    with pytest.raises(PersonaSchemaError, match="top-level"):
        load_personas(p)


def test_load_missing_personas_key(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 3\nother_key: value")
    with pytest.raises(PersonaSchemaError, match="personas"):
        load_personas(p)


def test_load_missing_required_field(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: incomplete
    archetype: 散户
    # missing display_name, voice_prompt, market_share, decision_mode, role
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="missing required fields"):
        load_personas(p)


def test_load_missing_market_share_dimension(tmp_path) -> None:
    """market_share must set at least one dimension."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: empty_share
    archetype: A
    display_name: a
    voice_prompt: x
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      citations: []
    sandbox:
      instance_count: 1
      capital_distribution: {type: fixed, value_cny: 100}
      initial_position_distribution: {type: none}
      risk: {max_position_pct: 0.5}
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="at least one dimension"):
        load_personas(p)


def test_load_invalid_decision_mode(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: bad_mode
    archetype: A
    display_name: a
    voice_prompt: x
    decision_mode: chaotic
    role: directional_speculator
    market_share:
      by_volume: 0.1
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="decision_mode"):
        load_personas(p)


def test_load_duplicate_ids(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: dup
    archetype: A
    display_name: a
    voice_prompt: x
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      by_volume: 0.5
  - id: dup
    archetype: B
    display_name: b
    voice_prompt: y
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      by_volume: 0.5
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="duplicate"):
        load_personas(p)


def test_load_wrong_schema_version(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 999
personas: []
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="schema_version"):
        load_personas(p)


def test_load_v1_rejected(tmp_path) -> None:
    """Schema v1 is no longer supported — must raise."""
    p = tmp_path / "v1.yaml"
    p.write_text(
        """
schema_version: 1
personas:
  - id: x
    archetype: A
    display_name: a
    model: gpt-4o-mini
    voice_prompt: x
    weight: 1.0
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="schema_version"):
        load_personas(p)


def test_strategic_persona_default_aggregation_flags(tmp_path) -> None:
    """Personas with decision_mode=strategic default to NOT contributing to
    sentiment_mean and DO contributing to strategic_signal."""
    p = tmp_path / "strategic.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: strat_holder
    archetype: 大股东
    display_name: 控股股东方
    voice_prompt: 我代表控股股东方
    decision_mode: strategic
    role: strategic_holder
    market_share:
      by_holdings: 0.30
    sandbox:
      instance_count: 1
      capital_distribution: {type: fixed, value_cny: 1000000000}
      initial_position_distribution: {type: fixed, value: 0.8}
      risk: {max_position_pct: 1.0}
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
""".strip()
    )
    personas = load_personas(p)
    assert personas[0].contributes_to_sentiment_mean is False
    assert personas[0].contributes_to_strategic_signal is True
    # Strategic personas auto-get a default StrategicSignalSchema
    assert isinstance(personas[0].strategic_signal_schema, StrategicSignalSchema)


def test_persona_set_hash_is_stable(sample_personas_yaml) -> None:
    personas1 = load_personas(sample_personas_yaml)
    personas2 = load_personas(sample_personas_yaml)
    assert persona_set_hash(personas1) == persona_set_hash(personas2)


def test_persona_set_hash_changes_on_modification(sample_personas_yaml) -> None:
    personas = load_personas(sample_personas_yaml)
    h1 = persona_set_hash(personas)
    personas[0].voice_prompt = personas[0].voice_prompt + " modified"
    h2 = persona_set_hash(personas)
    assert h1 != h2


def test_persona_set_hash_changes_on_market_share_modification(sample_personas_yaml) -> None:
    personas = load_personas(sample_personas_yaml)
    h1 = persona_set_hash(personas)
    personas[0].market_share.by_volume = (personas[0].market_share.by_volume or 0) + 0.05
    h2 = persona_set_hash(personas)
    assert h1 != h2


# ─────────────────────── Phase I — schema v3 tests ───────────────────────


def test_load_pure_information_entity_no_sandbox(tmp_path) -> None:
    """A non-trader (analyst, media, etc) loads without a sandbox block."""
    p = tmp_path / "info_entity.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: news_wire_test
    archetype: 财经新闻媒体
    display_name: 财联社快讯
    voice_prompt: 财联社快讯口吻, 客观陈述
    decision_mode: discretionary
    role: information_intermediary
    entity_role: media
    market_share:
      by_volume: 0.0
      citations: []
    publishes:
      - content_type: news_brief
        style_hint: "客观, 简短, 数字优先"
        trigger_prob: 0.5
        authority_weight: 0.7
""".strip()
    )
    personas = load_personas(p)
    assert personas[0].entity_role == "media"
    assert personas[0].sandbox is None
    assert len(personas[0].publishes) == 1
    assert personas[0].publishes[0].content_type == "news_brief"
    assert personas[0].publishes[0].trigger_prob == 0.5
    assert personas[0].publishes[0].authority_weight == 0.7


def test_trader_without_sandbox_rejected(tmp_path) -> None:
    """entity_role=trader (default) requires a sandbox block."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: bad_trader
    archetype: 散户
    display_name: 散户测试
    voice_prompt: x
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      by_volume: 0.1
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="entity_role=trader requires a sandbox"):
        load_personas(p)


def test_non_trader_with_sandbox_rejected(tmp_path) -> None:
    """A media / analyst persona must NOT carry a sandbox block."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: bad_media
    archetype: 财经媒体
    display_name: 财经媒体测试
    voice_prompt: x
    decision_mode: discretionary
    role: information_intermediary
    entity_role: media
    market_share:
      by_volume: 0.0
    sandbox:
      instance_count: 1
      capital_distribution: {type: fixed, value_cny: 100}
      initial_position_distribution: {type: none}
      risk: {max_position_pct: 0.5}
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="must NOT have a sandbox"):
        load_personas(p)


def test_invalid_entity_role_rejected(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: x
    archetype: A
    display_name: a
    voice_prompt: x
    decision_mode: discretionary
    role: r
    entity_role: cyborg
    market_share:
      by_volume: 0.1
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="entity_role"):
        load_personas(p)


def test_invalid_content_type_rejected(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: x
    archetype: 测试
    display_name: 测试
    voice_prompt: x
    decision_mode: discretionary
    role: information_intermediary
    entity_role: media
    market_share:
      by_volume: 0.0
    publishes:
      - content_type: tweet_storm
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="content_type"):
        load_personas(p)


def test_follows_unknown_id_rejected(tmp_path) -> None:
    """A persona that follows an id not in the same pack must fail loading."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: trader_a
    archetype: 散户
    display_name: 散户A
    voice_prompt: x
    decision_mode: discretionary
    role: directional_speculator
    follows: [nonexistent_oracle]
    market_share:
      by_volume: 0.1
    sandbox:
      instance_count: 1
      capital_distribution: {type: fixed, value_cny: 100}
      initial_position_distribution: {type: none}
      risk: {max_position_pct: 0.5}
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="follows unknown id"):
        load_personas(p)


def test_follows_special_values_accepted(tmp_path) -> None:
    """`*` and `__market__` are special and don't need to exist as personas."""
    p = tmp_path / "ok.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: news_wire
    archetype: 财经媒体
    display_name: 财经媒体
    voice_prompt: x
    decision_mode: discretionary
    role: information_intermediary
    entity_role: media
    follows: ["*", "__market__"]
    market_share:
      by_volume: 0.0
""".strip()
    )
    personas = load_personas(p)
    assert personas[0].follows == ["*", "__market__"]


def test_follows_known_id_accepted(tmp_path) -> None:
    """A persona can follow another persona in the same pack."""
    p = tmp_path / "ok.yaml"
    p.write_text(
        """
schema_version: 3
personas:
  - id: source
    archetype: 财经媒体
    display_name: 财经媒体
    voice_prompt: x
    decision_mode: discretionary
    role: information_intermediary
    entity_role: media
    market_share:
      by_volume: 0.0
  - id: reader
    archetype: 散户
    display_name: 散户
    voice_prompt: x
    decision_mode: discretionary
    role: directional_speculator
    follows: [source]
    market_share:
      by_volume: 0.1
    sandbox:
      instance_count: 1
      capital_distribution: {type: fixed, value_cny: 100}
      initial_position_distribution: {type: none}
      risk: {max_position_pct: 0.5}
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
""".strip()
    )
    personas = load_personas(p)
    assert personas[1].follows == ["source"]
