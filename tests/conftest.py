"""Shared pytest fixtures for ssFish tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Make sure tests don't accidentally hit the real .env or pollute the cost ledger."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-for-tests")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://yourapi.cn/v1")
    monkeypatch.setenv("SSFISH_COST_LEDGER", str(tmp_path / "cost.json"))
    monkeypatch.setenv("SSFISH_SCORECARD_DB", str(tmp_path / "scorecard.db"))
    monkeypatch.setenv("SSFISH_PASSWORD", "test-password")
    # Force settings re-init
    import ssfish.config
    ssfish.config.settings = ssfish.config.Settings()  # type: ignore[call-arg]
    yield


@pytest.fixture
def sample_personas_yaml(tmp_path):
    """Write a minimal valid persona YAML file and return its path."""
    p = tmp_path / "personas.yaml"
    p.write_text(
        """
schema_version: 1
personas:
  - id: test_aunt
    archetype: 散户大妈
    display_name: 退休教师, 50岁, 上海
    model: gpt-4o-mini
    voice_prompt: |
      短句子, 多用涨/跌/亏. 经常提 2018 套牢经历.
    biases:
      loss_averse: 0.8
      herd_following: 0.7
    knowledge:
      holdings: [银行股, 白酒]
      information_sources: [微信群, 央视财经]
    weight: 1.0
  - id: test_youzi
    archetype: 短线游资
    display_name: 90后短线交易员, 跟龙虎榜
    model: gpt-4o-mini
    voice_prompt: |
      简短直接, 关注涨停板和资金流向. 不在乎基本面.
    biases:
      momentum_chase: 0.9
      patience: 0.1
    knowledge:
      information_sources: [龙虎榜, 同花顺]
    weight: 1.2
""".strip()
    )
    return p
