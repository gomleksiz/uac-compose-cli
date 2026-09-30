"""File -> UAC objects in simplified, stamped form (SPEC §6.1, §5.5).

Invariant: `compile(emit(x, style)) == simplify(x)`. The compiled object is
what import compares with the controller (one-directionally) and inflates to
create.

On the way:
- `type:` is resolved per section; routed kinds get their routing key and keep
  their own `type` in `_uacType`;
- an extension parameter written as its numbered slot becomes its name, so
  both spellings compile to one object (`inflate` puts the slots back);
- top-level numbers <-> digit strings follow the spec's declared type (a
  string field written `100` comes back `'100'` and never matches again);
  booleans are never coerced;
- an enum written as its integer id or its UI label becomes its name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from uac_compose import groups, idioms, registry, typealias, uacdocs
from uac_compose.core.extensions import collapse
from uac_compose.core.templates import TemplateSet
from uac_compose.groups import GroupError
from uac_compose.idioms import IdiomError
from uac_compose.routing import UAC_TYPE_KEY
from uac_compose.transport import KIND_BY_KEY
from uac_compose.yamlio import Bundle


class CompileError(ValueError):
    pass


@dataclass
class Planned:
    section: str
    name: str
    obj: dict[str, Any]
    create_only: bool = False
    was: str | None = None
    """The name it has on the controller now, when the file renames it (`was:`)."""
    notes: list[str] = field(default_factory=list)
    """What compiling changed that a reader should hear about (a followed rename)."""

    @property
    def renamed(self) -> bool:
        return self.was is not None and self.was != self.name

    @property
    def type(self) -> str:
        return str(self.obj.get("type", ""))

    @property
    def label(self) -> str:
        return f"{self.section}/{self.name}"


def resolve_type(section: str, written: Any) -> str:
    """A task or trigger `type:`: the short name (`Unix`) or the raw one (`taskUnix`)."""
    if not isinstance(written, str) or not written:
        raise CompileError("needs a `type:`")
    found = typealias.to_uac(section, written)
    if found is None:
        raise CompileError(
            f"unknown {section[:-1]} type {written!r}; known: {', '.join(typealias.known(section))}"
        )
    return found


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def as_declared(obj: dict[str, Any], types: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in obj.items():
        want = types.get(name)
        number = isinstance(value, int | float) and not isinstance(value, bool)
        if want == "string" and number:
            out[name] = str(value)
        elif want in ("integer", "number") and isinstance(value, str) and _is_number(value):
            out[name] = float(value) if want == "number" or "." in value else int(value)
        else:
            out[name] = value
    return out


def enum_names(obj: dict[str, Any], type_name: str) -> dict[str, Any]:
    out = dict(obj)
    for name, doc in uacdocs.fields(type_name).items():
        value = out.get(name)
        if not doc.values or value is None or isinstance(value, bool):
            continue
        if isinstance(value, str) and value in doc.values:
            continue
        if isinstance(value, str) and value in doc.ui_labels:
            out[name] = doc.ui_labels[value]
            continue
        as_id = value if isinstance(value, int) else int(value) if str(value).isdigit() else None
        if as_id is not None:
            by_id = {i: n for n, i in doc.values.items()}
            if as_id in by_id:
                out[name] = by_id[as_id]
    return out


Lookup = Callable[[str], dict[str, Any] | None]
"""A `like:` source by name: a `templates:` entry or another object in the file."""


def _written_flat(
    section: str, name: str, body: dict[str, Any], lookup: Lookup | None, seen: tuple[str, ...]
) -> dict[str, Any]:
    """The body with every `like:` applied and `sla:` flattened; idioms still as written.

    `like:` copies a whole definition -- a `templates:` entry or another object
    in the file -- and whatever this definition states wins over it. Several
    sources are applied **in the order written**, so a later one overrides an
    earlier one, and the definition's own fields override them all.
    """
    if name in seen:
        raise CompileError(f"`like:` loops: {' -> '.join((*seen, name))}")
    seen = (*seen, name)
    merged: dict[str, Any] = {}
    for source in _sources(body.get("like")):
        found = lookup(source) if lookup else None
        if found is None:
            raise CompileError(f"`like: {source}` names nothing in this file")
        merged = _over(merged, _written_flat(section, source, found, lookup, seen))
    own = {k: v for k, v in body.items() if k != "like"}
    whole = _over(merged, own)
    if seen[:-1]:
        # A `like:` source is a bag of fields with no type of its own; it is
        # flattened by whoever copies it, which is what knows the type.
        return whole
    type_name = _type_of(section, whole.get("type"))
    if type_name is not None and type_name in registry.load():
        whole = groups.ungroup(type_name, whole, name)
    return whole


def _sources(written: Any) -> list[str]:
    """`like: a` or `like: [a, b]`, in the order written."""
    if written is None:
        return []
    if isinstance(written, str):
        return [written]
    if isinstance(written, list) and all(isinstance(s, str) for s in written):
        return list(written)
    raise CompileError("`like:` takes a name, or a list of names applied in order")


def apply_like(
    body: dict[str, Any], lookup: Lookup | None, seen: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`like:` on a bag of fields with no type of its own (a row of a list),
    by the rules an object's `like:` follows: sources in the order written,
    a later one over an earlier one, the body's own fields over them all."""
    merged: dict[str, Any] = {}
    for source in _sources(body.get("like")):
        if source in seen:
            raise CompileError(f"`like:` loops: {' -> '.join((*seen, source))}")
        found = lookup(source) if lookup else None
        if found is None:
            raise CompileError(f"`like: {source}` names nothing in this file")
        merged = _over(merged, apply_like(found, lookup, (*seen, source)))
    return _over(merged, {k: v for k, v in body.items() if k != "like"})


