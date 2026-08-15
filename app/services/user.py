"""User profile. The only REQUIRED upstream.

Language, tone preference and subscription tier all come from here, and each is
an input to *how* the answer is written rather than what it says. Without them
there is no personalization left to do, so this service failing is fatal by
policy (see Criticality in config/services.yaml).
"""

from __future__ import annotations

from typing import Any

from app.services.base import UpstreamClient


class UserClient(UpstreamClient):
    name = "user"

    def path(self, identifier: str | None) -> str:
        return f"/users/{identifier}"

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        # Only the fields the pipeline actually reads are required. `name` and
        # `birthDetails` are nice to have; refusing the whole profile because a
        # birth place is missing would take down a request we could still serve.
        for required in ("id", "language", "subscription"):
            if not raw.get(required):
                raise ValueError(f"missing '{required}'")
        return raw
