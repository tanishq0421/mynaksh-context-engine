"""Natal chart. Per-user and effectively immutable, hence the long TTL."""

from __future__ import annotations

from typing import Any

from app.services.base import UpstreamClient


class KundliClient(UpstreamClient):
    name = "kundli"

    def path(self, identifier: str | None) -> str:
        return f"/kundli/{identifier}"

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        for required in ("lagna", "moonSign"):
            if not raw.get(required):
                raise ValueError(f"missing '{required}'")
        # Houses and dasha are validated as *containers* only. Which houses are
        # present varies by intent (6th for health, 7th for relationship, 10th
        # for career), so demanding a fixed set here would reject charts that are
        # perfectly usable for the question actually being asked.
        if not isinstance(raw.get("houses", {}), dict):
            raise ValueError("'houses' must be an object")
        if not isinstance(raw.get("currentDasha", {}), dict):
            raise ValueError("'currentDasha' must be an object")
        return raw
