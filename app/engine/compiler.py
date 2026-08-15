"""Boot-time compilation of personalization config into a runtime lookup.

Config is authored intent-keyed because that is the shape a human reviews:
"what does career use?" has to be one readable block, or nobody will maintain
it. Runtime asks the mirror-image question — it walks candidate *items* and
needs "what is your weight under the currently active intents?" Rather than
make every request pay for that mismatch, the flip happens once at startup.

Two transformations and one validation:

1. Substitute tier names for numbers (primary -> 1.0, secondary -> 0.5), so the
   numbers live in exactly one place and a global policy change is one edit.
2. Flip the nesting to item-keyed, MERGING an item that appears under several
   intents. That merge is precisely what makes multi-intent scoring work:
   `dasha_current` being secondary for both career and finance is what lets an
   ambiguous "will my job pay better?" surface it above either intent alone.

Exclusions compile to a separate map because they are not a weight. A
subtractive penalty could be outvoted by enough positive weight from another
intent; a hard zero applied last cannot. "Do not discuss relationships" has to
survive contact with a 0.6/0.4 merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.config_schema import IntentContextRules, PersonalizationConfig
from app.domain import Intent
from app.engine.registry import known_keys


class ConfigCompileError(ValueError):
    """Raised at startup so a dangling context key kills the process at boot
    rather than a request at 3am."""


@dataclass(frozen=True)
class CompiledRules:
    """Item-keyed runtime view of the rules. Immutable: it is shared by every
    request and planning must never be able to mutate policy."""

    # key -> intent -> tier weight, e.g. {"dasha_current": {career: 0.5, finance: 0.5}}
    item_weights: Mapping[str, Mapping[Intent, float]]
    # key -> intent -> tier name. Carried only so the plan can explain itself
    # ("career primary (1.0)"); scoring never reads it.
    item_tiers: Mapping[str, Mapping[Intent, str]]
    # Kept apart from weights on purpose — see module docstring.
    exclusions: Mapping[Intent, frozenset[str]]

    def weight_for(self, key: str, intent: Intent) -> float:
        return self.item_weights.get(key, {}).get(intent, 0.0)

    def tier_for(self, key: str, intent: Intent) -> str | None:
        return self.item_tiers.get(key, {}).get(intent)

    def excluded_by(self, key: str, intents: Mapping[Intent, float] | frozenset[Intent]) -> list[Intent]:
        """Every active intent that hard-zeroes this key. Plural because the
        plan's reason list should name all of them, not just the first."""
        return [i for i in intents if key in self.exclusions.get(i, frozenset())]


def _freeze(nested: dict[str, dict[Intent, object]]) -> Mapping[str, Mapping[Intent, object]]:
    return MappingProxyType({k: MappingProxyType(dict(v)) for k, v in nested.items()})


def _validate(intent: Intent, rules: IntentContextRules, valid: set[str]) -> None:
    for tier_name, keys in (
        ("primary", rules.primary),
        ("secondary", rules.secondary),
        ("exclude", rules.exclude),
    ):
        for key in keys:
            if key not in valid:
                raise ConfigCompileError(
                    f"intent '{intent.value}' {tier_name} references unknown context key "
                    f"'{key}'. Known keys: {sorted(valid)}"
                )


def compile_rules(config: PersonalizationConfig) -> CompiledRules:
    valid = known_keys()

    item_weights: dict[str, dict[Intent, float]] = {}
    item_tiers: dict[str, dict[Intent, str]] = {}
    exclusions: dict[Intent, frozenset[str]] = {}

    tiers = (("primary", config.weights.primary), ("secondary", config.weights.secondary))

    # `general` needs no special case. Its rule is an empty `primary` and an
    # all-items `secondary`, which compiles to every item at 0.5 — a flat menu
    # ranked only by time-scope modifiers, which is exactly right for a question
    # that named no topic.
    for intent, rules in config.rules.items():
        _validate(intent, rules, valid)

        for tier_name, tier_weight in tiers:
            for key in getattr(rules, tier_name):
                # setdefault, not assignment: this is the merge across intents.
                item_weights.setdefault(key, {})[intent] = tier_weight
                item_tiers.setdefault(key, {})[intent] = tier_name

        if rules.exclude:
            exclusions[intent] = frozenset(rules.exclude)

    # Time-scope modifiers name items too, and a typo there is just as fatal.
    for key in config.time_scope_modifiers:
        if key not in valid:
            raise ConfigCompileError(
                f"time_scope_modifiers references unknown context key '{key}'. "
                f"Known keys: {sorted(valid)}"
            )

    return CompiledRules(
        item_weights=_freeze(item_weights),  # type: ignore[arg-type]
        item_tiers=_freeze(item_tiers),  # type: ignore[arg-type]
        exclusions=MappingProxyType(dict(exclusions)),
    )
