"""Application wiring.

Everything expensive happens once, at startup: config is parsed and validated,
the lexicon is compiled to an inverted index, and the context tiers are compiled
to item-keyed weights. A malformed config therefore kills the process on boot
rather than one request in production, and a request costs only arithmetic.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import admin_router, router
from app.classifier.rules import RulesClassifier
from app.config import get_settings
from app.config_loader import load_config, validate_against_registry
from app.engine.compiler import compile_rules
from app.engine.registry import known_keys
from app.llm.factory import build_llm_client
from app.middleware.context import RequestContextMiddleware
from app.middleware.logging import LatencyLoggingMiddleware, configure_logging
from app.services.aggregator import build_clients
from app.services.cache import TTLCache

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    config = load_config(settings.config_dir)
    # Cross-check the two halves that cannot see each other: YAML names context
    # keys, the registry owns them. A rename in one and not the other is caught
    # here rather than surfacing as a silently missing source.
    validate_against_registry(config, known_keys())

    # One client for the process. Per-request clients would discard connection
    # pooling, which is most of the benefit of fanning out concurrently.
    http_client = httpx.AsyncClient()
    cache = TTLCache()

    app.state.settings = settings
    app.state.config = config
    app.state.cache = cache
    app.state.http = http_client
    app.state.classifier = RulesClassifier(config.intents)
    app.state.compiled_rules = compile_rules(config.personalization)
    app.state.clients = build_clients(settings, config.services, cache, http_client)
    app.state.llm = build_llm_client(settings)

    logger.info(
        "context engine ready",
        extra={
            "event": "startup",
            "llm_provider": app.state.llm.name,
            "intents": sorted(i.value for i in config.intents.intents),
            "services": sorted(config.services.services),
            "context_token_budget": settings.context_token_budget,
            "admin_endpoints": settings.enable_admin_endpoints,
        },
    )

    try:
        yield
    finally:
        await http_client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="MyNaksh Personalized AI Context Engine",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Order matters: request id is set outermost so latency logging — and every
    # log line beneath it — is already correlated.
    app.add_middleware(LatencyLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(router)
    if settings.enable_admin_endpoints:
        app.include_router(admin_router)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Last resort. The pipeline turns expected failures into values, so
        # anything arriving here is a bug worth a stack trace in the logs and
        # nothing but a generic message to the caller.
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    return app


app = create_app()
