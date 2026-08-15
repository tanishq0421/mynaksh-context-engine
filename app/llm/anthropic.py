"""Anthropic provider.

The SDK is an optional dependency and is imported lazily, never at module
scope. That is load-bearing: `app.llm.factory` imports this module to decide
whether it *can* use Anthropic, so a top-level `import anthropic` would make the
whole service refuse to start unless both provider SDKs were installed — which
is exactly the situation the mock provider exists to avoid.

(This module shares a name with the SDK package. Safe under Python 3's absolute
imports — `import anthropic` here resolves to the installed package, never to
this file — but the reason is worth stating so nobody "fixes" it.)
"""

from __future__ import annotations

import logging
import time

from app.llm.base import LLMClient, LLMError, LLMResponse, max_tokens_for_words

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"

_MISSING_SDK = (
    "the 'anthropic' package is not installed. "
    "Install it with: pip install 'mynaksh-context-engine[anthropic]' "
    "(or set LLM_PROVIDER=mock to run without a provider)"
)


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ModuleNotFoundError as exc:  # noqa: F841 — message is the useful part
            raise LLMError(self.name, _MISSING_SDK) from exc

        # Async client because the API layer is async end to end; the sync one
        # would block the event loop for the whole generation.
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def generate(self, system: str, user: str, max_words: int) -> LLMResponse:
        import anthropic  # already imported by __init__; this is a cache hit

        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens_for_words(max_words),
                system=system,
                messages=[{"role": "user", "content": user}],
                # Adaptive thinking is on by default on Sonnet 5 and would spend
                # the token budget reasoning about a two-paragraph horoscope.
                # This is a single-shot generation from pre-selected facts — the
                # thinking has already happened, in the engine.
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(self.name, f"HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(self.name, f"could not reach the API: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(self.name, str(exc)) from exc

        latency_ms = (time.perf_counter() - started) * 1000

        # A safety decline arrives as a normal 200 with empty content, so this
        # has to be checked before reading blocks — otherwise it surfaces as a
        # baffling empty answer rather than an error.
        if message.stop_reason == "refusal":
            raise LLMError(self.name, "the model declined to answer this request")

        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise LLMError(self.name, f"empty completion (stop_reason={message.stop_reason})")

        logger.info(
            "anthropic completion: model=%s in=%s out=%s latency=%.0fms stop=%s",
            message.model,
            message.usage.input_tokens,
            message.usage.output_tokens,
            latency_ms,
            message.stop_reason,
        )

        return LLMResponse(
            text=text,
            provider=self.name,
            # The response's own model id, not the requested one — they differ
            # when an alias resolves to a dated snapshot, and the served model
            # is what a support ticket needs.
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            latency_ms=latency_ms,
        )
