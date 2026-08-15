"""Signal extractors: text in, weighted evidence out.

Each extractor is a pure function over the token list plus a compiled index, so
each is testable on its own without constructing a classifier or loading YAML.
None of them decides anything — they only contribute to the score vector that
the decision policy in rules.py reads. Keeping "what matched" and "what we
conclude" apart is what lets a threshold move without touching a matcher, and
what lets a new evidence source appear without touching either.

The compiled indices live here rather than in rules.py because they are part of
what a matcher needs to be exercised in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from app.classifier.tokenizer import Token, tokenize_terms
from app.config_schema import IntentLexicon, TimeScopePatterns
from app.domain import Intent, TimeScope

_STEM_MARKER = "*"


@dataclass(frozen=True)
class Match:
    """One lexicon or phrase hit, with the token span it covers.

    The span is load-bearing twice over: consumption is expressed as "these
    indices are spoken for", and the negation window is measured against
    `start`. `weights` maps every intent this single hit is evidence for —
    `salary` is one match carrying {career: 0.5, finance: 0.8}, not two
    independent pieces of evidence, which is exactly what the absolute-evidence
    gate needs to avoid double counting.
    """

    term: str
    start: int
    end: int  # exclusive
    weights: dict[Intent, float]
    kind: str  # "term" | "phrase"
    discount: float = 1.0

    @property
    def indices(self) -> range:
        return range(self.start, self.end)

    @property
    def topical_peak(self) -> float:
        """Strongest single reading of this match, BEFORE any negation discount.

        The evidence gate asks "did the user raise a topic we know about", and
        negation does not unraise a topic — see negation_signal. Discounting
        here would recreate the exact failure the discount exists to avoid: on
        a question with one topical term, "Will I not lose my job?" would fall
        under min_evidence and answer generically, which is boolean zeroing
        wearing a multiplier. The discount belongs in the ranking, where the
        question "which intent" is actually decided.
        """
        return max(self.weights.values(), default=0.0)

    def score(self, intent: Intent) -> float:
        return self.weights.get(intent, 0.0) * self.discount

    def discounted(self, factor: float) -> "Match":
        return replace(self, discount=self.discount * factor)


@dataclass(frozen=True)
class SignalOutput:
    matches: tuple[Match, ...] = ()
    # Indices this extractor claimed. The caller unions them and passes them
    # forward so a later extractor cannot score the same token again.
    consumed: frozenset[int] = frozenset()
    trace: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimeScopeSignal:
    scope: TimeScope
    consumed: frozenset[int] = frozenset()
    trace: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LexiconIndex:
    """Inverted index: token -> intents that token is evidence for.

    Built once at startup so classification costs O(tokens) rather than
    O(intents x terms). The alternative — scanning every intent's term list per
    question — gets slower with exactly the change we most want to be cheap,
    which is adding intents.
    """

    terms: dict[str, dict[Intent, float]]
    stems: dict[str, dict[Intent, float]]  # prefix, marker stripped
    phrases: dict[tuple[str, ...], dict[Intent, float]]
    max_phrase_len: int
    min_stem_len: int


def _merge(target: dict[Intent, float], intent: Intent, weight: float) -> None:
    # A term repeated under one intent is a config duplicate, not two votes.
    target[intent] = max(target.get(intent, 0.0), weight)


def compile_lexicons(intents: Mapping[Intent, IntentLexicon]) -> LexiconIndex:
    terms: dict[str, dict[Intent, float]] = {}
    stems: dict[str, dict[Intent, float]] = {}
    phrases: dict[tuple[str, ...], dict[Intent, float]] = {}

    for intent, lexicon in intents.items():
        for phrase in lexicon.phrases:
            key = tokenize_terms(phrase.match)
            if not key:
                raise ValueError(f"phrase {phrase.match!r} under {intent.value} normalizes to nothing")
            _merge(phrases.setdefault(key, {}), intent, phrase.weight)

        for term in lexicon.terms:
            raw = term.match.strip()
            is_stem = raw.endswith(_STEM_MARKER)
            key_tokens = tokenize_terms(raw.rstrip(_STEM_MARKER))
            # Fail at startup, not at 3am: a multi-word entry in `terms` would
            # silently never match, since matching is token-level by design.
            if len(key_tokens) != 1:
                raise ValueError(
                    f"term {term.match!r} under {intent.value} must normalize to exactly one "
                    f"token (got {list(key_tokens)}); multi-word entries belong in `phrases`"
                )
            bucket = stems if is_stem else terms
            _merge(bucket.setdefault(key_tokens[0], {}), intent, term.weight)

    return LexiconIndex(
        terms=terms,
        stems=stems,
        phrases=phrases,
        max_phrase_len=max((len(k) for k in phrases), default=0),
        min_stem_len=min((len(k) for k in stems), default=1),
    )


@dataclass(frozen=True)
class TimeScopeIndex:
    patterns: dict[tuple[str, ...], TimeScope]
    max_len: int


def compile_time_scope(config: TimeScopePatterns) -> TimeScopeIndex:
    patterns: dict[tuple[str, ...], TimeScope] = {}
    for scope, entries in config.patterns.items():
        for entry in entries:
            key = tokenize_terms(entry)
            if not key:
                raise ValueError(f"time scope pattern {entry!r} normalizes to nothing")
            patterns[key] = scope
    return TimeScopeIndex(patterns=patterns, max_len=max((len(k) for k in patterns), default=0))


def compile_negation_cues(cues: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Cues are token sequences, not words — "do not care about" is one cue."""
    compiled = [tokenize_terms(cue) for cue in cues]
    return tuple(sorted((c for c in compiled if c), key=len, reverse=True))


