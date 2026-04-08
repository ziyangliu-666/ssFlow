"""Async LLM client wrapper around yourapi.cn (OpenAI-compatible).

Pattern source: /home/rufus/research/agent-coach/src/coach/llm.py
Adaptations from the source:
    - AsyncOpenAI instead of OpenAI (so simulation can fan out via asyncio)
    - Reads from ssfish.config.settings (no hardcoded fallback key)
    - Extended PRICING dict for multi-family models
    - Tenacity retry on API errors with exponential backoff
    - JSON parsing fallback chain (direct -> markdown fence -> outer braces -> {})
    - BudgetExceeded raised before any call if accumulated session cost > budget
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, APIError, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings


# ─────────────────────── Cost Tracking ───────────────────────


class BudgetExceeded(RuntimeError):
    """Raised when cumulative session cost has exceeded the configured budget."""


# Approximate USD per 1M tokens. yourapi.cn rebills these from underlying providers
# at slightly different rates than upstream; treat as guard rails, not invoices.
PRICING_PER_M: dict[str, dict[str, float]] = {
    # OpenAI family
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    # Anthropic family (via yourapi)
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    # Google Gemini family
    "gemini-2.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    # DeepSeek
    "deepseek-v3.2": {"input": 0.27, "output": 1.10},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    # Qwen
    "qwen-plus": {"input": 0.40, "output": 1.20},
    "qwen-long": {"input": 0.50, "output": 2.00},
    "qwen-max": {"input": 1.60, "output": 6.40},
    # GLM (Zhipu)
    "glm-4-plus": {"input": 0.50, "output": 1.50},
}

# Fallback pricing for models not in the table
DEFAULT_PRICING = {"input": 1.0, "output": 3.0}


@dataclass
class CostTracker:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_calls: int = 0
    cost_by_model: dict[str, float] = field(default_factory=dict)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_calls += 1
        pricing = PRICING_PER_M.get(model, DEFAULT_PRICING)
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
        self.cost_by_model[model] = self.cost_by_model.get(model, 0.0) + cost
        self._persist()

    @property
    def total_cost_usd(self) -> float:
        return sum(self.cost_by_model.values())

    def summary(self) -> str:
        lines = [f"API Cost: ~${self.total_cost_usd:.4f} ({self.total_calls} calls)"]
        for model, cost in sorted(self.cost_by_model.items()):
            lines.append(f"  {model}: ${cost:.4f}")
        lines.append(
            f"  Tokens: {self.total_input_tokens:,} in / {self.total_output_tokens:,} out"
        )
        return "\n".join(lines)

    def _persist(self) -> None:
        try:
            ledger_path = Path(settings.cost_ledger_path).resolve()
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "total_cost_usd": self.total_cost_usd,
                "total_calls": self.total_calls,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "cost_by_model": self.cost_by_model,
            }
            ledger_path.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    def load_prior(self) -> None:
        try:
            ledger_path = Path(settings.cost_ledger_path).resolve()
            if not ledger_path.exists():
                return
            data = json.loads(ledger_path.read_text())
            self.total_input_tokens = data.get("total_input_tokens", 0)
            self.total_output_tokens = data.get("total_output_tokens", 0)
            self.total_calls = data.get("total_calls", 0)
            self.cost_by_model = data.get("cost_by_model", {})
        except (OSError, json.JSONDecodeError):
            pass


# Module-level singleton
cost_tracker = CostTracker()
cost_tracker.load_prior()


# ─────────────────────── Client Singleton ───────────────────────

_client: AsyncOpenAI | None = None
_client_lock = asyncio.Lock()


async def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = AsyncOpenAI(
                    api_key=settings.openai_api_key.get_secret_value(),
                    base_url=settings.openai_base_url,
                )
    return _client


# ─────────────────────── Public API ───────────────────────


def _check_budget() -> None:
    if cost_tracker.total_cost_usd >= settings.budget_usd:
        raise BudgetExceeded(
            f"Session cost ${cost_tracker.total_cost_usd:.4f} >= budget ${settings.budget_usd:.2f}. "
            f"Increase SSFISH_BUDGET_USD or reset {settings.cost_ledger_path}."
        )


async def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 2000,
    json_mode: bool = False,
) -> str:
    """Single chat completion. Returns the message text content.

    Retries up to 3 times on transient API errors with exponential backoff.
    Records cost after each successful call.
    """
    _check_budget()
    model = model or settings.default_model
    temperature = settings.temperature if temperature is None else temperature
    client = await get_client()

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type((APIError, RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        reraise=True,
    ):
        with attempt:
            response = await client.chat.completions.create(**kwargs)

    usage = response.usage
    if usage is not None:
        cost_tracker.record(
            model,
            usage.prompt_tokens or 0,
            usage.completion_tokens or 0,
        )

    content = response.choices[0].message.content or ""
    return content


async def chat_json(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 2000,
    retries: int = 2,
) -> dict[str, Any] | list[Any]:
    """Chat with JSON-mode parsing fallback.

    Tries response_format=json_object first. If parsing fails, tries:
        1. direct json.loads
        2. extract from markdown fence
        3. extract outermost JSON object
        4. raise ValueError if retries exhausted

    Returns the parsed JSON (dict or list).
    """
    last_raw = ""
    for attempt in range(retries + 1):
        # First attempt with json_mode; subsequent attempts toggle off in case
        # the upstream proxy doesn't honor response_format
        try:
            raw = await chat(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=(attempt == 0),
            )
        except APIError as exc:
            # Some yourapi-routed models reject response_format. Retry without it.
            if attempt == 0 and "response_format" in str(exc).lower():
                raw = await chat(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=False,
                )
            else:
                raise

        last_raw = raw

        # Path 1: direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Path 2: markdown fence
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Path 3: outer object
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Path 4: outer array
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        if attempt < retries:
            continue

    raise ValueError(
        f"Failed to parse JSON after {retries + 1} attempts. Last raw output (truncated): "
        f"{last_raw[:500]}"
    )


__all__ = [
    "BudgetExceeded",
    "CostTracker",
    "PRICING_PER_M",
    "chat",
    "chat_json",
    "cost_tracker",
    "get_client",
]
