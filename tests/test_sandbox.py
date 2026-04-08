"""Tests for sandbox.py — Kyle price impact + action distribution wrapper.

This file covers the Phase B foundational pieces (B1c + B2):
    - compute_price_impact()       — square-root formula + edge cases
    - normalize_action_distribution() — Gotcha 2 lock-in (clip + rescale)
    - chat_action_distribution()   — LLM wrapper, mocked

The Agent + OrderBook tests live in a separate test_sandbox_agents.py
file once those classes ship in B1a + B1b.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ssfish.sandbox import (
    ActionDistributionParseError,
    ActionDistributionResult,
    LAMBDA_LITERATURE,
    NORMALIZATION_TOLERANCE,
    chat_action_distribution,
    compute_price_impact,
    normalize_action_distribution,
)


# ─────────────────────── compute_price_impact (B1c) ───────────────────────


class TestComputePriceImpact:
    """Square-root price impact formula tests."""

    def test_spec_byd_example(self):
        """The canonical spec §9.3 example: BYD net flow -3億 / ADV 80億 / λ=0.5
        should yield ≈ -9.7%."""
        result = compute_price_impact(
            net_flow_cny=-3e8,  # -3億
            adv_cny=8e9,         # 80億
            lambda_market=0.5,
        )
        # Hand calc: 0.5 * (-1) * sqrt(3e8 / 8e9)
        #          = -0.5 * sqrt(0.0375)
        #          = -0.5 * 0.19365
        #          ≈ -0.0968
        assert result == pytest.approx(-0.0968, abs=0.001)

    def test_zero_net_flow_returns_zero(self):
        """Exactly zero net flow → exactly zero price change."""
        assert compute_price_impact(0.0, adv_cny=1e9) == 0.0

    def test_positive_net_flow_yields_positive_delta(self):
        """Net buy pressure pushes price up."""
        result = compute_price_impact(net_flow_cny=2e8, adv_cny=8e9, lambda_market=0.5)
        assert result > 0
        assert result == pytest.approx(0.5 * math.sqrt(2e8 / 8e9), abs=1e-6)

    def test_negative_net_flow_yields_negative_delta(self):
        """Net sell pressure pushes price down."""
        result = compute_price_impact(net_flow_cny=-5e8, adv_cny=1e10, lambda_market=0.4)
        assert result < 0

    def test_concavity_large_flow_underestimated_relative_to_linear(self):
        """sqrt is concave, so doubling the flow should less-than-double the impact.
        This is the empirical reason we use sqrt rather than linear (Bouchaud 2010)."""
        small = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.5)
        large = compute_price_impact(2e8, adv_cny=1e10, lambda_market=0.5)
        # large / small should be sqrt(2) ≈ 1.414, NOT 2
        ratio = large / small
        assert ratio == pytest.approx(math.sqrt(2), rel=0.01)
        assert ratio < 1.5

    def test_zero_adv_raises(self):
        with pytest.raises(ValueError, match="adv_cny must be > 0"):
            compute_price_impact(net_flow_cny=1e8, adv_cny=0)

    def test_negative_adv_raises(self):
        with pytest.raises(ValueError, match="adv_cny must be > 0"):
            compute_price_impact(net_flow_cny=1e8, adv_cny=-1e9)

    def test_default_lambda_used_when_omitted(self):
        """If lambda_market is not passed, the default literature value is used."""
        result_default = compute_price_impact(1e8, adv_cny=1e9)
        result_explicit = compute_price_impact(
            1e8, adv_cny=1e9, lambda_market=LAMBDA_LITERATURE["default"]
        )
        assert result_default == result_explicit

    def test_lambda_scales_linearly(self):
        """Doubling λ should double the resulting price impact."""
        a = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.3)
        b = compute_price_impact(1e8, adv_cny=1e10, lambda_market=0.6)
        assert b == pytest.approx(2 * a, rel=1e-6)

    def test_market_lambdas_make_sense(self):
        """A股 λ should be higher than US equity λ (less liquid market)."""
        assert LAMBDA_LITERATURE["ashare"] > LAMBDA_LITERATURE["us-equity"]


# ─────────────────────── normalize_action_distribution (B2 helper) ───────────────────────


class TestNormalizeActionDistribution:

    def test_clean_distribution_pass_through(self):
        """A distribution that already sums to 1.0 with valid keys is unchanged."""
        raw = {"hold": 0.5, "buy": 0.3, "sell": 0.2}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized == pytest.approx({"hold": 0.5, "buy": 0.3, "sell": 0.2})
        assert warning is None

    def test_sum_slightly_off_within_tolerance_silent(self):
        """Sum within ±tolerance is silently renormalized without warning."""
        raw = {"hold": 0.51, "buy": 0.30, "sell": 0.21}  # sums to 1.02
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0, abs=1e-9)
        # Within tolerance band of 0.05 → no warning about sum
        assert warning is None or "sum" not in (warning or "")

    def test_sum_outside_tolerance_warns(self):
        """Sum outside ±tolerance triggers a warning but still normalizes."""
        raw = {"hold": 0.7, "buy": 0.2, "sell": 0.2}  # sums to 1.10, > 1.05
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0, abs=1e-9)
        assert warning is not None
        assert "sum was 1.1" in warning

    def test_negative_clipped(self):
        """Negative probabilities are clipped to 0 and warned."""
        raw = {"hold": 0.6, "buy": 0.5, "sell": -0.1}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized["sell"] == 0.0
        assert sum(normalized.values()) == pytest.approx(1.0)
        assert warning is not None
        assert "negative" in warning

    def test_unknown_action_dropped_and_warned(self):
        """Actions not in expected_actions are silently dropped + warned."""
        raw = {"hold": 0.5, "buy": 0.3, "panic_dance": 0.2}  # invalid action
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert "panic_dance" not in normalized
        assert sum(normalized.values()) == pytest.approx(1.0)
        assert warning is not None
        assert "unknown" in warning

    def test_missing_expected_action_filled_with_zero(self):
        """Expected actions not in raw distribution are added with prob=0."""
        raw = {"hold": 0.5, "buy": 0.5}  # missing "sell"
        normalized, _ = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert normalized["sell"] == 0.0

    def test_all_zero_distribution_falls_back_to_uniform(self):
        """If everything is zero/negative, fall back to uniform with warning."""
        raw = {"hold": 0.0, "buy": 0.0, "sell": 0.0}
        normalized, warning = normalize_action_distribution(
            raw, expected_actions=["hold", "buy", "sell"]
        )
        assert sum(normalized.values()) == pytest.approx(1.0)
        # Each action gets 1/3
        for v in normalized.values():
            assert v == pytest.approx(1.0 / 3)
        assert warning is not None
        assert "uniform" in warning

    def test_empty_raw_distribution_falls_back_to_uniform(self):
        """An empty input falls back to uniform over expected_actions."""
        normalized, warning = normalize_action_distribution(
            {}, expected_actions=["a", "b"]
        )
        assert normalized == pytest.approx({"a": 0.5, "b": 0.5})
        assert warning is not None


# ─────────────────────── chat_action_distribution (B2 wrapper) ───────────────────────


def _make_mock_json_result(parsed: dict[str, Any]):
    """Build a stand-in JsonChatResult."""
    from ssfish.llm_client import JsonChatResult

    return JsonChatResult(
        parsed=parsed,
        model="gpt-4o-mini",
        system_fingerprint="fp_test_001",
        prompt_tokens=1500,
        completion_tokens=230,
    )


class TestChatActionDistribution:

    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch):
        """Standard well-formed LLM response: distribution + rationale + confidence."""
        mock_response = _make_mock_json_result(
            {
                "action_distribution": {
                    "hold": 0.4,
                    "panic_sell_50pct": 0.3,
                    "average_down_10pct": 0.2,
                    "fomo_buy_30pct": 0.1,
                },
                "rationale": "Q1 beat 但 margin miss 引发分歧, 短线散户分裂",
                "confidence": 0.7,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock_response

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "test"}],
            persona_id="retail_short_term_chaser",
            expected_actions=["hold", "panic_sell_50pct", "average_down_10pct", "fomo_buy_30pct"],
        )

        assert isinstance(result, ActionDistributionResult)
        assert result.persona_id == "retail_short_term_chaser"
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.action_distribution["hold"] == pytest.approx(0.4)
        assert result.rationale.startswith("Q1 beat")
        assert result.confidence == 0.7
        assert result.system_fingerprint == "fp_test_001"
        assert result.normalization_warning is None
        assert result.strategic_signal is None

    @pytest.mark.asyncio
    async def test_unknown_actions_dropped(self, monkeypatch):
        """LLM hallucinates an action name → dropped, warning recorded."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {
                    "hold": 0.5,
                    "buy": 0.3,
                    "telepath_market": 0.2,  # hallucinated
                },
                "rationale": "test",
                "confidence": 0.5,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert "telepath_market" not in result.action_distribution
        assert result.normalization_warning is not None
        assert "unknown" in result.normalization_warning

    @pytest.mark.asyncio
    async def test_distribution_renormalized_to_one(self, monkeypatch):
        """LLM returns distribution summing to 1.15 → renormalized + warned."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"hold": 0.6, "buy": 0.3, "sell": 0.25},  # 1.15
                "rationale": "x",
                "confidence": 0.5,
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.normalization_warning is not None

    @pytest.mark.asyncio
    async def test_missing_action_distribution_key_raises(self, monkeypatch):
        """LLM forgets the wrapping key entirely → ParseError."""
        mock = _make_mock_json_result({"some_random_key": "blah"})

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        with pytest.raises(ActionDistributionParseError, match="missing 'action_distribution'"):
            await chat_action_distribution(
                messages=[{"role": "user", "content": "x"}],
                persona_id="test",
                expected_actions=["hold", "buy"],
            )

    @pytest.mark.asyncio
    async def test_flat_response_with_action_keys_recovered(self, monkeypatch):
        """LLM flattens the dict — top-level keys match expected actions → recovered."""
        mock = _make_mock_json_result(
            {
                "hold": 0.4,
                "buy": 0.3,
                "sell": 0.3,
                "rationale": "flat response",
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="test",
            expected_actions=["hold", "buy", "sell"],
        )
        assert sum(result.action_distribution.values()) == pytest.approx(1.0)
        assert result.action_distribution["hold"] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_strategic_signal_parsed_when_expected(self, monkeypatch):
        """Strategic personas can also emit a strategic_signal field."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"do_nothing": 0.95, "block_sale_5pct": 0.05},
                "rationale": "维持持有, 等待 Q2 解禁窗口",
                "confidence": 0.6,
                "strategic_signal": {
                    "direction": "reduce",
                    "magnitude": "medium",
                    "time_horizon_days": 90,
                },
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="industrial_capital",
            expected_actions=["do_nothing", "block_sale_5pct", "increase_holding_2pct"],
            expects_strategic_signal=True,
        )
        assert result.strategic_signal is not None
        assert result.strategic_signal["direction"] == "reduce"
        assert result.strategic_signal["time_horizon_days"] == 90

    @pytest.mark.asyncio
    async def test_strategic_signal_ignored_when_not_expected(self, monkeypatch):
        """Non-strategic personas: strategic_signal field is ignored."""
        mock = _make_mock_json_result(
            {
                "action_distribution": {"hold": 1.0},
                "rationale": "x",
                "confidence": 0.5,
                "strategic_signal": {"direction": "anything"},  # should be ignored
            }
        )

        async def fake_chat_json(*args, **kwargs):
            return mock

        monkeypatch.setattr("ssfish.sandbox.chat_json", fake_chat_json)

        result = await chat_action_distribution(
            messages=[{"role": "user", "content": "x"}],
            persona_id="retail",
            expected_actions=["hold"],
            expects_strategic_signal=False,
        )
        assert result.strategic_signal is None
