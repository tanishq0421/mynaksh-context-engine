"""Today's almanac. Global — the same payload for every user.

The odd one out in two ways, both handled here rather than by special-casing the
aggregator: the endpoint takes no user id, and the cache key is the date instead
of the user, so one fetch serves the whole user base and rolls over at midnight.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import UpstreamClient

_ELEMENTS = ("tithi", "nakshatra", "yoga", "karana")


class PanchangClient(UpstreamClient):
    name = "panchang"

    def path(self, identifier: str | None) -> str:
        return "/panchang"  # global; the user id is meaningless here

    def cache_identifier(self, identifier: str | None) -> str | None:
        # Date-scoped, not user-scoped. Local date is correct because panchang is
        # computed for a locale, and a UTC key would roll the day over at the
        # wrong moment for the audience this serves.
        return date.today().isoformat()

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        missing = [element for element in _ELEMENTS if not raw.get(element)]
        if missing:
            raise ValueError(f"missing {missing}")
        return raw
