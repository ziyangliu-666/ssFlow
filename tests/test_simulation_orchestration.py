"""Orchestration tests for simulation.py — the _coerce_reactions alignment logic.

This test file is the retrospective fix for the Eng phase's #1 critical gap:
both Codex and the Claude subagent independently found that `_coerce_reactions`
had 6 silent failure modes and zero tests. The prior version could:

  1. Silently reuse an id-matched row via positional fallback (attribution bug)
  2. Drop personas when the LLM returned fewer rows than personas
  3. Accept hallucinated persona_ids as matches
  4. Return all-neutral on a `reactions: {..}` dict instead of a list
  5. Return all-neutral on a missing `reactions` key
  6. Crash the whole round on a malformed `sentiment` field type

These tests lock in the retrospective refactor: match-by-id-first, positional
fallback only consumes unmatched items, each unmatched item consumed at most
once, neutral fallback for truly missing personas, and structured logging
for every degraded-alignment path.

Also includes one end-to-end `run_simulation` test with a fake `chat_json`
so the batched-output orchestration path gets real branch coverage (was ~5%
per the retrospective's coverage table).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ssfish.event import Event
from ssfish.persona import MarketShare, Persona
from ssfish.simulation import (
    Reaction,
    SimulationResult,
    _coerce_reactions,
    _neutral_reaction,
    _normalize_items,
    run_simulation,
)


# ─────────────────────── Fixtures ───────────────────────


def _make_persona(pid: str, archetype: str = "test") -> Persona:
    return Persona(
        id=pid,
        archetype=archetype,
        display_name=f"{pid} display",
        voice_prompt="short test prompt",
        model="gpt-4o-mini",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.1),
    )


@pytest.fixture
def three_personas() -> list[Persona]:
    return [
        _make_persona("aunt", "散户大妈"),
        _make_persona("youzi", "短线游资"),
        _make_persona("quant", "量化机器人"),
    ]


def _reaction_dict(
    persona_id: str,
    sentiment: float = 0.0,
    intent: str = "observe",
    comment: str = "x",
    confidence: float = 0.5,
) -> dict[str, Any]:
    return {
        "persona_id": persona_id,
        "sentiment": sentiment,
        "action_intent": intent,
        "comment": comment,
        "confidence": confidence,
    }


# ─────────────────────── _normalize_items ───────────────────────


def test_normalize_items_from_reactions_key() -> None:
    parsed = {"reactions": [{"persona_id": "a"}, {"persona_id": "b"}]}
    items = _normalize_items(parsed)
    assert len(items) == 2


def test_normalize_items_from_data_key() -> None:
    parsed = {"data": [{"persona_id": "a"}]}
    assert len(_normalize_items(parsed)) == 1


def test_normalize_items_from_bare_list() -> None:
    parsed = [{"persona_id": "a"}, {"persona_id": "b"}]
    assert len(_normalize_items(parsed)) == 2


def test_normalize_items_reactions_as_dict_not_list_returns_empty() -> None:
    """Retrospective failure mode #4: {'reactions': {...}} instead of list."""
    parsed = {"reactions": {"persona_id": "a", "sentiment": 0.3}}
    assert _normalize_items(parsed) == []


def test_normalize_items_missing_reactions_key_returns_empty() -> None:
    """Retrospective failure mode #5."""
    parsed = {"summary": "looks bad"}
    assert _normalize_items(parsed) == []


def test_normalize_items_none_returns_empty() -> None:
    assert _normalize_items(None) == []  # type: ignore[arg-type]


def test_normalize_items_string_returns_empty() -> None:
    assert _normalize_items("not a dict or list") == []  # type: ignore[arg-type]


# ─────────────────────── _coerce_reactions — happy path ───────────────────────