def _over(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """`top` written over `base`. Mappings merge; anything else replaces.

    A list replaces: three variables copied from a template and two written
    here means two, not five.
    """
    out = dict(base)
    for key, value in top.items():
        held = out.get(key)
        out[key] = (
            _over(held, value) if isinstance(value, dict) and isinstance(held, dict) else value
        )
    return out


def _type_of(section: str, written: Any) -> str | None:
    kind = KIND_BY_KEY[section]
    if kind.routing is not None:
        return kind.routing
    return resolve_type(section, written)


def compile_object(
    section: str,
    name: str,
    body: dict[str, Any],
    templates: TemplateSet | None = None,
    lookup: Lookup | None = None,
) -> dict[str, Any]:
    kind = KIND_BY_KEY[section]
    ext = templates.extensions if templates is not None else None
    subs = templates.sub_templates if templates is not None else {}
    try:
        flat = _written_flat(section, name, body, lookup, ())
        type_name = _type_of(section, flat.get("type"))
        assert type_name is not None
        rest = {k: v for k, v in flat.items() if k != "type"}
        if type_name in registry.load():
            if type_name == "taskWorkflow":
                from uac_compose.workflow import parse_members

                rest = parse_members(rest, name, subs.get("runCriteria"))
            rest = idioms.parse(type_name, rest, name, ext)
    except (GroupError, IdiomError, ValueError) as error:
        if isinstance(error, CompileError):
            raise
        raise CompileError(str(error)) from error
    if kind.routing is not None:
        head: dict[str, Any] = {"type": type_name, kind.name_key: name}
        if "type" in flat:
            if not kind.own_type:
                raise CompileError(f"{section} take no `type:`")
            head[UAC_TYPE_KEY] = flat["type"]
    else:
        head = {"type": type_name, "name": name}
    obj = {**head, **rest}
    if type_name in registry.load():
        entry = registry.get(type_name)
        obj = enum_names(as_declared(obj, entry.field_types), type_name)
        if ext is not None:
            # Collapsed, not expanded: a parameter written by name and the same
            # one written as its numbered slot must compile to one object, and
            # that object is what `compare` and `merge_update` work in.
            # `inflate` puts the slots back on the way to the controller.
            obj = collapse(obj, ext, frozenset(entry.fields))
    return obj


def bundle_lookup(bundle: Bundle) -> Lookup:
    """`like:` sources: `templates:` first, then tasks, triggers, then any section."""
    order = ["tasks", "triggers", *[s for s in bundle.sections if s not in ("tasks", "triggers")]]

    def lookup(name: str) -> dict[str, Any] | None:
        if name in bundle.templates:
            found: dict[str, Any] = bundle.templates[name] or {}
            return found
        for section in order:
            entries = bundle.sections.get(section, {})
            if name in entries:
                entry: dict[str, Any] = entries[name]
                return entry
        return None

    return lookup


def compile_bundle(bundle: Bundle, templates: TemplateSet | None = None) -> list[Planned]:
    """Every object of the file, in creation order (SPEC §6.2)."""
    planned: list[Planned] = []
    errors: list[str] = []
    lookup = bundle_lookup(bundle)
    for section, entries in bundle.sections.items():
        for name, body in entries.items():
            try:
                obj = compile_object(section, name, body, templates, lookup)
            except CompileError as error:
                errors.append(f"{section}/{name}: {error}")
                continue
            if obj.get("opswiseGroups") == []:
                del obj["opswiseGroups"]  # none is the default
            planned.append(
                Planned(
                    section,
                    name,
                    obj,
                    (section, name) in bundle.create_only,
                    bundle.was.get((section, name)),
                )
            )
    if errors:
        raise CompileError("\n".join(errors))
    follow_renames(planned, templates)
    return creation_order(planned)


def follow_renames(planned: list[Planned], templates: TemplateSet | None) -> None:
    """A name the file renames, wherever the file itself still uses the old one.

    A workflow in the same file that names the old task would otherwise fight
    the rename: its compare sees the old name, and its write names a task that
    no longer exists. Each rewrite becomes a note on the object.
    """
    from uac_compose import references

    renames = {(p.section, p.was): p.name for p in planned if p.renamed and p.was}
    if not renames:
        return
    ext = templates.extensions if templates is not None else None
    slot_kinds_of = references.slot_kinds()
    for p in planned:
        slot_kinds: dict[str, str] = {}
        if ext is not None:
            for param, slot in ext.for_template(p.obj.get("template")).items():
                prefix = re.sub(r"\d+$", "", slot.slot)
                if prefix in slot_kinds_of:
                    slot_kinds[param] = slot_kinds_of[prefix]
        p.notes += references.rename(p.type, p.obj, renames, slot_kinds)


def _depends_on(obj: dict[str, Any]) -> list[str]:
    """Task names this task needs to exist first: workflow members, launched
    tasks, the Universal task a Universal Monitor watches."""
    names: list[str] = []
    watched = obj.get("taskUniversal")
    if obj.get("type") == "taskUniversalMonitor" and isinstance(watched, str) and watched:
        names.append(watched)
    for vertex in obj.get("workflowVertices") or []:
        task = vertex.get("task") if isinstance(vertex, dict) else None
        if isinstance(task, dict) and isinstance(task.get("value"), str):
            names.append(task["value"])
    actions = obj.get("actions")
    operations = actions.get("systemOperations") if isinstance(actions, dict) else None
    for op in operations or []:
        launch = isinstance(op, dict) and op.get("operation") == "Launch Task"
        if launch and isinstance(op.get("task"), str):
            names.append(op["task"])
    return names


def _parent_group(obj: dict[str, Any]) -> list[str]:
    """A user group's parent group, which the controller needs to exist first."""
    parent = obj.get("parent")
    return [parent] if isinstance(parent, str) and parent else []


#: Sections whose objects name others of the same section, and how.
DEPENDS_ON: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "tasks": _depends_on,
    "user_groups": _parent_group,
}


def _order_tasks(
    items: list[Planned], depends_on: Callable[[dict[str, Any]], list[str]] = _depends_on
) -> list[Planned]:
    defined = [p.name for p in items]
    done: list[str] = []
    out: list[Planned] = []
    waiting = list(items)

    def ready(item: Planned) -> bool:
        needs = [n for n in depends_on(item.obj) if n in defined and n != item.name]
        return all(n in done for n in needs)

    while waiting:
        # a cycle: take the first waiting item
        index = next((i for i, item in enumerate(waiting) if ready(item)), 0)
        item = waiting.pop(index)
        done.append(item.name)
        out.append(item)
    return out


def creation_order(planned: list[Planned]) -> list[Planned]:
    """Sections in SPEC order; objects in file order, dependencies first (stable).

    A cycle takes the first waiting item. Lists only -- never a set -- so the
    order is the same in every process.
    """
    from uac_compose.yamlio import SECTIONS

    out: list[Planned] = []
    for section in SECTIONS:
        items = [p for p in planned if p.section == section]
        out += _order_tasks(items, DEPENDS_ON[section]) if section in DEPENDS_ON else items
    return out
