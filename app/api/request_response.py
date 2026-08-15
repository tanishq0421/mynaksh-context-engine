"""Wire contracts for the public API.

Kept separate from app/domain.py on purpose: domain types are free to change
shape as the pipeline evolves, but these are what clients depend on. Collapsing
them would make every internal refactor a breaking API change.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain import Confidence


class PersonalizeRequest(BaseModel):
    user_id: str = Field(alias="userId", min_length=1)
    question: str = Field(min_length=1, max_length=1000)

    model_config = {"populate_by_name": True}


class PersonalizeResponse(BaseModel):
    """The shape the assignment specifies.

    `sourcesUsed` is derived from what the selector actually put in the prompt,
    never from the model's own account of what it used — asking a model to cite
    invites hallucinated citations, and what we sent is a fact we own.
    """

    answer: str
    confidence: Confidence
    sources_used: list[str] = Field(serialization_alias="sourcesUsed")

    model_config = {"populate_by_name": True}


class ScoredItemView(BaseModel):
    key: str
    display_name: str = Field(serialization_alias="displayName")
    score: float
    reasons: list[str]


class DebugPersonalizationResponse(BaseModel):
    """Explains how the engine interpreted the request. Never calls the LLM.

    Reports the three reasons an item can be absent separately. The spec only
    asks for `excludedContext`, but collapsing them discards the interesting
    part: excluded means the rules judged it irrelevant, unavailable means an
    upstream let us down, and droppedForBudget means it was relevant but did not
    fit. One list would make all three look like the same decision.
    """

    intent: str
    intent_reason: str = Field(serialization_alias="intentReason")
    in_domain: bool = Field(serialization_alias="inDomain")
    time_scope: str = Field(serialization_alias="timeScope")
    intent_weights: dict[str, float] = Field(serialization_alias="intentWeights")
    absolute_evidence: float = Field(serialization_alias="absoluteEvidence")
    relative_dominance: float = Field(serialization_alias="relativeDominance")
    matched_terms: dict[str, list[str]] = Field(serialization_alias="matchedTerms")

    language: str
    tone: str
    max_words: int = Field(serialization_alias="maxWords")

    selected_context: list[str] = Field(serialization_alias="selectedContext")
    excluded_context: list[str] = Field(serialization_alias="excludedContext")
    unavailable_context: list[str] = Field(serialization_alias="unavailableContext")
    dropped_for_budget: list[str] = Field(serialization_alias="droppedForBudget")

    ranked: list[ScoredItemView]
    context_tokens: int = Field(serialization_alias="contextTokens")
    upstream_failures: dict[str, str] = Field(serialization_alias="upstreamFailures")

    model_config = {"populate_by_name": True}


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
