"""Tests for persona schema validation and YAML loading."""

from __future__ import annotations

import pytest

from ssfish.persona import (
    Persona,
    PersonaSchemaError,
    SCHEMA_VERSION,
    load_personas,
    persona_set_hash,
)


def test_load_valid_personas(sample_personas_yaml) -> None:
    personas = load_personas(sample_personas_yaml)
    assert len(personas) == 2
    assert all(isinstance(p, Persona) for p in personas)
    assert personas[0].id == "test_aunt"
    assert personas[0].archetype == "散户大妈"
    assert personas[0].model == "gpt-4o-mini"
    assert personas[0].biases.get("loss_averse") == 0.8
    assert "银行股" in personas[0].knowledge.get("holdings", [])
    assert personas[1].id == "test_youzi"
    assert personas[1].weight == 1.2


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
    p.write_text("schema_version: 1\nother_key: value")
    with pytest.raises(PersonaSchemaError, match="personas"):
        load_personas(p)


def test_load_missing_required_field(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 1
personas:
  - id: incomplete
    archetype: 散户
    # missing display_name, model, voice_prompt
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="missing required fields"):
        load_personas(p)


def test_load_duplicate_ids(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 1
personas:
  - id: dup
    archetype: A
    display_name: a
    model: gpt-4o-mini
    voice_prompt: x
  - id: dup
    archetype: B
    display_name: b
    model: gpt-4o-mini
    voice_prompt: y
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="duplicate"):
        load_personas(p)


def test_load_wrong_schema_version(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
schema_version: 999
personas:
  - id: x
    archetype: A
    display_name: a
    model: gpt-4o-mini
    voice_prompt: x
""".strip()
    )
    with pytest.raises(PersonaSchemaError, match="schema_version"):
        load_personas(p)


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


def test_real_personas_yaml_loads_when_present() -> None:
    """If personas/ashare-v1.yaml exists in the repo, it must be loadable."""
    from pathlib import Path
    real = Path(__file__).resolve().parents[1] / "personas" / "ashare-v1.yaml"
    if not real.exists():
        pytest.skip("personas/ashare-v1.yaml not yet authored")
    personas = load_personas(real)
    assert len(personas) >= 2, "Real persona file must have at least 2 personas"
    ids = {p.id for p in personas}
    assert len(ids) == len(personas), "Real persona file has duplicate IDs"
