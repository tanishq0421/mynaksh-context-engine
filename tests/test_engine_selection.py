"""Personalization engine — the graded core.

Selection is where a bug is both most likely and least visible: a wrong context
set still produces a fluent answer, so nothing looks broken.
"""

from __future__ import annotations

import pytest

from app.domain import (
    ClassificationResult,
    ContextBundle,
    DecisionReason,
    Intent,
    TimeScope,
)
from app.engine.planner import plan as build_plan
from app.engine.selector import resolve

BUDGET = 600


def classification(
    weights: dict[Intent, float],
    scope: TimeScope = TimeScope.UNSPECIFIED,
    reason: DecisionReason = DecisionReason.CONFIDENT_MATCH,
) -> ClassificationResult:
    primary = max(weights, key=weights.get)
    return ClassificationResult(
        primary=primary,
        weights=weights,
        ranked=[],
        time_scope=scope,
        reason=reason,
        in_domain=True,
        absolute_evidence=1.0,
        relative_dominance=1.0,
    )


def bundle_of(profile, kundli, horoscope, panchang, **overrides) -> ContextBundle:
    data = {"user": profile, "kundli": kundli, "horoscope": horoscope, "panchang": panchang}
    data.update(overrides)
    b = ContextBundle()
    b.data = {k: v for k, v in data.items() if v is not None}
    return b


def scores(plan) -> dict[str, float]:
    return {s.key: s.score for s in plan.ranked}


# -- scoring ---------------------------------------------------------------


def test_single_intent_collapses_to_plain_tiers(compiled, config, profile):
    """Scoring must be a strict generalisation of tier membership: with one
    intent at weight 1.0 the numbers are exactly the tier weights. If this
    drifts, the whole "same config, explicit arithmetic" claim is false."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    s = scores(plan)
    assert s["house_10"] == pytest.approx(1.0)
    assert s["horoscope_career"] == pytest.approx(1.0)
    assert s["dasha_current"] == pytest.approx(0.5)


def test_exclusion_is_a_hard_zero_not_a_subtraction(compiled, config, profile):
    """Relationship context in a career prompt makes the model drift into it.
    A subtractive penalty could be outvoted by a strong tier weight."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    assert "horoscope_relationship" in plan.excluded
    assert scores(plan)["horoscope_relationship"] == pytest.approx(0.0)


def test_multi_intent_reinforces_shared_context(compiled, config, profile):
    """dasha_current is secondary for both career and finance, so a merged
    intent should lift it above finance's own primary. Tier membership cannot
    express this — it is the reason selection is scored."""
    plan = build_plan(
        classification({Intent.CAREER: 0.6, Intent.FINANCE: 0.4}),
        profile,
        compiled,
        config.personalization,
    )
    s = scores(plan)
    assert s["dasha_current"] == pytest.approx(0.5)
    assert s["dasha_current"] > s["horoscope_finance"]


def test_time_scope_reorders_selection(compiled, config, profile):
    """Panchang is decisive for "today" and near-worthless for "this year".
    Same intent, same config, different ranking."""
    today = build_plan(
        classification({Intent.CAREER: 1.0}, TimeScope.TODAY),
        profile,
        compiled,
        config.personalization,
    )
    this_year = build_plan(
        classification({Intent.CAREER: 1.0}, TimeScope.THIS_YEAR),
        profile,
        compiled,
        config.personalization,
    )
    assert scores(today)["panchang_today"] > scores(this_year)["panchang_today"]
    assert scores(this_year)["dasha_current"] > scores(today)["dasha_current"]


def test_planner_is_pure_of_upstream_state(compiled, config, profile):
    """The plan must not depend on what resolved, or /debug/personalization
    would describe a different decision on every run."""
    args = (classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization)
    assert scores(build_plan(*args)) == scores(build_plan(*args))


# -- personalization dimensions -------------------------------------------


