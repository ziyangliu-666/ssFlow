"""Tests for the compliance output filter — CRITICAL launch blocker.

Per the design doc P4 and the eng review test plan CP2, this filter is the
load-bearing wall of the entire compliance posture. If a single forbidden
phrase slips through to a user-facing report, the legal posture collapses.

Coverage targets:
    - Every forbidden word in isolation -> REJECT
    - Forbidden words inside Chinese sentences -> REJECT
    - Mixed case English (BUY, Buy, buy) -> REJECT
    - Whitelisted descriptive phrases -> PASS
    - Edge cases (empty string, punctuation surroundings) -> handled
"""

from __future__ import annotations

import pytest

from ssfish.output_filter import (
    ComplianceViolation,
    FORBIDDEN_VOCAB,
    assert_compliant,
    is_compliant,
    regex_filter_violations,
)


# ─────────────────────── REJECT cases (must catch) ───────────────────────


@pytest.mark.parametrize(
    "text,expected_word",
    [
        # Direct Chinese advice verbs
        ("我建议明天减仓 30%", "建议"),
        ("推荐持有", "推荐"),
        ("应该卖出", "应该"),
        ("必须立即卖出", "必须"),
        # Trading actions in context
        ("散户应该买入", "应该"),  # multiple matches OK; this asserts at least one
        ("分析师建议卖出该股票", "建议"),
        ("机构在加仓", "加仓"),
        ("游资正在建仓", "建仓"),
        ("北上资金清仓离场", "清仓"),
        ("散户大妈减仓避险", "减仓"),
        # Price targets / ratings
        ("目标价 ¥185", "目标价"),
        ("目标股价 200 元", "目标股价"),
        ("评级: 买入", "评级"),
        ("强烈买入", "强烈买入"),
        ("止损位 ¥120", "止损位"),
        ("止盈位 ¥150", "止盈位"),
        # English equivalents
        ("Rating: BUY", "BUY"),
        ("Rating: SELL", "SELL"),
        ("STRONG BUY", "STRONG BUY"),
        ("target price: $50", "target price"),
        ("price target raised to $60", "price target"),
        ("buy rating maintained", "buy rating"),
        # Mixed case
        ("Buy now", "BUY"),
        ("buy the dip", "BUY"),
        ("Sell signal", "SELL"),
        # Chinese first-person
        ("我建议你考虑入场", "我建议"),
        ("我推荐这只票", "我推荐"),
        # Punctuation surroundings
        ("[建议]:减仓", "建议"),
        ("(目标价: ¥120)", "目标价"),
        ("『卖出』信号明确", "卖出"),
    ],
)
def test_forbidden_phrases_are_caught(text: str, expected_word: str) -> None:
    """Each forbidden phrase MUST be detected by the filter."""
    violations = regex_filter_violations(text)
    assert violations, f"Filter MISSED forbidden text: {text!r}"
    assert expected_word in violations, (
        f"Expected to find {expected_word!r} in violations, got {violations} for text {text!r}"
    )


def test_assert_compliant_raises_on_violation() -> None:
    with pytest.raises(ComplianceViolation) as exc_info:
        assert_compliant("我建议立即减仓 50%")
    assert "建议" in exc_info.value.violations
    assert "减仓" in exc_info.value.violations


def test_is_compliant_returns_false_on_violation() -> None:
    assert is_compliant("建议买入") is False
    assert is_compliant("clean text") is True


# ─────────────────────── PASS cases (must NOT trigger) ───────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # Whitelisted descriptive phrases from the design doc
        "群体情绪分布: 散户 negative 62%, 中性 28%, positive 10%",
        "implied price range: -4% to +1%",
        "模拟群体的 implied price move: -3.2% ± 1.5%",
        "模拟显示 73% 的散户倾向负面解读",
        "群体讨论区间在 ¥118-¥122",
        "模拟群体在 6 小时内的情绪转向",
        "1000 个 agent 中有 620 个表达 panic 情绪",
        # Counterfactual framing (allowed)
        "如果你已经持仓, 群体压力集中在开盘第一小时",
        "如果你正在考虑入场, 群体讨论区间在 ¥118-¥122",
        # Pure narrative
        "散户大妈倾向情绪化, 容易跟风",
        "雪球大 V 倾向理性多头观点",
        "机构分析师等待问答环节再表态",
        # Empty / minimal
        "",
        "report empty",
        # Words that contain substrings of forbidden English (must not false-positive)
        "BUYER beware",  # BUYER contains BUY but with word boundary
        "the SELLER market",  # SELLER contains SELL but with word boundary
        "this is a robust analysis",  # ROBUST contains BUS, no overlap
        # Numeric / structural
        "ticker: 002594  date: 2026-04-09  sentiment_mean: -0.42",
        "盲点清单: 1. 政策不确定性 2. 海外订单风险 3. 库存周期",
    ],
)
def test_whitelisted_phrases_pass(text: str) -> None:
    """Descriptive / counterfactual phrases MUST NOT be flagged."""
    violations = regex_filter_violations(text)
    assert violations == [], f"Filter false-positive on {text!r}: {violations}"
    assert is_compliant(text)


def test_word_boundary_for_english_terms() -> None:
    """English forbidden terms must use word boundaries to avoid false positives."""
    # BUY should match 'BUY' but not 'BUYER' or 'BUYBACK'
    assert "BUY" in regex_filter_violations("BUY now")
    assert regex_filter_violations("BUYER beware") == []
    assert regex_filter_violations("BUYBACK announced") == []
    # SELL should match 'SELL' but not 'SELLER'
    assert "SELL" in regex_filter_violations("SELL signal")
    assert regex_filter_violations("SELLER's market") == []


