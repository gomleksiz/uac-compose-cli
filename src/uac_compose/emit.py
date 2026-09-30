"""UAC objects -> file: find the named objects, then write them in a style (SPEC §7).

`--with-members` brings what the objects depend on: workflow members
(recursively), the triggers that launch any collected task, the monitor tasks
those triggers watch, calendars and their custom days, scripts, event
templates, credentials, connections and virtual resources -- also those a
Universal task's credential, script and SAP connection parameters name -- and
business services.

Business services (SPEC §6.8): every service the export defines or an object
names is written once, in the `business_services:` list (`- Name` or
`- Name: description`), and each object states its own.
"""

from __future__ import annotations

import copy
import fnmatch
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import yaml

from uac_compose import references, registry, typealias, uacbundle, yamlio
from uac_compose.core.normalize import normalize
from uac_compose.core.templates import TemplateSet
from uac_compose.ingest.archive import envelope_for
from uac_compose.learn import Workspace
from uac_compose.paths import data_root
from uac_compose.style import compact, uac
from uac_compose.transport import (
    KIND_BY_KEY,
    KINDS,
    Kind,
    Transport,
    TransportError,
    in_parallel,
    kind_for_type,
    object_name,
)

# `--type` accepts the section name or its singular.
TYPE_WORDS: dict[str, str] = {
    **{k.key: k.key for k in KINDS},
    **{k.key[:-1]: k.key for k in KINDS},
    "event-template": "event_templates",
    "custom-day": "custom_days",
    "virtual-resource": "virtual_resources",
    "business-service": "business_services",
}


class LookupError_(Exception):
    """A name that cannot be exported (absent, or ambiguous without --type)."""


Echo = Callable[[str], None]


def _silent(_: str) -> None:
    """The default `echo`: a caller that wants only the file hears nothing."""


#: Kinds whose objects carry a type a `-t` word can pick (`-t linux`, `-t time`).
TYPED = ("tasks", "triggers", "agent_clusters")


def type_filter(word: str | None) -> dict[str, frozenset[str] | None]:
    """kind key -> the UAC types `-t` allows in it (None: all of them).

    `-t` is a comma list. Each word is a kind (`task`, `credential`, `script`)
    or a type of task, trigger or agent cluster, as the compact style or the
    UI writes it (`linux`, `Windows`, `timer`, `workflow`, `Time`).
    """
    if word is None:
        return {k.key: None for k in KINDS}
    out: dict[str, frozenset[str] | None] = {}
    for part in (w.strip() for w in word.split(",")):
        if not part:
            continue
        key = TYPE_WORDS.get(part.replace("-", "_")) or TYPE_WORDS.get(part)
        if key is not None:
            out[key] = None
            continue
        typed = [(s, t) for s in TYPED if (t := typealias.to_uac(s, part)) is not None]
        if not typed:
            words = ", ".join(sorted(TYPE_WORDS))
            raise LookupError_(
                f"unknown --type {part!r}; use a kind ({words}) or a task or trigger type"
            )
        for section, uac_type in typed:
            if section in out and out[section] is None:
                continue
            out[section] = (out.get(section) or frozenset()) | {uac_type}
    return out


def kinds_for(word: str | None) -> list[Kind]:
    return [KIND_BY_KEY[key] for key in type_filter(word)]


# --- names with wildcards or a regular expression -----------------------------------


def is_pattern(word: str, regex: bool) -> bool:
    """A regex under `-E`; otherwise a word holding `*`, `?` or `[` is a wildcard.
    Any other word is an exact name, looked up as before."""
    return regex or any(c in word for c in "*?[")


def matcher(word: str, regex: bool) -> Callable[[str], bool]:
    """Case is ignored: `(?-i:...)` in a regex asks for it."""
    try:
        if regex:
            found = re.compile(word, re.IGNORECASE)
            return lambda name: found.search(name) is not None
        whole = re.compile(fnmatch.translate(word), re.IGNORECASE)
        return lambda name: whole.match(name) is not None
    except re.error as error:
        raise LookupError_(f"{word!r} is not a valid regular expression: {error}") from error


