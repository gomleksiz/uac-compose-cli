"""Universal Extensions, saved locally so an import can install them (SPEC §9).

A `taskUniversal` / `taskUniversalMonitor` names an extension through
`template:`, and that extension must already be installed on the target --
nothing about the task creates it. `uac universal-template export` / `import`
move the whole thing as one zip, and `import` upserts by name.

**There is no version field.** `extension` is an internal id and
`minReleaseLevel` is the minimum controller release, so the manifest records a
content `checksum` instead: it cannot claim to be unchanged the way a stale
version number could. A compiled extension also ships `extension_archive.zip`,
which no `get` can read back, so its `archive_checksum` is informational only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uac_compose.core.extensions import ExtensionMap, declared_map
from uac_compose.transport import TEMPLATE_GROUP, Transport, TransportError

MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".freeze.lock"

# Only these two types' `template:` names an extension. `taskEmail.template`
# names an Email Template, a different object entirely.
UNIVERSAL_TASK_TYPES = frozenset({"taskUniversal", "taskUniversalMonitor"})

# Identity and bookkeeping the reading controller stamps on, not content.
_NOT_CONTENT = frozenset(
    {
        "sysId",
        "exportReleaseLevel",
        "retainSysIds",
        "excludeRelated",
        "changeHistory",
        "version",
        "minReleaseLevel",
        "iconDateCreated",
        "iconFilename",
        "iconFilesize",
    }
)

#: The extension's own code. A Universal Extension has no version field, so
#: these are what "the same extension" means for a scripted one: the script
#: itself, per platform. Hashed apart from the rest so a freeze can say whether
#: the CODE changed or only the definition around it (labels, fields, choices).
SCRIPT_FIELDS = ("script", "scriptUnix", "scriptWindows")

#: Kept in the manifest so a person -- and the next freeze -- can tell one
#: build of an extension from another without downloading it again.
METADATA_FIELDS = ("extension", "version", "minReleaseLevel", "exportReleaseLevel", "label")

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


def safe_stem(name: str) -> str:
    """A file stem for an extension name, stable across runs."""
    cleaned = re.sub(r"-{2,}", "-", _UNSAFE.sub("-", name).strip(" ."))[:80].rstrip(" .-")
    if cleaned == name:
        return cleaned
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{cleaned or 'extension'}-{digest}"


def _sum(values: dict[str, Any]) -> str:
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def fingerprint(template: dict[str, Any]) -> str:
    """A checksum of one extension's whole content, bookkeeping stripped."""
    return _sum({k: v for k, v in template.items() if k not in _NOT_CONTENT})


def scripts_checksum(template: dict[str, Any]) -> str | None:
    """A checksum of the extension's code alone (SCRIPT_FIELDS), or None for none.

    None rather than the hash of an empty mapping: 166 of one controller's 271
    extensions are compiled and carry no script at all, and printing one
    identical checksum for all of them says nothing. Their code travels as
    `extension_archive.zip`, which no `get` can read back.
    """
    found = {k: template[k] for k in SCRIPT_FIELDS if template.get(k)}
    return _sum(found) if found else None


def definition_checksum(template: dict[str, Any]) -> str:
    """A checksum of everything except the code and the bookkeeping."""
    return _sum(
        {k: v for k, v in template.items() if k not in _NOT_CONTENT and k not in SCRIPT_FIELDS}
    )


def metadata_of(template: dict[str, Any]) -> dict[str, Any]:
    return {k: template[k] for k in METADATA_FIELDS if template.get(k) is not None}


def template_names(objects: list[dict[str, Any]]) -> list[str]:
    """Every extension a compiled bundle's Universal tasks name, in file order.

    A `${...}` value is resolved when the task runs and is not a dependency.
    """
    found: list[str] = []
    for obj in objects:
        if str(obj.get("type", "")) not in UNIVERSAL_TASK_TYPES:
            continue
        name = obj.get("template")
        if isinstance(name, str) and name and "${" not in name and name not in found:
            found.append(name)
    return found


