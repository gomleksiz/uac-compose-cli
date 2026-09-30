"""The type registry: fields, field types and sub-object fields per UAC type.

Shipped data (`data/registry/types.yaml`), generated once from the OpenAPI
spec. Never derived at run time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache

import yaml

from uac_compose.paths import data_root


@dataclass(frozen=True)
class TypeEntry:
    type: str
    kind: str
    fields: tuple[str, ...]
    field_types: dict[str, str] = field(hash=False)
    sub_fields: dict[str, tuple[str, ...]] = field(hash=False)
    export_table: str | None = None

    def declares(self, name: str) -> bool:
        return name in self.fields


@cache
def load() -> dict[str, TypeEntry]:
    raw = yaml.safe_load((data_root() / "registry" / "types.yaml").read_text())["types"]
    return {
        name: TypeEntry(
            type=name,
            kind=body["kind"],
            fields=tuple(body["fields"]),
            field_types=dict(body.get("field_types") or {}),
            sub_fields={p: tuple(f) for p, f in (body.get("sub_fields") or {}).items()},
            export_table=body.get("export_table"),
        )
        for name, body in raw.items()
    }


def get(type_name: str) -> TypeEntry:
    try:
        return load()[type_name]
    except KeyError:
        raise KeyError(f"unknown UAC type: {type_name}") from None


def types() -> list[str]:
    return list(load())


@cache
def _by_export_table() -> dict[str, TypeEntry]:
    return {e.export_table: e for e in load().values() if e.export_table}


def resolve(export_table: str | None, type_value: str | None) -> TypeEntry | None:
    """The entry for an exported object. `exportTable` decides; `type` is a fallback.

    `type` is not always a discriminator: a virtual resource's is
    Renewable/Boundary/Depletable, a credential's Standard/Resolvable.
    """
    entry = _by_export_table().get(export_table or "")
    if entry is None and type_value:
        entry = load().get(type_value)
    return entry


VAR = "Var"


@cache
def var_pairs(type_name: str) -> tuple[tuple[str, str], ...]:
    """Every `(field, fieldVar)` pair of the type, top level and nested (dotted paths)."""
    entry = get(type_name)
    found = [
        (name, name + VAR)
        for name in sorted(entry.fields)
        if not name.endswith(VAR) and name + VAR in entry.fields
    ]
    for path, names in sorted(entry.sub_fields.items()):
        inventory = set(names)
        found += [
            (f"{path}.{name}", f"{path}.{name}{VAR}")
            for name in sorted(inventory)
            if not name.endswith(VAR) and name + VAR in inventory
        ]
    return tuple(found)


# Fields that name another object but have no `*Var` twin.
_UNPAIRED_REFERENCES = ("template", "opswiseGroups", "calendar", "eventTemplate")

# Pairs whose plain half is a value, not a name: resPriority is an integer 1-100.
_SCALAR_PAIRS = ("resPriority",)


@cache
def reference_fields(type_name: str) -> frozenset[str]:
    """Top-level fields whose value names another object. Never defaulted (SPEC §5.4)."""
    paired = {n for n, _ in var_pairs(type_name) if "." not in n} - set(_SCALAR_PAIRS)
    return frozenset(paired) | frozenset(_UNPAIRED_REFERENCES)


@cache
def reference_sub_fields() -> frozenset[str]:
    """Dotted sub-object paths that name another object, across every type."""
    return frozenset(n for t in load() for n, _ in var_pairs(t) if "." in n)
