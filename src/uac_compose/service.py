"""export, import and apply as functions: what the CLI runs, without the CLI.

A Universal Extension runs these in its own process, with `ApiTransport`.
The command line runs them with `UacCli` and its own prompts. Nothing here
reads a terminal or exits:

- `say` gets the progress lines (the CLI puts them on stderr), `out` the
  results (stdout): the decisions, the plan, the findings;
- a failure raises `ServiceError`, whose text the CLI shows as `Error: ...`;
- a question is a callback. With none, the answer is "no": export of more than
  `ASK_ABOVE` matches needs `yes`, and so does a real apply.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uac_compose import typealias
from uac_compose.learn import Workspace, current_extensions
from uac_compose.transport import Transport, TransportError

Echo = Callable[[str], None]

#: More matches than this and export asks which to take (or needs `yes`).
ASK_ABOVE = 15


class ServiceError(Exception):
    """A command could not do what it was asked; the text says why."""


def _silent(_: str) -> None:
    pass


def too_many(count: int, patterns: Sequence[str]) -> str:
    return (
        f"{count} objects match {', '.join(repr(p) for p in patterns)}; "
        "add -y to export them all, narrow with -t, or see them with -l"
    )


# --- export ------------------------------------------------------------------


@dataclass
class ExportResult:
    text: str = ""
    """The file. Empty when only listing."""
    warnings: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    """What `suggest_templates` wrote."""
    extensions: list[str] = field(default_factory=list)
    """The Universal Extensions the exported tasks use."""
    listed: list[str] = field(default_factory=list)
    """With `list_only`: one line per match."""


Chooser = Callable[[list[Any], list[str], Echo], list[Any]]


def export_bundle(
    transport: Transport,
    ws: Workspace,
    names: Sequence[str],
    *,
    kind: str | None = None,
    uac_bundle: str | None = None,
    style: str = "compact",
    with_members: bool = False,
    show_inactive: bool = False,
    variablize_agents: bool = False,
    suggest_templates: bool = False,
    parallel: int = 4,
    regex: bool = False,
    list_only: bool = False,
    yes: bool = False,
    purpose: str = "promote",
    wheres: Sequence[str] = (),
    profile_label: str = "",
    say: Echo = _silent,
    choose: Chooser | None = None,
) -> ExportResult:
    """Read definitions and write them as one `.uac.yaml` text.

    A NAME may be a wildcard ('*Sleep*') or, with `regex`, a regular
    expression: the names come from a live list of the controller, narrowed by
    `kind`. More than `ASK_ABOVE` matches need `yes`, or a `choose` that picks.
    """
    from uac_compose import emit
    from uac_compose.emit import LookupError_, export_text

    names = tuple(names)
    if uac_bundle and names:
        raise ServiceError("name objects or pass --bundle, not both")
    if uac_bundle and with_members:
        raise ServiceError(
            "--bundle already resolves what the objects need (the controller's own "
            "followReferences rule); --with-members would second-guess it"
        )
    tests = []
    if wheres:
        from uac_compose import where

        if uac_bundle:
            raise ServiceError("--where selects objects; a --bundle already names them")
        try:
            tests = where.parse_terms(list(wheres))
        except where.WhereError as error:
            raise ServiceError(str(error)) from error
        names = names or ("*",)
    if not uac_bundle and not names:
        raise ServiceError("name something to export, or pass --bundle")
    result = ExportResult()

    # With `wheres` every name goes through the list, so every match is tested.
    patterns = [w for w in names if tests or emit.is_pattern(w, regex)]
    if list_only and not patterns:
        raise ServiceError("--list shows what a pattern matches; give one ('*Sleep*')")
    targets = None
    count = len(names)
    if patterns:
        say(f"profile {profile_label}: finding {', '.join(repr(p) for p in patterns)}")
        try:
            found = _matches(patterns, regex, kind, transport, parallel, say)
            found = emit.filtered(found, tests, transport, parallel=parallel, echo=say)
        except (LookupError_, TransportError) as error:
            raise ServiceError(str(error)) from error
        if list_only:
            for item in found:
                shown = typealias.alias_for(item.type) if item.type else ""
                result.listed.append(f"{item.kind.key:24s} {shown:20s} {item.name}")
            return result
        if len(found) > ASK_ABOVE and not yes:
            if choose is None:
                raise ServiceError(too_many(len(found), patterns))
            found = choose(found, patterns, say)
        exact = [w for w in names if w not in patterns]
        try:
            exact_kinds = emit.kinds_for(kind)
        except LookupError_ as error:
            raise ServiceError(str(error)) from error
        targets = [(w, exact_kinds) for w in exact] + [(i.name, [i.kind]) for i in found]
        if not targets:
            raise ServiceError("nothing matches; nothing was written")
        count = len(targets)
    if uac_bundle:
        say(f"profile {profile_label}: asking for UAC Bundle {uac_bundle!r}...")
    else:
        members = " and what they need" if with_members else ""
        say(
            f"profile {profile_label}: reading {count} object(s){members}, "
            f"up to {parallel} at a time"
        )
    try:
        # Learn the extensions first: without the map a Universal task's
        # parameters are written as raw slots (`choiceField8`), not by name.
        say("   reading the controller's extensions...")
        templates = ws.template_set(current_extensions(ws, transport))
        result.text, result.warnings = export_text(
            list(names),
            kind,
            ws,
            transport,
            templates,
            style=style,
            show_inactive=show_inactive,
            with_members=with_members,
            uac_bundle=uac_bundle,
            variablize=variablize_agents,
            suggest_templates=suggest_templates,
            report=result.report,
            echo=say,
            parallel=parallel,
            extensions=result.extensions,
            targets=targets,
            purpose=purpose,
        )
    except (LookupError_, TransportError) as error:
        raise ServiceError(str(error)) from error
    return result


def _matches(
    patterns: list[str], regex: bool, kind: str | None, transport: Any, parallel: int, say: Echo
) -> list[Any]:
    """What the patterns match on the controller, within `kind`."""
    from uac_compose import emit

    warnings: list[str] = []
    listed = emit.list_names(
        emit.type_filter(kind), transport, parallel=parallel, echo=say, warnings=warnings
    )
    if not listed and warnings:
        # Not one kind could be listed: a wrong token or URL, not "no match".
        # Measured: a refused token said "nothing matches; nothing was written".
        raise ServiceError(f"nothing could be listed: {warnings[0]}")
    for warning in warnings:
        say(f"?? {warning}")
    found = emit.select(patterns, regex, listed)
    say(f"   {len(found)} of {len(listed)} listed object(s) match")
    return found


# --- import ------------------------------------------------------------------


@dataclass
class ImportResult:
    decisions: list[Any] = field(default_factory=list)
    written: int = 0
    dry_run: bool = False


def import_bundle(
    transport: Transport,
    ws: Workspace,
    text: str,
    *,
    source: str = "bundle",
    dry_run: bool = False,
    update: bool = False,
    extensions: str | None = None,
    update_extensions: bool = False,
    parallel: int = 4,
    profile_label: str = "",
    say: Echo = _silent,
    out: Echo = _silent,
) -> ImportResult:
    """Apply a bundle idempotently (create / reuse / update / conflict).

    `source` names the bundle in messages (a file name, a URL...). Nothing is
    written when any object has an error or a conflict, or with `dry_run`.
    """
    from uac_compose import importer, validate
    from uac_compose.compile import CompileError, compile_bundle
    from uac_compose.yamlio import BundleError, read_bundle

    if update_extensions and not extensions:
        raise ServiceError("--update-extensions needs --extensions DIR")
    started = time.monotonic()
    try:
        bundle = read_bundle(text)
    except BundleError as error:
        raise ServiceError(f"{source}:\n{error}") from error
    say(f"profile {profile_label}: {source}")
    say("   reading the controller's extensions...")
    # The extension map must describe the controller as it WILL BE, and be
    # read before the file is compiled: a named parameter (`action: upload`)
    # becomes its slot only through the map. With a stale or missing map it
    # stayed an unknown field, and a create sent it as one.
    try:
        ext = current_extensions(ws, transport)
        if extensions:
            from uac_compose.core.extensions import merge_maps
            from uac_compose.freeze import frozen_map

            ext = merge_maps(frozen_map(Path(extensions)), ext)
    except TransportError as error:
        raise ServiceError(str(error)) from error
    templates = ws.template_set(ext)
    try:
        planned = compile_bundle(bundle, templates)
    except CompileError as error:
        raise ServiceError(f"{source}:\n{error}") from error
    say(f"   checking {len(planned)} object(s) against the rules")
    findings = validate.static(planned, None, ext)
    if ws.references.exists():
        known = validate.known_from(json.loads(ws.references.read_text()))
        findings += validate.references_(planned, known, ext)
    for p in planned:
        findings += validate.inactive_stated(p, templates)
    # Errors first, each group in file order. Mixed in file order, the one
    # error that mattered sat 16th of 18 lines, under warnings that did not.
    for finding in sorted(findings, key=lambda f: f.level != "error"):
        out(str(finding))
    if validate.errors(findings):
        raise ServiceError(
            f"{len(validate.errors(findings))} error(s); nothing was read or written"
        )
    try:
        extension_steps = (
            _plan_extensions(planned, Path(extensions), transport, update_extensions, out)
            if extensions
            else []
        )
        say(f"reading {len(planned)} object(s) from the controller, up to {parallel} at a time")
        at = time.monotonic()
        decisions = importer.decide_all(
            planned,
            transport,
            ext,
            update=update,
            services=bundle.joins,
            echo=say,
            parallel=parallel,
        )
        missing = importer.missing_members(planned, transport, say, parallel)
        say(f"read in {time.monotonic() - at:.1f}s")
    except TransportError as error:
        raise ServiceError(str(error)) from error
    create_errors = [
        f
        for d in decisions
        if d.action == "create"
        for f in validate.for_create(d.planned, templates)
    ]
    for finding in create_errors:
        out(str(finding))
    for line in missing:
        out(f"!! {line}")
    if create_errors or missing:
        raise ServiceError(f"{len(create_errors) + len(missing)} error(s); nothing was written")
    for d in decisions:
        was = f"  (was {d.planned.was!r})" if d.planned.renamed else ""
        out(f"{d.action:8s} {d.planned.label}{was}")
        for difference in d.differences:
            out(f"   - {difference}")
        for note in d.notes:
            out(f"   ~ {note}")
    result = ImportResult(decisions=decisions, dry_run=dry_run)
    conflicts = [d for d in decisions if d.action == "conflict"]
    if conflicts:
        hint = "" if update else " (--update rewrites them)"
        raise ServiceError(f"{len(conflicts)} conflict(s); nothing was written{hint}")
    if dry_run:
        out("dry run: nothing was written")
        return result
    try:
        if extensions:
            from uac_compose import freeze as freeze_module

            freeze_module.install(extension_steps, Path(extensions), transport, say)
            if extension_steps:
                # Learn again: the controller now has the extensions, and its
                # own declaration is saved for the next run.
                say("   reading the controller's extensions again...")
                from uac_compose.core.extensions import merge_maps

                ext = merge_maps(ext, current_extensions(ws, transport))
                templates = ws.template_set(ext)
        importer.write(decisions, transport, templates, out)
        result.written = sum(1 for d in decisions if d.action in ("create", "update", "rename"))
        say(f"wrote {result.written} object(s); {time.monotonic() - started:.1f}s in all")
    except TransportError as error:
        raise ServiceError(f"{error}\nstopped; the objects above it were written") from error
    return result


def _plan_extensions(
    planned: list[Any], directory: Path, transport: Any, update: bool, out: Echo
) -> list[Any]:
    """What the target's extensions need. Nothing is installed until objects agree."""
    from uac_compose import freeze as freeze_module

    names = freeze_module.template_names([p.obj for p in planned])
    if not names:
        return []
    if not (directory / freeze_module.MANIFEST_NAME).exists():
        raise ServiceError(
            f"no {freeze_module.MANIFEST_NAME} in {directory} -- run `uac-compose freeze` first"
        )
    out(f"-- extensions from {directory}: {', '.join(names)}")
    steps = freeze_module.plan_sync(names, directory, transport, update=update)
    for step in steps:
        if step.action == "warn":
            out(f"?? extension {step.name}: {step.message}")
        else:
            why = f"  ({step.message})" if step.message else ""
            out(f"{step.action:8s} extension {step.name}{why}")
    return steps


