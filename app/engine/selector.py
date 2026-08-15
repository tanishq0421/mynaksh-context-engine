"""Phase 2 of personalization: resolve the plan against what actually arrived.

The planner decided what *should* matter. This walks that ranking in descending
order and finds out what is real, spending a token budget as it goes.

Backfill falls out for free, and that is the argument that decided scoring over
plain tier membership. If house_10 is unavailable there is nothing to repair —
the walk simply continues down the ranked list until the budget fills, and a
0.5 item takes the place a 1.0 item could not. With tier sets this needs
explicit "primary tier came back short, go fetch from secondary" logic, and
that logic is where the ordering bugs live.
"""

from __future__ import annotations

from app.domain import ContextBundle, ContextItem, PersonalizationPlan, ResolvedContext
from app.engine.registry import REGISTRY


def resolve(
    plan: PersonalizationPlan,
    bundle: ContextBundle,
    token_budget: int,
) -> ResolvedContext:
    selected: list[ContextItem] = []
    values: dict[str, object] = {}
    excluded: list[str] = []
    unavailable: list[str] = []
    dropped_for_budget: list[str] = []
    tokens_used = 0

    excluded_keys = set(plan.excluded)

    for scored in plan.ranked:
        item = REGISTRY.get(scored.key)
        if item is None:
            # Compilation validates config against the registry, so this can
            # only mean a plan outlived a registry change. Skip rather than
            # crash a live request over it.
            continue

        # Exclusion means the rules actively judged this harmful to include —
        # relationship context in a career prompt makes the model drift into it.
        # That is an answer-quality decision, and it is not the same thing as an
        # item that simply never scored for this intent. Reporting both here
        # would make "we deliberately kept this out" indistinguishable from "it
        # was never a candidate", so only rule exclusions land in `excluded`.
        if item.key in excluded_keys:
            excluded.append(item.key)
            continue

        # Scored out of contention: irrelevant to this intent, or pushed under
        # by a negative time-scope modifier. Not a decision worth reporting —
        # the debug endpoint's ranked list already shows every item and score.
        if scored.score <= 0:
            continue

        # An upstream let us down — either the whole service failed, or it
        # answered 200 with this particular slice missing. One branch, because
        # from here the two are the same fact: no value to render.
        if item.service in bundle.failures:
            unavailable.append(item.key)
            continue

        value = item.extractor(bundle.payload(item.service))
        if value is None:
            unavailable.append(item.key)
            continue

        # Relevant, real, but does not fit. Note the `continue`: a cheaper
        # lower-ranked item may still fit, and stopping here would waste the
        # tail of the budget on nothing.
        if tokens_used + item.token_cost > token_budget:
            dropped_for_budget.append(item.key)
            continue

        selected.append(item)
        values[item.key] = value
        tokens_used += item.token_cost

    return ResolvedContext(
        selected=selected,
        values=values,
        excluded=excluded,
        unavailable=unavailable,
        dropped_for_budget=dropped_for_budget,
        tokens_used=tokens_used,
    )
