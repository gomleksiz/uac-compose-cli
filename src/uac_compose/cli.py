"""The commands: learn, export, import, freeze, show, apply, find."""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import click

from uac_compose import __version__, typealias
from uac_compose.paths import ConfigError, profile_dir, profile_name

_profile = click.option(
    "-p", "--profile", default=None, help="Controller profile (env UAC_COMPOSE_PROFILE)."
)


#: Shown under the command list, so plain `uac-compose` says them. `\b` keeps
#: click from joining the lines into one paragraph.
ENVIRONMENT = """\b
Environment variables:
  UAC_COMPOSE_PROFILE    the controller profile when -p is not given
                         (default: default)
  UAC_COMPOSE_WORKSPACE  where the cache, extensions and reports go
                         (default: ~/.uac/compose)
  UAC_COMPOSE_UAC        the full path to the uac CLI, when it is not on PATH
                         (e.g. C:\\Tools\\uac\\uac.exe)
"""


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, epilog=ENVIRONMENT)
@click.version_option(__version__, prog_name="uac_compose")
def cli() -> None:
    """Export UAC definitions to readable .uac.yaml bundles and import them back."""


@cli.command()
@_profile
@click.option("-r", "--refresh", is_flag=True, help="Re-fetch instead of reusing the cache.")
def learn(profile: str | None, refresh: bool) -> None:
    """Read a controller once: cache its definitions and map its extensions."""
    from uac_compose.learn import Workspace
    from uac_compose.learn import learn as run_learn
    from uac_compose.transport import TransportError, UacCli

    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    click.echo(f"profile {name}: {ws.root}")
    try:
        result = run_learn(ws, UacCli(name), refresh, click.echo)
    except TransportError as error:
        raise click.ClickException(str(error)) from error

    total = sum(result.counts.values())
    click.echo(f"release {result.release}, {total} objects cached")
    if not result.fetched:
        click.echo("   (from the cache; --refresh re-fetches)")
    click.echo(f"extension templates mapped: {result.extension_templates}")
    click.echo("defaults: the ones uac-compose ships, the same on every controller")
    if result.disagreements:
        click.echo(f"   this controller sets {len(result.disagreements)} field(s) differently;")
        click.echo("   a file states those fields instead of leaving them out:")
        for line in result.disagreements:
            click.echo(f"     {line}")
    check = result.check
    click.echo(f"self-check: {check.total - len(check.failures)}/{check.total} ok")
    if not check.ok:
        for type_name, n in check.by_type().most_common():
            click.echo(f"   {type_name}: {n} failed")
        raise click.ClickException(
            f"extension map not written; failures listed in {ws.root / 'learn-report.txt'}"
        )