# --- apply -------------------------------------------------------------------


@dataclass
class ApplyResult:
    changes: int = 0
    """Changes the plan holds (not counting "no change")."""
    done: bool = False
    """True when they were written."""


def apply_changes(
    transport: Transport,
    ws: Workspace,
    text: str,
    *,
    source: str = "changes",
    dry_run: bool = False,
    yes: bool = False,
    verbose: bool = False,
    parallel: int = 4,
    backup: str | None = None,
    backup_format: str | None = None,
    profile_label: str = "",
    say: Echo = _silent,
    out: Echo = _silent,
    confirm: Callable[[str], bool] | None = None,
) -> ApplyResult:
    """Run a change file: update, rename and delete steps, in order. Creates only users.

    Every step is planned first, reading live; nothing is written until the
    whole plan holds and `yes` (or `confirm`) agrees. The first failed write
    stops the rest. With `backup`, the objects the plan changes are saved
    first; if the backup cannot be written, nothing is changed.
    """
    from uac_compose import apply as apply_module

    if backup and Path(backup).exists() and not dry_run:
        raise ServiceError(f"--backup {backup} exists; give a new file name")
    try:
        changes = apply_module.read(text)
        say(f"profile {profile_label}: {source}, {len(changes.steps)} step(s)")
        say("   reading the controller's extensions...")
        ext = current_extensions(ws, transport)
        say("planning: every step in order, reading live, writing nothing")
        planner = apply_module.Planner(
            transport, ws.template_set(ext), ext, parallel=parallel, echo=say
        )
        plan = planner.plan(changes)
    except (apply_module.ApplyError, TransportError) as error:
        raise ServiceError(f"{source}:\n{error}") from error
    shown: dict[int, int] = {}
    for change in plan:
        number = change.step.number
        shown[number] = shown.get(number, 0) + 1
        if verbose or shown[number] <= ASK_ABOVE:
            for line in apply_module.describe(change):
                out(line)
        elif shown[number] == ASK_ABOVE + 1:
            more = sum(1 for c in plan if c.step.number == number) - ASK_ABOVE
            out(f"[{number}] ... and {more} more (-v shows them all)")
    todo = [c for c in plan if c.action != "no change"]
    result = ApplyResult(changes=len(todo))
    if not todo:
        out("nothing to change")
        return result
    summary = f"{len(todo)} change(s) to {apply_module.object_count(plan)} object(s)"
    if dry_run:
        if backup:
            out(f"dry run: no backup written ({backup} is written before real writes)")
        out(f"dry run: {summary}; nothing was written")
        return result
    if not yes:
        if confirm is None:
            raise ServiceError(f"{summary}; add -y to run them, or -n to only look")
        if not confirm(summary):
            raise ServiceError("not run; nothing was written")
    if backup:
        _apply_backup(
            apply_module.saved(plan, planner),
            backup,
            apply_module.backup_format(backup, backup_format),
            ws,
            transport,
            ext,
            profile_label,
            source,
            parallel,
            say,
            out,
        )
    try:
        apply_module.run(plan, transport, say)
    except apply_module.ApplyError as error:
        raise ServiceError(str(error)) from error
    out(f"done: {summary}")
    result.done = True
    return result


def _apply_backup(
    items: list[Any],
    path: str,
    fmt: str,
    ws: Workspace,
    transport: Transport,
    ext: Any,
    profile_label: str,
    source: str,
    parallel: int,
    say: Echo,
    out: Echo,
) -> None:
    """Write the backup, or stop: without it, nothing is changed."""
    from uac_compose import apply as apply_module
    from uac_compose.emit import LookupError_

    say(f"backup: saving {len(items)} object(s) to {path} ({fmt})")
    try:
        if fmt == "json":
            text = apply_module.backup_json(items, profile_label, source)
        else:
            text, warnings = apply_module.backup_compose(
                items, ws, transport, ws.template_set(ext), ext, parallel=parallel, echo=say
            )
            for warning in warnings:
                say(f"?? {warning}")
        Path(path).write_text(text)
    except (LookupError_, TransportError, OSError) as error:
        raise ServiceError(f"backup failed, nothing was changed: {error}") from error
    undo = (
        f"uac-compose import {path} -u -p {profile_label}"
        if fmt == "compose"
        else "the objects are in the file, as the controller returned them"
    )
    out(f"wrote backup {path}; to undo: {undo}")
