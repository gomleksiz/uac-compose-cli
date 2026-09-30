"""Wrap exported objects: UAC "export as JSON" zips, one `<table>_<sysId>.json` each."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from uac_compose import registry
from uac_compose.envelope import ObjectEnvelope
from uac_compose.transport import object_name

_DISCRIMINATED = ("task", "workflow", "trigger")


def envelope_for(obj: dict[str, Any], source: str, export_table: str = "") -> ObjectEnvelope:
    """One exported or fetched object, routed by `exportTable` (or its stamped `type`)."""
    table = str(obj.get("exportTable") or export_table)
    type_value = obj.get("type")
    entry = registry.resolve(table, type_value)
    warnings: list[str] = []
    if entry is None:
        warnings.append(f"unknown export table {table!r} (type {type_value!r})")
        type_name, kind = str(type_value or table), "task"
    elif entry.kind in _DISCRIMINATED and type_value:
        type_name, kind = str(type_value), entry.kind
    else:
        type_name, kind = entry.type, entry.kind
    return ObjectEnvelope(
        type=type_name,
        name=object_name(obj),
        obj=obj,
        release=str(obj.get("exportReleaseLevel", "unknown")),
        kind=kind,
        export_table=table or (entry.export_table or "" if entry else ""),
        source=source,
        warnings=warnings,
    )


def iter_archive(archive: Path) -> Iterator[ObjectEnvelope]:
    """One envelope per JSON member, in member-name order. Unknown types are kept, warned."""
    with zipfile.ZipFile(archive) as zf:
        for member in sorted(zf.namelist()):
            if member.endswith(".json"):
                obj = json.loads(zf.read(member))
                yield envelope_for(obj, f"{archive.name}:{member}", member.rsplit("_", 1)[0])


def read_corpus(archives: Iterable[Path]) -> list[ObjectEnvelope]:
    """Every distinct object across the archives (the tasks and workflows exports overlap)."""
    seen: set[str] = set()
    out: list[ObjectEnvelope] = []
    for archive in archives:
        for envelope in iter_archive(archive):
            digest = envelope.content_hash()
            if digest not in seen:
                seen.add(digest)
                out.append(envelope)
    return out
