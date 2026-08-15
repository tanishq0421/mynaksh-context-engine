"""Phase 1 of personalization: the static plan.

This function takes no upstream data and has no idea which services succeeded.
That purity is the point. /debug/personalization can replay any question and
get byte-identical output because nothing here depends on the weather at
kundli-service; the live path then adapts in Phase 2 (selector.py), where
reality is allowed in.

Scoring, stated once:

    score(item) = SUM over active intents [ intent_weight * tier_weight(item, intent) ]
                + time_scope_modifier(item, scope)
    then hard-zero if excluded by ANY active intent      # applied last

This is not a different model from "primary and secondary tiers". It is the
same tiers with the merge arithmetic written down. Single intent is the
degenerate case: one weight of 1.0 collapses every score back to exactly 1.0 or
0.5, item for item identical to plain tier membership. The sum only starts
doing work when the classifier is genuinely unsure and hands back 0.6/0.4 — and
then an item that is merely secondary for *both* intents can legitimately
out-rank one that is primary for the weaker intent alone.
"""

from __future__ import annotations

from typing import Any

from app.config_schema import PersonalizationConfig
from app.domain import (
    ClassificationResult,
    Intent,
    PersonalizationPlan,
    ScoredItem,
)
from app.engine.compiler import CompiledRules
from app.engine.registry import REGISTRY

# Scores are built from small floats; 0.6*0.5 + 0.4*0.5 + 0.4 lands on
# 0.9000000000000001 without this. Rounding keeps the debug output readable and
# keeps ties actually tying.
_PRECISION = 6

# Used when config declines to cap a dimension: min() then simply ignores it.
_NO_CAP = 10_000


def plan(
    classification: ClassificationResult,
    user_profile: dict[str, Any],
    compiled: CompiledRules,
    config: PersonalizationConfig,
) -> PersonalizationPlan:
    weights = classification.weights
    scope = classification.time_scope

    ranked: list[ScoredItem] = []
    excluded: list[str] = []

    for key in REGISTRY:
        score = 0.0
        reasons: list[str] = []

        for intent, intent_weight in weights.items():
            tier_weight = compiled.weight_for(key, intent)
            if not tier_weight:
                continue
            contribution = intent_weight * tier_weight
            score += contribution
            tier = compiled.tier_for(key, intent)
            if intent_weight == 1.0:
                # Single-intent: show the bare tier weight, because that is
                # literally what the score is.
                reasons.append(f"{intent.value} {tier} ({tier_weight:.1f})")
            else:
                reasons.append(
                    f"{intent.value} {tier} "
                    f"({tier_weight:.1f} x {intent_weight:.2f} = {contribution:.2f})"
                )

        # Applied to every item, including ones with no intent weight at all:
        # "what about this year?" should push panchang_today below zero and out
        # of contention entirely, not merely fail to promote it.
        modifier = config.time_scope_modifiers.get(key, {}).get(scope, 0.0)
        if modifier:
            score += modifier
            reasons.append(f"{scope.value} modifier ({modifier:+.2f})")

        # Last, and absolute. A subtractive penalty here could be outvoted by a
        # strong enough weight from a co-active intent; a hard zero cannot.
        blockers = compiled.excluded_by(key, weights)
        if blockers:
            score = 0.0
            reasons.append("excluded by " + ", ".join(i.value for i in blockers))
            excluded.append(key)

        ranked.append(ScoredItem(key=key, score=round(score, _PRECISION), reasons=reasons))

    # Alphabetical tie-break so equal scores rank identically on every run —
    # a plan that reshuffles between calls is not reproducible.
    ranked.sort(key=lambda s: (-s.score, s.key))

    positive_items = sum(1 for s in ranked if s.score > 0)

    return PersonalizationPlan(
        intent=classification.primary,
        weights=dict(weights),
        time_scope=scope,
        language=_language(user_profile),
        tone=_tone(user_profile, weights, config),
        max_words=_max_words(user_profile, classification.primary, positive_items, config),
        ranked=ranked,
        excluded=excluded,
    )


def _language(user_profile: dict[str, Any]) -> str:
    """Profile language wins; English is the fallback when the profile is thin
    or the user service degraded to a stub."""
    lang = user_profile.get("language")
    return lang if isinstance(lang, str) and lang.strip() else "en"


def _tone(
    user_profile: dict[str, Any],
    weights: dict[Intent, float],
    config: PersonalizationConfig,
) -> str:
    """User preference, then per-intent guardrails.

    The guardrail exists because "motivational" must not become cheerleading
    about a Weak 6th house — the same preference that is delightful on a career
    question is irresponsible on a health one.

    Every ACTIVE intent gets a veto, not just the primary. On a 0.6 career /
    0.4 health merge the answer still discusses health, so health's cap still
    has to bind. Intents are applied in ascending weight order so that when
    several would substitute, the strongest intent's fallback is the one left
    standing.
    """
    tone = user_profile.get("tonePreference") or config.tone.default

    for intent, _ in sorted(weights.items(), key=lambda kv: kv[1]):
        allowed = config.tone.allowed_by_intent.get(intent)
        if allowed and tone not in allowed:
            tone = config.tone.fallback_by_intent.get(intent, config.tone.default)

    return tone


def _max_words(
    user_profile: dict[str, Any],
    intent: Intent,
    positive_items: int,
    config: PersonalizationConfig,
) -> int:
    """The MINIMUM of three caps, floored.

    Subscription is commercial, intent target is editorial, and the third —
    words_per_context_item * surviving items — is a HALLUCINATION CONTROL, not
    a product setting. Ask for 250 words when the fan-out yielded two thin
    facts and the model will not return 80; it will invent 250 words' worth of
    astrology. Capping supply to the evidence is cheaper than detecting the
    invention afterwards.
    """
    length = config.length

    subscription = user_profile.get("subscription")
    sub_cap = length.subscription.get(subscription) if isinstance(subscription, str) else None
    if sub_cap is None:
        # Unknown or missing tier fails closed to the most restrictive
        # configured cap: under-delivering to a paying user is a support
        # ticket, over-delivering to a free one is a billing problem.
        sub_cap = min(length.subscription.values(), default=_NO_CAP)

    intent_cap = length.intent.get(intent, _NO_CAP)
    volume_cap = length.words_per_context_item * positive_items

    return max(length.floor, min(sub_cap, intent_cap, volume_cap))
