"""Resolves the configured provider into a client, and says so out loud.

The single most confusing thing a demo can do is silently answer from the mock
while everyone in the room believes they are watching a real model — or the
reverse, quietly spending money during a load test. Every branch below logs
which provider won and why, at INFO for the expected paths and WARNING for the
ones that mean something is misconfigured.
"""

from __future__ import annotations

import logging

from app.config import LLMProvider, Settings
from app.llm.anthropic import AnthropicClient
from app.llm.base import LLMClient, LLMError
from app.llm.mock import MockLLMClient
from app.llm.openai import OpenAIClient

logger = logging.getLogger(__name__)


def build_llm_client(settings: Settings) -> LLMClient:
    provider = settings.llm_provider

    if provider is LLMProvider.AUTO:
        return _auto(settings)

    if provider is LLMProvider.MOCK:
        logger.info("LLM provider: mock (explicitly configured). No API calls will be made.")
        return MockLLMClient()

    key = _key_for(settings, provider)
    if not key:
        # Deliberately a warning and a fallback rather than a hard failure: the
        # engine's own behaviour is what this service is for, and refusing to
        # boot over a missing key would make it unreviewable. The volume of the
        # log line is the mitigation.
        logger.warning(
            "LLM provider: mock — %s was requested but %s_API_KEY is not set. "
            "Answers are composed by a deterministic stub, NOT by a language model.",
            provider.value,
            provider.value.upper(),
        )
        return MockLLMClient()

    client = _build(provider, key, settings)
    logger.info(
        "LLM provider: %s (explicitly configured), model=%s",
        client.name,
        _model_for(settings, provider),
    )
    return client


def _auto(settings: Settings) -> LLMClient:
    """Key presence is the signal: configuring a key is the act of choosing.

    Anthropic wins a tie because it is the primary target; a deployment that
    wants otherwise sets llm_provider explicitly rather than unsetting a key.
    """
    for provider in (LLMProvider.ANTHROPIC, LLMProvider.OPENAI):
        key = _key_for(settings, provider)
        if not key:
            continue
        try:
            client = _build(provider, key, settings)
        except LLMError as exc:
            # Key present but SDK missing. Under AUTO the operator never named
            # this provider — they just have a key in the environment — so this
            # is a misconfiguration to report, not a reason to refuse to start.
            # An explicit provider choice does raise; see _build's caller above.
            logger.warning("LLM provider: %s unavailable (%s). Trying the next option.", provider.value, exc)
            continue
        logger.info(
            "LLM provider: %s (auto-resolved: %s_API_KEY is set), model=%s",
            client.name,
            provider.value.upper(),
            _model_for(settings, provider),
        )
        return client

    logger.info(
        "LLM provider: mock (auto-resolved: no API key configured). "
        "Answers are composed by a deterministic stub, NOT by a language model."
    )
    return MockLLMClient()


def _build(provider: LLMProvider, key: str, settings: Settings) -> LLMClient:
    if provider is LLMProvider.ANTHROPIC:
        return AnthropicClient(api_key=key, model=settings.anthropic_model)
    return OpenAIClient(api_key=key, model=settings.openai_model)


def _key_for(settings: Settings, provider: LLMProvider) -> str | None:
    return getattr(settings, f"{provider.value}_api_key", None)


def _model_for(settings: Settings, provider: LLMProvider) -> str:
    return getattr(settings, f"{provider.value}_model")