@cli.command()
@_profile
@click.argument("names", nargs=-1)
@click.option("-t", "--type", "kind", default=None, help="Object kind, when a name is ambiguous.")
@click.option(
    "-b",
    "--bundle",
    "uac_bundle",
    default=None,
    metavar="NAME",
    help="Export a UAC Bundle: every object `bundle get-report` resolves it to, with the "
    "controller's own followReferences rule. Its excludeOnExistence becomes create_only:.",
)
@click.option(
    "-s",
    "--style",
    type=click.Choice(["compact", "standard", "uac"]),
    default="compact",
    help="compact: short names and idioms. standard: UAC's own field names and nesting, "
    "defaults removed (`uac` is the old name for it).",
)
@click.option("-w", "--with-members", is_flag=True, help="Include what the objects depend on.")
@click.option("-i", "--show-inactive", is_flag=True, help="Keep fields whose feature is off.")
@click.option(
    "-a",
    "--variablize-agents",
    is_flag=True,
    help="Name each agent through a variable (agent: ${agent_x}) and add the variables.",
)
@click.option(
    "-T",
    "--suggest-templates",
    is_flag=True,
    help="Write fields that several objects share once, as a `templates:` entry, "
    "and give those objects `like:` it.",
)
@click.option(
    "-j",
    "--parallel",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="How many objects to read from the controller at once.",
)
@click.option(
    "-d",
    "-dex",
    "--export-extensions",
    "--download-extensions",
    "export_extensions",
    is_flag=True,
    help="Also freeze the Universal Extensions the exported tasks use, as `uac-compose "
    "freeze` does, into <workspace>/<profile>/extensions (only new or changed ones).",
)
@click.option(
    "-E",
    "--regex",
    is_flag=True,
    help="Read every NAME as a regular expression (case ignored). Without it, a NAME "
    "holding * ? or [ is a wildcard: '*Sleep*'.",
)
@click.option(
    "-W",
    "--where",
    "wheres",
    multiple=True,
    metavar="TEST",
    help="Keep only objects where TEST holds; repeat for more. field=wildcard, "
    "field!=x, field~regex, field*=text, field=[a,b]. Case ignored. "
    "Without NAMEs it looks at every object of the -t kinds.",
)
@click.option(
    "-l",
    "--list",
    "list_only",
    is_flag=True,
    help="Print what the patterns match and stop; nothing is read or written.",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Export every match without asking, even more than 15.",
)
@click.option(
    "-P",
    "--purpose",
    type=click.Choice(["promote", "update"]),
    default="promote",
    show_default=True,
    help="promote: a file for another controller. update: a file for changing these "
    "objects in place; each gets `was: <name>`, so a changed key renames it.",
)
@click.option("-o", "--out", type=click.Path(dir_okay=False), default=None)
def export(
    profile: str | None,
    names: tuple[str, ...],
    kind: str | None,
    uac_bundle: str | None,
    style: str,
    with_members: bool,
    show_inactive: bool,
    variablize_agents: bool,
    suggest_templates: bool,
    parallel: int,
    export_extensions: bool,
    regex: bool,
    list_only: bool,
    yes: bool,
    purpose: str,
    wheres: tuple[str, ...],
    out: str | None,
) -> None:
    """Write definitions to a bundle file.

    A NAME may be a wildcard ('*Sleep*') or, with -E, a regular expression:
    the names come from a live list of the controller, narrowed by -t
    (a kind like task or credential, or a type like linux or timer).
    """
    from uac_compose import emit
    from uac_compose.emit import LookupError_, export_text
    from uac_compose.learn import Workspace
    from uac_compose.transport import TransportError, UacCli

    if uac_bundle and names:
        raise click.ClickException("name objects or pass --bundle, not both")
    if uac_bundle and with_members:
        raise click.ClickException(
            "--bundle already resolves what the objects need (the controller's own "
            "followReferences rule); --with-members would second-guess it"
        )
    tests = []
    if wheres:
        from uac_compose import where

        if uac_bundle:
            raise click.ClickException("--where selects objects; a --bundle already names them")
        try:
            tests = where.parse_terms(list(wheres))
        except where.WhereError as error:
            raise click.ClickException(str(error)) from error
        names = names or ("*",)
    if not uac_bundle and not names:
        raise click.ClickException("name something to export, or pass --bundle")
    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    report: list[str] = []
    needed: list[str] = []

    # Progress goes to stderr: without --out the file itself is on stdout.
    def say(line: str) -> None:
        click.echo(line, err=True)

    # With --where every name goes through the list, so every match is tested.
    patterns = [w for w in names if tests or emit.is_pattern(w, regex)]
    if list_only and not patterns:
        raise click.ClickException("--list shows what a pattern matches; give one ('*Sleep*')")
    targets = None
    count = len(names)
    if patterns:
        say(f"profile {name}: finding {', '.join(repr(p) for p in patterns)}")
        try:
            found = _matches(patterns, regex, kind, UacCli(name), parallel, say)
            found = emit.filtered(found, tests, UacCli(name), parallel=parallel, echo=say)
        except (LookupError_, TransportError) as error:
            raise click.ClickException(str(error)) from error
        if list_only:
            for item in found:
                shown = typealias.alias_for(item.type) if item.type else ""
                click.echo(f"{item.kind.key:24s} {shown:20s} {item.name}")
            return
        if len(found) > ASK_ABOVE and not yes:
            found = _choose(found, patterns, say)
        exact = [w for w in names if w not in patterns]
        try:
            exact_kinds = emit.kinds_for(kind)
        except LookupError_ as error:
            raise click.ClickException(str(error)) from error
        targets = [(w, exact_kinds) for w in exact] + [(i.name, [i.kind]) for i in found]
        if not targets:
            raise click.ClickException("nothing matches; nothing was written")
        count = len(targets)
    if uac_bundle:
        say(f"profile {name}: asking for UAC Bundle {uac_bundle!r}...")
    else:
        members = " and what they need" if with_members else ""
        say(f"profile {name}: reading {count} object(s){members}, up to {parallel} at a time")
    try:
        text, warnings = export_text(
            list(names),
            kind,
            ws,
            UacCli(name),
            ws.template_set(),
            style=style,
            show_inactive=show_inactive,
            with_members=with_members,
            uac_bundle=uac_bundle,
            variablize=variablize_agents,
            suggest_templates=suggest_templates,
            report=report,
            echo=say,
            parallel=parallel,
            extensions=needed,
            targets=targets,
            purpose=purpose,
        )
    except (LookupError_, TransportError) as error:
        raise click.ClickException(str(error)) from error
    for warning in warnings:
        click.echo(f"?? {warning}", err=True)
    for line in report:
        click.echo(line, err=True)
    if out:
        Path(out).write_text(text)
        click.echo(f"wrote {out}")
    else:
        click.echo(text, nl=False)
    if export_extensions:
        _freeze_after_export(needed, ws, UacCli(name), name, out, say, parallel)