@dataclass(frozen=True)
class Listed:
    """One object the controller lists: enough to match it and read it."""

    kind: Kind
    name: str
    type: str | None
    """The UAC type (`taskUnix`), for the kinds `-t` can narrow by type."""


def list_names(
    allowed: dict[str, frozenset[str] | None],
    transport: Transport,
    *,
    parallel: int = 1,
    echo: Echo = _silent,
    warnings: list[str] | None = None,
) -> list[Listed]:
    """Every object of the allowed kinds and types, from a live `uac <group> list`.

    Plain `list`, never `list-advanced`: it answers with name and type only
    for tasks -- 4,984 of them in 1.0 s on a real controller. Live, not from
    the cache: a pattern must see what was made since the last `learn`.
    A kind that cannot be listed is reported and skipped, so one kind the
    user may not read does not stop `'*'`.
    """
    kinds = [KIND_BY_KEY[key] for key in allowed]
    echo(f"   listing the names of {len(kinds)} kind(s), up to {max(1, parallel)} at a time")

    def one(kind: Kind) -> list[dict[str, Any]] | TransportError:
        try:
            return transport.list_raw(kind.group)
        except TransportError as error:
            return error

    answers = in_parallel(kinds, one, parallel, lambda *_: None)
    out: list[Listed] = []
    for kind, rows in zip(kinds, answers, strict=True):
        if isinstance(rows, TransportError):
            if warnings is not None:
                warnings.append(f"{kind.key} not searched: {rows}")
            continue
        types = allowed[kind.key]
        for row in rows:
            name, shown = object_name(row), row.get("type")
            uac_type = None
            if kind.key in TYPED and isinstance(shown, str):
                uac_type = typealias.to_uac(kind.key, shown) or shown
            if name and (types is None or uac_type in types):
                out.append(Listed(kind, name, uac_type))
    return out


#: Above this many objects of one kind, one `list-advanced` beats a `get` each.
#: Measured: 4,973 tasks in 41.1 s by `list-advanced`; a `get` is 0.5 s, 4 at a time.
BULK_ABOVE = 200


def read_objects(
    kind: Kind,
    names: list[str],
    transport: Transport,
    *,
    parallel: int = 1,
    echo: Echo = _silent,
) -> dict[str, dict[str, Any] | None]:
    """name -> the object as the controller has it now (None: not there)."""
    if len(names) > BULK_ABOVE:
        echo(f"   reading every {kind.key[:-1]} at once ({len(names)} to test)...")
        every = {object_name(o): o for o in transport.list_kind(kind)}
        return {n: every.get(n) for n in names}
    count = 0

    def done(i: int, _: Any, seconds: float) -> None:
        nonlocal count
        count += 1
        echo(f"   [{count}/{len(names)}] {kind.key}/{names[i]}  {seconds:.1f}s")

    found = in_parallel(names, lambda n: transport.get_in(kind, n), parallel, done)
    return dict(zip(names, found, strict=True))


def filtered(
    listed: list[Listed],
    tests: list[Any],
    transport: Transport,
    *,
    parallel: int = 1,
    echo: Echo = _silent,
) -> list[Listed]:
    """The listed objects every `where` test holds on, read live to test them."""
    if not tests:
        return listed
    from uac_compose import where

    keep: set[tuple[str, str]] = set()
    for key in dict.fromkeys(item.kind.key for item in listed):
        names = [i.name for i in listed if i.kind.key == key]
        objects = read_objects(KIND_BY_KEY[key], names, transport, parallel=parallel, echo=echo)
        keep |= {
            (key, n) for n, o in objects.items() if o is not None and where.holds(tests, o, key)
        }
    out = [i for i in listed if (i.kind.key, i.name) in keep]
    echo(f"   {len(out)} of {len(listed)} pass the where filter")
    return out


def select(words: list[str], regex: bool, listed: list[Listed]) -> list[Listed]:
    """What the patterns match, in list order, each object once."""
    tests = [matcher(w, regex) for w in words]
    return [item for item in listed if any(test(item.name) for test in tests)]


