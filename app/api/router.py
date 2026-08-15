"""HTTP endpoints.

Both /personalize and /debug/personalization run the SAME pipeline; debug simply
stops before the LLM. They are not two implementations of the same idea — if
debug reimplemented the decision path it would eventually describe behaviour the
live path no longer has, which is worse than having no debug endpoint at all.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.request_response import (
    DebugPersonalizationResponse,
    PersonalizeRequest,
    PersonalizeResponse,
    ScoredItemView,
)
from app.confidence import score_confidence, sources_used
from app.domain import (
    ClassificationResult,
    Confidence,
    ContextBundle,
    DecisionReason,
    PersonalizationPlan,
    ResolvedContext,
)
from app.engine.planner import plan as build_plan
from app.engine.registry import REGISTRY
from app.engine.selector import resolve
from app.llm.base import LLMError
from app.prompt.builder import build_prompt
from app.services.aggregator import CriticalServiceError, gather_context

logger = logging.getLogger(__name__)

router = APIRouter()

# Returned instead of calling the model when the question is not ours to answer.
# Spending an LLM call to decline is money burnt on a decision already made.
OUT_OF_DOMAIN_ANSWER = (
    "That question falls outside astrological guidance, so there is nothing in "
    "your chart or today's panchang that speaks to it. Ask me about your career, "
    "relationships, health, finances, or the day ahead and I can help."
)

# Every contributing upstream failed. Answering anyway would mean generating
# astrology from nothing, which is worse than saying so.
NO_CONTEXT_ANSWER = (
    "I could not reach enough of your chart data to answer that reliably right "
    "now. Please try again shortly."
)


async def _run_pipeline(
    request: Request, payload: PersonalizeRequest
) -> tuple[ClassificationResult, PersonalizationPlan, ResolvedContext, ContextBundle, dict]:
    """Classify, gather, plan, resolve. Everything both endpoints share."""
    state = request.app.state

    classification = state.classifier.classify(payload.question)

    try:
        bundle = await gather_context(payload.user_id, state.clients)
    except CriticalServiceError as exc:
        # A missing user is an answer about the request, not an outage.
        if exc.status_code == 404:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"unknown user '{payload.user_id}'"
            ) from exc
        logger.error("required service unavailable", extra={"service": exc.service})
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"cannot personalize: {exc.service} is unavailable",
        ) from exc

    profile = bundle.payload("user") or {}
    plan = build_plan(classification, profile, state.compiled_rules, state.config.personalization)

    if classification.in_domain:
        resolved = resolve(plan, bundle, state.settings.context_token_budget)
    else:
        # Selecting context for a question we are about to decline would make
        # /debug/personalization report sources that /personalize never uses.
        # The debug endpoint is only worth having if it describes the live path.
        resolved = ResolvedContext(
            selected=[], values={}, excluded=[], unavailable=[],
            dropped_for_budget=[], tokens_used=0,
        )

    logger.info(
        "personalization planned",
        extra={
            "event": "personalization",
            "user_id": payload.user_id,
            "intent": plan.intent.value,
            "intent_reason": classification.reason.value,
            "time_scope": plan.time_scope.value,
            "selected": [i.key for i in resolved.selected],
            "unavailable": resolved.unavailable,
            "dropped_for_budget": resolved.dropped_for_budget,
            "context_tokens": resolved.tokens_used,
        },
    )
    return classification, plan, resolved, bundle, profile


@router.post("/personalize", response_model=PersonalizeResponse, response_model_by_alias=True)
async def personalize(request: Request, payload: PersonalizeRequest) -> PersonalizeResponse:
    classification, plan, resolved, _bundle, profile = await _run_pipeline(request, payload)

    if not classification.in_domain:
        return PersonalizeResponse(
            answer=OUT_OF_DOMAIN_ANSWER, confidence=Confidence.LOW, sources_used=[]
        )

    if not resolved.selected:
        # Every upstream that could have contributed failed. The system prompt
        # permits the model to decline, but relying on that means the honesty of
        # the answer depends on which provider is configured — and a model given
        # no facts and asked for 180 words will supply them. Refusing here makes
        # it a property of the system instead of a hope about the model.
        logger.warning(
            "no context resolved; declining without an LLM call",
            extra={"event": "empty_context", "user_id": payload.user_id},
        )
        return PersonalizeResponse(
            answer=NO_CONTEXT_ANSWER, confidence=Confidence.LOW, sources_used=[]
        )

    system, user = build_prompt(plan, resolved, payload.question, profile.get("name", "there"))

    try:
        completion = await request.app.state.llm.generate(system, user, plan.max_words)
    except LLMError as exc:
        # Degrading to a fabricated answer would be worse than failing: the
        # caller cannot tell a generated answer from a placeholder one.
        logger.error("llm generation failed", extra={"provider": exc.provider})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="answer generation failed"
        ) from exc

    return PersonalizeResponse(
        answer=completion.text,
        confidence=score_confidence(classification, plan, resolved),
        sources_used=sources_used(resolved),
    )


@router.post(
    "/debug/personalization",
    response_model=DebugPersonalizationResponse,
    response_model_by_alias=True,
)
async def debug_personalization(
    request: Request, payload: PersonalizeRequest
) -> DebugPersonalizationResponse:
    """Explains the engine's reasoning. Never calls the LLM.

    It does still fetch upstreams, because reporting `unavailable` separately
    from `excluded` is only possible if we know what actually resolved.
    """
    classification, plan, resolved, bundle, _profile = await _run_pipeline(request, payload)

    return DebugPersonalizationResponse(
        intent=plan.intent.value,
        intent_reason=classification.reason.value,
        in_domain=classification.in_domain,
        time_scope=plan.time_scope.value,
        intent_weights={i.value: round(w, 4) for i, w in classification.weights.items()},
        absolute_evidence=round(classification.absolute_evidence, 4),
        relative_dominance=round(classification.relative_dominance, 4),
        matched_terms={s.intent.value: s.matched_terms for s in classification.ranked if s.matched_terms},
        # Display forms here, raw values internally. This endpoint exists to be
        # read by a human explaining a decision; the prompt builder keeps the
        # BCP-47 code it needs.
        language=_language_name(plan.language),
        tone=plan.tone.capitalize(),
        max_words=plan.max_words,
        selected_context=[i.display_name for i in resolved.selected],
        excluded_context=[_name(k) for k in resolved.excluded],
        unavailable_context=[_name(k) for k in resolved.unavailable],
        dropped_for_budget=[_name(k) for k in resolved.dropped_for_budget],
        ranked=[
            ScoredItemView(
                key=s.key, display_name=_name(s.key), score=s.score, reasons=s.reasons
            )
            for s in plan.ranked
        ],
        context_tokens=resolved.tokens_used,
        upstream_failures=bundle.failures,
    )


@router.get("/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "llm_provider": request.app.state.llm.name}


# Separate router, included only when ENABLE_ADMIN_ENDPOINTS is set. Registering
# conditionally rather than stripping routes afterwards keeps the endpoint out of
# the route table and the OpenAPI schema, and does not depend on how a given
# FastAPI version stores included routes.
admin_router = APIRouter()


@admin_router.delete("/_cache", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cache(request: Request) -> None:
    """Dev-only: forces the cold-cache path so a fault demo is not masked.

    Exposed publicly this is a free denial of service — every request after a
    flush stampedes the upstreams at once.
    """
    request.app.state.cache.clear()
    logger.warning("cache cleared via admin endpoint", extra={"event": "cache_clear"})


def _name(key: str) -> str:
    item = REGISTRY.get(key)
    return item.display_name if item else key


# Only English is supported; the map exists so an unknown code degrades to the
# code itself rather than to a wrong language name.
_LANGUAGE_NAMES = {"en": "English"}


def _language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code, code)
