"""Apply a bundle idempotently (SPEC §8).

Every object is decided first -- create / reuse / update / conflict / keep --
and only if nothing conflicts is anything written. Comparison is
one-directional: does the controller agree with everything the file states?
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

import yaml

from uac_compose import registry
from uac_compose.compile import Planned
from uac_compose.core.extensions import ExtensionMap, collapse
from uac_compose.core.inflate import inflate
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.paths import data_root
from uac_compose.transport import Transport, in_parallel, kind_for_type, object_name

Echo = Callable[[str], None]


def _silent(_: str) -> None:
    """The default `echo`: a caller that wants only the result hears nothing."""

Action = Literal["create", "reuse", "update", "rename", "conflict", "keep"]

# Canvas layout, not meaning.
IGNORED = frozenset({"vertexX", "vertexY"})

# Types that declare no opswiseGroups: never fenced, never assigned a service.
NO_SERVICES = frozenset({"variable", "customDay", "universalEventTemplate", "businessService"})


@dataclass
class Decision:
    planned: Planned
    action: Action
    differences: list[str] = field(default_factory=list)
    stored: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


# --- compare -------------------------------------------------------------------


def _diff(want: Any, have: Any, path: str) -> list[str]:
    if isinstance(want, dict):
        if not isinstance(have, dict):
            return [f"{path}: file states an object, controller has {have!r}"]
        out: list[str] = []
        for key, value in want.items():
            if key not in IGNORED:
                out += _diff(value, have.get(key), f"{path}.{key}" if path else key)
        return out
    if isinstance(want, list):
        if not isinstance(have, list):
            return [f"{path}: file states a list, controller has {have!r}"]
        if want and all(isinstance(i, str) for i in want):
            missing = [i for i in want if i not in have]
            return [f"{path}: controller lacks {missing}"] if missing else []
        if want and all(isinstance(i, dict) for i in want):
            return _diff_unordered(want, have, path)
        if want != have:
            return [f"{path}: file says {want!r}, controller has {have!r}"]
        return []
    if want != have:
        return [f"{path}: file says {want!r}, controller has {have!r}"]
    return []


def _diff_unordered(want: list[Any], have: list[Any], path: str) -> list[str]:
    """Each stated row must find a stored row that agrees with it; order is forgiven."""
    remaining = [h for h in have if isinstance(h, dict)]
    unmatched: list[tuple[int, Any]] = []
    for index, item in enumerate(want):
        for candidate in remaining:
            if not _diff(item, candidate, ""):
                remaining.remove(candidate)
                break
        else:
            unmatched.append((index, item))
    out: list[str] = []
    for index, item in unmatched:
        if not remaining:
            out.append(f"{path}[{index}]: no row on the controller matches it")
            continue
        closest = min(remaining, key=lambda c: len(_diff(item, c, "")))
        remaining.remove(closest)
        out += _diff(item, closest, f"{path}[{index}]")
    return out


def compare(
    desired: dict[str, Any], stored: dict[str, Any], ext: ExtensionMap | None = None
) -> list[str]:
    """What the file states that the controller does not agree with.

    The stored object is normalized and collapsed, not simplified, so a file
    restating a default still agrees.
    """
    type_name = str(desired.get("type", ""))
    reserved = (
        frozenset(registry.get(type_name).fields) if type_name in registry.load() else frozenset()
    )
    want, have = normalize(desired), normalize(stored)
    if ext is not None:
        want, have = collapse(want, ext, reserved), collapse(have, ext, reserved)
    return _diff(want, have, "")


# --- fields the target will not store (data/rules/guards.yaml) -------------------------


@dataclass(frozen=True)
class Guard:
    type_name: str
    when: tuple[tuple[str, tuple[Any, ...]], ...]
    dead: tuple[str, ...]


@cache
def guards() -> tuple[Guard, ...]:
    raw = yaml.safe_load((data_root() / "rules" / "guards.yaml").read_text())
    return tuple(
        Guard(
            str(row["type"]),
            tuple((k, tuple(v if isinstance(v, list) else [v])) for k, v in row["when"].items()),
            tuple(row["dead"]),
        )
        for row in (raw.get("guards") or [])
    )


def unstorable(stored: dict[str, Any]) -> set[str]:
    """Fields this target will discard, because a switch on IT is off.

    Judged against the target, never against the file: the switch is the
    target's own setting and an import does not change it.
    """
    have = normalize(stored)
    type_name = str(have.get("type", ""))
    found: set[str] = set()
    for guard in guards():
        if guard.type_name != type_name:
            continue
        if all(have.get(name) in allowed for name, allowed in guard.when):
            found.update(guard.dead)
    return found


def _switch_note(guard_field: str, fields: list[str], stored: dict[str, Any]) -> str:
    named = ", ".join(sorted(fields))
    return (
        f"{named} not stored: {guard_field} is {normalize(stored).get(guard_field)!r} "
        f"on the controller, and that switches {'them' if len(fields) > 1 else 'it'} off"
    )


def split_unstorable(
    differences: list[str], stored: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """(real differences, notes) -- a dead field is reported once, not fought over."""
    dead = unstorable(stored)
    if not dead:
        return differences, []
    kept = [d for d in differences if d.split(":", 1)[0].split(".")[0] not in dead]
    hit = sorted({d.split(":", 1)[0].split(".")[0] for d in differences} & dead)
    if not hit:
        return kept, []
    by_switch: dict[str, list[str]] = {}
    for guard in guards():
        for name in guard.dead:
            if name in hit:
                by_switch.setdefault(guard.when[0][0], []).append(name)
    return kept, [_switch_note(g, f, stored) for g, f in sorted(by_switch.items())]


# --- workflow members the file does not define ------------------------------------------


def missing_members(
    planned: list[Planned], transport: Transport, echo: Echo = _silent, parallel: int = 1
) -> list[str]:
    """Members a workflow names that neither the file nor the controller has.

    An undefined member means an existing task (SPEC §6.6); this checks it exists.
    """
    from uac_compose import references

    defined = {p.name for p in planned if p.section == "tasks"}
    named = [
        (p, task)
        for p in planned
        for task in references.member_tasks(p.obj)
        if task not in defined and "${" not in task
    ]
    asked = list(dict.fromkeys(task for _, task in named))
    if asked:
        echo(f"   {len(asked)} workflow member(s) are not in this file; asking the controller")
    count = 0

    def done(i: int, found: bool, seconds: float) -> None:
        nonlocal count
        count += 1
        state = "" if found else ": not there"
        echo(f"[{count}/{len(asked)}] member {asked[i]!r}{state}  {seconds:.1f}s")

    there = in_parallel(asked, lambda t: transport.get("taskUnix", t) is not None, parallel, done)
    exists = dict(zip(asked, there, strict=True))
    return [
        f"{p.label}: member {task!r} is not in this file or on the controller"
        for p, task in named
        if not exists[task]
    ]


# --- decide ----------------------------------------------------------------------


def in_services(stored: dict[str, Any], services: list[str]) -> bool:
    """May `--update` rewrite it: in one of the file's services, or in none."""
    if not services or str(stored.get("type", "")) in NO_SERVICES:
        return True
    groups = set(stored.get("opswiseGroups") or [])
    return not groups or bool(groups & set(services))


