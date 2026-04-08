"""Shared pytest fixtures for ssFish tests.

IMPORTANT: env vars are set at module-import time (below), BEFORE any
`import ssfish.*` happens in test files. This is required because
`ssfish/config.py` now fails fast on missing OPENAI_API_KEY instead of
falling back to a placeholder — see the retrospective (config.py silent
fallback was the #1 DX bug).
"""

from __future__ import annotations

import os

import pytest


# ── Module-level env bootstrap (runs before any test module is imported) ──
# pytest loads conftest.py before collecting/importing test files, so these
# assignments happen before `from ssfish.X import Y` in any test file triggers
# `import ssfish.config` (which instantiates `Settings()` at module scope).
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-tests")
os.environ.setdefault("OPENAI_BASE_URL", "https://yourapi.cn/v1")
os.environ.setdefault("SSFISH_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Make sure tests don't accidentally pollute the cost ledger or scorecard db."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-for-tests")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://yourapi.cn/v1")
    monkeypatch.setenv("SSFISH_COST_LEDGER", str(tmp_path / "cost.json"))
    monkeypatch.setenv("SSFISH_SCORECARD_DB", str(tmp_path / "scorecard.db"))
    monkeypatch.setenv("SSFISH_PASSWORD", "test-password")
    # Force settings re-init so per-test paths take effect
    import ssfish.config
    ssfish.config.settings = ssfish.config.Settings()  # type: ignore[call-arg]
    yield


@pytest.fixture
def sample_personas_yaml(tmp_path):
    """Write a minimal valid persona pack v3 YAML file and return its path."""
    p = tmp_path / "personas.yaml"
    p.write_text(
        """
schema_version: 3
market: ashare
last_updated: 2026-04-08
data_sources:
  - id: test-source
    name: Synthetic test source
    accessed: 2026-04-08
personas:
  - id: test_aunt
    archetype: 散户大妈
    sub_archetype: retail_passive
    display_name: 退休教师, 50岁, 上海
    model: gpt-4o-mini
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      by_volume: 0.10
      by_holdings: 0.08
      by_account_count: 0.20
      citations:
        - source_id: test-source
    voice_prompt: |
      短句子, 多用涨/跌/亏. 经常提 2018 套牢经历.
    biases:
      loss_averse: 0.8
      herd_following: 0.7
    knowledge:
      holdings: [银行股, 白酒]
      information_sources: [微信群, 央视财经]
    sandbox:
      instance_count: 1000
      capital_distribution:
        type: lognormal
        median_cny: 200000
        sigma: 0.6
      initial_position_distribution:
        type: bernoulli
        prob_holding: 0.50
      risk:
        max_position_pct: 0.90
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
        - {name: panic_sell_50pct, side: sell, pool: holdings_in_target, fraction: 0.5}
  - id: test_youzi
    archetype: 短线游资
    sub_archetype: retail_active
    display_name: 90后短线交易员, 跟龙虎榜
    model: gpt-4o-mini
    decision_mode: discretionary
    role: directional_speculator
    market_share:
      by_volume: 0.18
      by_holdings: 0.04
      by_account_count: 0.02
      citations:
        - source_id: test-source
    voice_prompt: |
      简短直接, 关注涨停板和资金流向. 不在乎基本面.
    biases:
      momentum_chase: 0.9
      patience: 0.1
    knowledge:
      information_sources: [龙虎榜, 同花顺]
    sandbox:
      instance_count: 500
      capital_distribution:
        type: lognormal
        median_cny: 5000000
        sigma: 1.0
      initial_position_distribution:
        type: bernoulli
        prob_holding: 0.30
      risk:
        max_position_pct: 1.0
        leverage_max: 2.0
      action_space:
        - {name: hold, side: none, pool: none, fraction: 0.0}
        - {name: chase_buy_30pct, side: buy, pool: cash, fraction: 0.30}
""".strip()
    )
    return p
