"""How well-grounded is this answer in the data we actually had?

Two inputs, both already computed by the time we get here:

1. **Primary-source coverage** — did the items the engine most wanted actually
   make it into the prompt?
2. **Intent certainty** — did the classifier know what was being asked?

Deliberately not a third input: the LLM's own opinion. Models are badly
calibrated at self-reporting confidence and will happily rate a hallucination
9/10. Deriving it here also makes it reproducible and testable without a network
call, which is the whole reason /debug endpoints can exist.

Also deliberately not an input: astrological house strength. That was considered
and rejected. Confidence is a statement about our data pipeline, not about the
reading — "your 7th house is weak" delivered from complete, fresh, unambiguous
data is a HIGH confidence answer. Conflating the two would mean every difficult
reading looked like a system failure, and users would learn to read our
confidence badge as a horoscope rather than as a quality signal.
"""

from __future__ import annotations

from app.domain import (
    ClassificationResult,
    Confidence,
    DecisionReason,
    PersonalizationPlan,
    ResolvedContext,
)

# How many of the top-ranked candidates count as "primary sources".
#
# Using the head of the ranking rather than the config's primary/secondary tier
# boundary keeps this module independent of personalization.yaml — confidence
# should not change meaning because someone retuned a tier weight. Three
# approximates "the primary items plus the first backup", which is the set whose
# absence a reader would actually notice.
PRIMARY_SOURCE_SAMPLE = 3

# Coverage thresholds. 2-of-3 primary sources present is a usable answer;
# 1-of-3 means we are mostly extrapolating and should say so.
_HIGH_COVERAGE = 0.8
_MEDIUM_COVERAGE = 0.4

_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

# The classifier's own verdict caps the result regardless of coverage: perfect
# data answering the wrong question is not a confident answer.
_CAP_BY_REASON = {
    DecisionReason.CONFIDENT_MATCH: Confidence.HIGH,
    # A merged intent means we hedged across two readings — defensible, but the
    # user asked one question and we did not establish which.
    DecisionReason.AMBIGUOUS_MERGED: Confidence.MEDIUM,
    # No evidence at all; the general fallback is a guess dressed as a default.
    DecisionReason.NO_EVIDENCE_DEFAULT: Confidence.MEDIUM,
    DecisionReason.OUT_OF_DOMAIN: Confidence.LOW,
}


def score_confidence(
    classification: ClassificationResult,
    plan: PersonalizationPlan,
    resolved: ResolvedContext,
) -> Confidence:
    cap = _CAP_BY_REASON.get(classification.reason, Confidence.MEDIUM)
    if cap is Confidence.LOW:
        # Out of domain: coverage is irrelevant, we are answering a question we
        # do not believe is ours to answer.
        return Confidence.LOW

    coverage = _primary_coverage(plan, resolved)
    if not resolved.selected:
        grounding = Confidence.LOW
    elif coverage >= _HIGH_COVERAGE:
        grounding = Confidence.HIGH
    elif coverage >= _MEDIUM_COVERAGE:
        grounding = Confidence.MEDIUM
    else:
        grounding = Confidence.LOW

    return grounding if _RANK[grounding] <= _RANK[cap] else cap


def _primary_coverage(plan: PersonalizationPlan, resolved: ResolvedContext) -> float:
    """Fraction of the top-ranked candidates that reached the prompt.

    Absence is counted the same whether the item was unavailable (an upstream
    failed) or dropped for budget: the distinction matters for debugging, but
    for confidence the only fact is that the model did not see it. Excluded
    items never appear in `ranked`, so a rule-based exclusion cannot depress
    confidence — that was a decision, not a loss.
    """
    primary = [scored.key for scored in plan.ranked[:PRIMARY_SOURCE_SAMPLE]]
    if not primary:
        # Nothing was even a candidate — there is no chart data to be confident
        # about, whatever the classifier thought.
        return 0.0

    selected = {item.key for item in resolved.selected}
    return sum(key in selected for key in primary) / len(primary)


def sources_used(resolved: ResolvedContext) -> list[str]:
    """Display names of the context items that went into the prompt.

    Derived from selection, never from the model's output. Asking a model to
    cite its sources invites invented citations — and it is unnecessary here,
    because what we sent is a fact we own. This list is true by construction.

    Order follows selection order, which is descending relevance, so the most
    important source is first.
    """
    return [item.display_name for item in resolved.selected]