def type_change(planned: Planned, stored: dict[str, Any]) -> str | None:
    """Why an update cannot turn the stored object into the file's, or None.

    An update keeps the object's type. Measured live: a Sleep task named
    `Approval Task` on the target and an Approval task of that name in the
    file. The merged payload carried the Sleep task's `sleepAmount` under an
    Approval type, and the controller answered "Unrecognized property:
    sleepAmount" -- after the tasks before it had already been updated.
    """
    from uac_compose.typealias import alias_for

    have, want = str(stored.get("type", "")), str(planned.obj.get("type", ""))
    if not have or not want or have == want:
        return None
    return (
        f"type: {alias_for(have)} on the controller, {alias_for(want)} in the file. "
        f"An update cannot change a type. Rename one of them, or delete it on the "
        f"controller and import again"
    )


def decide(
    planned: Planned,
    stored: dict[str, Any] | None,
    ext: ExtensionMap | None = None,
    *,
    update: bool = False,
    services: list[str] | None = None,
) -> Decision:
    if stored is None:
        return Decision(planned, "create", notes=_command_notes(planned, None))
    differences, notes = split_unstorable(compare(planned.obj, stored, ext), stored)
    notes += _command_notes(planned, stored)
    if not differences:
        return Decision(planned, "reuse", stored=stored, notes=notes)
    if planned.create_only:  # before --update: a seed's drift is not drift
        return Decision(planned, "keep", differences, stored, notes)
    if (moved := type_change(planned, stored)) is not None:
        # Never written, not even with --update: the controller refuses it,
        # and only after the objects before it in the file were written.
        return Decision(planned, "conflict", [moved], stored, notes)
    if update:
        if in_services(stored, services or []):
            return Decision(planned, "update", differences, stored, notes)
        differences.append(
            f"not updated: it belongs to {sorted(stored.get('opswiseGroups') or []) or 'no'} "
            f"business service(s), not {sorted(services or [])}"
        )
    return Decision(planned, "conflict", differences, stored, notes)



