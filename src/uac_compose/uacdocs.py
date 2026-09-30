"""Vendor field knowledge from the Stonebranch AI 2.0 docs (`data/uacdocs.yaml`).

A type sees its own documented fields plus the fields of every scope it
includes -- but a scope field only where the registry declares it on the type,
and never one its agent doc says does not apply (`exclude`: Timer has no retry
fields, although the spec declares them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any

import yaml

from uac_compose import registry
from uac_compose.paths import data_root

_UNSET: Any = object()


@dataclass(frozen=True)
class FieldDoc:
    name: str
    label: str | None = None
    category: str | None = None
    group: str | None = None
    kind: str | None = None
    values: dict[str, int] = field(default_factory=dict, hash=False)
    ui_labels: dict[str, str] = field(default_factory=dict, hash=False)
    default: Any = _UNSET
    format: str | None = None
    required: bool | str = False
    applies_when: dict[str, Any] = field(default_factory=dict, hash=False)
    required_when: dict[str, Any] = field(default_factory=dict, hash=False)
    source: str | None = None
    scope: str | None = None  # the scope it came from, or None for the type's own doc

    @property
    def has_default(self) -> bool:
        return self.default is not _UNSET


@cache
def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((data_root() / "uacdocs.yaml").read_text())
    return data


def _doc(name: str, body: dict[str, Any], scope: str | None) -> FieldDoc:
    known = {k: v for k, v in body.items() if k in FieldDoc.__dataclass_fields__}
    return FieldDoc(name=name, scope=scope, **known)


@cache
def fields(type_name: str) -> dict[str, FieldDoc]:
    """Every documented top-level field of a type, own fields winning over scopes."""
    data = raw()
    body = data["types"].get(type_name)
    if body is None:
        return {}
    entry = registry.get(type_name)
    excluded = set(body.get("exclude", []))
    out: dict[str, FieldDoc] = {}
    for scope in body.get("include", []):
        for name, spec in data["scopes"][scope].items():
            if entry.declares(name) and name not in excluded:
                out[name] = _doc(name, spec, scope)
    for name, spec in (body.get("fields") or {}).items():
        out[name] = _doc(name, spec, None)
    return out


def field_doc(type_name: str, name: str) -> FieldDoc | None:
    return fields(type_name).get(name)


def values(type_name: str, name: str) -> dict[str, int]:
    doc = field_doc(type_name, name)
    return dict(doc.values) if doc else {}


def default(type_name: str, name: str) -> Any:
    """The documented default, or None when the docs give none."""
    doc = field_doc(type_name, name)
    return doc.default if doc and doc.has_default else None


def category(type_name: str, name: str) -> str | None:
    doc = field_doc(type_name, name)
    return doc.category if doc else None


def documented_types() -> list[str]:
    return list(raw()["types"])


def rules() -> dict[str, Any]:
    r: dict[str, Any] = raw()["rules"]
    return r
