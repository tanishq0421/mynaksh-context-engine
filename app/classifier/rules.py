"""Scoring and the decision policy, kept as two separate steps.

Classification here is: accumulate weighted evidence into a score vector
(dict[Intent, float]), then apply a decision policy to that vector. The two
halves never touch each other's concerns — a matcher cannot decide, and the
policy cannot look at text.

That indirection is the point even though exactly one signal source currently
feeds the vector. An embedding classifier, a user-history prior, or a
click-through signal would each contribute into the same vector and change
nothing downstream: the policy, the weights contract, and every consumer stay
as they are. Collapsing the vector into "the winning term" would make each of
those a rewrite instead of an addition.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.classifier.base import Classifier
from app.classifier.domain_gate import (
    ZODIAC,
    DomainGateIndex,
    compile_domain_gate,
    domain_gate_signal,
)
from app.classifier.signals import (
    LexiconIndex,
    Match,
    compile_lexicons,
    compile_negation_cues,
    compile_time_scope,
    lexicon_signal,
    negation_signal,
    phrase_signal,
    time_scope_signal,
)
from app.classifier.tokenizer import Token, tokenize
from app.config_schema import IntentsConfig
from app.domain import (
    ClassificationResult,
    DecisionReason,
    Intent,
    IntentScore,
    TimeScope,
)


def _reject_zodiac_in_lexicons(gate: DomainGateIndex, lexicon: LexiconIndex) -> None:
    """A zodiac term in an intent lexicon is a config bug, so refuse to boot.

    `cancer` is a sign and a disease; `leo` is a sign and a name. The schema
    states the rule but cannot enforce it, because neither the gate nor a
    lexicon can see the other — this constructor is the first place both exist.
    Enforcing it here turns "my Cancer moon classified as health at full
    confidence" from a bug someone has to notice in production into a startup
    failure with the offending word in the message.
    """
    offenders = sorted(
        " ".join(key)
        for key, group in gate.entries.items()
        if group == ZODIAC
        and (
            key in lexicon.phrases
            or (
                len(key) == 1
                and (
                    key[0] in lexicon.terms
                    # A stem is just as capable of swallowing a sign name:
                    # `cance*` for cancellation would catch `cancer`.
                    or any(key[0].startswith(stem) for stem in lexicon.stems)
                )
            )
        )
    )
    if offenders:
        raise ValueError(
            f"zodiac terms {offenders} also appear in an intent lexicon: a sign name must "
            f"only ever be domain evidence, or 'my Cancer moon' classifies as health"
        )


@dataclass(frozen=True)
class _Evidence:
    """The score vector plus the two numbers the policy actually reads."""

    ranked: list[IntentScore]
    absolute_evidence: float
    relative_dominance: float


class RulesClassifier(Classifier):
    """Config-driven classifier. Everything expensive happens in __init__.

    Compilation is done once at startup so a request costs O(tokens), and so a
    malformed lexicon kills the process at boot rather than one request in
    production — the same fail-fast posture as the rest of the app.
    """

    def __init__(self, config: IntentsConfig) -> None:
        self._thresholds = config.thresholds
        self._lexicon = compile_lexicons(config.intents)
        self._time_scope = compile_time_scope(config.time_scope)
        self._cues = compile_negation_cues(config.negation_cues)
        self._gate = compile_domain_gate(config.domain_gate)
        _reject_zodiac_in_lexicons(self._gate, self._lexicon)

    # -- public -----------------------------------------------------------

    def classify(self, question: str) -> ClassificationResult:
        text = tokenize(question)

        # Time scope is extracted first and reported on every path, including
        # out-of-domain: the horizon is a property of the question, not a
        # consequence of which intent won.
        time_signal = time_scope_signal(text.tokens, self._time_scope)

        verdict = domain_gate_signal(text.tokens, self._gate)
        if not verdict.in_domain:
            return self._out_of_domain(time_signal.scope)

        matches = self._collect(text.tokens, time_signal.consumed)
        evidence = self._accumulate(matches)
        return self._decide(evidence, time_signal.scope)

    # -- scoring ----------------------------------------------------------

    def _collect(self, tokens: tuple[Token, ...], consumed: frozenset[int]) -> tuple[Match, ...]:
        """Run the extractors in consumption order: time, phrases, then terms."""
        phrases = phrase_signal(tokens, self._lexicon, consumed=consumed)
        terms = lexicon_signal(tokens, self._lexicon, consumed=consumed | phrases.consumed)
        negated = negation_signal(
            tokens,
            phrases.matches + terms.matches,
            self._cues,
            self._thresholds.negation_window,
            self._thresholds.negation_discount,
        )
        return negated.matches

    def _accumulate(self, matches: tuple[Match, ...]) -> _Evidence:
        scores: dict[Intent, float] = {}
        terms: dict[Intent, list[str]] = {}
        for match in matches:
            for intent in match.weights:
                # GENERAL is the absence of an intent, never a scored peer; the
                # config schema forbids giving it a lexicon, and this is the
                # matching guard on the scoring side.
                if intent is Intent.GENERAL:
                    continue
                contribution = match.score(intent)
                if contribution <= 0:
                    continue
                scores[intent] = scores.get(intent, 0.0) + contribution
                terms.setdefault(intent, []).append(match.term)

        ranked = sorted(
            (
                IntentScore(intent=intent, score=score, matched_terms=terms.get(intent, []))
                for intent, score in scores.items()
            ),
            # Intent value breaks ties so the same question always ranks the
            # same way; an arbitrary dict order here would make the ambiguous
            # branch nondeterministic.
            key=lambda s: (-s.score, s.intent.value),
        )

        # Absolute evidence sums the MAX weight per match, not the sum across
        # intents. A polysemous term like `salary` (0.5 career + 0.8 finance)
        # must contribute 0.8, not 1.3 — otherwise the terms we trust least,
        # the ones that fire for several intents at once, inflate the
        # "did we match anything real" gate the most, which is backwards.
        absolute = sum((match.topical_peak for match in matches), 0.0)

        return _Evidence(
            ranked=ranked,
            absolute_evidence=absolute,
            relative_dominance=self._dominance(ranked),
        )

    @staticmethod
    def _dominance(ranked: list[IntentScore]) -> float:
        """Margin over the runner-up: top1 / (top1 + top2).

        Not top1/sum(all). Share-of-total conflates "clear winner" with "few
        competitors": a 4:1 leader over three rivals scores 0.667 and reads as
        ambiguous purely because there are more buckets to divide by, so every
        intent added would quietly re-tune a threshold nobody touched. The
        margin form only ever compares the two contenders and stays stable as
        the lexicon grows.
        """
        if not ranked:
            return 0.0
        if len(ranked) == 1:
            return 1.0
        top, second = ranked[0].score, ranked[1].score
        total = top + second
        return top / total if total > 0 else 0.0

    # -- decision policy --------------------------------------------------

    def _decide(self, evidence: _Evidence, scope: TimeScope) -> ClassificationResult:
        if evidence.absolute_evidence < self._thresholds.min_evidence:
            # Something may have matched, just not enough to act on. Answering
            # generally is honest; picking the strongest of several weak
            # signals is a confident guess dressed as a decision.
            return ClassificationResult(
                primary=Intent.GENERAL,
                weights={Intent.GENERAL: 1.0},
                ranked=evidence.ranked,
                time_scope=scope,
                reason=DecisionReason.NO_EVIDENCE_DEFAULT,
                in_domain=True,
                absolute_evidence=evidence.absolute_evidence,
                relative_dominance=evidence.relative_dominance,
            )

        top = evidence.ranked[0]
        if evidence.relative_dominance >= self._thresholds.dominance:
            return ClassificationResult(
                primary=top.intent,
                weights={top.intent: 1.0},
                ranked=evidence.ranked,
                time_scope=scope,
                reason=DecisionReason.CONFIDENT_MATCH,
                in_domain=True,
                absolute_evidence=evidence.absolute_evidence,
                relative_dominance=evidence.relative_dominance,
            )

        # Genuinely ambiguous: "Will I get a salary hike this year?" is a career
        # question and a finance question, and answering it as either alone
        # drops half the context the user needed. Blend the top two by their
        # relative strength rather than forcing a winner.
        second = evidence.ranked[1]
        share = top.score / (top.score + second.score)
        return ClassificationResult(
            primary=top.intent,
            # Complement rather than a second division, so the pair sums to
            # exactly 1.0 — the personalization engine multiplies tier weights
            # by these, and a float that sums to 0.9999 silently shrinks every
            # context score downstream.
            weights={top.intent: share, second.intent: 1.0 - share},
            ranked=evidence.ranked,
            time_scope=scope,
            reason=DecisionReason.AMBIGUOUS_MERGED,
            in_domain=True,
            absolute_evidence=evidence.absolute_evidence,
            relative_dominance=evidence.relative_dominance,
        )

    @staticmethod
    def _out_of_domain(scope: TimeScope) -> ClassificationResult:
        return ClassificationResult(
            primary=Intent.GENERAL,
            weights={Intent.GENERAL: 1.0},
            ranked=[],
            time_scope=scope,
            reason=DecisionReason.OUT_OF_DOMAIN,
            in_domain=False,
            absolute_evidence=0.0,
            relative_dominance=0.0,
        )