#: More matches than this and export asks which to take (or needs -y).
ASK_ABOVE = 15


def _matches(
    patterns: list[str], regex: bool, kind: str | None, transport: Any, parallel: int, say: Any
) -> list[Any]:
    """What the patterns match on the controller, within -t."""
    from uac_compose import emit

    warnings: list[str] = []
    listed = emit.list_names(
        emit.type_filter(kind), transport, parallel=parallel, echo=say, warnings=warnings
    )
    for warning in warnings:
        say(f"?? {warning}")
    found = emit.select(patterns, regex, listed)
    say(f"   {len(found)} of {len(listed)} listed object(s) match")
    return found


def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask(question: str) -> str:
    answer: str = click.prompt(question, default="", show_default=False, err=True)
    return answer


def _choose(found: list[Any], patterns: list[str], say: Any) -> list[Any]:
    """Many matches: say how many of each kind, and ask which to export.

    Without a terminal there is no one to ask, so nothing is read: `-y` says
    take them all, `-t` narrows, `-l` shows them first.
    """
    groups: dict[str, list[Any]] = {}
    for item in found:
        groups.setdefault(item.kind.key, []).append(item)
    keys = list(groups)
    if not _interactive():
        raise click.ClickException(
            f"{len(found)} objects match {', '.join(repr(p) for p in patterns)}; "
            "add -y to export them all, narrow with -t, or see them with -l"
        )
    say(f"{len(found)} objects match:")
    while True:
        for i, key in enumerate(keys, 1):
            items = groups[key]
            types = Counter(typealias.alias_for(x.type) for x in items if x.type)
            top = ", ".join(f"{t} {n:,}" for t, n in types.most_common(4))
            more = ", ..." if len(types) > 4 else ""
            say(f"  {i}) {key:24s} {len(items):6,}" + (f"  ({top}{more})" if top else ""))
        answer = _ask("Export which? all, numbers like 1,3, list, or none").strip().lower()
        if answer in ("all", "a", "*"):
            return found
        if answer in ("", "none", "n", "q"):
            raise click.ClickException("nothing chosen; nothing was written")
        if answer == "list":
            for item in found:
                say(f"     {item.kind.key[:-1]:22s} {item.name}")
            continue
        picks = [p.strip() for p in answer.split(",")]
        if all(p.isdigit() and 1 <= int(p) <= len(keys) for p in picks):
            return [x for p in dict.fromkeys(picks) for x in groups[keys[int(p) - 1]]]
        say(f"?? {answer!r}: answer all, none, list, or numbers from 1 to {len(keys)}")


