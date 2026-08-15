"""Multi-intent handling, end to end.

Merging is the case that justifies scoring context selection instead of looking
it up: a question that is genuinely about two things needs context from both,
and tier membership has no way to express "secondary for career *and* secondary
for finance, therefore more relevant than either alone".

These tests run the real config. A weight change that quietly collapses a merge
into a single confident intent is a behaviour regression, not a tuning detail —
it silently halves the context the model is given.
"""

from __future__ import annotations

import pytest

from app.confidence import score_confidence
from app.domain import Confidence, ContextBundle, DecisionReason, Intent
from app.engine.planner import plan as build_plan
from app.engine.selector import resolve

# (question, the two intents it is genuinely about)
GENUINELY_AMBIGUOUS = [
    ("Will I get a salary hike this year?", {Intent.CAREER, Intent.FINANCE}),
    ("Should I invest my bonus or change jobs?", {Intent.CAREER, Intent.FINANCE}),
    ("Will my income improve if I switch companies?", {Intent.CAREER, Intent.FINANCE}),
    ("Is work stress affecting my health?", {Intent.CAREER, Intent.HEALTH}),
    ("Will my marriage survive my job pressure?", {Intent.CAREER, Intent.RELATIONSHIP}),
    ("Will my anxiety about debt improve?", {Intent.FINANCE, Intent.HEALTH}),
    ("Should I quit my job for my mental health?", {Intent.CAREER, Intent.HEALTH}),
    ("Will my spouse support my business plans?", {Intent.CAREER, Intent.RELATIONSHIP}),
    ("Can I afford a house with my partner?", {Intent.FINANCE, Intent.RELATIONSHIP}),
]

UNAMBIGUOUS = [
    ("Should I change my job this year?", Intent.CAREER),
    ("How does this month look for my relationship?", Intent.RELATIONSHIP),
    ("What should I focus on for my health?", Intent.HEALTH),
    ("Should I invest in property?", Intent.FINANCE),
]


@pytest.mark.parametrize("question,intents", GENUINELY_AMBIGUOUS)
def test_two_topic_questions_merge_rather_than_pick(classifier, question, intents):
    result = classifier.classify(question)
    assert result.reason is DecisionReason.AMBIGUOUS_MERGED, (
        f"collapsed to {result.primary.value} at dominance "
        f"{result.relative_dominance:.3f}; context from the other intent is lost"
    )
    assert set(result.weights) == intents


@pytest.mark.parametrize("question,intent", UNAMBIGUOUS)
def test_single_topic_questions_do_not_merge(classifier, question, intent):
    """The merge must not fire on everything, or it stops meaning anything and
    every answer pulls in irrelevant context."""
    result = classifier.classify(question)
    assert result.reason is DecisionReason.CONFIDENT_MATCH
    assert result.weights == {intent: 1.0}


@pytest.mark.parametrize("question,_intents", GENUINELY_AMBIGUOUS)
def test_merged_weights_are_a_distribution(classifier, question, _intents):
    """The planner multiplies tier weights by these. If they stop summing to 1
    every context score is silently rescaled and the token budget misbehaves."""
    result = classifier.classify(question)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert all(0 < w < 1 for w in result.weights.values())


def test_merge_pulls_context_from_both_intents(compiled, config, profile, classifier):
    """The point of merging. A salary question must see the 10th house (career)
    and the finance horoscope — picking one intent would drop half of that."""
    result = classifier.classify("Will I get a salary hike this year?")
    plan = build_plan(result, profile, compiled, config.personalization)
    keys = {s.key for s in plan.ranked if s.score > 0}

    assert "house_10" in keys, "career context missing from a career+finance question"
    assert "horoscope_finance" in keys, "finance context missing"
    assert "horoscope_career" in keys


def test_shared_context_is_reinforced_above_either_primary(compiled, config, profile, classifier):
    """dasha_current is secondary (0.5) for career and for finance. Under a merge
    it accumulates from both and should outrank a primary held by only the
    weaker intent. This is the behaviour tier membership cannot express, and the
    reason selection is scored."""
    result = classifier.classify("Will I get a salary hike this year?")
    plan = build_plan(result, profile, compiled, config.personalization)
    scores = {s.key: s.score for s in plan.ranked}

    assert scores["dasha_current"] > scores["horoscope_finance"]


def test_exclusion_survives_a_merge(compiled, config, profile, classifier):
    """Career excludes the relationship horoscope. A merge must not let a second
    intent smuggle back in what the first actively removed — exclusion is a hard
    zero precisely so it cannot be outvoted."""
    result = classifier.classify("Will I get a salary hike this year?")
    plan = build_plan(result, profile, compiled, config.personalization)
    assert "horoscope_relationship" in plan.excluded


def test_merged_intent_caps_confidence(
    classifier, compiled, config, profile, kundli, horoscope, panchang
):
    """An ambiguous read is a weaker read. Even with every source resolved, a
    merge must not report HIGH — that would claim certainty about the intent
    that the classifier explicitly does not have."""
    result = classifier.classify("Will I get a salary hike this year?")
    plan = build_plan(result, profile, compiled, config.personalization)

    bundle = ContextBundle()
    bundle.data = {
        "user": profile, "kundli": kundli, "horoscope": horoscope, "panchang": panchang
    }
    resolved = resolve(plan, bundle, 600)

    assert resolved.selected, "full health should still select context"
    assert score_confidence(result, plan, resolved) is not Confidence.HIGH


def test_merge_is_deterministic(classifier):
    """Same question, same split. A merge that varies would make the selected
    context vary with it."""
    a = classifier.classify("Will I get a salary hike this year?")
    b = classifier.classify("Will I get a salary hike this year?")
    assert a.weights == b.weights
    assert a.relative_dominance == pytest.approx(b.relative_dominance)


def test_dominance_boundary_is_actually_exercised(classifier):
    """Guards against the whole merge path becoming dead config. If every
    question sits at dominance 1.0, the threshold is untested and could be any
    value at all."""
    dominances = [
        classifier.classify(q).relative_dominance for q, _ in GENUINELY_AMBIGUOUS
    ]
    assert all(d < 0.65 for d in dominances)
    assert any(0.5 <= d < 0.65 for d in dominances), "no case near the boundary"
