"""A deterministic stand-in for a language model. NOT a language model.

There is no generation here and no network call — this composes a sentence per
selected context item from a fixed set of templates. It exists because the
default configuration has no API key, so this is the path a reviewer actually
runs, and a demo that returns "lorem ipsum" would hide the one thing worth
demonstrating: which context the engine chose.

So it deliberately echoes the supplied context back. If the selection worked,
you can read that off the answer; if the wrong items were chosen, the answer
says so in plain English. That makes it a debugging surface, not just a filler.

Determinism is a hard requirement, not a convenience: identical input must give
byte-identical output so tests can assert on it and two runs of the demo can be
compared. Nothing here consults the clock, the RNG, or `hash()`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.llm.base import LLMClient, LLMResponse
from app.prompt.builder import (
    CONTEXT_BULLET,
    CONTEXT_HEADER,
    HORIZON_PREFIX,
    NAME_PREFIX,
    NO_CONTEXT_MARKER,
    QUESTION_PREFIX,
    TONE_PREFIX,
    estimate_tokens,
)


@dataclass(frozen=True)
class _Voice:
    """The minimum needed to make tone visible in the output.

    Tone is a real product setting — health readings must not sound like a pep
    talk — so a mock that ignored it would hide tone bugs until a real key was
    plugged in.
    """

    opening: str  # takes {name} and {horizon}
    # leads[0] introduces the first fact; the rest rotate. Splitting them out
    # matters — "Alongside that," as the opening clause of a reading is the kind
    # of seam that makes generated text obviously generated.
    leads: tuple[str, ...]
    closing: str
    thin: str  # used when no context survived selection


_NEUTRAL = _Voice(
    opening="{name}, here is what the available chart data suggests for {horizon}.",
    leads=("Starting with", "There is also", "Alongside that,", "Set against that,"),
    closing="Treat these as tendencies to work with rather than fixed outcomes.",
    thin="There is not enough chart data available right now to answer that properly.",
)

_VOICES: dict[str, _Voice] = {
    "neutral": _NEUTRAL,
    "warm": _Voice(
        opening="{name}, let's look at what your chart is saying for {horizon}.",
        leads=("The clearest signal is", "You also have", "Sitting alongside that,", "And there is"),
        closing="None of this is fixed — it is the ground you are working on.",
        thin="I do not have enough of your chart in front of me to answer that honestly.",
    ),
    "calm": _Voice(
        opening="{name}, taking {horizon} steadily, this is what the chart holds.",
        leads=("Begin with", "There is also", "Held alongside that,", "Quietly behind that,"),
        closing="Move at a pace that suits you; nothing here asks to be rushed.",
        thin="The chart data available is too thin to say anything useful about that.",
    ),
    "supportive": _Voice(
        opening="{name}, here is an honest read of {horizon} from what your chart shows.",
        leads=("Most of the weight sits on", "You also carry", "Together with that,", "There is also"),
        closing="Take what is useful here and set the rest aside.",
        thin="I would rather say nothing than guess: there is not enough chart data for this one.",
    ),
    "motivational": _Voice(
        opening="{name}, {horizon} has something to work with — here is what the chart shows.",
        leads=("Your strongest card is", "You also hold", "Adding to that,", "Backing that up,"),
        closing="The openings are there; the effort is the part that is yours.",
        thin="There is not enough chart data to build anything on for that question yet.",
    ),
}


class MockLLMClient(LLMClient):
    """Composes a plausible reading from the prompt. No model involved."""

    name = "mock"

    def __init__(self, model: str = "deterministic-stub-v1") -> None:
        self._model = model

    async def generate(self, system: str, user: str, max_words: int) -> LLMResponse:
        voice = _VOICES.get(_field(system, TONE_PREFIX).lower(), _NEUTRAL)
        horizon = _field(system, HORIZON_PREFIX).split("—")[0].strip() or "the period ahead"
        name = _field(user, NAME_PREFIX) or "Friend"
        question = _field(user, QUESTION_PREFIX)
        facts = _context_bullets(user)

        text = _compose(voice, name, horizon, question, facts, max_words)

        return LLMResponse(
            text=text,
            provider=self.name,
            model=self._model,
            input_tokens=estimate_tokens(system) + estimate_tokens(user),
            output_tokens=estimate_tokens(text),
            # Fixed, not measured. A stand-in has no latency worth reporting,
            # and a real elapsed time would break the byte-identical guarantee
            # this class exists to provide.
            latency_ms=0.0,
        )


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def _compose(
    voice: _Voice,
    name: str,
    horizon: str,
    question: str,
    facts: list[str],
    max_words: int,
) -> str:
    if not facts:
        # Mirrors rule 4 of the system prompt. The insufficient-context path is
        # the one most likely to be wrong in production, so the mock exercises
        # it rather than papering over it with generic filler.
        return _fit([voice.thin], max_words)

    # Rotating the connectives by a hash of the question keeps output varied
    # across questions while staying identical for the same one. hashlib, not
    # hash(): the builtin is salted per process, so it would differ per run.
    offset = int.from_bytes(hashlib.blake2s(question.encode(), digest_size=2).digest(), "big")
    rotating = voice.leads[1:]

    sentences = [voice.opening.format(name=name, horizon=horizon)]
    for i, fact in enumerate(facts):
        lead = voice.leads[0] if i == 0 else rotating[(offset + i) % len(rotating)]
        sentences.append(f"{lead} {_as_clause(fact)}.")
    sentences.append(voice.closing)

    return _fit(sentences, max_words)


def _as_clause(fact: str) -> str:
    """`Moon Sign: Taurus, steady` -> `your moon sign: Taurus, steady`.

    Keeps the registry's rendered phrase intact — that phrase is the evidence
    the selection worked — while framing it as prose rather than a printed
    field. A mock that dumps the bullets verbatim proves the context arrived but
    not that it can be used.
    """
    label, _, value = fact.partition(":")
    if not value.strip():
        return fact.rstrip(".").strip()
    return f"your {label.strip().lower()}: {value.strip().rstrip('.')}"


def _fit(sentences: list[str], max_words: int) -> str:
    """Respect the word cap the plan computed — it is a real product constraint.

    Drops whole sentences rather than cutting mid-clause: a reading that ends
    "There is also 10th" is a visible bug, and the cap exists to keep the answer
    proportionate to the evidence, not to hit an exact count. The first sentence
    is kept even if it overruns, word-clipped as a last resort, so a very small
    cap still produces something addressed to the user.
    """
    kept: list[str] = []
    used = 0
    for sentence in sentences:
        length = len(sentence.split())
        if kept and used + length > max_words:
            break
        kept.append(sentence)
        used += length

    text = " ".join(kept)
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:— ") + "…"


# --------------------------------------------------------------------------
# Reading the prompt back
# --------------------------------------------------------------------------


def _field(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _context_bullets(user: str) -> list[str]:
    lines = user.splitlines()
    try:
        start = lines.index(CONTEXT_HEADER) + 1
    except ValueError:
        return []
    facts = [line[len(CONTEXT_BULLET) :].strip() for line in lines[start:] if line.startswith(CONTEXT_BULLET)]
    return [f for f in facts if f != NO_CONTEXT_MARKER]