@dataclass(frozen=True)
class Frozen:
    """One extension's manifest row: the file, what it is, and how to tell it apart.

    Three checksums, all taken from what `universal-template get` answers, so
    the next run compares like with like:

    `scripts_checksum`   the code (SCRIPT_FIELDS) -- there is no version field
    `definition_checksum` everything else: fields, choices, labels, commands
    `checksum`           both together, and what the local zip holds

    A freeze re-downloads only when one of the first two differs, and says
    which. `metadata` is kept so a person can read what was frozen without
    opening the zip.
    """

    file: str
    checksum: str
    scripts_checksum: str | None = None
    definition_checksum: str | None = None
    extension: str | None = None
    min_release: str | None = None
    source_release: str | None = None
    archive_checksum: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "checksum": self.checksum,
            "scripts_checksum": self.scripts_checksum,
            "definition_checksum": self.definition_checksum,
            "extension": self.extension,
            "min_release": self.min_release,
            "source_release": self.source_release,
            "archive_checksum": self.archive_checksum,
            "metadata": self.metadata,
        }


def row_for(file: str, template: dict[str, Any], archive_checksum: str | None = None) -> Frozen:
    """A manifest row for one template, as `get` or the export zip reports it."""
    return Frozen(
        file=file,
        checksum=fingerprint(template),
        scripts_checksum=scripts_checksum(template),
        definition_checksum=definition_checksum(template),
        extension=template.get("extension"),
        min_release=template.get("minReleaseLevel"),
        source_release=template.get("exportReleaseLevel"),
        archive_checksum=archive_checksum,
        metadata=metadata_of(template),
    )


def differences(live: dict[str, Any], frozen: Frozen) -> list[str]:
    """What changed between the controller's copy and the frozen one; empty = same.

    The code and the definition are compared apart, so the answer says which
    moved. A manifest written before those two checksums existed falls back to
    the whole-content checksum.
    """
    if frozen.definition_checksum is None:
        return [] if fingerprint(live) == frozen.checksum else ["content"]
    found = []
    if scripts_checksum(live) != frozen.scripts_checksum:
        # None on both sides is a match: a compiled extension has no script.
        found.append("code")
    if definition_checksum(live) != frozen.definition_checksum:
        found.append("definition")
    return found