# --------------------------------------------------------------------------
# Extractors
# --------------------------------------------------------------------------


def scan_longest(
    texts: tuple[str, ...],
    table: Mapping[tuple[str, ...], object],
    max_len: int,
    blocked: frozenset[int],
) -> list[tuple[int, int, tuple[str, ...]]]:
    """Leftmost-longest non-overlapping scan. Shared by phrases and time scope.

    Longest-match-wins with consumption is the only policy under which
    overlapping entries cannot double-fire: once "love life" is claimed, bare
    "life" has no token left to score against.
    """
    hits: list[tuple[int, int, tuple[str, ...]]] = []
    taken: set[int] = set()
    i = 0
    n = len(texts)
    while i < n:
        if i in blocked or i in taken:
            i += 1
            continue
        for length in range(min(max_len, n - i), 0, -1):
            span = range(i, i + length)
            if any(j in blocked or j in taken for j in span):
                continue
            key = texts[i : i + length]
            if key in table:
                hits.append((i, i + length, key))
                taken.update(span)
                i += length
                break
        else:
            i += 1
    return hits


def time_scope_signal(tokens: tuple[Token, ...], index: TimeScopeIndex) -> TimeScopeSignal:
    """Extract the horizon, and consume the tokens that expressed it.

    Consumption matters because time words are not neutral: "year" sitting in a
    career lexicon would let "this year" quietly add career evidence to every
    question that merely mentions a horizon.
    """
    if not index.max_len:
        return TimeScopeSignal(scope=TimeScope.UNSPECIFIED)

    texts = tuple(token.text for token in tokens)
    hits = scan_longest(texts, index.patterns, index.max_len, frozenset())
    if not hits:
        return TimeScopeSignal(scope=TimeScope.UNSPECIFIED)

    consumed: set[int] = set()
    for start, end, _ in hits:
        consumed.update(range(start, end))
    # Several horizons in one question is genuinely ambiguous; prefer the most
    # specific phrasing (longest match), leftmost on a tie.
    start, end, key = max(hits, key=lambda hit: (hit[1] - hit[0], -hit[0]))
    scope = index.patterns[key]
    return TimeScopeSignal(
        scope=scope,
        consumed=frozenset(consumed),
        trace=tuple(f"{' '.join(k)} -> {index.patterns[k].value}" for _, _, k in hits),
    )


