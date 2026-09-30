"""Inactive-field rules (SPEC §6.5a): hide fields whose feature is switched off.

DISPLAY ONLY. Never part of simplify: some objects carry residue that is not
constant, so dropping it in the core would change the object. Import is safe
because comparison is one-directional -- a hidden field is simply not stated.

Guards are read against the object's real values (normalized, defaults
included), not the simplified form, where a default guard would look absent.
Only the measured `rules:` of data/rules/inactive.yaml are used; `candidates:`
wait until they are measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from uac_compose.paths import data_root


@dataclass(frozen=True)
class Rule:
    when: tuple[tuple[str, tuple[Any, ...]], ...]
    hide: tuple[str, ...]
    path: str = ""
    """A dotted path to a list of sub-objects, or "" for the object's own fields."""

    def matches(self, obj: dict[str, Any]) -> bool:
        for name, allowed in self.when:
            value = obj.get(name)
            if value == "" or value is None:
                value = None
            if not any(value == a and type(value) is type(a) for a in allowed):
                return False
        return True


@cache
def rules() -> tuple[Rule, ...]:
    raw = yaml.safe_load((data_root() / "rules" / "inactive.yaml").read_text())
    return tuple(
        Rule(
            when=tuple((k, tuple(v if isinstance(v, list) else [v])) for k, v in r["when"].items()),
            hide=tuple(r["hide"]),
            path=str(r.get("path") or ""),
        )
        for r in raw.get("rules") or []
    )


def inactive_fields(full: dict[str, Any], path: str = "") -> set[str]:
    """Fields a rule makes inactive here (whether or not the object holds them)."""
    found: set[str] = set()
    for rule in rules():
        if rule.path == path and rule.matches(full):
            found.update(rule.hide)
    return found


def _row(small: Any, full: Any, path: str) -> Any:
    """One sub-object (or list of them) with its own inactive fields left out."""
    if isinstance(small, list):
        rows = full if isinstance(full, list) and len(full) == len(small) else small
        return [_row(s, f, path) for s, f in zip(small, rows, strict=False)]
    if not isinstance(small, dict):
        return small
    whole = full if isinstance(full, dict) else small
    gone = inactive_fields(whole, path)
    return {
        k: _row(v, whole.get(k), f"{path}.{k}" if path else k)
        for k, v in small.items()
        if k not in gone
    }


def hide(small: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    """The simplified object without the fields `full` makes inactive, at any depth.

    A sub-object row is read against the **stored** row beside it, so a guard
    the simplified form dropped as a default still decides.
    """
    gone = inactive_fields(full)
    return {k: _row(v, full.get(k), k) for k, v in small.items() if k not in gone}