@dataclass
class Manifest:
    profile: str = ""
    frozen_at: str = ""
    templates: dict[str, Frozen] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(
            profile=str(data.get("profile", "")),
            frozen_at=str(data.get("frozen_at", "")),
            templates={
                name: Frozen(
                    file=str(row["file"]),
                    checksum=str(row["checksum"]),
                    scripts_checksum=row.get("scripts_checksum"),
                    definition_checksum=row.get("definition_checksum"),
                    extension=row.get("extension"),
                    min_release=row.get("min_release"),
                    source_release=row.get("source_release"),
                    archive_checksum=row.get("archive_checksum"),
                    metadata=dict(row.get("metadata") or {}),
                )
                for name, row in (data.get("templates") or {}).items()
            },
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "profile": self.profile,
                    "frozen_at": self.frozen_at,
                    "templates": {n: r.as_row() for n, r in sorted(self.templates.items())},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def _read_zip(path: Path) -> tuple[dict[str, Any], str | None]:
    with zipfile.ZipFile(path) as archive:
        template = dict(json.loads(archive.read("template.json")))
        archive_sum = None
        if "extension_archive.zip" in archive.namelist():
            blob = archive.read("extension_archive.zip")
            archive_sum = "sha256:" + hashlib.sha256(blob).hexdigest()
    return template, archive_sum


class Busy(Exception):
    """Another `freeze` is writing to this folder."""


@contextmanager
def only_one(out_dir: Path) -> Iterator[None]:
    """Refuse a second freeze into the same folder.

    Each run holds the manifest in memory and writes it back as exports land,
    so two runs sharing a folder let the later save drop rows the earlier one
    added -- measured: 85 zips on disk and 35 rows left in the manifest. A
    lock is cheaper than merging two views of the same file.

    A lock left behind by a run that was killed is taken over, since its
    process is gone; only a live one refuses.
    """
    lock = out_dir / LOCK_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _alive(lock):
            raise Busy(
                f"another uac-compose freeze is writing to {out_dir}; wait for it, or delete "
                f"{lock} if it is gone"
            ) from None
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{os.getpid()}\n")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _alive(lock: Path) -> bool:
    try:
        pid = int(lock.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False  # unreadable: treat as stale rather than block for ever
    # Our own pid counts as live: two freezes in one process (two threads)
    # clobber the manifest exactly as two processes do. The lock is always
    # removed in a `finally`, so this cannot block a later run of this process.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, so it is running
    return True


@dataclass(frozen=True)
class FreezeStep:
    """What happened to one extension. `row` is set unless `error` is."""

    name: str
    row: Frozen | None = None
    error: str | None = None
    skipped: bool = False
    changed: tuple[str, ...] = ()
    """What differed from the frozen copy: `code`, `definition`, or both."""


def freeze_one(
    name: str,
    previous: Frozen | None,
    out_dir: Path,
    transport: Transport,
    refresh: bool,
    live: dict[str, Any] | None = None,
) -> FreezeStep:
    """Export one extension unless the controller still holds what was frozen.

    The check is the definition the controller already reported, not the export
    download: an extension whose code and definition both match the manifest is
    not fetched again. There is no version field, so the code's own checksum
    (SCRIPT_FIELDS) is what "the same extension" means for a scripted one.

    `live` is that definition when the caller already has it (one
    `universal-template list` answers for every extension at once); without it
    this asks for the one extension itself.

    Never raises: one slow or failing extension must cost only that one.
    """
    have_zip = previous is not None and (out_dir / previous.file).exists()
    changed: tuple[str, ...] = ()
    if previous is not None and not refresh and have_zip:
        if live is None:
            try:
                live = transport.get_template(name)
            except TransportError:
                live = None  # could not ask: export rather than trust a stale zip
        if live is not None:
            changed = tuple(differences(live, previous))
            if not changed:
                return FreezeStep(name, previous, skipped=True)
    path = out_dir / f"{safe_stem(name)}.zip"
    try:
        transport.export_template(name, path)
        template, archive_sum = _read_zip(path)
    except Exception as error:  # noqa: BLE001 - see below; KeyboardInterrupt still stops the run
        # Anything at all, because "one slow or failing extension costs only
        # that one" has to hold whatever the transport raises. Measured: a
        # `subprocess.TimeoutExpired` (not an OSError, so not in the list this
        # replaced) ended a freeze of 271 extensions at 269 with a traceback.
        return FreezeStep(name, error=f"{type(error).__name__}: {error}"[:300])
    # The row is taken from the live template where we have it, so the next run
    # compares two answers from the same call rather than a `get` against a zip.
    return FreezeStep(name, row_for(path.name, live or template, archive_sum), changed=changed)


Echo = Callable[[str], None]


#: Above this many, the opening line counts them instead of listing them all.
MAX_LISTED = 12


def name_list(names: list[str]) -> str:
    if len(names) <= MAX_LISTED:
        return ", ".join(names)
    return ", ".join(names[:MAX_LISTED]) + f", and {len(names) - MAX_LISTED} more"


def _timed(
    name: str,
    previous: Frozen | None,
    out_dir: Path,
    transport: Transport,
    refresh: bool,
    live: dict[str, Any] | None,
) -> tuple[FreezeStep, float]:
    at = time.monotonic()
    return freeze_one(name, previous, out_dir, transport, refresh, live), time.monotonic() - at


def catalogue(transport: Transport, echo: Echo) -> dict[str, dict[str, Any]]:
    """Every installed extension's definition, in one call.

    `universal-template list` answers with the same content `get` does -- the
    one extra key it lacks, `retainSysIds`, is bookkeeping the checksums strip
    (measured on 271 real extensions). So deciding what to re-download costs
    one call rather than one per extension.
    """
    try:
        rows = transport.list_raw(TEMPLATE_GROUP)
    except TransportError as error:
        echo(f"   could not list the extensions ({error}); asking for each one instead")
        return {}
    return {str(r["name"]): r for r in rows if isinstance(r, dict) and r.get("name")}


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def describe(
    step: FreezeStep, index: int, total: int, out_dir: Path, seconds: float | None = None
) -> list[str]:
    """The lines one extension prints: what happened, where it went, what it is."""
    head = f"[{index}/{total}]"
    took = f"  {seconds:.1f}s" if seconds is not None else ""
    if step.error:
        return [f"{head} FAILED    {step.name}{took}", f"          {step.error}"]
    row = step.row
    assert row is not None
    path = out_dir / row.file
    size = human_size(path.stat().st_size) if path.exists() else "missing"
    state = "unchanged" if step.skipped else "exported "
    why = f"  ({' and '.join(step.changed)} changed)" if step.changed else ""
    lines = [
        f"{head} {state} {step.name}{took}{why}",
        f"          {row.file}  {size}  {row.checksum[:23]}",
    ]
    detail = []
    if row.scripts_checksum:
        detail.append(f"code {row.scripts_checksum[7:23]}")
    if row.extension:
        detail.append(f"extension {row.extension}")
    if row.min_release:
        detail.append(f"needs controller {row.min_release} or later")
    if row.source_release:
        detail.append(f"exported from {row.source_release}")
    if row.archive_checksum:
        detail.append("carries a compiled archive")
    if detail:
        lines.append("          " + "; ".join(detail))
    return lines


def freeze(
    names: list[str],
    out_dir: Path,
    transport: Transport,
    profile: str,
    echo: Echo,
    *,
    refresh: bool = False,
    parallel: int = 4,
) -> list[FreezeStep]:
    """Export each extension into `out_dir` and write the manifest beside them.

    Plain progress lines, never a redrawn bar: in a pipe or a log a redrawn
    bar reads as a hang.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with only_one(out_dir):
        return _freeze(names, out_dir, transport, profile, echo, refresh, parallel)


def _freeze(
    names: list[str],
    out_dir: Path,
    transport: Transport,
    profile: str,
    echo: Echo,
    refresh: bool,
    parallel: int,
) -> list[FreezeStep]:
    manifest = Manifest.load(out_dir / MANIFEST_NAME)
    total = len(names)
    workers = max(1, parallel)
    echo(f"profile {profile}: {total} extension(s) into {out_dir}")
    echo(f"   {name_list(names)}")
    echo(f"   {workers} at a time; each is exported, checked and written before the next line")
    known = {} if refresh else catalogue(transport, echo)

    done: dict[str, FreezeStep] = {}
    started = time.monotonic()
    manifest.profile = profile
    path = out_dir / MANIFEST_NAME
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            running = {
                pool.submit(
                    _timed,
                    name,
                    manifest.templates.get(name),
                    out_dir,
                    transport,
                    refresh,
                    known.get(name),
                ): name
                for name in names
            }
            # As each one finishes, not after they all have: a batch of 271 must
            # not look like a hang for the whole run.
            for future in as_completed(running):
                step, seconds = future.result()
                done[step.name] = step
                for line in describe(step, len(done), total, out_dir, seconds):
                    echo(line)
                if step.row is not None and not step.skipped:
                    # Recorded as it lands, not at the end. A run of 271 that is
                    # interrupted must not lose what it already downloaded --
                    # without this the next run fetches every one of them again.
                    manifest.templates[step.name] = step.row
                    manifest.frozen_at = datetime.now(UTC).isoformat(timespec="seconds")
                    manifest.save(path)
    except BaseException:  # Ctrl-C included: what was downloaded must not be lost
        manifest.save(path)
        echo(f"stopped after {len(done)}/{total}; {path} keeps what was exported")
        raise
    steps = [done[name] for name in names]
    manifest.frozen_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest.save(path)
    exported = sum(1 for s in steps if s.row and not s.skipped)
    unchanged = sum(1 for s in steps if s.skipped)
    failed = sum(1 for s in steps if s.error)
    total_bytes = sum(
        (out_dir / s.row.file).stat().st_size
        for s in steps
        if s.row and (out_dir / s.row.file).exists()
    )
    echo(
        f"{exported} exported, {unchanged} unchanged, {failed} failed "
        f"in {time.monotonic() - started:.1f}s "
        f"({human_size(total_bytes)} in {len(manifest.templates)} zip(s))"
    )
    echo(f"manifest {path}")
    return steps


# --- import --extensions (SPEC §8.3) -------------------------------------------


@dataclass(frozen=True)
class SyncStep:
    name: str
    action: str  # "install" | "reuse" | "update" | "warn"
    message: str = ""
    file: str = ""
    """The zip to import, on an `install` or `update` step."""


def plan_sync(
    names: list[str], extensions_dir: Path, transport: Transport, *, update: bool = False
) -> list[SyncStep]:
    """Decide what each extension needs, writing nothing.

    Read live: a warning may be stale, a write must not be. Present and
    different is never overwritten silently -- the same fence `--update` draws.
    Deciding here and installing in `install` keeps SPEC §8.6: a conflict on
    any object means nothing was written, extensions included.
    """
    manifest = Manifest.load(extensions_dir / MANIFEST_NAME)
    out: list[SyncStep] = []
    for name in names:
        frozen = manifest.templates.get(name)
        live = transport.get_template(name)
        if live is None and frozen is None:
            out.append(SyncStep(name, "warn", "not installed here, and no local copy frozen"))
        elif live is None:
            assert frozen is not None
            out.append(SyncStep(name, "install", file=frozen.file))
        elif frozen is None or not (changed := differences(live, frozen)):
            out.append(SyncStep(name, "reuse"))
        elif update:
            out.append(SyncStep(name, "update", f"{' and '.join(changed)} differs", frozen.file))
        else:
            out.append(
                SyncStep(
                    name,
                    "warn",
                    f"installed here, but its {' and '.join(changed)} differs from the "
                    "frozen copy (--update-extensions replaces it)",
                )
            )
    return out


def install(
    steps: list[SyncStep], extensions_dir: Path, transport: Transport, echo: Echo | None = None
) -> None:
    """Import the zips `plan_sync` chose. Runs after every object has been decided."""
    chosen = [step for step in steps if step.file]
    for n, step in enumerate(chosen, 1):
        at = time.monotonic()
        transport.import_template(extensions_dir / step.file)
        if echo is not None:
            took = time.monotonic() - at
            verb = "installed" if step.action == "install" else "replaced "
            echo(f"[{n}/{len(chosen)}] {verb} extension {step.name}  {took:.1f}s")


def frozen_map(directory: Path) -> ExtensionMap:
    """What the extensions in a freeze folder declare, read from their zips.

    An extension this run is about to install is not on the target yet, so the
    controller cannot describe it and a task that uses it would be refused
    parameter by parameter. Each zip carries `template.json`, which declares
    exactly what `declared_map` reads on a controller.

    A zip that cannot be read is skipped rather than fatal: this only ever
    *adds* to what is known, and `install` reports a broken archive itself.
    """
    definitions: list[dict[str, Any]] = []
    for archive in sorted(directory.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive) as zf:
                definition = json.loads(zf.read("template.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        if isinstance(definition, list):
            definition = definition[0] if definition else {}
        if isinstance(definition, dict):
            definitions.append(definition)
    return declared_map(definitions)
