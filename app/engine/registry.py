"""The context registry: every slice of upstream data the engine may use.

This lives in code rather than YAML because each item needs two *functions* —
one to pull its slice out of a service payload, one to render it as a phrase.
Config expresses policy (which items matter for which intent); it must never
express logic. So config references these items by `key` alone.

Two consequences worth stating up front:

1. `extractor` returning None is the whole mechanism for partial failure
   *within* a healthy service. Kundli answers 200 but omits `houses.10`? The
   10th-house item is simply unavailable, decided by the same code path as any
   other missing slice. There is no special case anywhere downstream.

2. `renderer` produces a short natural phrase, never raw JSON. Dumping
   `{"lord": "Moon", "strength": "Strong"}` at an LLM spends tokens on braces
   and quotes, and reads worse in the output than "10th House: lord Moon,
   strength Strong".
"""

from __future__ import annotations

from typing import Any, Callable

from app.domain import ContextItem

# --------------------------------------------------------------------------
# Extraction helpers
# --------------------------------------------------------------------------


def _dig(payload: Any, *path: str) -> Any | None:
    """Walk a nested dict, yielding None the moment the path stops existing.

    Tolerant by design: extractors run against whatever an upstream actually
    returned, which includes None (service failed), a partially populated dict,
    or — on a bad day — a list where a dict was promised.
    """
    node: Any = payload
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
        if node is None:
            return None
    return node