def _freeze_after_export(
    names: list[str],
    ws: Any,
    transport: Any,
    profile_label: str,
    out: str | None,
    say: Any,
    parallel: int,
) -> None:
    """`uac-compose freeze FILE`, run on what was just exported. After the file
    is written, so a failed extension never costs the bundle."""
    from uac_compose import freeze as freeze_module

    if not names:
        say("   the exported tasks name no Universal Extension; nothing to freeze")
        return
    target = ws.root / "extensions"
    try:
        steps = freeze_module.freeze(
            names, target, transport, profile_label, say, parallel=parallel
        )
    except freeze_module.Busy as error:
        raise click.ClickException(str(error)) from error
    failed = [s for s in steps if s.error]
    if failed:
        raise click.ClickException(
            f"{len(failed)} of {len(steps)} extension(s) could not be exported: "
            + freeze_module.name_list([s.name for s in failed])
            + "\nthe bundle was written; run the same command again to retry them"
        )
    say(
        "install them with: uac-compose import "
        + (out or "FILE")
        + f" --extensions {target} --profile TARGET"
    )


@cli.command(name="import")
@_profile
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("-n", "--dry-run", is_flag=True, help="Decide and compare; write nothing.")
@click.option("-u", "--update", is_flag=True, help="Update objects that differ.")
@click.option(
    "-e",
    "--extensions",
    type=click.Path(file_okay=False),
    default=None,
    metavar="DIR",
    help="Folder of Universal Extension zips from `uac-compose freeze` (it holds manifest.json). "
    "An extension the target lacks is installed from here before any object is written.",
)
@click.option(
    "-U",
    "--update-extensions",
    is_flag=True,
    help="Also replace an extension the target has but that differs from the frozen copy.",
)
@click.option(
    "-j",
    "--parallel",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="How many objects to read from the controller at once.",
)
def import_(
    profile: str | None,
    file: str,
    dry_run: bool,
    update: bool,
    extensions: str | None,
    update_extensions: bool,
    parallel: int,
) -> None:
    """Apply a bundle idempotently (create / reuse / update / conflict)."""
    from uac_compose import importer, validate
    from uac_compose.compile import CompileError, compile_bundle
    from uac_compose.learn import Workspace, current_extensions
    from uac_compose.transport import TransportError, UacCli
    from uac_compose.yamlio import BundleError, read_bundle

    if update_extensions and not extensions:
        raise click.ClickException("--update-extensions needs --extensions DIR")
    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    templates = ws.template_set()
    transport = UacCli(name)
    started = time.monotonic()

    # Progress goes to stderr, as export's does; the decisions stay on stdout.
    def say(line: str) -> None:
        click.echo(line, err=True)

    try:
        bundle = read_bundle(Path(file).read_text())
        planned = compile_bundle(bundle, templates)
    except (BundleError, CompileError) as error:
        raise click.ClickException(f"{file}:\n{error}") from error
    say(f"profile {name}: {file}, {len(planned)} object(s)")
    say("   reading the controller's extensions...")
    # The extension map must describe the controller as it WILL BE, or a task
    # using an extension the stored map has never seen is refused parameter by
    # parameter, with nothing saying the map is stale.
    try:
        ext = current_extensions(ws, transport)
        if extensions:
            from uac_compose.core.extensions import merge_maps
            from uac_compose.freeze import frozen_map

            ext = merge_maps(frozen_map(Path(extensions)), ext)
    except TransportError as error:
        raise click.ClickException(str(error)) from error
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
        click.echo(str(finding))
    if validate.errors(findings):
        raise click.ClickException(
            f"{len(validate.errors(findings))} error(s); nothing was read or written"
        )
    try:
        extension_steps = (
            _plan_extensions(planned, Path(extensions), transport, update_extensions)
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
        raise click.ClickException(str(error)) from error
    create_errors = [
        f
        for d in decisions
        if d.action == "create"
        for f in validate.for_create(d.planned, templates)
    ]
    for finding in create_errors:
        click.echo(str(finding))
    for line in missing:
        click.echo(f"!! {line}")
    if create_errors or missing:
        raise click.ClickException(
            f"{len(create_errors) + len(missing)} error(s); nothing was written"
        )
    for d in decisions:
        was = f"  (was {d.planned.was!r})" if d.planned.renamed else ""
        click.echo(f"{d.action:8s} {d.planned.label}{was}")
        for difference in d.differences:
            click.echo(f"   - {difference}")
        for note in d.notes:
            click.echo(f"   ~ {note}")
    conflicts = [d for d in decisions if d.action == "conflict"]
    if conflicts:
        hint = "" if update else " (--update rewrites them)"
        raise click.ClickException(f"{len(conflicts)} conflict(s); nothing was written{hint}")
    if dry_run:
        click.echo("dry run: nothing was written")
        return
    try:
        if extensions:
            from uac_compose import freeze as freeze_module

            freeze_module.install(extension_steps, Path(extensions), transport, say)
        importer.write(decisions, transport, templates, click.echo)
        written = sum(1 for d in decisions if d.action in ("create", "update", "rename"))
        say(f"wrote {written} object(s); {time.monotonic() - started:.1f}s in all")
    except TransportError as error:
        raise click.ClickException(
            f"{error}\nstopped; the objects above it were written"
        ) from error


@cli.command()
@_profile
@click.argument("file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o",
    "--out",
    type=click.Path(file_okay=False),
    default=None,
    help="Folder for the zips and manifest.json. Default: <workspace>/<profile>/extensions.",
)
@click.option("-r", "--refresh", is_flag=True, help="Re-export every extension.")
@click.option(
    "-j",
    "--parallel",
    type=int,
    default=4,
    show_default=True,
    help="How many extensions to export at once.",
)
def freeze(
    profile: str | None, file: str | None, out: str | None, refresh: bool, parallel: int
) -> None:
    """Export the Universal Extensions a bundle needs."""
    from uac_compose import freeze as freeze_module
    from uac_compose.compile import CompileError, compile_bundle
    from uac_compose.learn import Workspace
    from uac_compose.transport import TEMPLATE_GROUP, TransportError, UacCli
    from uac_compose.yamlio import BundleError, read_bundle

    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    transport = UacCli(name)
    try:
        if file:
            bundle = read_bundle(Path(file).read_text())
            planned = compile_bundle(bundle, ws.template_set())
            names = freeze_module.template_names([p.obj for p in planned])
            click.echo(f"{file}: {len(planned)} object(s)")
            if not names:
                click.echo("   it names no Universal Extension; nothing to freeze")
                return
        else:
            click.echo(f"asking {name} for its Universal Extensions...")
            names = sorted(
                str(row["name"]) for row in transport.list_raw(TEMPLATE_GROUP) if row.get("name")
            )
            if not names:
                click.echo(f"profile {name} has no Universal Extension; nothing to freeze")
                return
    except (BundleError, CompileError) as error:
        raise click.ClickException(f"{file}:\n{error}") from error
    except TransportError as error:
        raise click.ClickException(str(error)) from error

    target = Path(out) if out else ws.root / "extensions"
    try:
        steps = freeze_module.freeze(
            names, target, transport, name, click.echo, refresh=refresh, parallel=parallel
        )
    except freeze_module.Busy as error:
        raise click.ClickException(str(error)) from error
    failed = [s for s in steps if s.error]
    if failed:
        # Run it again to retry them: what succeeded is skipped, so a run that
        # lost a controller part way through costs only what it did not get.
        raise click.ClickException(
            f"{len(failed)} of {len(steps)} extension(s) could not be exported: "
            + freeze_module.name_list([s.name for s in failed])
            + "\nrun the same command again to retry them; the rest are kept"
        )
    click.echo(
        "install them with: uac-compose import "
        + (file or "FILE")
        + f" --extensions {target}"
        + (f" --profile {name}" if profile else "")
    )


def _plan_extensions(
    planned: list[Any], directory: Path, transport: Any, update: bool
) -> list[Any]:
    """What the target's extensions need. Nothing is installed until objects agree."""
    from uac_compose import freeze as freeze_module

    names = freeze_module.template_names([p.obj for p in planned])
    if not names:
        return []
    if not (directory / freeze_module.MANIFEST_NAME).exists():
        raise click.ClickException(
            f"no {freeze_module.MANIFEST_NAME} in {directory} -- run `uac-compose freeze` first"
        )
    click.echo(f"-- extensions from {directory}: {', '.join(names)}")
    steps = freeze_module.plan_sync(names, directory, transport, update=update)
    for step in steps:
        if step.action == "warn":
            click.echo(f"?? extension {step.name}: {step.message}")
        else:
            why = f"  ({step.message})" if step.message else ""
            click.echo(f"{step.action:8s} extension {step.name}{why}")
    return steps


@cli.command(name="apply")
@_profile
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("-n", "--dry-run", is_flag=True, help="Show the plan; write nothing.")
@click.option("-y", "--yes", is_flag=True, help="Run the plan without asking.")
@click.option("-v", "--verbose", is_flag=True, help="Show every object of a big step.")
@click.option(
    "-j",
    "--parallel",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="How many objects to read from the controller at once.",
)
@click.option(
    "-B",
    "--backup",
    type=click.Path(dir_okay=False),
    default=None,
    metavar="FILE",
    help="Before the first write, save every object the plan changes to FILE (a new "
    "file). Undo with: uac-compose import FILE -u (compose format).",
)
@click.option(
    "--backup-format",
    type=click.Choice(["compose", "json"]),
    default=None,
    help="compose: a .uac.yaml bundle that `import -u` restores. json: the objects as "
    "the controller returns them. Default: json for a .json FILE, else compose.",
)
def apply_(
    profile: str | None,
    file: str,
    dry_run: bool,
    yes: bool,
    verbose: bool,
    parallel: int,
    backup: str | None,
    backup_format: str | None,
) -> None:
    """Run a change file: update, rename and delete steps, in order. Creates only users.

    Every step is planned first, reading live; nothing is written until the
    whole plan holds and you say yes. The first failed write stops the rest.
    With --backup, the objects the plan changes are saved first; if the backup
    cannot be written, nothing is changed.
    """
    from uac_compose import apply as apply_module
    from uac_compose.learn import Workspace, current_extensions
    from uac_compose.transport import TransportError, UacCli

    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    transport = UacCli(name)

    def say(line: str) -> None:
        click.echo(line, err=True)

    if backup and Path(backup).exists() and not dry_run:
        raise click.ClickException(f"--backup {backup} exists; give a new file name")
    try:
        changes = apply_module.read(Path(file).read_text())
        say(f"profile {name}: {file}, {len(changes.steps)} step(s)")
        say("   reading the controller's extensions...")
        ext = current_extensions(ws, transport)
        say("planning: every step in order, reading live, writing nothing")
        planner = apply_module.Planner(
            transport, ws.template_set(), ext, parallel=parallel, echo=say
        )
        plan = planner.plan(changes)
    except (apply_module.ApplyError, TransportError) as error:
        raise click.ClickException(f"{file}:\n{error}") from error
    shown: dict[int, int] = {}
    for change in plan:
        number = change.step.number
        shown[number] = shown.get(number, 0) + 1
        if verbose or shown[number] <= ASK_ABOVE:
            for line in apply_module.describe(change):
                click.echo(line)
        elif shown[number] == ASK_ABOVE + 1:
            more = sum(1 for c in plan if c.step.number == number) - ASK_ABOVE
            click.echo(f"[{number}] ... and {more} more (-v shows them all)")
    todo = [c for c in plan if c.action != "no change"]
    if not todo:
        click.echo("nothing to change")
        return
    summary = f"{len(todo)} change(s) to {apply_module.object_count(plan)} object(s)"
    if dry_run:
        if backup:
            click.echo(f"dry run: no backup written ({backup} is written before real writes)")
        click.echo(f"dry run: {summary}; nothing was written")
        return
    if not yes:
        if not _interactive():
            raise click.ClickException(f"{summary}; add -y to run them, or -n to only look")
        if _ask(f"Run {summary}? [y/N]").strip().lower() not in ("y", "yes"):
            raise click.ClickException("not run; nothing was written")
    if backup:
        _apply_backup(
            apply_module.saved(plan, planner),
            backup,
            apply_module.backup_format(backup, backup_format),
            ws,
            transport,
            ext,
            name,
            file,
            parallel,
            say,
        )
    try:
        apply_module.run(plan, transport, say)
    except apply_module.ApplyError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"done: {summary}")


