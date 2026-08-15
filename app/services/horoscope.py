"""Daily horoscope, one line per life area. Per-user, per-day."""

from __future__ import annotations

from typing import Any

from app.services.base import UpstreamClient

# Deliberately not required-all: the selector picks the area matching the
# classified intent, so a payload carrying only `career` still answers a career
# question. Requiring all four would discard a usable response.
_AREAS = ("career", "finance", "health", "relationship")


class HoroscopeClient(UpstreamClient):
    name = "horoscope"

    def path(self, identifier: str | None) -> str:
        return f"/horoscope/{identifier}"

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        present = [area for area in _AREAS if isinstance(raw.get(area), str) and raw[area]]
        if not present:
            raise ValueError(f"no usable areas; expected any of {list(_AREAS)}")
        return {area: raw[area] for area in present}