def phrase_signal(
    tokens: tuple[Token, ...],
    index: LexiconIndex,
    consumed: frozenset[int] = frozenset(),
) -> SignalOutput:
    """Multi-word entries, weighted above their constituent tokens."""
    if not index.max_phrase_len:
        return SignalOutput()

    texts = tuple(token.text for token in tokens)
    hits = scan_longest(texts, index.phrases, index.max_phrase_len, consumed)
    matches = tuple(
        Match(
            term=" ".join(key),
            start=start,
            end=end,
            weights=dict(index.phrases[key]),
            kind="phrase",
        )
        for start, end, key in hits
    )
    claimed = frozenset(i for m in matches for i in m.indices)
    return SignalOutput(
        matches=matches,
        consumed=claimed,
        trace=tuple(f"phrase {m.term!r}" for m in matches),
    )


def lexicon_signal(
    tokens: tuple[Token, ...],
    index: LexiconIndex,
    consumed: frozenset[int] = frozenset(),
) -> SignalOutput:
    """Single weighted terms, matched at token level.

    Never substring: `art` must not fire inside `heart`, and no amount of
    careful word choice makes substring matching safe once a lexicon is edited
    by non-engineers.
    """
    matches: list[Match] = []
    for token in tokens:
        if token.index in consumed:
            continue
        weights = index.terms.get(token.text)
        term = token.text
        if weights is None:
            # Stems are prefixes, so scan the token's own prefixes rather than
            # every configured stem: bounded by word length, not lexicon size.
            # Longest prefix wins, for the same no-double-fire reason phrases
            # use longest-match.
            for length in range(len(token.text), index.min_stem_len - 1, -1):
                candidate = token.text[:length]
                if candidate in index.stems:
                    weights = index.stems[candidate]
                    term = candidate + _STEM_MARKER
                    break
        if weights:
            matches.append(
                Match(
                    term=term,
                    start=token.index,
                    end=token.index + 1,
                    weights=dict(weights),
                    kind="term",
                )
            )
    return SignalOutput(
        matches=tuple(matches),
        consumed=frozenset(m.start for m in matches),
        trace=tuple(f"term {m.term!r}" for m in matches),
    )


def negation_signal(
    tokens: tuple[Token, ...],
    matches: Iterable[Match],
    cues: tuple[tuple[str, ...], ...],
    window: int,
    discount: float,
) -> SignalOutput:
    """Discount evidence near a negation cue. Never zero it.

    Two reasons this is a multiplier and not a boolean, both learned the hard
    way on real phrasings:

    1. No fixed window is correct in general. "not really all that worried
       about work" pushes the topic six tokens past the cue; widening the window
       to catch it starts swallowing the next clause instead. The window will
       therefore be wrong in both directions, and a discount caps the cost of
       being wrong either way — a mis-fire shaves a match instead of deleting
       it, and a miss leaves the match slightly overweight instead of intact.
    2. Negation scopes over sentiment, not topic. "Will I not lose my job?" is
       still a career question; zeroing `job` there would answer a career
       question with generic filler. The user raised the subject either way,
       which is all the classifier is actually being asked to determine.
    """
    if not cues or discount >= 1.0:
        return SignalOutput(matches=tuple(matches))

    texts = tuple(token.text for token in tokens)
    cue_ends = [
        i + len(cue)
        for cue in cues
        for i in range(len(texts) - len(cue) + 1)
        if texts[i : i + len(cue)] == cue
    ]

    adjusted: list[Match] = []
    trace: list[str] = []
    for match in matches:
        if any(end <= match.start < end + window for end in cue_ends):
            adjusted.append(match.discounted(discount))
            trace.append(f"negated {match.term!r} (x{discount})")
        else:
            adjusted.append(match)
    return SignalOutput(matches=tuple(adjusted), trace=tuple(trace))
