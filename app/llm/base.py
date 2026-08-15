"""The seam between the pipeline and whatever actually writes the answer.

Everything upstream — classification, selection, prompt building — is provider
agnostic and stays that way. Only the three modules next to this one know that
Anthropic or OpenAI exist, which is what lets the default demo path run with no
API key at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    """One completion, plus the metadata the response envelope reports.

    Token counts are optional because not every provider returns them and the
    mock only estimates. A `None` means "not measured", never "zero" — the
    difference matters if these numbers are ever billed against.
    """

    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float


class LLMError(RuntimeError):
    """A provider call failed.

    Deliberately not caught and swallowed here. The generation step is the one
    part of the pipeline with no useful degraded mode — an answer assembled from
    a failed call is worse than an error — so the caller decides whether to
    surface a 502, retry, or fall back, with the provider name in hand.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


# A word is ~1.4 tokens in English and more in transliterated Sanskrit terms
# ("mahadasha", "nakshatra"), which tokenizers split hard. The 2x plus a floor
# is headroom, not a target: too tight a cap truncates mid-sentence, which is a
# far more visible failure than a few unused tokens.
def max_tokens_for_words(max_words: int) -> int:
    return max(256, max_words * 2)


class LLMClient(ABC):
    """One generation call. No streaming, no tools, no chat history.

    A single-turn interface is the whole requirement, and keeping it that narrow
    is why the mock can be a faithful stand-in rather than an approximation.
    """

    name: str

    @abstractmethod
    async def generate(self, system: str, user: str, max_words: int) -> LLMResponse:
        """Raises LLMError on any provider failure."""
