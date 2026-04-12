"""Regression tests for persona_set_hash completeness.

Ensures that changing any simulation-relevant persona field produces a
different hash. Prevents the bug where persona_set_hash only hashed
id+model+voice_prompt+market_share, making scorecard comparisons
unreliable after sub_populations or action_space changes.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from ssflow.persona import load_personas, persona_set_hash


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ASHARE = _REPO_ROOT / "personas" / "ashare.yaml"


@pytest.fixture
def personas():
    return load_personas(str(_ASHARE))


def test_identical_sets_produce_identical_hash(personas):
    h1 = persona_set_hash(personas)
    h2 = persona_set_hash(personas)
    assert h1 == h2


def test_changing_id_changes_hash(personas):
    original = persona_set_hash(personas)
    modified = copy.deepcopy(personas)
    modified[0].id = modified[0].id + "_changed"
    assert persona_set_hash(modified) != original


def test_changing_action_space_changes_hash(personas):
    original = persona_set_hash(personas)
    trader = next(p for p in personas if p.sandbox and p.sandbox.action_space)
    modified = copy.deepcopy(personas)
    target = next(p for p in modified if p.id == trader.id)
    target.sandbox.action_space.append({"id": "test_action", "side": "buy", "quantity_pct": 0.5})
    assert persona_set_hash(modified) != original


def test_changing_instance_count_changes_hash(personas):
    original = persona_set_hash(personas)
    trader = next(p for p in personas if p.sandbox)
    modified = copy.deepcopy(personas)
    target = next(p for p in modified if p.id == trader.id)
    target.sandbox.instance_count += 1
    assert persona_set_hash(modified) != original


def test_changing_follows_changes_hash(personas):
    original = persona_set_hash(personas)
    with_follows = next(p for p in personas if p.follows)
    modified = copy.deepcopy(personas)
    target = next(p for p in modified if p.id == with_follows.id)
    target.follows.append("fake_persona_id")
    assert persona_set_hash(modified) != original


def test_changing_sub_population_changes_hash(personas):
    from dataclasses import replace
    with_subpop = next((p for p in personas if p.sub_populations), None)
    if with_subpop is None:
        pytest.skip("No persona with sub_populations in ashare.yaml")
    original = persona_set_hash(personas)
    modified = copy.deepcopy(personas)
    target = next(p for p in modified if p.id == with_subpop.id)
    target.sub_populations[0] = replace(target.sub_populations[0], fraction=0.99)
    assert persona_set_hash(modified) != original


def test_changing_risk_changes_hash(personas):
    original = persona_set_hash(personas)
    with_risk = next(
        p for p in personas
        if p.sandbox and p.sandbox.risk and p.sandbox.risk.get("stop_loss_threshold") is not None
    )
    modified = copy.deepcopy(personas)
    target = next(p for p in modified if p.id == with_risk.id)
    target.sandbox.risk["stop_loss_threshold"] = -0.99
    assert persona_set_hash(modified) != original