def decide_rename(
    planned: Planned,
    stored: dict[str, Any] | None,
    earlier: dict[str, Any] | None,
    ext: ExtensionMap | None = None,
    *,
    update: bool = False,
    services: list[str] | None = None,
) -> Decision:
    """The file renames an object: `planned.was` is its name on the controller now.

    Renamed in place -- an update that carries the old object's sysId and the
    new name -- so what the controller links by sysId (workflows, triggers,
    monitors not in this file) follows. It changes the object, so it needs
    --update, like any other change.

    Measured live on a test controller: a task renamed this way kept its
    sysId, and the workflow that ran it showed the new name with no change
    of its own.
    """
    old, new = planned.was, planned.name
    if stored is not None and earlier is not None:
        refused = f"both {old!r} and {new!r} exist; rename refused (delete one, or drop was:)"
        return Decision(planned, "conflict", [refused], stored)
    if stored is not None:  # renamed already, by an earlier run or by hand
        decision = decide(planned, stored, ext, update=update, services=services)
        decision.notes.append(f"{old!r} is already called {new!r}")
        return decision
    if earlier is None:
        return Decision(planned, "create", notes=[f"was: {old!r} is not on the controller"])
    decision = decide(planned, earlier, ext, update=update, services=services)
    if decision.action == "update":
        return Decision(planned, "rename", decision.differences, earlier, decision.notes)
    if decision.action == "keep":
        refused = "rename refused: it is create_only (drop create_only to rename it)"
        return Decision(planned, "conflict", [refused, *decision.differences], earlier)
    return decision

# --- write -------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    value: Any
    when: dict[str, list[Any]]


@cache
def oncreate() -> dict[str, Fill]:
    raw = yaml.safe_load((data_root() / "rules" / "oncreate.yaml").read_text())
    return {
        name: Fill(
            row["value"],
            {k: v if isinstance(v, list) else [v] for k, v in (row.get("when") or {}).items()},
        )
        for name, row in (raw.get("fields") or {}).items()
    }


def fill_on_create(obj: dict[str, Any]) -> list[str]:
    """Add what a create needs and the inflated object lacks. Returns the fields added.

    Only on create: never on update, never in comparison.
    """
    type_name = str(obj.get("type", ""))
    if type_name not in registry.load():
        return []
    entry = registry.get(type_name)
    added = []
    for name, fill in oncreate().items():
        if not entry.declares(name) or obj.get(name) not in (None, ""):
            continue
        held = {k: (obj.get(k) if obj.get(k) not in (None, "") else None) for k in fill.when}
        if all(held[k] in allowed for k, allowed in fill.when.items()):
            obj[name] = fill.value
            added.append(name)
    return added


