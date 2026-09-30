"""The `sla:` block -- the one nesting the compact style keeps (data/rules/compact.yaml).

Field groups were tried over real converted objects and removed: they bought
structure nobody asked for, and UAC's own shape is `--style standard`. Late
start, late finish and early finish stay together because they are three
settings of one thing and each third is meaningless without its `*Enabled`
flag beside it.

Display only: a grouped object compiles to exactly the same UAC object as a
flat one, and a file may write the fields flat instead.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import yaml

from uac_compose import registry
from uac_compose.paths import data_root

SLA = "sla"


class GroupError(ValueError):
    pass


@cache
def _spec() -> dict[str, Any]:
    raw = yaml.safe_load((data_root() / "rules" / "compact.yaml").read_text())
    spec: dict[str, Any] = raw[SLA]
    return spec


@cache
def blocks(type_name: str) -> dict[str, tuple[str, ...]]:
    """Sub-block -> the fields of it this type declares, in file order."""
    entry = registry.load().get(type_name)
    if entry is None or SLA in entry.fields:
        return {}  # a type with a field called `sla` cannot also have the block
    found = {}
    for block, fields in _spec()["blocks"].items():
        kept = tuple(f for f in fields if entry.declares(f))
        if kept:
            found[block] = kept
    return found


@cache
def own(type_name: str) -> tuple[str, ...]:
    entry = registry.load().get(type_name)
    if entry is None or not blocks(type_name):
        return ()
    return tuple(f for f in _spec()["own"] if entry.declares(f))


@cache
def short_names(type_name: str, block: str) -> dict[str, str]:
    """UAC field -> the name written inside this block (`lfDuration` -> `duration`).

    Mechanical: the two-letter family prefix off, the first letter lowered.
    Checked for collisions by a test, not by hope.
    """
    out = {}
    for field_name in blocks(type_name).get(block, ()):
        stem = field_name[2:]
        out[field_name] = stem[:1].lower() + stem[1:]
    return out


@cache
def type_field(type_name: str, block: str) -> str | None:
    return next((f for f in blocks(type_name).get(block, ()) if f.endswith("Type")), None)


@cache
def implied_types() -> dict[str, str]:
    """Short field -> the `*Type` its presence settles."""
    found: dict[str, str] = _spec().get("implies") or {}
    return found


@cache
def fields_of(type_name: str) -> dict[str, str | None]:
    """Field -> sub-block name, or None for a field of `sla` itself."""
    found: dict[str, str | None] = {f: None for f in own(type_name)}
    for block, fields in blocks(type_name).items():
        for f in fields:
            found[f] = block
    return found


def regroup(type_name: str, body: dict[str, Any], imply_enabled: bool = True) -> dict[str, Any]:
    """A flat body with its SLA fields gathered into `sla:`, written last.

    A block is only written when its constraint is on, so `lsEnabled: true`
    and friends are left out: writing the block is what says it.

    `imply_enabled` is false for `--show-inactive`, and there the fields are
    written **flat** instead of blocked. In that mode the residue of a
    constraint that is **off** is shown too, and `lfEnabled: false` is the
    type's default and so is not in the object at all -- a block with no flag
    would read as the exact opposite of what is stored. That mode exists to
    show what is stored, so it shows it plainly.
    """
    if not imply_enabled:
        return body
    where = fields_of(type_name)
    if not where:
        return body
    rest: dict[str, Any] = {}
    sla: dict[str, Any] = {}
    for key, value in body.items():
        block = where.get(key, "") if key != "type" else ""
        if block == "":
            rest[key] = value
        elif block is None:
            sla[key] = value
        else:
            sla.setdefault(block, {})[key] = value
    if not sla:
        return body
    ordered = {f: sla[f] for f in own(type_name) if f in sla}
    for block in blocks(type_name):
        if block in sla:
            ordered[block] = _trim(sla[block], block, type_name)
    return {**rest, SLA: ordered}


def enabled_field(block: str, type_name: str) -> str | None:
    """The `*Enabled` flag of a block, if this type has one."""
    return next((f for f in blocks(type_name).get(block, ()) if f.endswith("Enabled")), None)


def _trim(written: dict[str, Any], block: str, type_name: str) -> dict[str, Any]:
    """One block as it is written: type implied, flag implied, prefixes off."""
    short = short_names(type_name, block)
    out = dict(written)

    # A block that states a duration IS a Duration constraint.
    kind = type_field(type_name, block)
    if kind is not None:
        for field_name, settles in implied_types().items():
            full = next((f for f, s in short.items() if s == field_name), None)
            if full is not None and full in out and out.get(kind) == settles:
                out.pop(kind, None)

    # Writing the block is what switches it on -- unless that leaves nothing.
    flag = enabled_field(block, type_name)
    if flag is not None and out.get(flag) is True and len(out) > 1:
        out.pop(flag)
    return {short.get(k, k): v for k, v in out.items()}


def ungroup(type_name: str, body: dict[str, Any], where: str) -> dict[str, Any]:
    """`sla:` back into flat fields. A block holds only its own fields."""
    written = body.get(SLA)
    if not isinstance(written, dict):
        return body
    known = fields_of(type_name)
    sub = blocks(type_name)
    flat: dict[str, Any] = {}
    for key, value in written.items():
        if key in sub:
            if not isinstance(value, dict):
                raise GroupError(f"{where}: sla.{key} must be a mapping")
            long: dict[str, str] = {s: f for f, s in short_names(type_name, key).items()}
            stated: dict[str, Any] = {}
            for name, held in value.items():
                field_name: str = long.get(str(name), str(name))
                if known.get(field_name) != key:
                    raise GroupError(
                        f"{where}: {name!r} is not part of sla.{key}, which holds "
                        f"{', '.join(sorted(long))}"
                    )
                stated[field_name] = held
            flat.update(stated)
            # A stated duration settles the type; a stated type still wins.
            kind_field = type_field(type_name, key)
            if kind_field is not None and kind_field not in stated:
                for short_field, settles in implied_types().items():
                    named = long.get(short_field)
                    if named is not None and named in stated:
                        flat[kind_field] = settles
            # Writing the block is what switches it on; `lsEnabled: false`
            # inside one still wins, because the file said it.
            flag = enabled_field(key, type_name)
            if flag is not None and flag not in stated:
                flat[flag] = True
        elif known.get(key, "") is None:
            flat[key] = value
        else:
            raise GroupError(
                f"{where}: {key!r} is not part of sla, which holds "
                f"{', '.join([*own(type_name), *sub])}"
            )
    out = {k: v for k, v in body.items() if k != SLA}
    for name, value in flat.items():
        if name in out:
            raise GroupError(f"{where}: {name} is written both on its own and inside sla")
        out[name] = value
    return out
