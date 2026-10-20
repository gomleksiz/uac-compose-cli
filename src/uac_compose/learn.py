"""`uac-compose learn`: read a controller once so export and import can work from a cache (SPEC §4).

Workspace, one per profile (never shared between controllers):

    <workspace>/<profile>/
      source.json          release, fetched_at, counts
      cache/<kind>.json    complete definitions, routing key stamped
      references.json      names of what objects may refer to (identity only)
      templates/<release>/ _extensions.json: this controller's extension map

Defaults are **not** learned: they are the ones uac-compose ships, the same on every
controller, so a field a file leaves out means the same at both ends of a
promotion. See docs/LESSONS.md, "A default is a promise between two
controllers"
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from uac_compose import registry
from uac_compose.core.derive import Derived, TypeReport, derive
from uac_compose.core.extensions import ExtensionMap, declared_map, merge_maps, observed_map
from uac_compose.core.inflate import inflate
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet, shipped_release
from uac_compose.envelope import ObjectEnvelope
from uac_compose.ingest.archive import envelope_for
from uac_compose.transport import KINDS, Transport, object_name

# Kinds whose `list` is only a summary: names (and type) are all we keep.
# A user is not an object a bundle creates, but an Approval task names one and
# the controller refuses a create that names one it does not have
# ("User 'jdoe' not found"), so the names are worth knowing.
SUMMARY_REFERENCES = {"agents": "agent", "users": "user"}

#: Where a kind keeps its name, when it is not `name`.
REFERENCE_NAME_KEYS = {"users": "userName"}
# Identity fields kept for a reference. Never `runtimeUser` or other attributes.
IDENTITY = ("name", "type")


@dataclass
class Workspace:
    root: Path

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def source(self) -> Path:
        return self.root / "source.json"

    @property
    def references(self) -> Path:
        return self.root / "references.json"

    def templates(self, release: str) -> Path:
        return self.root / "templates" / release

    def cache_file(self, key: str) -> Path:
        return self.cache / f"{key}.json"

    def release(self) -> str | None:
        if not self.source.exists():
            return None
        release: str | None = json.loads(self.source.read_text()).get("release")
        return release

    def read_cache(self, key: str) -> list[dict[str, Any]]:
        path = self.cache_file(key)
        return json.loads(path.read_text()) if path.exists() else []

    def by_name(self, key: str) -> dict[str, dict[str, Any]]:
        """One kind's cache as name -> object, parsed once for each version of the file.

        `export` looks every object up here. A large controller's tasks file
        is 25.6 MB and takes 0.13 s to parse, and that was paid again for each
        object read -- a quarter of the time of a 0.5 s `get`. Shared by every
        caller, so read it and never change it.
        """
        path = self.cache_file(key)
        if not path.exists():
            return {}
        stat = path.stat()
        return _by_name(path, stat.st_mtime_ns, stat.st_size)

    def template_set(self, extensions: ExtensionMap | None = None) -> TemplateSet:
        """The shipped defaults, read with this profile's extension map.

        Pass `extensions` (from `current_extensions`) to read with the map the
        controller has now, not the one saved on the last run.

        The defaults are deliberately **not** this controller's: they are the
        same on every controller, so a field left out of a file means the same
        thing at both ends. What `learn` contributes is the extension map --
        which Universal templates this controller has, and which slot each of
        their parameters sits in.
        """
        release = self.release()
        return TemplateSet(
            shipped_release(release),
            extensions_path=self.templates(release or "").joinpath("_extensions.json"),
            extensions=extensions,
        )

    def envelopes(self) -> list[ObjectEnvelope]:
        return [
            envelope_for(obj, f"cache/{kind.key}")
            for kind in KINDS
            for obj in self.read_cache(kind.key)
        ]


@lru_cache(maxsize=64)
def _by_name(path: Path, mtime_ns: int, size: int) -> dict[str, dict[str, Any]]:
    """The first object of each name, as a scan of the file finds it. The time
    and size are only the key: a file `learn` rewrites is parsed again."""
    index: dict[str, dict[str, Any]] = {}
    for obj in json.loads(path.read_text()):
        index.setdefault(object_name(obj), obj)
    return index


@dataclass
class SelfCheck:
    total: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def by_type(self) -> Counter[str]:
        return Counter(f.split("/", 1)[0] for f in self.failures)


@dataclass
class LearnResult:
    release: str
    counts: dict[str, int]
    fetched: list[str]
    report: dict[str, TypeReport]
    check: SelfCheck
    extension_templates: int
    disagreements: list[str] = field(default_factory=list)


Echo = Callable[[str], None]


def fetch(ws: Workspace, transport: Transport, refresh: bool, echo: Echo) -> list[str]:
    """Fetch each kind into the cache, or keep the cached copy. Returns kinds fetched."""
    ws.cache.mkdir(parents=True, exist_ok=True)
    fetched: list[str] = []
    for kind in KINDS:
        path = ws.cache_file(kind.key)
        if path.exists() and not refresh:
            continue
        rows = transport.list_kind(kind)
        path.write_text(json.dumps(rows, indent=1))
        fetched.append(kind.key)
        echo(f"fetched {kind.key}: {len(rows)}")
    return fetched


def release_of(envelopes: list[ObjectEnvelope]) -> str:
    """The release most objects carry (never the spec's: that is another controller's)."""
    counts = Counter(e.release for e in envelopes if e.release != "unknown")
    return counts.most_common(1)[0][0] if counts else "unknown"


def references(ws: Workspace, transport: Transport) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for key, group in SUMMARY_REFERENCES.items():
        out[key] = _identities(transport.list_raw(group), name_key=REFERENCE_NAME_KEYS.get(key))
    for kind in KINDS:
        out[kind.key] = _identities(ws.read_cache(kind.key), own_type=kind.own_type)
    return out


def _identities(
    rows: list[dict[str, Any]], own_type: bool = False, name_key: str | None = None
) -> list[dict[str, str]]:
    found = []
    for row in rows:
        name = str(row.get(name_key) or "") if name_key else object_name(row)
        if not name:
            continue
        ident = {"name": name}
        kind_of = row.get("_uacType") if own_type else row.get("type")
        if isinstance(kind_of, str) and kind_of:
            ident["type"] = kind_of
        found.append(ident)
    return sorted(found, key=lambda r: r["name"])


def extension_map(ws: Workspace, transport: Transport) -> ExtensionMap:
    declared = declared_map(transport.list_raw("universal-template"))
    observed = observed_map([normalize(e.obj) for e in ws.envelopes()])
    return merge_maps(declared, observed)


def current_extensions(ws: Workspace, transport: Transport) -> ExtensionMap:
    """The extension map, re-read from the controller and saved to the profile.

    An extension installed since the last `learn` -- by a person, or by
    `import --extensions` on the run before -- is absent from the stored map,
    and every parameter of a task that uses it is then refused one line at a
    time. Measured live: one such extension produced 17 error lines, none of
    which said the map was out of date.

    Every command that reads or writes extension tasks calls this first, as
    Ansible gathers facts on each connect. A profile never learned gets its
    map here too (saved where `template_set` looks for it): without one,
    export wrote raw `choiceField8` slots and import compiled named
    parameters as unknown fields.

    One `universal-template list`: 1.8s for 30 extensions, 4.8s for 276.
    """
    merged = extension_map(ws, transport)  # the same merge learn does, re-read
    target = ws.templates(ws.release() or "")
    target.mkdir(parents=True, exist_ok=True)
    merged.save(target / "_extensions.json")
    return merged


def write_templates(target: Path, derived: Derived, ext: ExtensionMap) -> None:
    (target / "_sub").mkdir(parents=True, exist_ok=True)
    for type_name, template in derived.templates.items():
        rep = derived.report[type_name]
        meta = {"_instances": rep.instances, "_source": rep.source}
        (target / f"{type_name}.json").write_text(
            json.dumps({**meta, **template}, indent=2, sort_keys=True) + "\n"
        )
    for path, template in derived.sub_templates.items():
        (target / "_sub" / f"{path}.json").write_text(
            json.dumps(template, indent=2, sort_keys=True) + "\n"
        )
    ext.save(target / "_extensions.json")


def self_check(envelopes: list[ObjectEnvelope], templates: TemplateSet) -> SelfCheck:
    """Round trip every cached object through simplify/inflate with extension collapse."""
    check = SelfCheck(total=len(envelopes))
    subs, ext = templates.sub_templates, templates.extensions
    for e in envelopes:
        if e.type not in registry.load():
            check.failures.append(f"{e.type}/{e.name} (unknown type)")
            continue
        t = templates.get(e.type)
        reserved = frozenset(registry.get(e.type).fields)
        small = simplify(e.obj, t, subs, extensions=ext, reserved=reserved)
        if inflate(small, t, subs, extensions=ext, reserved=reserved) != normalize(e.obj):
            check.failures.append(f"{e.type}/{e.name}")
    return check


def disagreements(derived: Derived, shipped: TemplateSet) -> list[str]:
    """Where this controller's own habit differs from the default uac-compose travels on.

    Reported, never used. A field uac-compose leaves out of a file must mean the same
    at both ends, so the default that decides it is the shipped one. What this
    controller does instead is still worth saying out loud: it is exactly the
    set of fields a file will now state rather than imply.
    """
    out = []
    for type_name, template in sorted(derived.templates.items()):
        theirs = shipped.get(type_name)
        for field_name, value in sorted(template.items()):
            if field_name in theirs and theirs[field_name] != value:
                out.append(
                    f"{type_name}.{field_name}: "
                    f"uac-compose {theirs[field_name]!r}, here {value!r}"
                )
    return out


def learn(ws: Workspace, transport: Transport, refresh: bool, echo: Echo) -> LearnResult:
    fetched = fetch(ws, transport, refresh, echo)
    envelopes = ws.envelopes()
    release = release_of(envelopes)
    counts = {kind.key: len(ws.read_cache(kind.key)) for kind in KINDS}

    ws.references.write_text(json.dumps(references(ws, transport), indent=1) + "\n")
    ext = extension_map(ws, transport)

    # The defaults are the shipped ones, for every controller. Deriving still
    # happens, but only to say where this controller disagrees with them.
    shipped = TemplateSet(shipped_release(release))
    derived = derive(envelopes, shipped if shipped.root.is_dir() else None)
    differs = disagreements(derived, shipped)

    # Write the extension map into a new directory; it replaces the old one
    # only if every cached object survives the round trip with the shipped
    # defaults read through it.
    final = ws.templates(release)
    staging_root = final.parent / ".staging"
    staging = staging_root / release
    shutil.rmtree(staging_root, ignore_errors=True)
    staging.mkdir(parents=True)
    ext.save(staging / "_extensions.json")
    check = self_check(
        envelopes,
        TemplateSet(shipped.release, extensions_path=staging / "_extensions.json"),
    )
    if check.ok:
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
        staging_root.rmdir()
        ws.source.write_text(
            json.dumps(
                {
                    "release": release,
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "counts": counts,
                },
                indent=1,
            )
            + "\n"
        )
    else:
        shutil.rmtree(staging_root, ignore_errors=True)
        (ws.root / "learn-report.txt").write_text("\n".join(check.failures) + "\n")
    return LearnResult(
        release=release,
        counts=counts,
        fetched=fetched,
        report=derived.report,
        check=check,
        extension_templates=len(ext.templates),
        disagreements=differs,
    )