def merge_update(stored: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """Stored (simplified) with the file written over it. Dicts merge, lists replace.

    Stating one half of an `x`/`xVar` pair clears the other: UAC reads the
    filled half, and keeping both made an update report success and change
    nothing.
    """
    out = dict(stored)
    type_name = str(desired.get("type", ""))
    if type_name in registry.load():
        for name, twin in registry.var_pairs(type_name):
            if "." in name:
                continue
            if name in desired and twin not in desired:
                out.pop(twin, None)
            elif twin in desired and name not in desired:
                out.pop(name, None)
    for key, value in desired.items():
        current = out.get(key)
        out[key] = (
            _merge_dict(current, value)
            if isinstance(value, dict) and isinstance(current, dict)
            else value
        )
    return out


def _merge_dict(stored: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    out = dict(stored)
    for key, value in desired.items():
        current = out.get(key)
        out[key] = (
            _merge_dict(current, value)
            if isinstance(value, dict) and isinstance(current, dict)
            else value
        )
    return out




def _reserved(type_name: str) -> frozenset[str]:
    return (
        frozenset(registry.get(type_name).fields) if type_name in registry.load() else frozenset()
    )


def create_payload(planned: Planned, templates: TemplateSet) -> tuple[dict[str, Any], list[str]]:
    payload = inflate(
        planned.obj,
        templates.get(planned.type),
        templates.sub_templates,
        extensions=templates.extensions,
        reserved=_reserved(planned.type),
    )
    return payload, fill_on_create(payload)


def update_payload(decision: Decision, templates: TemplateSet) -> dict[str, Any]:
    assert decision.stored is not None
    type_name = decision.planned.type
    template, subs = templates.get(type_name), templates.sub_templates
    reserved = _reserved(type_name)
    stored = simplify(
        decision.stored, template, subs, extensions=templates.extensions, reserved=reserved
    )
    merged = merge_update(stored, decision.planned.obj)
    payload = inflate(merged, template, subs, extensions=templates.extensions, reserved=reserved)
    if decision.stored.get("sysId"):
        payload["sysId"] = decision.stored["sysId"]
    return payload


def _command_notes(planned: Planned, stored: dict[str, Any] | None) -> list[str]:
    from uac_compose import commands

    group = kind_for_type(str(planned.obj.get("type", ""))).group
    return [c.note(group) for c in commands.needed(planned.obj, stored)]


def run_commands(
    obj: dict[str, Any], stored: dict[str, Any] | None, transport: Transport, echo: Echo
) -> None:
    """After a write: set the fields create and update do not store."""
    from uac_compose import commands

    kind = kind_for_type(str(obj.get("type", "")))
    for c in commands.needed(obj, stored):
        transport.command(kind, object_name(obj), c.verb, c.args)
        echo(f"   {kind.key}/{object_name(obj)}: {c.field_name} {c.value} ({kind.group} {c.verb})")


def write(
    decisions: list[Decision], transport: Transport, templates: TemplateSet, echo: Echo
) -> None:
    """One at a time, in creation order: an object may name one written before it."""
    writes = [d for d in decisions if d.action in ("create", "update", "rename")]
    for n, d in enumerate(writes, 1):
        at = time.monotonic()
        if d.action == "create":
            payload, filled = create_payload(d.planned, templates)
            for name in filled:
                echo(f"   {d.planned.label}: {name} not stated, creating with {payload[name]!r}")
            transport.create(payload)
            verb = "created "
        else:
            transport.update(update_payload(d, templates))
            verb = "renamed " if d.action == "rename" else "updated "
        echo(f"[{n}/{len(writes)}] {verb} {d.planned.label}  {time.monotonic() - at:.1f}s")
        run_commands(d.planned.obj, d.stored, transport, echo)


def decide_all(
    planned: list[Planned],
    transport: Transport,
    ext: ExtensionMap | None,
    *,
    update: bool,
    services: list[str],
    echo: Echo = _silent,
    parallel: int = 1,
) -> list[Decision]:
    """Read live before deciding: a cache can be stale, a write must not be.

    One `get` per object, `parallel` at a time, a line as each answers
    (`transport.in_parallel`); the decisions keep the file's order.
    """
    total = len(planned)
    count = 0

    def done(i: int, _: dict[str, Any] | None, seconds: float) -> None:
        nonlocal count
        count += 1
        echo(f"[{count}/{total}] {planned[i].label}  {seconds:.1f}s")

    stored = in_parallel(planned, lambda p: transport.get(p.type, p.name), parallel, done)
    renamed = [p for p in planned if p.renamed]
    earlier: dict[int, dict[str, Any] | None] = {}
    if renamed:
        echo(f"   {len(renamed)} renamed object(s): reading them under their old names")
        total, count = len(renamed), 0

        def got(i: int, _: dict[str, Any] | None, seconds: float) -> None:
            nonlocal count
            count += 1
            echo(f"[{count}/{total}] {renamed[i].section}/{renamed[i].was}  {seconds:.1f}s")

        old = in_parallel(renamed, lambda p: transport.get(p.type, str(p.was)), parallel, got)
        earlier = {id(p): o for p, o in zip(renamed, old, strict=True)}
    out = []
    for p, live in zip(planned, stored, strict=True):
        if p.renamed:
            d = decide_rename(p, live, earlier[id(p)], ext, update=update, services=services)
        else:
            d = decide(p, live, ext, update=update, services=services)
        d.notes = [*p.notes, *d.notes]
        out.append(d)
    return out