def test_coerce_reactions_happy_path_all_matched_by_id(three_personas: list[Persona]) -> None:
    parsed = {
        "reactions": [
            _reaction_dict("aunt", sentiment=-0.4, comment="我很担心"),
            _reaction_dict("youzi", sentiment=0.1, comment="首板观察"),
            _reaction_dict("quant", sentiment=-0.2, comment="vol spike"),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert [r.persona_id for r in result] == ["aunt", "youzi", "quant"]
    assert result[0].sentiment == pytest.approx(-0.4)
    assert result[1].comment == "首板观察"
    assert result[2].sentiment == pytest.approx(-0.2)


def test_coerce_reactions_handles_bare_list(three_personas: list[Persona]) -> None:
    parsed = [
        _reaction_dict("aunt", sentiment=0.5),
        _reaction_dict("youzi", sentiment=-0.5),
        _reaction_dict("quant", sentiment=0.0),
    ]
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert result[0].sentiment == pytest.approx(0.5)


# ─────────────────────── _coerce_reactions — reorder + partial ───────────────────────


def test_coerce_reactions_ignores_llm_order_when_ids_match(three_personas: list[Persona]) -> None:
    """LLM returns reactions in a different order than the persona roster.
    With id-matching the alignment should still be correct."""
    parsed = {
        "reactions": [
            _reaction_dict("quant", sentiment=-0.9),
            _reaction_dict("aunt", sentiment=0.1),
            _reaction_dict("youzi", sentiment=0.4),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert result[0].persona_id == "aunt"
    assert result[0].sentiment == pytest.approx(0.1)
    assert result[1].persona_id == "youzi"
    assert result[1].sentiment == pytest.approx(0.4)
    assert result[2].persona_id == "quant"
    assert result[2].sentiment == pytest.approx(-0.9)


def test_coerce_reactions_partial_output_fills_neutral(
    three_personas: list[Persona], caplog: pytest.LogCaptureFixture
) -> None:
    """Retrospective failure mode #2: LLM returns 5 reactions for 10 personas.
    Test here with 1 of 3 personas to keep it small."""
    import logging

    caplog.set_level(logging.WARNING)
    parsed = {"reactions": [_reaction_dict("aunt", sentiment=-0.3)]}
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert result[0].persona_id == "aunt"
    assert result[0].sentiment == pytest.approx(-0.3)
    # The other two should be neutral (no unmatched items available)
    assert result[1].persona_id == "youzi"
    assert result[1].sentiment == 0.0
    assert result[1].confidence == 0.0
    assert result[1].comment == "(no response, neutral fallback)"
    assert result[2].persona_id == "quant"
    # Operator visibility: the degraded alignment must be logged
    assert any("neutral fallback" in rec.message for rec in caplog.records)


def test_coerce_reactions_extra_items_dropped(three_personas: list[Persona]) -> None:
    """LLM returns 5 reactions for 3 personas. Extras are dropped."""
    parsed = {
        "reactions": [
            _reaction_dict("aunt", sentiment=0.1),
            _reaction_dict("youzi", sentiment=-0.1),
            _reaction_dict("quant", sentiment=0.0),
            _reaction_dict("extra_one", sentiment=0.99),
            _reaction_dict("extra_two", sentiment=-0.99),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert [r.persona_id for r in result] == ["aunt", "youzi", "quant"]
    # Neither extra should show up
    assert all(r.sentiment not in (0.99, -0.99) for r in result)


# ─────────────────────── _coerce_reactions — hallucinated + duplicate ids ───────────────────────


def test_coerce_reactions_hallucinated_ids_fall_to_positional(
    three_personas: list[Persona], caplog: pytest.LogCaptureFixture
) -> None:
    """Retrospective failure mode #3: LLM returns 3 reactions with persona_ids
    that don't match any persona. Should fall to positional fallback."""
    import logging

    caplog.set_level(logging.WARNING)
    parsed = {
        "reactions": [
            _reaction_dict("ghost_one", sentiment=-0.5, comment="hallucinated_1"),
            _reaction_dict("ghost_two", sentiment=-0.3, comment="hallucinated_2"),
            _reaction_dict("ghost_three", sentiment=-0.1, comment="hallucinated_3"),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    # Persona ids must be correct (overwritten during positional fallback)
    assert [r.persona_id for r in result] == ["aunt", "youzi", "quant"]
    # Sentiment should come from the hallucinated rows, in order
    assert result[0].sentiment == pytest.approx(-0.5)
    assert result[1].sentiment == pytest.approx(-0.3)
    assert result[2].sentiment == pytest.approx(-0.1)
    # Each fallback consumption should be logged at WARNING level
    consume_logs = [rec for rec in caplog.records if "consumed unmatched item" in rec.message]
    assert len(consume_logs) == 3, f"expected 3 consume logs, got {len(consume_logs)}: {[r.message for r in caplog.records]}"
    # And the summary line should show degraded alignment
    assert any("alignment degraded" in rec.message for rec in caplog.records)


def test_coerce_reactions_duplicate_id_keeps_first(three_personas: list[Persona]) -> None:
    """When the LLM emits two rows with the same persona_id, only the first
    counts as id-matched. The duplicate goes to unmatched → positional fallback."""
    parsed = {
        "reactions": [
            _reaction_dict("aunt", sentiment=0.2, comment="first"),
            _reaction_dict("aunt", sentiment=-0.2, comment="duplicate"),
            _reaction_dict("quant", sentiment=0.4, comment="quant_reaction"),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    # aunt should get the FIRST row
    assert result[0].comment == "first"
    # youzi has no id-match → takes the duplicate aunt row positionally (with
    # persona_id overwritten) — the comment field survives even though it says
    # "duplicate", which is fine because operators see the fallback in logs
    assert result[1].persona_id == "youzi"
    assert result[1].comment == "duplicate"
    # quant is id-matched
    assert result[2].persona_id == "quant"
    assert result[2].comment == "quant_reaction"


def test_coerce_reactions_does_not_reuse_id_matched_row(three_personas: list[Persona]) -> None:
    """The bug Codex flagged: prior version could reuse an id-matched row
    via positional fallback. New version must NOT let an id-matched row
    show up in the unmatched pool."""
    parsed = {
        "reactions": [
            _reaction_dict("aunt", sentiment=0.5, comment="aunt_reaction"),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    # aunt is id-matched
    assert result[0].comment == "aunt_reaction"
    # youzi and quant should be neutral, NOT reusing aunt's row
    assert result[1].comment == "(no response, neutral fallback)"
    assert result[2].comment == "(no response, neutral fallback)"


# ─────────────────────── _coerce_reactions — malformed input ───────────────────────


def test_coerce_reactions_non_dict_items_skipped(three_personas: list[Persona]) -> None:
    """Stray non-dict items (strings, numbers) should not crash."""
    parsed = {
        "reactions": [
            "stray string",
            42,
            _reaction_dict("aunt", sentiment=0.1),
            None,
            _reaction_dict("youzi", sentiment=0.2),
            _reaction_dict("quant", sentiment=0.3),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert result[0].sentiment == pytest.approx(0.1)
    assert result[1].sentiment == pytest.approx(0.2)
    assert result[2].sentiment == pytest.approx(0.3)


def test_coerce_reactions_malformed_sentiment_uses_neutral(
    three_personas: list[Persona], caplog: pytest.LogCaptureFixture
) -> None:
    """Retrospective failure mode #6: LLM returns 'sentiment': '-0.3 to +0.1'.
    Prior version crashed the round. New version uses safe_float and logs."""
    import logging

    caplog.set_level(logging.WARNING)
    parsed = {
        "reactions": [
            {
                "persona_id": "aunt",
                "sentiment": "-0.3 to +0.1",  # ← this is the crash trigger
                "action_intent": "observe",
                "comment": "hedge",
                "confidence": 0.5,
            },
            _reaction_dict("youzi", sentiment=0.0),
            _reaction_dict("quant", sentiment=0.0),
        ]
    }
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    # Malformed sentiment should have fallen back to default=0.0
    assert result[0].sentiment == pytest.approx(0.0)
    assert result[0].comment == "hedge"  # other fields preserved
    # The warning should have fired
    assert any("sentiment" in rec.message for rec in caplog.records)


def test_coerce_reactions_missing_reactions_key_all_neutral(
    three_personas: list[Persona],
) -> None:
    """Retrospective failure mode #5: parsed dict has no 'reactions' key at all."""
    parsed = {"some_other_key": "value"}
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert all(r.comment == "(no response, neutral fallback)" for r in result)
    assert all(r.confidence == 0.0 for r in result)


def test_coerce_reactions_reactions_is_dict_not_list(three_personas: list[Persona]) -> None:
    """Retrospective failure mode #4: {'reactions': {...}} with a dict value."""
    parsed = {"reactions": {"persona_id": "aunt", "sentiment": 0.5}}
    result = _coerce_reactions(parsed, three_personas)
    assert len(result) == 3
    assert all(r.comment == "(no response, neutral fallback)" for r in result)


def test_neutral_reaction_shape() -> None:
    r = _neutral_reaction("test_persona")
    assert r.persona_id == "test_persona"
    assert r.sentiment == 0.0
    assert r.confidence == 0.0
    assert r.action_intent == "observe"
    assert "neutral fallback" in r.comment


# ─────────────────────── run_simulation end-to-end with fake chat_json ───────────────────────


class _FakeJsonChatResult:
    """Mimics llm_client.JsonChatResult without importing the dataclass directly
    (keeps this test file independent of the llm_client's exact module layout)."""

    def __init__(self, parsed: Any, model: str = "fake-model", fingerprint: str | None = "fp-test-1234"):
        self.parsed = parsed
        self.model = model
        self.system_fingerprint = fingerprint
        self.prompt_tokens = 100
        self.completion_tokens = 200


@pytest.mark.asyncio
async def test_run_simulation_happy_path_captures_fingerprints(
    three_personas: list[Persona], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: 2 rounds × 3 personas with a fake chat_json that returns
    canned reactions and a fixed fingerprint. Verifies the orchestration loop,
    fingerprint capture, and the seed-pass-through."""

    event = Event(
        ticker="002594",
        event_text="test event: ambiguous beat-and-miss",
        event_type="earnings",
        event_date="2026-04-09",
    )

    calls = []

    async def fake_chat_json(messages, model=None, temperature=None, max_tokens=None, seed=None, **kwargs):
        calls.append({"messages_len": len(messages), "seed": seed, "model": model})
        return _FakeJsonChatResult(
            parsed={
                "reactions": [
                    _reaction_dict("aunt", sentiment=-0.4, comment=f"round {len(calls) - 1}"),
                    _reaction_dict("youzi", sentiment=-0.1, comment=f"round {len(calls) - 1}"),
                    _reaction_dict("quant", sentiment=0.1, comment=f"round {len(calls) - 1}"),
                ]
            }
        )

    monkeypatch.setattr("ssfish.simulation.chat_json", fake_chat_json)

    result = await run_simulation(event, three_personas, n_rounds=2)

    assert isinstance(result, SimulationResult)
    assert result.n_rounds == 2
    assert result.n_personas == 3
    assert len(result.rounds) == 2
    assert len(result.rounds[0]) == 3
    # Fingerprint capture
    assert len(result.round_fingerprints) == 2
    assert all(fp == "fp-test-1234" for fp in result.round_fingerprints)
    # Seed passed through
    assert result.llm_seed is not None
    assert all(call["seed"] == result.llm_seed for call in calls)
    # All 10 calls with the right model defaulted from settings
    assert len(calls) == 2
    assert result.final_reactions[0].comment == "round 1"


@pytest.mark.asyncio
async def test_run_simulation_rejects_empty_personas() -> None:
    event = Event(
        ticker="002594",
        event_text="x",
        event_type="other",
        event_date="2026-04-09",
    )
    with pytest.raises(ValueError, match="at least 1 persona"):
        await run_simulation(event, [], n_rounds=1)


@pytest.mark.asyncio
async def test_run_simulation_rejects_zero_rounds(three_personas: list[Persona]) -> None:
    event = Event(
        ticker="002594",
        event_text="x",
        event_type="other",
        event_date="2026-04-09",
    )
    with pytest.raises(ValueError, match="n_rounds must be >= 1"):
        await run_simulation(event, three_personas, n_rounds=0)
