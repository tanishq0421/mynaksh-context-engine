"""Shared vocabulary for the pipeline.

Every layer speaks these types, and nothing here imports from a layer — that is
what keeps the pipeline stages independently testable and independently
replaceable. Layer-private types live next to their layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Intent(str, Enum):
    CAREER = "career"
    RELATIONSHIP = "relationship"
    HEALTH = "health"
    FINANCE = "finance"
    # GENERAL is deliberately NOT a scored intent. It is the absence of one —
    # a label the fallback path emits. Scoring it would let filler words
    # ("focus", "advice") contaminate the dominance calculation.
    GENERAL = "general"

    @classmethod
    def scored(cls) -> list["Intent"]:
        return [c for c in cls if c is not cls.GENERAL]


class TimeScope(str, Enum):
    TODAY = "today"
    THIS_WEEK = "this_week"
    THIS_MONTH = "this_month"
    THIS_YEAR = "this_year"
    UNSPECIFIED = "unspecified"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Criticality(str, Enum):
    """Whether a failing upstream service is fatal or merely degrades the answer."""

    REQUIRED = "required"
    DEGRADABLE = "degradable"


class DecisionReason(str, Enum):
    CONFIDENT_MATCH = "CONFIDENT_MATCH"
    AMBIGUOUS_MERGED = "AMBIGUOUS_MERGED"
    NO_EVIDENCE_DEFAULT = "NO_EVIDENCE_DEFAULT"
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentScore:
    intent: Intent
    score: float
    matched_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClassificationResult:
    """What the classifier decided, and enough trace to explain why.

    `weights` is the contract with the personalization engine: it maps each
    active intent to its share of relevance and sums to 1.0. A confident single
    intent yields {career: 1.0}; an ambiguous merge yields {career: 0.6,
    finance: 0.4}. The engine multiplies context tier weights by these, so the
    single-intent case collapses to plain tier scoring.
    """

    primary: Intent
    weights: dict[Intent, float]
    ranked: list[IntentScore]
    time_scope: TimeScope
    reason: DecisionReason
    in_domain: bool
    absolute_evidence: float
    relative_dominance: float

    @property
    def is_general(self) -> bool:
        return self.primary is Intent.GENERAL


# --------------------------------------------------------------------------
# Upstream data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one upstream call. Never raises; failure is a value."""

    service: str
    ok: bool
    data: Any | None = None
    error: str | None = None
    stale: bool = False  # served from expired cache because the upstream failed


@dataclass
class ContextBundle:
    """Everything the fan-out managed to gather, plus what it could not.

    The personalization engine never sees this — it decides relevance as a pure
    function of intent and config. Only the selector resolves relevance against
    what actually arrived.
    """

    data: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    stale: set[str] = field(default_factory=set)

    def payload(self, service: str) -> Any | None:
        return self.data.get(service)

    @property
    def healthy(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------
# Context registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextItem:
    """A registered slice of an upstream payload.

    Config files reference these by `key` only. The extractor is a function, so
    the registry lives in code while the rules that use it live in YAML.
    """

    key: str
    display_name: str  # what surfaces in sourcesUsed
    service: str
    extractor: Callable[[Any], Any | None]
    renderer: Callable[[Any], str]  # short natural phrase, never raw JSON
    token_cost: int


# --------------------------------------------------------------------------
# Personalization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredItem:
    key: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersonalizationPlan:
    """Phase 1 — decided from config alone, before any upstream data is seen.

    Pure and reproducible, which is what makes /debug/personalization
    deterministic while the live path adapts to whatever actually resolved.
    """

    intent: Intent
    weights: dict[Intent, float]
    time_scope: TimeScope
    language: str
    tone: str
    max_words: int
    ranked: list[ScoredItem]  # candidates, descending relevance
    excluded: list[str]  # keys hard-zeroed by rules


@dataclass(frozen=True)
class ResolvedContext:
    """Phase 2 — the plan met reality.

    Three distinct reasons an item is absent. The spec only asks for
    `excluded`, but collapsing them would throw away the interesting part:
    excluded means the rules judged it irrelevant, unavailable means an upstream
    let us down, dropped_for_budget means it was relevant but did not fit.
    """

    selected: list[ContextItem]
    values: dict[str, Any]
    excluded: list[str]
    unavailable: list[str]
    dropped_for_budget: list[str]
    tokens_used: int
