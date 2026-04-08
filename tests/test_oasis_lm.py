"""Tests for oasis_lm.SsFishCamelModel.

Validates the three load-bearing properties of the OASIS LM adapter:

  1. Cost tracker increments after every successful call (sync + async paths)
  2. BudgetExceeded is raised pre-flight when cumulative cost is at/over budget
  3. Compliance filter sanitizes returned choice content (forbidden vocab → descriptive)

We never hit a real LLM. Every test patches the parent class's `_run` / `_arun`
with a fake that returns a baked OpenAI ChatCompletion-shaped object.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ssflow import llm_client
from ssflow.config import settings
from ssflow.llm_client import BudgetExceeded, CostTracker
from ssflow.oasis_lm import SsFishCamelModel


# ─────────────────────── Helpers ───────────────────────


class _FakeChatCompletion:
    """Mimics openai.types.chat.ChatCompletion for isinstance() checks.

    We register this class as ChatCompletion in the test module so the
    `isinstance(response, ChatCompletion)` branch in oasis_lm fires.
    """


def _fake_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 50):
    response = _FakeChatCompletion()
    response.choices = [
        SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
        )
    ]
    response.usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    response.model = "gpt-4o-mini"
    response.system_fingerprint = "fp_test"
    return response


@pytest.fixture
def fresh_cost_tracker(monkeypatch):
    """Replace the module-level cost_tracker with a fresh empty one."""
    fresh = CostTracker()
    monkeypatch.setattr(llm_client, "cost_tracker", fresh)
    import ssflow.oasis_lm as olm
    monkeypatch.setattr(olm, "cost_tracker", fresh)
    return fresh


@pytest.fixture
def lm_with_fake_parent(fresh_cost_tracker, monkeypatch):
    """Build an SsFishCamelModel and patch the parent's _run/_arun to fakes.

    Also patches `oasis_lm.ChatCompletion` to be `_FakeChatCompletion` so the
    `isinstance(response, ChatCompletion)` branch fires for our fake responses.
    """
    import ssflow.oasis_lm as olm
    monkeypatch.setattr(olm, "ChatCompletion", _FakeChatCompletion)

    lm = SsFishCamelModel(model_type="gpt-4o-mini")

    return lm


def _patch_run(lm: SsFishCamelModel, response_text: str) -> list[dict]:
    """Replace the parent class's _run with a fake. Returns a call log."""
    calls: list[dict] = []
    fake = _fake_response(response_text)

    def fake_super_run(messages, response_format=None, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return fake

    # Bypass the OpenAICompatibleModel's _run by patching it directly on this instance
    from camel.models.openai_compatible_model import OpenAICompatibleModel
    import ssflow.oasis_lm as olm

    def fake_run(self, messages, response_format=None, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return fake

    monkeypatch_target = OpenAICompatibleModel._run
    OpenAICompatibleModel._run = fake_run  # type: ignore[method-assign]
    return calls


def _patch_arun(lm: SsFishCamelModel, response_text: str) -> list[dict]:
    calls: list[dict] = []
    fake = _fake_response(response_text)

    from camel.models.openai_compatible_model import OpenAICompatibleModel

    async def fake_arun(self, messages, response_format=None, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return fake

    OpenAICompatibleModel._arun = fake_arun  # type: ignore[method-assign]
    return calls


# ─────────────────────── 1. Cost tracker integration ───────────────────────


class TestCostTrackerIntegration:

    def test_sync_run_increments_cost_tracker(self, lm_with_fake_parent, fresh_cost_tracker):
        lm = lm_with_fake_parent
        _patch_run(lm, "hello")

        assert fresh_cost_tracker.total_calls == 0

        lm._run(messages=[{"role": "user", "content": "ping"}])

        assert fresh_cost_tracker.total_calls == 1
        assert fresh_cost_tracker.total_input_tokens == 100
        assert fresh_cost_tracker.total_output_tokens == 50
        assert fresh_cost_tracker.total_cost_usd > 0
        assert "gpt-4o-mini" in fresh_cost_tracker.cost_by_model

    @pytest.mark.asyncio
    async def test_async_arun_increments_cost_tracker(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        _patch_arun(lm, "async hi")

        assert fresh_cost_tracker.total_calls == 0

        await lm._arun(messages=[{"role": "user", "content": "ping"}])

        assert fresh_cost_tracker.total_calls == 1
        assert fresh_cost_tracker.total_input_tokens == 100
        assert fresh_cost_tracker.total_output_tokens == 50

    def test_multiple_sync_calls_accumulate(self, lm_with_fake_parent, fresh_cost_tracker):
        lm = lm_with_fake_parent
        _patch_run(lm, "ok")

        for _ in range(3):
            lm._run(messages=[{"role": "user", "content": "ping"}])

        assert fresh_cost_tracker.total_calls == 3
        assert fresh_cost_tracker.total_input_tokens == 300


# ─────────────────────── 2. Budget guard ───────────────────────


class TestBudgetGuard:

    def test_budget_exceeded_raised_before_call(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        calls = _patch_run(lm, "ok")

        # Pin cost_tracker over budget
        fresh_cost_tracker.cost_by_model["gpt-4o-mini"] = settings.budget_usd + 1.0

        with pytest.raises(BudgetExceeded):
            lm._run(messages=[{"role": "user", "content": "ping"}])

        assert calls == [], "no LM call should have been made"

    @pytest.mark.asyncio
    async def test_async_budget_exceeded_raised(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        calls = _patch_arun(lm, "ok")
        fresh_cost_tracker.cost_by_model["gpt-4o-mini"] = settings.budget_usd + 1.0

        with pytest.raises(BudgetExceeded):
            await lm._arun(messages=[{"role": "user", "content": "ping"}])

        assert calls == []

    def test_budget_check_uses_module_settings(
        self, lm_with_fake_parent, fresh_cost_tracker, monkeypatch
    ):
        """Lowering the budget at runtime should make subsequent calls fail.

        gpt-4o-mini pricing: $0.15 / $0.60 per M tokens.
        Each fake call: 100 in + 50 out = $0.000045.
        Budget set just under that so 1 call passes and 2 calls trip the guard.
        """
        lm = lm_with_fake_parent
        _patch_run(lm, "ok")

        monkeypatch.setattr(settings, "budget_usd", 0.00004)
        # First call: 0 < 0.00004 → passes
        lm._run(messages=[{"role": "user", "content": "ping"}])
        # Second call: 0.000045 >= 0.00004 → fails
        with pytest.raises(BudgetExceeded):
            lm._run(messages=[{"role": "user", "content": "ping"}])


# ─────────────────────── 3. Compliance sanitization ───────────────────────


class TestComplianceSanitization:

    def test_forbidden_vocab_in_response_is_sanitized(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        _patch_run(lm, "我建议买入 BYD, 目标价 250")

        response = lm._run(messages=[{"role": "user", "content": "ping"}])
        text = response.choices[0].message.content

        assert "建议" not in text
        assert "买入" not in text
        assert "目标价" not in text

    def test_clean_text_passes_through(self, lm_with_fake_parent, fresh_cost_tracker):
        lm = lm_with_fake_parent
        _patch_run(lm, "市场对 BYD 的情绪偏多")

        response = lm._run(messages=[{"role": "user", "content": "ping"}])
        text = response.choices[0].message.content

        assert text == "市场对 BYD 的情绪偏多"

    def test_english_buy_sell_sanitized(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        _patch_run(lm, "Strong BUY signal on NVDA")

        response = lm._run(messages=[{"role": "user", "content": "ping"}])
        text = response.choices[0].message.content

        assert "BUY" not in text
        assert "[lean-in]" in text or "[strong lean-in]" in text

    @pytest.mark.asyncio
    async def test_async_path_also_sanitizes(
        self, lm_with_fake_parent, fresh_cost_tracker
    ):
        lm = lm_with_fake_parent
        _patch_arun(lm, "建议买入 BYD")

        response = await lm._arun(messages=[{"role": "user", "content": "ping"}])
        text = response.choices[0].message.content

        assert "建议" not in text
        assert "买入" not in text