def _non_empty_str(value: Any) -> str | None:
    """A blank string is an absent value, not a value. Treat it as missing so it
    never reaches the prompt as `Career horoscope: `."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_field(*path: str) -> Callable[[Any], Any | None]:
    def extract(payload: Any) -> Any | None:
        return _non_empty_str(_dig(payload, *path))

    return extract


def _dict_field(*path: str, requires: tuple[str, ...] = ()) -> Callable[[Any], Any | None]:
    """Extract a sub-object, treating "present but useless" as absent.

    `requires` names the fields the renderer cannot produce a sensible phrase
    without. A house entry with neither lord nor strength is structurally
    present and semantically empty; feeding it forward would spend budget on
    "10th House: " and teach the model nothing.
    """

    def extract(payload: Any) -> Any | None:
        node = _dig(payload, *path)
        if not isinstance(node, dict) or not node:
            return None
        if requires and not any(_non_empty_str(node.get(f)) for f in requires):
            return None
        return node

    return extract


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


# Renderers emit the VALUE only. The label is `display_name`, and the prompt
# builder prepends it — duplicating it here cost a doubled label on every
# selected item ("10th House: 10th House: lord Moon"), which wasted prompt
# tokens on every request and read as a formatting bug to the model.


def _render_lagna(value: Any) -> str:
    return f"{value} (ascendant)"


def _render_moon_sign(value: Any) -> str:
    return str(value)


def _render_dasha(value: Any) -> str:
    maha = _non_empty_str(value.get("mahadasha"))
    antar = _non_empty_str(value.get("antardasha"))
    parts = []
    if maha:
        parts.append(f"{maha} mahadasha")
    if antar:
        parts.append(f"{antar} antardasha")
    return ", ".join(parts)


def _house_renderer(number: str) -> Callable[[Any], str]:
    def render(value: Any) -> str:
        lord = _non_empty_str(value.get("lord"))
        strength = _non_empty_str(value.get("strength"))
        parts = []
        if lord:
            parts.append(f"lord {lord}")
        if strength:
            parts.append(f"strength {strength}")
        return ", ".join(parts)

    return render


def _horoscope_renderer(label: str) -> Callable[[Any], str]:
    def render(value: Any) -> str:
        return str(value)

    return render


def _render_panchang(value: Any) -> str:
    # Field order follows how a panchang is conventionally read aloud, so the
    # phrase is familiar to anyone who has seen one.
    labels = (("tithi", "tithi"), ("nakshatra", "nakshatra"), ("yoga", "yoga"), ("karana", "karana"))
    parts = [f"{value[f]} {label}" for f, label in labels if _non_empty_str(value.get(f))]
    return ", ".join(parts)


def _render_birth_details(value: Any) -> str:
    date = _non_empty_str(value.get("date"))
    time = _non_empty_str(value.get("time"))
    place = _non_empty_str(value.get("place"))
    phrase = "Born"
    if date:
        phrase += f" {date}"
    if time:
        phrase += f" at {time}"
    if place:
        phrase += f" in {place}"
    return phrase


# --------------------------------------------------------------------------
# Token costing
# --------------------------------------------------------------------------

# ~4 characters per token is the well-known rough ratio for English text. It is
# an ESTIMATE, and deliberately a cheap one: the budget it feeds is a guard
# rail, not an accounting ledger, and being off by a token on a 12-item menu
# changes nothing. The production upgrade is the real tokenizer for the model
# actually in use (tiktoken / the Anthropic count-tokens endpoint), which also
# removes the need for the representative samples below.
_CHARS_PER_TOKEN = 4


def _cost(renderer: Callable[[Any], str], sample: Any) -> int:
    """Cost the item once at import time from a representative value.

    Costing the *actual* value at request time would be more accurate but would
    make the plan's arithmetic depend on upstream data — and Phase 1 is
    deliberately pure. A fixed per-item cost keeps planning reproducible.
    """
    return max(1, len(renderer(sample)) // _CHARS_PER_TOKEN)


_SAMPLE_HOUSE = {"lord": "Saturn", "strength": "Moderate"}
_SAMPLE_HOROSCOPE = "Networking may bring new opportunities your way this week."
_SAMPLE_PANCHANG = {
    "date": "2026-08-15",
    "tithi": "Dwitiya",
    "nakshatra": "Bharani",
    "yoga": "Vyaghata",
    "karana": "Taitila",
}


def _item(
    key: str,
    display_name: str,
    service: str,
    extractor: Callable[[Any], Any | None],
    renderer: Callable[[Any], str],
    sample: Any,
) -> ContextItem:
    return ContextItem(
        key=key,
        display_name=display_name,
        service=service,
        extractor=extractor,
        renderer=renderer,
        token_cost=_cost(renderer, sample),
    )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

_ITEMS: list[ContextItem] = [
    _item(
        "lagna",
        "Lagna",
        "kundli",
        _string_field("lagna"),
        _render_lagna,
        "Scorpio",
    ),
    _item(
        "moon_sign",
        "Moon Sign",
        "kundli",
        _string_field("moonSign"),
        _render_moon_sign,
        "Aquarius",
    ),
    _item(
        "dasha_current",
        "Current Dasha",
        "kundli",
        _dict_field("currentDasha", requires=("mahadasha", "antardasha")),
        _render_dasha,
        {"mahadasha": "Rahu", "antardasha": "Mars"},
    ),
    _item(
        "house_6",
        "6th House",
        "kundli",
        # Houses are keyed by string in the payload ("6", not 6) — JSON has no
        # integer keys, and normalising here keeps the quirk in one place.
        _dict_field("houses", "6", requires=("lord", "strength")),
        _house_renderer("6"),
        _SAMPLE_HOUSE,
    ),
    _item(
        "house_7",
        "7th House",
        "kundli",
        _dict_field("houses", "7", requires=("lord", "strength")),
        _house_renderer("7"),
        _SAMPLE_HOUSE,
    ),
    _item(
        "house_10",
        "10th House",
        "kundli",
        _dict_field("houses", "10", requires=("lord", "strength")),
        _house_renderer("10"),
        _SAMPLE_HOUSE,
    ),
    _item(
        "horoscope_career",
        "Career Horoscope",
        "horoscope",
        _string_field("career"),
        _horoscope_renderer("Career"),
        _SAMPLE_HOROSCOPE,
    ),
    _item(
        "horoscope_finance",
        "Finance Horoscope",
        "horoscope",
        _string_field("finance"),
        _horoscope_renderer("Finance"),
        _SAMPLE_HOROSCOPE,
    ),
    _item(
        "horoscope_health",
        "Health Horoscope",
        "horoscope",
        _string_field("health"),
        _horoscope_renderer("Health"),
        _SAMPLE_HOROSCOPE,
    ),
    _item(
        "horoscope_relationship",
        "Relationship Horoscope",
        "horoscope",
        _string_field("relationship"),
        _horoscope_renderer("Relationship"),
        _SAMPLE_HOROSCOPE,
    ),
    _item(
        "panchang_today",
        "Today's Panchang",
        "panchang",
        # The whole payload is one item: tithi alone is not actionable, and
        # splitting it into four items would let the budget keep half a panchang.
        _dict_field(requires=("tithi", "nakshatra", "yoga", "karana")),
        _render_panchang,
        _SAMPLE_PANCHANG,
    ),
    _item(
        "birth_details",
        "Birth Details",
        "user",
        _dict_field("birthDetails", requires=("date", "time", "place")),
        _render_birth_details,
        {"date": "1995-08-12", "time": "14:30", "place": "Jaipur, India"},
    ),
]

REGISTRY: dict[str, ContextItem] = {item.key: item for item in _ITEMS}

# Guards against a copy-paste key collision silently shrinking the registry.
assert len(REGISTRY) == len(_ITEMS), "duplicate context item key in registry"


def known_keys() -> set[str]:
    """The set config is validated against at boot."""
    return set(REGISTRY)
