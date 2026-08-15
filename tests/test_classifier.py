"""Classifier behaviour.

These cases are the ones a paper trace found broken before implementation:
homonyms, negation, paraphrase gaps, and the out-of-domain hole. They are
regression tests for specific defects, not coverage for its own sake.
"""

from __future__ import annotations

import pytest

from app.domain import DecisionReason, Intent, TimeScope


@pytest.mark.parametrize(
    "question,intent,scope",
    [
        ("Should I consider changing my job this year?", Intent.CAREER, TimeScope.THIS_YEAR),
        ("How does this month look for my relationship?", Intent.RELATIONSHIP, TimeScope.THIS_MONTH),
        ("What should I focus on for my health?", Intent.HEALTH, TimeScope.UNSPECIFIED),
        ("What should I prioritize this week?", Intent.GENERAL, TimeScope.THIS_WEEK),
        ("Can you summarize today's guidance?", Intent.GENERAL, TimeScope.TODAY),
    ],
)
def test_spec_sample_questions(classifier, question, intent, scope):
    result = classifier.classify(question)
    assert result.primary is intent
    assert result.time_scope is scope


def test_weights_always_sum_to_one(classifier):
    """The engine multiplies tier weights by these, so a drift here silently
    rescales every context score."""
    for question in [
        "Should I change my job?",
        "Will I get a salary hike this year?",
        "What should I prioritize this week?",
        "What is the capital of France?",
    ]:
        total = sum(classifier.classify(question).weights.values())
        assert total == pytest.approx(1.0)


def test_out_of_domain_question_is_refused(classifier):
    """Without the gate this matched a finance term and returned finance at
    maximum confidence."""
    result = classifier.classify("What is the capital of France?")
    assert result.in_domain is False
    assert result.reason is DecisionReason.OUT_OF_DOMAIN


def test_zodiac_homonym_does_not_become_health(classifier):
    """`cancer` is a sign as well as a disease. In an astrology app this is a
    high-frequency, high-confidence misclassification if the lexicon allows it."""
    result = classifier.classify("What does my Cancer moon sign mean?")
    assert result.primary is not Intent.HEALTH


def test_substring_does_not_match_inside_word(classifier):
    """Token-level matching: `art` must not fire inside `heart`."""
    result = classifier.classify("My heart rate has been high lately")
    assert result.primary in (Intent.HEALTH, Intent.GENERAL)


def test_paraphrase_without_keyword(classifier):
    """`switch companies` has no `job`/`career` token. This fell through to
    general before the lexicon carried paraphrase vocabulary."""
    assert classifier.classify("Should I switch companies?").primary is Intent.CAREER


def test_negation_shifts_topic(classifier):
    result = classifier.classify(
        "I'm not worried about work, it's my health I'm concerned about"
    )
    assert result.primary is Intent.HEALTH


def test_negation_does_not_suppress_the_only_topic(classifier):
    """Negation scopes over sentiment, not topic — "will I not lose my job" is
    still a career question. Gating on the discounted weight would drop it to
    general, which is the boolean zeroing this design rejected."""
    result = classifier.classify("Will I not lose my job this year?")
    assert result.primary is Intent.CAREER


def test_genuine_overlap_is_reported_as_ambiguous(classifier):
    """A salary question is legitimately career and finance. Forcing a single
    winner would discard half the relevant context."""
    result = classifier.classify("Will I get a salary hike this year?")
    assert result.reason is DecisionReason.AMBIGUOUS_MERGED
    assert set(result.weights) == {Intent.CAREER, Intent.FINANCE}


def test_dominance_is_margin_form_not_share_of_total(classifier):
    """A clear winner must stay confident regardless of how many intents exist,
    otherwise adding an intent would silently make existing questions ambiguous."""
    result = classifier.classify("Should I resign from my job?")
    assert result.reason is DecisionReason.CONFIDENT_MATCH
    assert result.relative_dominance >= 0.65


@pytest.mark.parametrize("question", ["", "   ", "?!?", "a"])
def test_degenerate_input_is_safe(classifier, question):
    result = classifier.classify(question)
    assert result.primary is Intent.GENERAL


def test_trace_is_populated_for_debug_endpoint(classifier):
    """The matched-term trace is what /debug/personalization shows and what
    would bootstrap a labelled dataset in production."""
    result = classifier.classify("Should I consider changing my job?")
    matched = [s for s in result.ranked if s.matched_terms]
    assert matched and any(s.intent is Intent.CAREER for s in matched)
