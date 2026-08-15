"""Runtime settings, read from the environment (and .env).

This module holds *deployment* configuration only — URLs, keys, budgets, log
levels. The engine's behavioural configuration (intents, context tiers, service
timeouts) lives in config/*.yaml, because that is domain knowledge a non-engineer
should be able to edit and review.
"""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LLMProvider(str, Enum):
    AUTO = "auto"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    MOCK = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM ---
    llm_provider: LLMProvider = LLMProvider.AUTO
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    openai_model: str = "gpt-4o-mini"

    # --- Upstream services ---
    user_service_url: str = "http://localhost:8001"
    kundli_service_url: str = "http://localhost:8001"
    horoscope_service_url: str = "http://localhost:8001"
    panchang_service_url: str = "http://localhost:8001"

    # --- Prompt ---
    # Ceiling on tokens spent on selected context. Items are admitted by
    # descending relevance until this is exhausted; the rest are reported as
    # dropped-for-budget rather than silently discarded.
    context_token_budget: int = 600

    # --- Dev ---
    # Exposes DELETE /_cache so a demo can force a cold-cache path. Off in prod:
    # a public cache flush is a free way to stampede the upstream services.
    enable_admin_endpoints: bool = False

    log_level: str = "INFO"
    config_dir: Path = PROJECT_ROOT / "config"

    def service_url(self, service: str) -> str:
        """URL for a named upstream service ('user', 'kundli', ...)."""
        return getattr(self, f"{service}_service_url")


@lru_cache
def get_settings() -> Settings:
    return Settings()