def _apply_backup(
    items: list[Any],
    path: str,
    fmt: str,
    ws: Any,
    transport: Any,
    ext: Any,
    profile_label: str,
    source: str,
    parallel: int,
    say: Any,
) -> None:
    """Write the backup, or stop: without it, nothing is changed."""
    from uac_compose import apply as apply_module
    from uac_compose.emit import LookupError_
    from uac_compose.transport import TransportError

    say(f"backup: saving {len(items)} object(s) to {path} ({fmt})")
    try:
        if fmt == "json":
            text = apply_module.backup_json(items, profile_label, source)
        else:
            text, warnings = apply_module.backup_compose(
                items, ws, transport, ws.template_set(), ext, parallel=parallel, echo=say
            )
            for warning in warnings:
                say(f"?? {warning}")
        Path(path).write_text(text)
    except (LookupError_, TransportError, OSError) as error:
        raise click.ClickException(f"backup failed, nothing was changed: {error}") from error
    undo = (
        f"uac-compose import {path} -u -p {profile_label}"
        if fmt == "compose"
        else "the objects are in the file, as the controller returned them"
    )
    click.echo(f"wrote backup {path}; to undo: {undo}")


@cli.command(name="find")
@_profile
@click.argument("filters", nargs=-1)
@click.option(
    "-t",
    "--type",
    "kind_word",
    required=True,
    help="The kind to look in (task, trigger, variable...) or a type (linux, timer).",
)
@click.option(
    "-W",
    "--where",
    "wheres",
    multiple=True,
    metavar="TEST",
    help="A test to start with, as export -W takes it; repeat for more.",
)
def find_cmd(
    profile: str | None, filters: tuple[str, ...], kind_word: str, wheres: tuple[str, ...]
) -> None:
    """Build and test a filter for a change file, one line at a time.

    Start with a server filter (FILTER=VALUE, e.g. workflowname=Nightly WF):
    one list-advanced call. Then type tests (agent=AGT*, command~sleep) and
    see what matches at once; `fields` and `values FIELD` help to write them,
    `step delete` prints the change-file step, `help` says the rest.
    """
    from uac_compose import find
    from uac_compose.emit import LookupError_
    from uac_compose.transport import TransportError, UacCli

    name = profile_name(profile)

    def say(line: str) -> None:
        click.echo(line, err=True)

    try:
        session = find.Session.open(kind_word, UacCli(name), say)
    except (find.FindError, LookupError_) as error:
        raise click.ClickException(str(error)) from error
    try:
        lines = session.opening() if not filters else session.handle("from " + ",".join(filters))
    except TransportError as error:
        raise click.ClickException(str(error)) from error
    for test in wheres:
        lines += session.handle(test)
    _echo_lines(lines)
    if _interactive():
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401  (history and line editing for the prompt)
        click.echo("help lists what you can type; quit ends", err=True)
    while not session.done:
        prompt = "start> " if session.needs_source else "find> "
        try:
            line = click.prompt(prompt, default="", show_default=False, prompt_suffix="", err=True)
        except (EOFError, click.Abort):
            break
        _echo_lines(session.handle(line))


def _echo_lines(lines: list[str]) -> None:
    for line in lines:
        click.echo(line)


@cli.command(name="show")
@_profile
@click.argument("template")
def show_cmd(profile: str | None, template: str) -> None:
    """Show the parameters of a Universal Extension on this controller."""
    from uac_compose.learn import Workspace
    from uac_compose.show import describe, lines
    from uac_compose.transport import TransportError, UacCli

    ws = Workspace(profile_dir(profile))
    transport = UacCli(profile_name(profile))
    try:
        described = describe(template, ws.template_set().extensions, transport)
    except TransportError as error:
        raise click.ClickException(str(error)) from error
    for line in lines(described):
        click.echo(line)
    if not described.found:
        raise SystemExit(1)


def main() -> None:
    try:
        cli(standalone_mode=True)
    except ConfigError as exc:
        click.echo(f"uac-compose: {exc}", err=True)
        sys.exit(2)
