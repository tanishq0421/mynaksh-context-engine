"""The classifier seam.

One interface, one implementation. It exists because the thing most likely to
change here is *how* intent is decided (an embedding model, a small fine-tune,
a learned reranker over the rules output) while the rest of the pipeline only
ever needs the ClassificationResult. Nothing speculative lives behind it — no
registry, no factory, no plugin loader — because a second implementation is
what would tell us what those should look like.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain import ClassificationResult


class Classifier(ABC):
    @abstractmethod
    def classify(self, question: str) -> ClassificationResult:
        """Decide intent, weights and time scope, with enough trace to explain why."""
