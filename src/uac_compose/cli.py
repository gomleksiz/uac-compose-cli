"""The commands: learn, export, import, freeze, show, apply, find, publish."""

from __future__ import annotations

import contextlib
import sys
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
  UAC_COMPOSE_DATA       the shipped data folder, when the package cannot
                         read its own (imported from a zip)
  UAC_COMPOSE_HUB_URL    the Composer Hub for hub: references and publish
                         (default: https://composer.husey.in)
  UAC_COMPOSE_HUB_TOKEN  the hub API token for publish; it wins over the
                         file ~/.config/uac-compose/hub-token
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
    from uac_compose import service
    from uac_compose.learn import Workspace
    from uac_compose.transport import UacCli

    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))

    # Progress goes to stderr: without --out the file itself is on stdout.
    def say(line: str) -> None:
        click.echo(line, err=True)

    try:
        result = service.export_bundle(
            UacCli(name),
            ws,
            names,
            kind=kind,
            uac_bundle=uac_bundle,
            style=style,
            with_members=with_members,
            show_inactive=show_inactive,
            variablize_agents=variablize_agents,
            suggest_templates=suggest_templates,
            parallel=parallel,
            regex=regex,
            list_only=list_only,
            yes=yes,
            purpose=purpose,
            wheres=wheres,
            profile_label=name,
            say=say,
            choose=_choose,
        )
    except service.ServiceError as error:
        raise click.ClickException(str(error)) from error
    if list_only:
        for line in result.listed:
            click.echo(line)
        return
    for warning in result.warnings:
        click.echo(f"?? {warning}", err=True)
    for line in result.report:
        click.echo(line, err=True)
    if out:
        Path(out).write_text(result.text)
        click.echo(f"wrote {out}")
    else:
        click.echo(result.text, nl=False)
    if export_extensions:
        _freeze_after_export(result.extensions, ws, UacCli(name), name, out, say, parallel)


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
        from uac_compose.service import too_many

        raise click.ClickException(too_many(len(found), patterns))
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
@click.argument("file")
@click.option("-n", "--dry-run", is_flag=True, help="Decide and compare; write nothing.")
@click.option(
    "--sha256",
    default=None,
    metavar="HEX",
    help="Refuse the file unless its SHA-256 is this (pins a hub: or URL download).",
)
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
    sha256: str | None,
    update: bool,
    extensions: str | None,
    update_extensions: bool,
    parallel: int,
) -> None:
    """Apply a bundle idempotently (create / reuse / update / conflict).

    FILE is a .uac.yaml file, an https:// URL, or a Composer Hub reference:
    hub:NAME, hub:NAME==1.0, 'hub:NAME>=1.0,<2' (quote it in a shell).
    """
    from uac_compose import service
    from uac_compose.learn import Workspace
    from uac_compose.transport import UacCli

    name = profile_name(profile)
    text = _bundle_text(file, sha256)

    # Progress goes to stderr, as export's does; the decisions stay on stdout.
    def say(line: str) -> None:
        click.echo(line, err=True)

    try:
        service.import_bundle(
            UacCli(name),
            Workspace(profile_dir(profile)),
            text,
            source=file,
            dry_run=dry_run,
            update=update,
            extensions=extensions,
            update_extensions=update_extensions,
            parallel=parallel,
            profile_label=name,
            say=say,
            out=click.echo,
        )
    except service.ServiceError as error:
        raise click.ClickException(str(error)) from error


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
    from uac_compose.learn import Workspace, current_extensions
    from uac_compose.transport import TEMPLATE_GROUP, TransportError, UacCli
    from uac_compose.yamlio import BundleError, read_bundle

    name = profile_name(profile)
    ws = Workspace(profile_dir(profile))
    transport = UacCli(name)
    try:
        if file:
            bundle = read_bundle(Path(file).read_text())
            ext = current_extensions(ws, transport)
            planned = compile_bundle(bundle, ws.template_set(ext))
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
    from uac_compose import service
    from uac_compose.learn import Workspace
    from uac_compose.transport import UacCli

    name = profile_name(profile)

    def say(line: str) -> None:
        click.echo(line, err=True)

    def confirm(summary: str) -> bool:
        return _ask(f"Run {summary}? [y/N]").strip().lower() in ("y", "yes")

    try:
        service.apply_changes(
            UacCli(name),
            Workspace(profile_dir(profile)),
            Path(file).read_text(),
            source=file,
            dry_run=dry_run,
            yes=yes,
            verbose=verbose,
            parallel=parallel,
            backup=backup,
            backup_format=backup_format,
            profile_label=name,
            say=say,
            out=click.echo,
            confirm=confirm if _interactive() else None,
        )
    except service.ServiceError as error:
        raise click.ClickException(str(error)) from error


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


def _bundle_text(file: str, sha256: str | None) -> str:
    """A local file, a URL or a hub: reference, its checksum checked."""
    from uac_compose import hub

    try:
        if hub.is_reference(file):
            fetched = hub.fetch(file, sha256)
            what = f"{fetched.name}=={fetched.version}" if fetched.name else fetched.url
            click.echo(f"{what}: sha256 {fetched.sha256} (checked)", err=True)
            return fetched.text
        path = Path(file)
        if not path.is_file():
            raise click.ClickException(f"{file}: no such file (a hub bundle is hub:NAME==VERSION)")
        data = path.read_bytes()
        hub.check(data, hub.check_pin(sha256), file)
        return data.decode("utf-8")
    except hub.HubError as error:
        raise click.ClickException(str(error)) from error


@cli.command(name="publish")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.argument("requirement", metavar="NAME==VERSION")
@click.option("-d", "--description", default=None, help="One paragraph about the bundle.")
@click.option(
    "--description-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Read the description from a file.",
)
@click.option("--tag", "tags", multiple=True, help="A tag (up to 5); repeat for more.")
@click.option(
    "--require",
    "requires",
    multiple=True,
    help="A requirement for the package page (e.g. 'A database connection named X');"
    " repeat for more. The hub lists the extensions the file uses by itself.",
)
@click.option(
    "--draft", is_flag=True, help="Upload as a draft: check it on the site, publish there."
)
def publish_cmd(
    file: str,
    requirement: str,
    description: str | None,
    description_file: str | None,
    tags: tuple[str, ...],
    requires: tuple[str, ...],
    draft: bool,
) -> None:
    """Publish a bundle to the Composer Hub as NAME==VERSION.

    A published version never changes, and its number is never used again.
    The token comes from UAC_COMPOSE_HUB_TOKEN; when it is not set, from the
    file ~/.config/uac-compose/hub-token.
    """
    from uac_compose import hub

    if description_file:
        description = Path(description_file).read_text().strip()
    try:
        answer = hub.publish(
            Path(file).read_text(),
            requirement,
            description=description,
            tags=list(tags) or None,
            requirements=list(requires) or None,
            draft=draft,
            source=file,
        )
    except hub.HubError as error:
        raise click.ClickException(str(error)) from error
    state = "draft saved" if answer.get("status") == "draft" else "published"
    click.echo(f"{state}: {answer['name']}=={answer['version']}")
    click.echo(f"sha256: {answer['sha256']}")
    click.echo(f"page:   {answer['page']}")
    if answer.get("url"):
        click.echo(f"url:    {answer['url']}")
        click.echo(f"install: uac-compose import -n hub:{answer['name']}=={answer['version']} "
                   f"--sha256 {answer['sha256']}")  # fmt: skip


def main() -> None:
    try:
        cli(standalone_mode=True)
    except ConfigError as exc:
        click.echo(f"uac-compose: {exc}", err=True)
        sys.exit(2)
