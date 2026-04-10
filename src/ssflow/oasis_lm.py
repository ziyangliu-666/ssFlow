"""CAMEL/OASIS LanguageModel adapter routing through ssFlow guard rails.

This module is non-negotiable: every CAMEL LM call made by an OASIS agent
must hit our `cost_tracker` (so the budget guard fires) and our
`output_filter.sanitize_text` (so prescriptive vocab can never leak into
post text).

Strategy: subclass CAMEL's `OpenAICompatibleModel` (which already implements the
sync + async OpenAI-compatible request loop pointing at our yourapi.cn base_url)
and override `_run` / `_arun` to:

  1. Pre-flight budget check — raise `BudgetExceeded` before the wire call if
     cumulative session cost is already at/over the configured budget
  2. Delegate to the parent's `_run` / `_arun` (which performs the actual HTTP
     request and returns an OpenAI `ChatCompletion`)
  3. Read `response.usage` and feed our `cost_tracker.record(...)` so the
     singleton increments
  4. Walk every `choice.message.content` and run it through
     `output_filter.sanitize_text` so prescriptive vocab is rephrased before
     CAMEL caches it / OASIS persists it to the SQLite db

The same singleton (`cost_tracker`) used by `llm_client.chat_sync` /
`llm_client.chat` is shared here, so:
  - ssFlow trading layer LLM calls (via chat_json_sync) → cost_tracker
  - OASIS social layer LLM calls (via SsFishCamelModel)  → cost_tracker
  ... both contribute to the same cumulative budget guard.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type, Union

from camel.messages import OpenAIMessage
from camel.models.openai_compatible_model import OpenAICompatibleModel
from camel.types import ChatCompletion, ChatCompletionChunk, ModelType
from openai import AsyncStream, Stream
from openai.lib.streaming.chat import (
    AsyncChatCompletionStreamManager,
    ChatCompletionStreamManager,
)
from pydantic import BaseModel

from .config import settings
from .llm_client import BudgetExceeded, cost_tracker
from .output_filter import sanitize_text


log = logging.getLogger(__name__)


def _check_budget() -> None:
    """Re-implementation of llm_client._check_budget for the CAMEL path.

    Inlined here so this module has zero dependencies on async-path internals.
    """
    if cost_tracker.total_cost_usd >= settings.budget_usd:
        raise BudgetExceeded(
            f"Session cost ${cost_tracker.total_cost_usd:.4f} >= "
            f"budget ${settings.budget_usd:.2f}. "
            f"Increase SSFLOW_BUDGET_USD or reset {settings.cost_ledger_path}."
        )


def _record_cost_from_response(model_name: str, response: Any) -> None:
    """Pull token usage off an OpenAI ChatCompletion response and record it.

    Tolerant to streaming responses (which don't have `.usage` immediately) —
    in that case we silently skip recording. CAMEL's streaming path is rare
    in OASIS social action contexts.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    cost_tracker.record(model_name, prompt_tokens, completion_tokens)


def _sanitize_choices_in_place(response: Any) -> None:
    """Walk every `choice.message.content` and run it through `sanitize_text`.

    Operates in place on the OpenAI `ChatCompletion` response object so that
    when CAMEL hands the response back to OASIS, the cached / persisted content
    has already been compliance-sanitized.

    Tool calls are NOT sanitized (they're function arguments, not user-visible
    text).
    """
    choices = getattr(response, "choices", None)
    if not choices:
        return
    for choice in choices:
        message = getattr(choice, "message", None)
        if message is None:
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            message.content = sanitize_text(content)


class SsFishCamelModel(OpenAICompatibleModel):
    """CAMEL `OpenAICompatibleModel` subclass routing through ssFlow guard rails.

    Use this anywhere CAMEL/OASIS expects a `BaseModelBackend` instance — typically
    when constructing a `SocialAgent`. It's a drop-in replacement for the stock
    `OpenAICompatibleModel` that adds:

      - Budget guard (raises `BudgetExceeded` before exceeding `settings.budget_usd`)
      - Cost tracking (every successful call increments `cost_tracker`, which is
        the SAME singleton used by `llm_client.chat_sync`)
      - Compliance sanitization (`output_filter.sanitize_text` applied to every
        `choice.message.content` before the response is returned)

    Pulls API key + base URL from `settings.openai_api_key` + `settings.openai_base_url`
    by default, so the same env vars used everywhere else in ssFlow apply here.
    """

    def __init__(
        self,
        model_type: Union[ModelType, str, None] = None,
        model_config_dict: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # Default to ssFlow's settings if caller didn't override
        if model_config_dict is None:
            model_config_dict = {}
        if "temperature" not in model_config_dict:
            model_config_dict["temperature"] = settings.temperature
        if "seed" not in model_config_dict and settings.seed is not None:
            model_config_dict["seed"] = settings.seed
        resolved_model = model_type or settings.default_model
        resolved_api_key = api_key or settings.openai_api_key.get_secret_value()
        resolved_url = url or settings.openai_base_url
        super().__init__(
            model_type=resolved_model,
            model_config_dict=model_config_dict,
            api_key=resolved_api_key,
            url=resolved_url,
            **kwargs,
        )

    @property
    def model_name(self) -> str:
        """Convenience accessor for the resolved model name."""
        return str(self.model_type)

    def _run(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[
        ChatCompletion,
        Stream[ChatCompletionChunk],
        ChatCompletionStreamManager[BaseModel],
    ]:
        """Sync request path. Budget check → super()._run → record cost → sanitize."""
        _check_budget()
        response = super()._run(messages, response_format, tools)
        # Streaming responses don't have usage at this point — record + sanitize
        # only fire on the non-streaming path.
        if isinstance(response, ChatCompletion):
            _record_cost_from_response(self.model_name, response)
            _sanitize_choices_in_place(response)
        return response

    async def _arun(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[
        ChatCompletion,
        AsyncStream[ChatCompletionChunk],
        AsyncChatCompletionStreamManager[BaseModel],
    ]:
        """Async request path. Same shape as `_run` but awaits super()."""
        _check_budget()
        response = await super()._arun(messages, response_format, tools)
        if isinstance(response, ChatCompletion):
            _record_cost_from_response(self.model_name, response)
            _sanitize_choices_in_place(response)
        return response


def build_default_lm() -> SsFishCamelModel:
    """Convenience constructor returning an LM wired to the default ssFlow model."""
    return SsFishCamelModel()


__all__ = [
    "SsFishCamelModel",
    "build_default_lm",
]
