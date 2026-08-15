"""OpenAI provider.

Same shape and the same rules as the Anthropic client: the SDK is optional and
imported lazily so the service starts without it, and every failure leaves here
as an `LLMError`.

(This module shares a name with the SDK package. Safe under Python 3's absolute
imports — `import openai` here resolves to the installed package, never to this
file — but the reason is worth stating so nobody "fixes" it.)
"""

from __future__ import annotations

import logging
import time

from app.llm.base import LLMClient, LLMError, LLMResponse, max_tokens_for_words

logger = logging.getLogger(__name__)

_MISSING_SDK = (
    "the 'openai' package is not installed. "
    "Install it with: pip install 'mynaksh-context-engine[openai]' "
    "(or set LLM_PROVIDER=mock to run without a provider)"
)


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise LLMError(self.name, _MISSING_SDK) from exc

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def generate(self, system: str, user: str, max_words: int) -> LLMResponse:
        import openai  # already imported by __init__; this is a cache hit

        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens_for_words(max_words),
                messages=[
                    # The grounding rules go in the system role, where the model
                    # weights them above the user turn — putting them in the user
                    # message makes them just more context to be creative about.
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.APIStatusError as exc:
            raise LLMError(self.name, f"HTTP {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(self.name, f"could not reach the API: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(self.name, str(exc)) from exc

        latency_ms = (time.perf_counter() - started) * 1000

        choice = completion.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise LLMError(self.name, f"empty completion (finish_reason={choice.finish_reason})")

        usage = completion.usage
        logger.info(
            "openai completion: model=%s in=%s out=%s latency=%.0fms finish=%s",
            completion.model,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            latency_ms,
            choice.finish_reason,
        )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=completion.model,
            # `usage` is absent on some deployments; None means "not measured",
            # which is honest, where a 0 would look like a free call.
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=latency_ms,
        )