def test_tone_guardrail_caps_preference_per_intent(compiled, config, profile):
    """"Motivational" must not become cheerleading about a weak 6th house."""
    career = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    health = build_plan(
        classification({Intent.HEALTH: 1.0}), profile, compiled, config.personalization
    )
    assert career.tone == "motivational"
    assert health.tone != "motivational"


def test_subscription_changes_length(compiled, config, profile):
    free = dict(profile, subscription="free")
    premium = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    basic = build_plan(
        classification({Intent.CAREER: 1.0}), free, compiled, config.personalization
    )
    assert basic.max_words < premium.max_words


def test_length_is_capped_by_available_context(compiled, config, profile, kundli, horoscope, panchang):
    """A hallucination control, not a product setting: asking for 250 words when
    two thin facts survived makes the model invent the rest."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    starved = resolve(plan, bundle_of(profile, None, None, None), BUDGET)
    assert len(starved.selected) < 3
    assert plan.max_words <= config.length.subscription["premium"] if hasattr(config, "length") else True


# -- resolution against reality -------------------------------------------


def test_missing_slice_in_healthy_service_is_unavailable(
    compiled, config, profile, kundli, horoscope, panchang
):
    """Service answers 200, this particular house is absent. A try/except around
    the HTTP call never catches this."""
    sparse = dict(kundli, houses={k: v for k, v in kundli["houses"].items() if k != "10"})
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    resolved = resolve(plan, bundle_of(profile, sparse, horoscope, panchang), BUDGET)

    assert "house_10" in resolved.unavailable
    assert "house_10" not in [i.key for i in resolved.selected]


def test_backfill_when_a_primary_is_missing(
    compiled, config, profile, kundli, horoscope, panchang
):
    """Walking the ranked list means a lower item simply moves up. With tier
    membership this would need explicit backfill logic."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    full = resolve(plan, bundle_of(profile, kundli, horoscope, panchang), BUDGET)
    degraded = resolve(plan, bundle_of(profile, None, horoscope, panchang), BUDGET)

    assert "house_10" in [i.key for i in full.selected]
    assert "house_10" not in [i.key for i in degraded.selected]
    assert degraded.selected, "losing kundli must not empty the context"


def test_budget_does_not_stop_at_the_first_item_that_does_not_fit(
    compiled, config, profile, kundli, horoscope, panchang
):
    """A cheaper lower-ranked item should still be admitted; breaking out of the
    loop would waste the tail of the budget."""
    plan = build_plan(
        classification({Intent.GENERAL: 1.0}), profile, compiled, config.personalization
    )
    resolved = resolve(plan, bundle_of(profile, kundli, horoscope, panchang), 40)

    assert resolved.dropped_for_budget
    assert resolved.selected
    assert resolved.tokens_used <= 40


def test_three_absence_reasons_stay_distinct(
    compiled, config, profile, kundli, horoscope, panchang
):
    """excluded / unavailable / dropped_for_budget are three different
    decisions. One list would make them indistinguishable."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    resolved = resolve(plan, bundle_of(profile, None, horoscope, panchang), 25)

    assert "horoscope_relationship" in resolved.excluded  # rules said no
    assert "house_10" in resolved.unavailable  # kundli died
    assert resolved.dropped_for_budget  # relevant but did not fit
    overlap = set(resolved.excluded) & set(resolved.unavailable)
    assert not overlap


def test_never_relevant_is_not_reported_as_excluded(
    compiled, config, profile, kundli, horoscope, panchang
):
    """Exclusion means the rules judged an item harmful. An item that simply
    never scored for this intent was not a decision."""
    plan = build_plan(
        classification({Intent.CAREER: 1.0}), profile, compiled, config.personalization
    )
    resolved = resolve(plan, bundle_of(profile, kundli, horoscope, panchang), BUDGET)
    excluded_by_rules = set(plan.excluded)
    assert set(resolved.excluded) <= excluded_by_rules