def test_chinese_terms_match_anywhere() -> None:
    """Chinese forbidden terms match without word boundary requirements."""
    # 建议 should match even when sandwiched
    assert "建议" in regex_filter_violations("xxx建议xxx")
    assert "建议" in regex_filter_violations("强烈建议持有")
    # 买入 in compound words
    assert "买入" in regex_filter_violations("买入信号")


def test_full_realistic_compliant_report() -> None:
    """A realistic full report should pass cleanly."""
    report = """
# ssFish 模拟报告

## 群体反应叙事
模拟显示 1000 个散户中, 73% 倾向负面解读这次公告。短线游资的反应集中在
"先观望" 而非追涨杀跌, 与历史模式一致。机构分析师等待问答环节再表态,
北上资金保持中性。雪球热度在公告后 2 小时达到峰值。

## 盲点清单
1. 公告中的"暂缓"一词被散户群体多数误读为"取消", 引发情绪化抛压
2. 海外订单的影响在短期叙事中被忽略
3. 行业政策的二次解读窗口在第二个交易日

## Implied price range
模拟群体的 implied price move: -4.2% ~ -1.1%, 模型置信度 62%
"""
    violations = regex_filter_violations(report)
    assert violations == [], f"Realistic report false-positive: {violations}"


def test_full_realistic_violating_report_caught() -> None:
    """A report that slipped a forbidden phrase MUST be caught."""
    report = """
# ssFish 模拟报告
模拟显示散户群体倾向负面解读. 综合判断, 我建议明天减仓 30% 以避险.
目标价: ¥120.
"""
    with pytest.raises(ComplianceViolation):
        assert_compliant(report)


def test_forbidden_vocab_list_is_nonempty_and_unique() -> None:
    assert len(FORBIDDEN_VOCAB) >= 20
    assert len(set(FORBIDDEN_VOCAB)) == len(FORBIDDEN_VOCAB), "Duplicate entries in FORBIDDEN_VOCAB"


# ─────────────────────── Sanitizer tests ───────────────────────


from ssfish.output_filter import sanitize_text  # noqa: E402


def test_sanitize_replaces_chinese_forbidden_words() -> None:
    text = "我建议你减仓 30%, 目标价 ¥120, 评级买入"
    out = sanitize_text(text)
    assert "建议" not in out
    assert "减仓" not in out
    assert "目标价" not in out
    assert "评级" not in out
    assert "买入" not in out
    # And the result must pass the filter
    assert is_compliant(out), f"Sanitized output still not compliant: {out}"


def test_sanitize_preserves_clean_text() -> None:
    text = "模拟群体倾向负面解读, 73% 的散户在私聊中表达担忧"
    assert sanitize_text(text) == text


def test_sanitize_handles_empty() -> None:
    assert sanitize_text("") == ""


def test_sanitize_real_persona_output() -> None:
    """A real LLM persona output that triggered the smoke-test quarantine."""
    text = "业绩符合预期，但毛利率下降需关注，可能影响后续评级。"
    out = sanitize_text(text)
    assert "评级" not in out
    assert is_compliant(out)


# ─────────────────────── Disclaimer regression ───────────────────────
#
# The auto-generated disclaimer at the bottom of every report must itself be
# compliant. This is the bug that the smoke test caught: a "not investment
# advice" disclaimer that contained the word "建议".


def test_render_sandbox_disclaimer_is_compliant() -> None:
    """Catch the infinite-loop bug where the system's own disclaimer
    contains a forbidden word.

    The sandbox report's footer text is checked against the compliance
    filter to make sure ssFish doesn't quarantine its own boilerplate.
    """
    from ssfish.report import render_sandbox_markdown
    from ssfish.event import Event
    from ssfish.persona import MarketShare, Persona, SandboxConfig
    from ssfish.sandbox import (
        Agent, ClassFlowResult, SandboxResult, SandboxRoundRecord,
    )

    persona = Persona(
        id="test_class",
        archetype="test archetype",
        display_name="test",
        voice_prompt="x",
        decision_mode="discretionary",
        role="directional_speculator",
        market_share=MarketShare(by_volume=0.10),
        sandbox=SandboxConfig(
            instance_count=1,
            capital_distribution={"type": "lognormal", "median_cny": 100000, "sigma": 0.5},
            initial_position_distribution={"type": "fixed", "value": 0.5},
            risk={"max_position_pct": 0.95},
            action_space=[{"name": "hold", "side": "none", "pool": "none", "fraction": 0.0}],
        ),
    )
    event = Event(
        ticker="TEST",
        event_text="test event",
        event_type="other",
        event_date="2026-04-08",
        current_price=100.0,
        adv_value=1e9,
    )
    round_rec = SandboxRoundRecord(
        round_idx=0, price_before=100.0, price_after=98.0, delta_pct=-0.02,
        net_flow_cny=-1e7,
        class_flows={"test_class": ClassFlowResult(
            persona_id="test_class", net_flow_cny=-1e7,
            action_histogram={"hold": 1}, n_agents=1,
            rationale="测试 rationale", strategic_signal=None,
        )},
        fingerprints=["fp_test"],
    )
    result = SandboxResult(
        simulation_id="test",
        event=event,
        personas=[persona],
        initial_price=100.0,
        final_price=98.0,
        rounds=[round_rec],
        elapsed_seconds=1.0,
        cost_usd_at_start=0.0,
        cost_usd_at_end=0.01,
        llm_seed=42,
        lambda_used=0.5,
        adv_value_used=1e9,
        final_agents_by_class={"test_class": [Agent(
            persona_id="test_class", capital_cny=100000,
            cash_cny=50000, holdings_shares=500, max_holdings_value_cny=95000,
        )]},
    )
    text = render_sandbox_markdown(result)
    assert is_compliant(text), f"Sandbox boilerplate trips own filter: {text[:500]}"
