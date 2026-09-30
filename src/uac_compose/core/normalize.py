"""The definition form of an object: bookkeeping and empty values removed (SPEC §5.1).

`normalize(x)` is the right-hand side of the round trip
`inflate(simplify(x, t), t) == normalize(x)`: what it drops is never rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from uac_compose.paths import data_root

# Never dropped at the root, even when empty: these identify the object.
ALWAYS_KEEP = frozenset({"type", "name"})

# An extension slot nobody filled in carries only its own labelling.
SCHEMA_ONLY_KEYS = frozenset({"label", "name", "nameTitle", "valueTitle"})


@dataclass(frozen=True)
class Exclusions:
    """Exact key names only: `version` goes, `httpVersion` stays."""

    top_level: frozenset[str]
    any_depth: frozenset[str]

    def drops(self, key: str, *, root: bool) -> bool:
        return key in self.any_depth or (root and key in self.top_level)


@cache
def exclusions() -> Exclusions:
    raw = yaml.safe_load((data_root() / "rules" / "exclusions.yaml").read_text())
    return Exclusions(frozenset(raw["top_level"]), frozenset(raw["any_depth"]))


def is_empty(value: Any) -> bool:
    """`None`, `""`, `[]`, `{}`. Never `0` or `False`."""
    return value is None or (type(value) in (str, list, dict) and not value)


def is_unset_slot(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and set(value) <= SCHEMA_ONLY_KEYS


def normalize(obj: Any, excl: Exclusions | None = None, *, root: bool = True) -> Any:
    ex = excl or exclusions()
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if ex.drops(key, root=root):
                continue
            cleaned = normalize(value, ex, root=False)
            keep = root and key in ALWAYS_KEEP
            if not keep and (is_empty(cleaned) or is_unset_slot(cleaned)):
                continue
            out[key] = cleaned
        return out
    if isinstance(obj, list):
        return [normalize(item, ex, root=False) for item in obj]
    return obj