def find(
    name: str,
    kinds: list[Kind],
    ws: Workspace,
    transport: Transport,
    stale: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """(object, registry type), read from the controller.

    The cache says which **kind** the name is, which is free and saves probing
    every kind in turn. It never says what the object *holds*: the content
    always comes from the controller.

    Measured on a real export: an Approval task had been changed on the
    controller after the last `learn`, and the cache still held the version it
    was copied from. The file was written from the cache and looked perfectly
    normal. A bundle file is a promise about a controller, so it is read from
    that controller.
    """
    cached = [(kind, ws.by_name(kind.key).get(name)) for kind in kinds]
    hits = [(kind, obj) for kind, obj in cached if obj is not None]
    if not hits:
        for kind in kinds:
            obj = transport.get_in(kind, name)
            if obj is not None:
                hits.append((kind, obj))
    found_in = sorted({kind.key for kind, _ in hits})
    if not hits:
        raise LookupError_(f"no object named {name!r}")
    if len(found_in) > 1:
        raise LookupError_(f"{name!r} is a name in {', '.join(found_in)}; pass --type to choose")
    kind, obj = hits[0]
    live = transport.get_in(kind, name)
    if live is not None:
        obj = live
    else:
        obj = copy.deepcopy(obj)  # the cached copy is shared (Workspace.by_name)
        if stale is not None:
            stale.append(f"{kind.key}/{name}: not on the controller; wrote the cached copy")
    return obj, envelope_for(obj, f"{kind.key}").type


#: What reading one name came to: (object, registry type), or why there is none.
Found = tuple[dict[str, Any], str] | LookupError_


@dataclass
class Reader:
    """Reads objects from the controller, `parallel` at a time, a line as each arrives.

    `find` reads every object live, one `uac ... get` each: a new process and
    a round trip, 0.5 s on a real controller. One after another, an export of
    a few hundred objects ran for minutes with nothing on the screen, which
    looked like a hang. The calls do not depend on each other, so they overlap,
    as `freeze --parallel` does. Measured on a real controller, a workflow
    and its members, 166 objects: 76.5 s one by one before, 18.1 s four at a
    time, 11.0 s eight at a time -- the same file, byte for byte, each time.
    Plain lines, never a redrawn bar: in a pipe or a log that reads as a hang.

    The lines come in the order the answers do. What `read` returns, and the
    warnings it adds, come in the order asked: the file never depends on which
    call answered first.
    """

    ws: Workspace
    transport: Transport
    echo: Echo = _silent
    parallel: int = 1
    total: int = 0
    """What has been asked for so far: under `--with-members` it grows."""
    done: int = 0

    def read(self, wanted: list[tuple[str, list[Kind]]], warnings: list[str]) -> list[Found]:
        # Parse the cache files here, once. Left to the threads, each would
        # parse the tasks file itself: 62 MB in memory apiece on a large controller.
        for key in dict.fromkeys(kind.key for _, kinds in wanted for kind in kinds):
            self.ws.by_name(key)
        self.total += len(wanted)
        stale: list[list[str]] = [[] for _ in wanted]

        def one(i: int) -> Found:
            name, kinds = wanted[i]
            try:
                return find(name, kinds, self.ws, self.transport, stale[i])
            except LookupError_ as error:
                return error

        def done(i: int, found: Found, seconds: float) -> None:
            self.done += 1
            what = (
                str(found)
                if isinstance(found, LookupError_)
                else f"{found[1]} {wanted[i][0]!r}  {seconds:.1f}s"
            )
            self.echo(f"[{self.done}/{self.total}] {what}")

        results = in_parallel(range(len(wanted)), one, self.parallel, done)
        for lines in stale:
            warnings.extend(lines)
        return results


@dataclass
class Collected:
    objects: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    create_only: list[str] = field(default_factory=list)
    """Sections the source says to create but never overwrite."""


def collect(
    names: list[str],
    kinds: list[Kind],
    ws: Workspace,
    transport: Transport,
    *,
    with_members: bool = False,
    echo: Echo = _silent,
    parallel: int = 1,
    targets: list[tuple[str, list[Kind]]] | None = None,
) -> Collected:
    """The named objects and, with `with_members`, what they need, breadth first.

    `targets` names each object with its own kinds, in place of `names` and
    `kinds`: a pattern can match one name in two kinds, and wants both.

    A generation at a time: every member the last generation names is known
    before the first of them is read, so they are read together (`Reader`),
    and they come back in the order that reading them one by one gave.
    """
    out = Collected()
    reader = Reader(ws, transport, echo, parallel)
    generation: list[tuple[dict[str, Any], str]] = []
    roots = targets if targets is not None else [(n, kinds) for n in names]
    for got in reader.read(roots, out.warnings):
        if isinstance(got, LookupError_):
            raise got
        generation.append(got)
    seen = {(kind_for_type(t).key, object_name(obj)) for obj, t in generation}
    triggers: list[dict[str, Any]] | None = None

    def members(obj: dict[str, Any], type_name: str) -> list[tuple[str, str]]:
        nonlocal triggers
        flat = normalize(obj)
        found = [("tasks", task) for task in references.member_tasks(flat)]
        found += [(kind_key, name) for _, kind_key, name in references.named(type_name, flat)]
        entry = registry.load().get(type_name)
        if entry is not None and entry.kind in ("task", "workflow"):
            if triggers is None:
                triggers = ws.read_cache("triggers")
                if not triggers:
                    echo("   no triggers in the cache; listing them from the controller...")
                    triggers = transport.list_kind(KIND_BY_KEY["triggers"])
            me = object_name(obj)
            for trigger in triggers:
                if me in (trigger.get("tasks") or []):
                    found.append(("triggers", object_name(trigger)))
        return found

    while generation:
        out.objects.extend(generation)
        if not with_members:
            break
        wanted: list[tuple[str, str, str]] = []
        for obj, type_name in generation:
            for kind_key, name in members(obj, type_name):
                if kind_key in KIND_BY_KEY and (kind_key, name) not in seen:
                    seen.add((kind_key, name))
                    wanted.append((kind_key, name, f"{type_name} {object_name(obj)!r}"))
        answers = reader.read([(name, [KIND_BY_KEY[k]]) for k, name, _ in wanted], out.warnings)
        generation = []
        for (kind_key, name, why), got in zip(wanted, answers, strict=True):
            if isinstance(got, LookupError_):
                out.warnings.append(f"{why} names {kind_key[:-1]} {name!r}, which was not found")
            else:
                generation.append(got)
    return out


def from_uac_bundle(
    bundle_name: str,
    ws: Workspace,
    transport: Transport,
    echo: Echo = _silent,
    parallel: int = 1,
) -> Collected:
    """Every object a UAC Bundle resolves to, as the controller resolves it.

    `bundle get-report` applies the Bundle's own `followReferences`: that is
    the controller's rule about what belongs, and reimplementing it here would
    mean getting it subtly different. A member the report names but this format
    has no section for -- or that no longer exists -- is reported and skipped,
    because failing a 46-object export over one of them would refuse the 45
    that are fine.
    """
    report = transport.bundle_report(bundle_name)
    if report is None:
        raise LookupError_(f"no UAC Bundle named {bundle_name!r}")
    out = Collected(create_only=uacbundle.create_only(report))
    for missing in uacbundle.unsupported(report):
        out.warnings.append(f"not exported ({missing}): this format has no section for it")
    named = uacbundle.members(report)
    if not named:
        raise LookupError_(f"UAC Bundle {bundle_name!r} resolves to nothing this format can hold")
    echo(f"   it holds {len(named)} object(s); reading them {max(1, parallel)} at a time")
    reader = Reader(ws, transport, echo, parallel)
    answers = reader.read([(name, [KIND_BY_KEY[section]]) for section, name in named], out.warnings)
    for (section, name), got in zip(named, answers, strict=True):
        if isinstance(got, LookupError_):
            # A Bundle spans more of UAC than a `.uac.yaml` file holds, and not only
            # by kind: `bundleUniversalEventTemplates` lists a Universal
            # Template's own events under a qualified name (`SQL ODBC:
            # Duration`), and those are created with the extension.
            out.warnings.append(f"not exported: no {section[:-1]} named {name!r}")
        else:
            out.objects.append(got)
    return out


def _member(type_name: str) -> bool:
    return type_name in registry.load() and registry.get(type_name).declares("opswiseGroups")


def service_items(defs: list[dict[str, Any]]) -> list[Any] | None:
    """`- Name` / `- Name: description` for definitions that hold nothing else."""
    items: list[Any] = []
    for obj in defs:
        flat = {k: v for k, v in normalize(obj).items() if k != "type"}
        name, description = object_name(obj), flat.pop("description", None)
        flat.pop("busservicename", None)
        flat.pop("name", None)
        if flat or not name:
            return None
        items.append({name: description} if description else name)
    return items


def list_services(
    objects: list[tuple[dict[str, Any], str]], as_list: bool = True
) -> tuple[list[Any], list[tuple[dict[str, Any], str]]]:
    """(the `business_services:` list, the objects left to write).

    The list names every service the export defines or an object names, so
    import can create the missing ones; each object states its own. When a
    definition holds more than a description the section is a mapping, and
    the services come back as objects instead.
    """
    defs = {object_name(obj): obj for obj, t in objects if t == "businessService"}
    for obj, t in objects:
        for name in (normalize(obj).get("opswiseGroups") or []) if _member(t) else []:
            defs.setdefault(name, {"type": "businessService", "name": name})
    rest = [(obj, t) for obj, t in objects if t != "businessService"]
    items = service_items(list(defs.values())) if as_list else None
    if items is None:
        return [], rest + [(obj, "businessService") for obj in defs.values()]
    return items, rest


@cache
def default_create_only() -> tuple[str, ...]:
    """The sections every export marks create-only (data/rules/create_only.yaml)."""
    raw = yaml.safe_load((data_root() / "rules" / "create_only.yaml").read_text())
    return tuple(raw["sections"])


# --- --variablize-agents -----------------------------------------------------------


def agent_paths(type_name: str) -> list[tuple[str, str]]:
    """The (agent field, its *Var twin) pairs of a type, top level and in rows."""
    if type_name not in registry.load():
        return []
    return [(a, b) for a, b in registry.var_pairs(type_name) if a.split(".")[-1] == "agent"]


def variable_name(agent: str, taken: set[str]) -> str:
    """`AGT_LINUX-01` -> `agent_agt_linux_01`; a suffix when the name is taken."""
    base = "agent_" + (re.sub(r"[^0-9a-z]+", "_", agent.lower()).strip("_") or "x")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _rows(node: Any, path: list[str]) -> list[dict[str, Any]]:
    """The dicts that hold the last field of `path`, through dicts and lists."""
    if isinstance(node, list):
        return [r for item in node for r in _rows(item, path)]
    if not isinstance(node, dict):
        return []
    if len(path) == 1:
        return [node]
    return _rows(node.get(path[0]), path[1:])


def variablize_agents(
    objects: list[tuple[dict[str, Any], str]],
) -> list[tuple[dict[str, Any], str]]:
    """Every agent an object names becomes `${agent_...}`, and a variable holding it.

    Only fields with a `*Var` twin: `agent`, and the `agent` of action and
    step rows. A variable already in the export keeps its name; one named like
    a new variable but holding something else makes the new one take a suffix.
    """
    held = {object_name(o): o.get("value") for o, t in objects if t == "variable"}
    by_agent: dict[str, str] = {}
    out: list[tuple[dict[str, Any], str]] = []
    for obj, type_name in objects:
        paths = agent_paths(type_name)
        if paths:
            obj = copy.deepcopy(obj)
        for plain, twin in paths:
            field, var_field = plain.split(".")[-1], twin.split(".")[-1]
            for row in _rows(obj, plain.split(".")):
                agent = row.get(field)
                if not isinstance(agent, str) or not agent or "${" in agent:
                    continue
                if agent not in by_agent:
                    same = [n for n, v in held.items() if v == agent and n.startswith("agent_")]
                    by_agent[agent] = same[0] if same else variable_name(agent, set(held))
                    held[by_agent[agent]] = agent
                row.pop(field)
                row[var_field] = f"${{{by_agent[agent]}}}"
        out.append((obj, type_name))
    exported = {object_name(o) for o, t in objects if t == "variable"}
    for agent, name in by_agent.items():
        if name not in exported:
            out.append(({"type": "variable", "name": name, "value": agent}, "variable"))
    return out


def with_was(
    document: dict[str, Any], was: dict[tuple[str, str], str] | None = None
) -> dict[str, Any]:
    """`was: <name>` first on every object, for `--purpose update`.

    The file is for changing these objects where they are. To rename one,
    change its key and leave `was:`: import renames it in place (same
    sysId), so what links to it on the controller follows. Services come as a
    mapping here, since a `- Name` list item has nowhere to say it.
    `was` gives another name for some: (section, name) -> the `was:` name
    (an `apply --backup` names each object as it will be after the change).
    """
    was = was or {}
    out: dict[str, Any] = {}
    for section, body in document.items():
        if section in yamlio.SECTIONS and isinstance(body, dict):
            out[section] = {
                name: {yamlio.WAS: was.get((section, name), name), **(definition or {})}
                for name, definition in body.items()
            }
        else:
            out[section] = body
    return out


def export_text(
    names: list[str],
    kind_word: str | None,
    ws: Workspace,
    transport: Transport,
    templates: TemplateSet,
    *,
    style: str = "compact",
    show_inactive: bool = False,
    with_members: bool = False,
    uac_bundle: str | None = None,
    variablize: bool = False,
    suggest_templates: bool = False,
    report: list[str] | None = None,
    echo: Echo = _silent,
    parallel: int = 1,
    extensions: list[str] | None = None,
    targets: list[tuple[str, list[Kind]]] | None = None,
    purpose: str = "promote",
    was: dict[tuple[str, str], str] | None = None,
) -> tuple[str, list[str]]:
    """(file text, warnings). `report` collects what `suggest_templates` wrote;
    `echo` hears each object as it is read, `parallel` at a time (see `Reader`);
    `extensions` collects the Universal Extensions the exported tasks name;
    `targets` are (name, kinds) pairs used in place of `names` (see `collect`).

    `purpose="update"` writes a file for changing these objects where they are
    (see `for_update`); "promote", the default, one for another controller;
    `was` changes the `was:` name of some objects (see `with_was`)."""
    started = time.monotonic()
    if uac_bundle:
        found = from_uac_bundle(uac_bundle, ws, transport, echo, parallel)
    else:
        found = collect(
            names,
            kinds_for(kind_word),
            ws,
            transport,
            with_members=with_members,
            echo=echo,
            parallel=parallel,
            targets=targets,
        )
    echo(f"read {len(found.objects)} object(s) in {time.monotonic() - started:.1f}s")
    if extensions is not None:
        from uac_compose.freeze import template_names

        extensions += template_names([obj for obj, _ in found.objects])
    collected = variablize_agents(found.objects) if variablize else found.objects
    for_update = purpose == "update"
    services, objects = list_services(collected, as_list=not for_update)
    style = yamlio.style_named(style)
    emitter = compact.emit_document if style == "compact" else uac.emit_document
    document: dict[str, Any] = {}
    if services:
        document["business_services"] = services
    document.update(emitter(objects, templates, show_inactive=show_inactive))
    if suggest_templates:
        from uac_compose import suggest

        document, suggested = suggest.suggest(document, templates)
        if report is not None:
            report += suggest.report(suggested)
    if for_update:
        document = with_was(document, was)
    text = yamlio.dump(document)
    header = f"{yamlio.VERSION}: {yamlio.FORMAT_VERSION}\n{yamlio.STYLE}: {style}\n"
    create_only = [s for s in yamlio.SECTIONS if s in {*default_create_only(), *found.create_only}]
    if for_update:
        create_only = []  # an update must reach every object, credentials included
    if create_only:
        # At the top, where a reader meets it before the objects it governs,
        # and on one line, as the section list UAC's own `excludeOnExistence`
        # is. Written whole rather than narrowed to the sections this export
        # happens to fill: it is the Bundle's standing rule about those kinds,
        # and an object added to the file later is covered by it too.
        header += f"{yamlio.CREATE_ONLY}: [{', '.join(create_only)}]\n"
    return header + "\n" + text, found.warnings
