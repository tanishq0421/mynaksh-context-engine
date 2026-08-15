"""Fixture data for the stand-in MyNaksh backends.

These are the payloads MyNaksh's real services already return; the engine treats
them as opaque upstream JSON. Three users exist on purpose:

  user_101  the spec's canonical example, reproduced byte-for-byte so the demo
            output can be diffed against the assignment.
  user_102  free tier, different tone, different chart — with only one user
            every response looks identical and you cannot demonstrate that
            subscription changes the word budget or that tone changes the
            prompt. Personalization has to be shown *varying*.
  user_103  a deliberately sparse chart. See KUNDLIS below.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

USERS: dict[str, dict[str, Any]] = {
    "user_101": {
        "id": "user_101",
        "name": "Aarav Sharma",
        "language": "en",
        "subscription": "premium",
        "tonePreference": "motivational",
        "birthDetails": {
            "date": "1997-08-15",
            "time": "09:35",
            "place": "Delhi",
        },
    },
    "user_102": {
        "id": "user_102",
        "name": "Meera Iyer",
        "language": "en",
        "subscription": "free",
        # "direct" vs user_101's "motivational": the tone rules in
        # config/personalization.yaml only prove themselves against two values.
        "tonePreference": "direct",
        "birthDetails": {
            "date": "1990-03-22",
            "time": "21:10",
            "place": "Chennai",
        },
    },
    "user_103": {
        "id": "user_103",
        "name": "Rohan Verma",
        "language": "en",
        "subscription": "premium",
        "tonePreference": "calm",
        "birthDetails": {
            "date": "1985-11-02",
            "time": "04:15",
            "place": "Jaipur",
        },
    },
}

# --------------------------------------------------------------------------
# Kundli (natal chart)
# --------------------------------------------------------------------------

KUNDLIS: dict[str, dict[str, Any]] = {
    "user_101": {
        "lagna": "Libra",
        "moonSign": "Scorpio",
        "currentDasha": {"mahadasha": "Rahu", "antardasha": "Mars"},
        "houses": {
            "6": {"lord": "Jupiter", "strength": "Average"},
            "7": {"lord": "Mars", "strength": "Weak"},
            "10": {"lord": "Moon", "strength": "Strong"},
        },
    },
    "user_102": {
        "lagna": "Scorpio",
        "moonSign": "Leo",
        "currentDasha": {"mahadasha": "Saturn", "antardasha": "Venus"},
        "houses": {
            "6": {"lord": "Venus", "strength": "Strong"},
            "7": {"lord": "Venus", "strength": "Average"},
            "10": {"lord": "Sun", "strength": "Weak"},
        },
    },
    # Partial failure *inside a healthy service*: 200 OK, well-formed, but the
    # 10th house simply is not there. This is the case a try/except around the
    # HTTP call never catches — the extractor finds nothing and the item has to
    # degrade to `unavailable` instead of raising or inventing a placeholder.
    # A career question for this user must still answer, minus the house.
    "user_103": {
        "lagna": "Gemini",
        "moonSign": "Aquarius",
        "currentDasha": {"mahadasha": "Mercury", "antardasha": "Ketu"},
        "houses": {
            "6": {"lord": "Saturn", "strength": "Weak"},
            "7": {"lord": "Jupiter", "strength": "Strong"},
        },
    },
}

# --------------------------------------------------------------------------
# Daily horoscope
# --------------------------------------------------------------------------

HOROSCOPES: dict[str, dict[str, Any]] = {
    "user_101": {
        "career": "Networking may bring new opportunities.",
        "finance": "Avoid risky investments.",
        "health": "Prioritize proper sleep.",
        "relationship": "Communication with your partner improves.",
    },
    "user_102": {
        "career": "A pending decision at work finally resolves.",
        "finance": "A delayed payment is likely to arrive.",
        "health": "Low energy in the afternoon; eat earlier.",
        "relationship": "Old friction resurfaces; keep it brief.",
    },
    "user_103": {
        "career": "Routine work goes smoothly; avoid new commitments.",
        "finance": "Review recurring expenses before committing.",
        "health": "Joint stiffness eases with light movement.",
        "relationship": "A family conversation needs your patience.",
    },
}

# --------------------------------------------------------------------------
# Panchang — global, not per-user
# --------------------------------------------------------------------------

# The spec's sample values, kept verbatim so the demo matches the assignment.
# Only the date is live: a fixed 2026-08-01 in a "today's panchang" endpoint
# makes the whole fixture look stale, and the engine's time-scope logic scores
# `panchang_today` against the *current* day.
_PANCHANG: dict[str, Any] = {
    "tithi": "Shukla Panchami",
    "nakshatra": "Rohini",
    "yoga": "Siddhi",
    "karana": "Bava",
}


def panchang_today() -> dict[str, Any]:
    return {"date": date.today().isoformat(), **_PANCHANG}


# --------------------------------------------------------------------------
# Lookups. None means "no such user" — a 404, which is an *answer*, not a
# fault, and must not be retried by the client.
# --------------------------------------------------------------------------


def get_user(user_id: str) -> dict[str, Any] | None:
    return USERS.get(user_id)


def get_kundli(user_id: str) -> dict[str, Any] | None:
    return KUNDLIS.get(user_id)


def get_horoscope(user_id: str) -> dict[str, Any] | None:
    return HOROSCOPES.get(user_id)
