"""Is this an astrology question at all?

This runs before intent scoring because without it there is no answer the
pipeline can give that is not wrong. "What is the capital of France?" matched
the finance term `capital` and came back as finance at maximum confidence —
and deleting `capital` from the lexicon does not fix the class of bug, it only
moves it: with no term matching, the fallback path confidently generates an
astrology reading for a geography question. Both branches fail, because both
branches assume the question belongs somewhere in the intent simplex.

The fix is a third outcome that neither branch can express, so the gate is a
separate decision with its own evidence rather than a threshold on the intent
scores. Note it deliberately does NOT reuse the intent lexicons: `cancer` is
in-domain as a sign and out of any lexicon as a disease, which is precisely the
distinction the gate exists to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.classifier.signals import scan_longest
from app.classifier.tokenizer import Token, tokenize_terms
from app.config_schema import DomainGate as DomainGateConfig

PERSONAL = "personal_markers"
ASTRO = "astro_terms"
ZODIAC = "zodiac_terms"


@dataclass(frozen=True)
class DomainVerdict:
    in_domain: bool
    signals: int
    matched: dict[str, list[str]] = field(default_factory=dict)

    @property
    def trace(self) -> tuple[str, ...]:
        return tuple(f"{group}: {', '.join(terms)}" for group, terms in sorted(self.matched.items()))


@dataclass(frozen=True)
class DomainGateIndex:
    entries: dict[tuple[str, ...], str]  # token sequence -> group name
    max_len: int
    min_signals: int


def compile_domain_gate(config: DomainGateConfig) -> DomainGateIndex:
    entries: dict[tuple[str, ...], str] = {}
    for group, values in (
        (PERSONAL, config.personal_markers),
        (ASTRO, config.astro_terms),
        (ZODIAC, config.zodiac_terms),
    ):
        for value in values:
            key = tokenize_terms(value)
            if not key:
                raise ValueError(f"domain gate entry {value!r} in {group} normalizes to nothing")
            entries[key] = group
    return DomainGateIndex(
        entries=entries,
        max_len=max((len(k) for k in entries), default=0),
        min_signals=config.min_signals,
    )


def domain_gate_signal(tokens: tuple[Token, ...], index: DomainGateIndex) -> DomainVerdict:
    """Count distinct in-domain markers across all three groups.

    Distinct, not total: "my job, my health, my week" is one personal marker
    repeated, and letting repetition raise the count would make verbosity look
    like domain evidence. The gate runs on the full token list — time-scope and
    phrase consumption are about not double-scoring intent evidence, and have
    no bearing on whether the question is ours to answer.
    """
    if not index.max_len:
        # An unconfigured gate cannot be evidence of anything; refusing every
        # question would be worse than the bug the gate exists to prevent.
        return DomainVerdict(in_domain=True, signals=0)

    texts = tuple(token.text for token in tokens)
    matched: dict[str, list[str]] = {}
    seen: set[tuple[str, ...]] = set()
    for _, _, key in scan_longest(texts, index.entries, index.max_len, frozenset()):
        if key in seen:
            continue
        seen.add(key)
        matched.setdefault(index.entries[key], []).append(" ".join(key))

    return DomainVerdict(
        in_domain=len(seen) >= index.min_signals,
        signals=len(seen),
        matched=matched,
    )
