"""Typed shapes for config/*.yaml.

Config is *data*, never logic — weights, term lists, thresholds, TTLs. The
moment an expression appears in YAML we have built a programming language with
no type checker and no debugger. Anything that needs real logic belongs in a
named signal extractor in Python and is referenced from here by name.

Every model below is validated at startup, so a typo or a dangling context key
fails the process rather than a request at 3am.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.domain import Criticality, Intent, TimeScope


# --------------------------------------------------------------------------
# intents.yaml
# --------------------------------------------------------------------------


class Term(BaseModel):
    """One lexicon entry.

    `match` is matched at token level, never as a substring — otherwise `art`
    fires inside `heart`. A trailing `*` marks a stem prefix (`promot*` covers
    promotion/promoted/promotions) which avoids a stemmer dependency and its bugs.

    A term may appear under several intents with different weights; `salary`
    being 0.5 career and 0.8 finance is the point, since overlap is where real
    misclassification lives.
    """

    match: str
    weight: float = Field(gt=0)


class Phrase(Term):
    """A multi-word entry, weighted above its constituent tokens.

    Phrases consume the tokens they match (longest-match-wins), so `love life`
    cannot also score bare `life`.
    """


class IntentLexicon(BaseModel):
    terms: list[Term] = Field(default_factory=list)
    phrases: list[Phrase] = Field(default_factory=list)


class ClassifierThresholds(BaseModel):
    """Provisional. Deliberately not presented as tuned.

    A paper trace over 11 questions exercised `min_evidence` zero times and
    `dominance` exactly once, so these are defensible starting values and
    nothing more. Tuning them needs a few hundred labelled real questions with
    the boundary region oversampled.
    """

    min_evidence: float = 0.8
    dominance: float = 0.65
    negation_window: int = 3
    negation_discount: float = Field(0.3, ge=0, le=1)


class DomainGate(BaseModel):
    """Guards the "this is not an astrology question" case.

    Without it every input is forced into the intent simplex — "What is the
    capital of France?" matched `capital` and returned finance at maximum
    confidence. Deleting the offending term does not fix it; the fallback then
    confidently produces an astrology reading for a geography question.

    `zodiac_terms` are recognised as in-domain but must never appear in an
    intent lexicon: `cancer` is both a disease and a sign, and "my Cancer moon"
    would otherwise classify as health with full confidence.
    """

    personal_markers: list[str] = Field(default_factory=list)
    astro_terms: list[str] = Field(default_factory=list)
    zodiac_terms: list[str] = Field(default_factory=list)
    min_signals: int = 1


class TimeScopePatterns(BaseModel):
    """Literal phrases mapping to a horizon. Matched tokens are consumed so they
    cannot also score as intent evidence."""

    patterns: dict[TimeScope, list[str]] = Field(default_factory=dict)


class IntentsConfig(BaseModel):
    thresholds: ClassifierThresholds = ClassifierThresholds()
    negation_cues: list[str] = Field(default_factory=list)
    domain_gate: DomainGate = DomainGate()
    time_scope: TimeScopePatterns = TimeScopePatterns()
    intents: dict[Intent, IntentLexicon] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _general_is_not_scored(self) -> "IntentsConfig":
        if Intent.GENERAL in self.intents:
            raise ValueError(
                "general must not have a lexicon: it is the absence of an intent, "
                "not a peer. Scoring it lets filler words contaminate dominance."
            )
        return self


# --------------------------------------------------------------------------
# personalization.yaml
# --------------------------------------------------------------------------


class TierWeights(BaseModel):
    """Tier names -> numbers, in one place.

    Config is authored with tier *names* so a domain expert reviews categories
    ("is the 10th house primary for career?") rather than numbers. Keeping the
    numbers here means a global policy change is one edit, not a find-and-replace
    across every intent block.
    """

    primary: float = 1.0
    secondary: float = 0.5


class IntentContextRules(BaseModel):
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_item_in_two_places(self) -> "IntentContextRules":
        overlap = (set(self.primary) & set(self.secondary)) | (
            (set(self.primary) | set(self.secondary)) & set(self.exclude)
        )
        if overlap:
            raise ValueError(f"context keys listed in conflicting tiers: {sorted(overlap)}")
        return self


class LengthRule(BaseModel):
    """max_words is the *minimum* of three caps.

    The third is a hallucination control rather than a product setting: asking
    for 250 words when only two thin facts survived the fan-out makes the model
    invent filler. See tokens_per_word in the engine.
    """

    subscription: dict[str, int] = Field(default_factory=dict)
    intent: dict[Intent, int] = Field(default_factory=dict)
    words_per_context_item: int = 45
    floor: int = 60


class ToneRule(BaseModel):
    """User preference, with per-intent caps.

    "Motivational" must not become cheerleading about a Weak 6th house, so an
    intent may cap which tones are permitted and name a fallback.
    """

    default: str = "neutral"
    allowed_by_intent: dict[Intent, list[str]] = Field(default_factory=dict)
    fallback_by_intent: dict[Intent, str] = Field(default_factory=dict)


class PersonalizationConfig(BaseModel):
    weights: TierWeights = TierWeights()
    # item key -> time scope -> modifier. Only time-varying items appear here;
    # natal data (houses, lagna, moon sign) is permanent and takes no entry.
    time_scope_modifiers: dict[str, dict[TimeScope, float]] = Field(default_factory=dict)
    modifier_cap: float = 0.5
    rules: dict[Intent, IntentContextRules] = Field(default_factory=dict)
    length: LengthRule = LengthRule()
    tone: ToneRule = ToneRule()

    @model_validator(mode="after")
    def _modifiers_within_cap(self) -> "PersonalizationConfig":
        for key, scopes in self.time_scope_modifiers.items():
            for scope, value in scopes.items():
                if abs(value) > self.modifier_cap:
                    raise ValueError(
                        f"time scope modifier {key}.{scope.value}={value} exceeds "
                        f"cap {self.modifier_cap}"
                    )
        return self


# --------------------------------------------------------------------------
# services.yaml
# --------------------------------------------------------------------------


class ServicePolicy(BaseModel):
    """Per-service resilience policy.

    TTLs differ by nature, not by convenience: panchang is global and expires at
    midnight, kundli is per-user and near-immutable, horoscope is per-user
    per-day, the profile changes whenever preferences do. One global TTL would
    be wrong in four different ways.
    """

    timeout_seconds: float = 2.0
    retries: int = Field(1, ge=0)
    backoff_seconds: float = 0.1
    ttl_seconds: int = 300
    criticality: Criticality = Criticality.DEGRADABLE
    serve_stale_on_error: bool = True


class ServicesConfig(BaseModel):
    services: dict[str, ServicePolicy] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------


class AppConfig(BaseModel):
    intents: IntentsConfig
    personalization: PersonalizationConfig
    services: ServicesConfig
