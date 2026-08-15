"""Turns a resolved context into the two strings the model actually sees.

The selection work upstream is only as good as this step: sending the whole
upstream payload "just in case" would undo every decision the engine made, and
is also where hallucinated detail comes from — a model given a raw kundli JSON
will happily narrate fields nobody selected.

So the rule here is strict: only `resolved.selected`, only through each item's
registered renderer, never raw JSON.
"""

from __future__ import annotations

import logging

from app.domain import ContextItem, PersonalizationPlan, ResolvedContext, TimeScope

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Prompt vocabulary
# --------------------------------------------------------------------------
# These labels are a contract, not decoration: the mock provider reads the
# prompt back to compose its answer, so builder and mock must agree on the
# markers. Defining them once here is what keeps the two from drifting.

LANGUAGE_PREFIX = "Language:"
TONE_PREFIX = "Tone:"
HORIZON_PREFIX = "Time horizon:"
NAME_PREFIX = "Person asking:"
QUESTION_PREFIX = "Question:"
CONTEXT_HEADER = "CONTEXT — the only facts you may use:"
CONTEXT_BULLET = "- "
NO_CONTEXT_MARKER = "(no context available)"


# The horizon is one line of prompt and it changes the answer materially: the
# same 10th-house reading is "this week" advice or a "coming year" arc depending
# only on this. Without it the model defaults to a vague forever-tense.
_HORIZON_PHRASES: dict[TimeScope, str] = {
    TimeScope.TODAY: "today",
    TimeScope.THIS_WEEK: "the week ahead",
    TimeScope.THIS_MONTH: "the month ahead",
    TimeScope.THIS_YEAR: "the coming year",
    # Not "any horizon you like" — an unspecified scope is an invitation to
    # invent dates, so we forbid committing to one instead.
    TimeScope.UNSPECIFIED: "no specific timeframe",
}


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 characters per token.

    Good enough for budgeting and logging, and it costs nothing. The production
    upgrade is the provider's own tokenizer (Anthropic's count_tokens endpoint,
    tiktoken for OpenAI) — worth doing once budgets are enforced in money rather
    than in a config constant, but not before, since a network call per request
    to measure a request is a poor trade.
    """
    return max(1, len(text) // 4)


def build_prompt(
    plan: PersonalizationPlan,
    resolved: ResolvedContext,
    question: str,
    user_name: str,
) -> tuple[str, str]:
    """Return (system, user).

    Raises KeyError if a selected item has no resolved value — that is a broken
    invariant in the selector, not a runtime condition to paper over.
    """
    rendered = [(item, item.renderer(resolved.values[item.key])) for item in resolved.selected]

    system = _system_prompt(plan)
    user = _user_prompt(plan, rendered, question, user_name)

    _log_size(rendered, system, user, resolved)
    return system, user


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _system_prompt(plan: PersonalizationPlan) -> str:
    horizon = _HORIZON_PHRASES.get(plan.time_scope, _HORIZON_PHRASES[TimeScope.UNSPECIFIED])

    # Grounding rules are numbered and phrased as prohibitions with a stated
    # consequence, because "be accurate" is not an instruction a model can act
    # on. Rule 4 matters most: without an explicit permission to say "I don't
    # have enough", a thin context produces confident fiction.
    return "\n".join(
        [
            "You are an astrologer answering one question for one person.",
            "",
            "Rules:",
            "1. Answer only from the CONTEXT block in the user message. It is the",
            "   complete set of facts available to you.",
            "2. Do not invent astrological detail. No placements, dashas, transits,",
            "   nakshatras or dates beyond what the context states.",
            "3. Do not refer to sources that were not provided. If the context says",
            "   nothing about a house, do not mention that house.",
            "4. If the context is insufficient to answer the question, say so plainly",
            "   in a sentence or two and stop. A short honest answer is correct here;",
            "   padding it out is not.",
            "5. Weave the context into ordinary prose. Do not echo it back as a list,",
            "   and do not caveat every sentence.",
            "",
            f"{LANGUAGE_PREFIX} {plan.language} — reply entirely in this language.",
            f"{TONE_PREFIX} {plan.tone}",
            f"{HORIZON_PREFIX} {horizon} — frame the answer over this horizon and no further.",
            f"Length: at most {plan.max_words} words.",
        ]
    )


def _user_prompt(
    plan: PersonalizationPlan,
    rendered: list[tuple[ContextItem, str]],
    question: str,
    user_name: str,
) -> str:
    lines = [
        f"{NAME_PREFIX} {user_name}",
        f"{QUESTION_PREFIX} {question.strip()}",
        "",
        CONTEXT_HEADER,
    ]
    if rendered:
        lines += [f"{CONTEXT_BULLET}{item.display_name}: {phrase}" for item, phrase in rendered]
    else:
        # An explicit marker rather than an empty section: a blank list reads as
        # a formatting glitch, and the model tries to be helpful by filling it.
        lines.append(f"{CONTEXT_BULLET}{NO_CONTEXT_MARKER}")
    return "\n".join(lines)


def _log_size(
    rendered: list[tuple[ContextItem, str]],
    system: str,
    user: str,
    resolved: ResolvedContext,
) -> None:
    """Prompt size is the number that explains a cost or latency regression.

    Logged per request because the interesting failure is gradual — one more
    item registered per sprint until the budget quietly stops binding.
    """
    context_tokens = sum(estimate_tokens(f"{item.display_name}: {phrase}") for item, phrase in rendered)
    system_tokens = estimate_tokens(system)
    user_tokens = estimate_tokens(user)

    logger.info(
        "prompt built",
        extra={
            "event": "prompt_built",
            "items": len(rendered),
            "context_tokens": context_tokens,
            "system_tokens": system_tokens,
            "user_tokens": user_tokens,
            "total_prompt_tokens": system_tokens + user_tokens,
            "selector_budget_spend": resolved.tokens_used,
            "unavailable": len(resolved.unavailable),
            "dropped_for_budget": len(resolved.dropped_for_budget),
        },
    )
