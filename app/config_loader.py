"""Loads config/*.yaml into the typed models, and refuses to load a bad one.

Every check here runs at startup and raises. The alternative — tolerating a
dangling context key or a duplicated lexicon stem — does not remove the bug, it
just moves it to a 3am request where the symptom is a subtly wrong reading
rather than a stack trace with a filename in it.

The two lexicon linters exist because both bugs were found by inspection, not by
tests: `cancer` in the health lexicon classified "my Cancer moon" as health at
full confidence, and a `marri*` / `marriage` pair double-counted a single word
into a false HIGH confidence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from app.config_schema import (
    AppConfig,
    IntentsConfig,
    PersonalizationConfig,
    ServicesConfig,
)

M = TypeVar("M", bound=BaseModel)

INTENTS_FILE = "intents.yaml"
PERSONALIZATION_FILE = "personalization.yaml"
SERVICES_FILE = "services.yaml"


class ConfigError(ValueError):
    """Startup-fatal configuration problem. Messages name the file and the key."""


# --------------------------------------------------------------------------
# Where config comes from
# --------------------------------------------------------------------------


class ConfigSource(ABC):
    """The only thing the loader needs from a config backend.

    Config is expected to move to a database or a config service once
    non-engineers edit it; keeping the loader behind this one method means that
    swap touches nothing else. There is exactly one implementation today and no
    second one is sketched out here — the interface earns its place by being the
    single seam, not by anticipating the replacement's shape.
    """

    @abstractmethod
    def read(self, name: str) -> dict[str, Any]:
        """Return the parsed mapping for a named config document."""

    @abstractmethod
    def describe(self, name: str) -> str:
        """Human-readable origin of `name`, used in error messages."""


class FileConfigSource(ConfigSource):
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def read(self, name: str) -> dict[str, Any]:
        path = self.directory / name
        if not path.is_file():
            raise ConfigError(f"{path}: config file is missing")
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: not valid YAML — {exc}") from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping, got {type(loaded).__name__}")
        return loaded

    def describe(self, name: str) -> str:
        return str(self.directory / name)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_config(config_dir: Path) -> AppConfig:
    """Read and validate the three config documents from a directory."""
    return load_from_source(FileConfigSource(config_dir))


def load_from_source(source: ConfigSource) -> AppConfig:
    intents = _parse(IntentsConfig, source, INTENTS_FILE)
    personalization = _parse(PersonalizationConfig, source, PERSONALIZATION_FILE)
    services = _parse(ServicesConfig, source, SERVICES_FILE)

    origin = source.describe(INTENTS_FILE)
    lint_lexicon_prefixes(intents, origin)
    lint_zodiac_leakage(intents, origin)

    return AppConfig(intents=intents, personalization=personalization, services=services)


def _parse(model: type[M], source: ConfigSource, name: str) -> M:
    raw = source.read(name)
    try:
        return model.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError or our own ConfigError
        raise ConfigError(f"{source.describe(name)}: {exc}") from exc


# --------------------------------------------------------------------------
# Cross-file and lexicon validation
# --------------------------------------------------------------------------


def validate_against_registry(config: AppConfig, known_keys: set[str]) -> None:
    """Fail if personalization.yaml references a context item that does not exist.

    The registry itself is deliberately not imported: the loader would then
    depend on the extractor layer, and a config-only unit test would drag half
    the application in with it. The caller owns that wiring.
    """
    dangling: list[str] = []
    for intent, rules in config.personalization.rules.items():
        for tier in ("primary", "secondary", "exclude"):
            for key in getattr(rules, tier):
                if key not in known_keys:
                    dangling.append(f"rules.{intent.value}.{tier}: {key!r}")

    for key in config.personalization.time_scope_modifiers:
        if key not in known_keys:
            dangling.append(f"time_scope_modifiers: {key!r}")

    if dangling:
        raise ConfigError(
            f"{PERSONALIZATION_FILE} references unregistered context keys:\n  "
            + "\n  ".join(dangling)
            + f"\nregistered keys are: {sorted(known_keys)}"
        )


def _stem(match: str) -> str:
    """Comparable form of a lexicon entry — the literal part before any `*`."""
    return match[:-1] if match.endswith("*") else match


def lint_lexicon_prefixes(intents: IntentsConfig, origin: str) -> None:
    """Reject entries that would both fire on the same token.

    `marri*` and `marriage` in one intent means "marriage" scores twice, which
    is how a single word manufactures a confident classification.

    Terms and phrases are checked separately on purpose: phrase matching is
    longest-match-wins and consumes its tokens, so `love life` shadowing a bare
    `life` term is the designed behaviour, not a collision.
    """
    problems: list[str] = []
    for intent, lexicon in intents.intents.items():
        for label, entries in (("terms", lexicon.terms), ("phrases", lexicon.phrases)):
            stems = sorted({_stem(e.match) for e in entries})
            for i, a in enumerate(stems):
                for b in stems[i + 1 :]:
                    if b.startswith(a):
                        problems.append(
                            f"intents.{intent.value}.{label}: {a!r} is a prefix of {b!r} "
                            f"— both fire on the same token and double-count"
                        )
    if problems:
        raise ConfigError(f"{origin}: overlapping lexicon entries:\n  " + "\n  ".join(problems))


def lint_zodiac_leakage(intents: IntentsConfig, origin: str) -> None:
    """Reject any intent entry that can match a zodiac sign.

    `cancer` is the motivating case: as a health term it classified "what does
    my Cancer moon say" as a health question at full confidence. The check
    covers stems and individual phrase tokens too, since `cancer*` or a phrase
    containing the word would reintroduce the same bug by a different route.
    """
    zodiac = {z.lower() for z in intents.domain_gate.zodiac_terms}
    if not zodiac:
        return

    problems: list[str] = []
    for intent, lexicon in intents.intents.items():
        for label, entries in (("terms", lexicon.terms), ("phrases", lexicon.phrases)):
            for entry in entries:
                for sign in sorted(_zodiac_hits(entry.match, zodiac)):
                    problems.append(
                        f"intents.{intent.value}.{label}: {entry.match!r} matches the "
                        f"zodiac sign {sign!r}"
                    )
    if problems:
        raise ConfigError(
            f"{origin}: zodiac signs must never carry intent weight:\n  "
            + "\n  ".join(problems)
            + "\n  (a sign in a lexicon misclassifies e.g. 'my Cancer moon' as health)"
        )


def _zodiac_hits(match: str, zodiac: set[str]) -> set[str]:
    hits: set[str] = set()
    for token in match.lower().split():
        stem = _stem(token)
        if token.endswith("*"):
            hits |= {z for z in zodiac if z.startswith(stem)}
        elif stem in zodiac:
            hits.add(stem)
    return hits


__all__ = [
    "ConfigError",
    "ConfigSource",
    "FileConfigSource",
    "load_config",
    "load_from_source",
    "validate_against_registry",
]
