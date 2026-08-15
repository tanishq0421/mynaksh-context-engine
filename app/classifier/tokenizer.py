"""Normalization and tokenization, pinned before every other classifier module.

This is deliberately the first thing in the package: every downstream signal
measures distance, consumption and overlap in *these* token indices. If the
tokenizer changes shape, negation windows and phrase consumption silently
change meaning, so the token list produced here is treated as the contract and
the signal extractors never re-split text of their own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Contractions are expanded BEFORE tokenization, not after. A cue like
# "do not care about" can never match the token stream if "don't" is still one
# token at match time, and — worse — every negation window offset downstream is
# measured against a token list that disagrees with the one the cues were
# written for. Expanding first makes both the cue and the window correct.
_CONTRACTIONS: dict[str, str] = {
    "i'm": "i am",
    "don't": "do not",
    "isn't": "is not",
    "won't": "will not",
    "can't": "can not",
    "it's": "it is",
    "that's": "that is",
    "i've": "i have",
    "i'll": "i will",
    "you're": "you are",
    "doesn't": "does not",
    "didn't": "did not",
    "shouldn't": "should not",
    "wouldn't": "would not",
}

# Apostrophe-free variants ("dont", "isnt") are deliberately NOT expanded. The
# set is not safely separable from ordinary vocabulary: "ill" is a health word
# before it is "I will", "its" is a possessive, "wont" and "cant" are real
# words. Mis-expanding "ill" would strip the strongest health signal we have,
# which is a far worse failure than missing a negation on a sloppily typed
# question.
_CONTRACTION_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _CONTRACTIONS), key=len, reverse=True)) + r")\b"
)

# Unicode apostrophe variants, folded to ASCII so one contraction table suffices.
_APOSTROPHES = dict.fromkeys(map(ord, "’‘ʼʹ´`"), "'")

# Possessives are stripped so "today's" reaches the time-scope patterns as
# "today". Without this the scope extractor misses "Can you summarize today's
# guidance?" entirely and the answer is personalized to no horizon at all.
_POSSESSIVE_S = re.compile(r"(\w)'s\b")
_PLURAL_POSSESSIVE = re.compile(r"(\w)s'(?!\w)")

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Single characters left over from splitting ("o'clock" -> o, clock; stray
# initials; punctuation debris) carry no topical signal but do shift every
# index after them. "i" and "a" survive because "i" is the strongest personal
# marker the domain gate has.
_KEPT_SINGLE_CHARS = frozenset({"i", "a"})


@dataclass(frozen=True)
class Token:
    """A token that knows where it sits.

    The index is what makes the negation window measurable and phrase
    consumption expressible; a bare list of strings cannot express either.
    """

    text: str
    index: int


@dataclass(frozen=True)
class TokenizedText:
    tokens: tuple[Token, ...]
    # Joined form for phrase-level matching and for logging what the classifier
    # actually saw, which is rarely what the user typed.
    normalized: str

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(token.text for token in self.tokens)

    def __len__(self) -> int:
        return len(self.tokens)


def normalize(text: str) -> str:
    """Fold to a single canonical spelling before anything is split."""
    text = unicodedata.normalize("NFKC", text).translate(_APOSTROPHES).casefold()
    text = _CONTRACTION_RE.sub(lambda m: _CONTRACTIONS[m.group(0)], text)
    text = _POSSESSIVE_S.sub(r"\1", text)
    return _PLURAL_POSSESSIVE.sub(r"\1s", text)


def tokenize(text: str) -> TokenizedText:
    words = [w for w in _WORD.findall(normalize(text)) if len(w) > 1 or w in _KEPT_SINGLE_CHARS]
    tokens = tuple(Token(text=word, index=i) for i, word in enumerate(words))
    return TokenizedText(tokens=tokens, normalized=" ".join(words))


def tokenize_terms(text: str) -> tuple[str, ...]:
    """Token form of a config entry.

    Config phrases and cues must go through the identical pipeline as user
    input — a pattern written "this year's" would otherwise never match its own
    normalized token stream.
    """
    return tokenize(text).texts
