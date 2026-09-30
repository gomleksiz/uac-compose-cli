"""`--style compact` (the default): short types, idioms, UI-category groups.

Every object is checked before it is written: its compact body must compile
back to exactly the object the uac style would state. If it does not, the
uac body is written instead -- a verbose file is better than a wrong one.
"""

from __future__ import annotations

from typing import Any

from uac_compose import groups, idioms, inactive, registry, typealias
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.style.uac import body_of, without_redacted
from uac_compose.transport import kind_for_type, object_name
from uac_compose.workflow import canonical


def compact_body(
    type_name: str,
    section: str,
    body: dict[str, Any],
    templates: TemplateSet | None = None,
    imply_enabled: bool = True,
) -> dict[str, Any]:
    """A uac-style body in compact form (not yet checked)."""
    out = dict(body)
    if section in typealias.SECTION_KINDS and "type" in out:
        out["type"] = typealias.alias_for(type_name)
    if type_name not in registry.load():
        return out
    if type_name == "taskWorkflow":
        from uac_compose.workflow import emit_members

        subs = templates.sub_templates if templates is not None else {}
        out = emit_members(out, subs.get("runCriteria"))
    out = idioms.emit(type_name, out, templates.extensions if templates else None)
    return groups.regroup(type_name, out, imply_enabled)


def emit_object(
    obj: dict[str, Any], type_name: str, templates: TemplateSet, *, show_inactive: bool = False
) -> tuple[str, str, dict[str, Any], bool]:
    """(section, name, body, compact?) -- `compact?` is False when the check fell back."""
    from uac_compose.compile import CompileError, compile_object

    kind = kind_for_type(type_name)
    stamped = kind.stamp(obj)
    reserved = (
        frozenset(registry.get(type_name).fields) if type_name in registry.load() else frozenset()
    )
    small = simplify(
        stamped,
        templates.get(type_name),
        templates.sub_templates,
        extensions=templates.extensions,
        reserved=reserved,
    )
    if not show_inactive:
        small = inactive.hide(small, normalize(stamped))
    small, note = without_redacted(small)
    name = object_name(stamped)
    raw = body_of(small, kind)
    body = compact_body(type_name, kind.key, raw, templates, not show_inactive)
    try:
        back = compile_object(kind.key, name, body, templates)
        ok = canonical(idioms.without_labels(back)) == canonical(idioms.without_labels(small))
    except (CompileError, ValueError):
        ok = False
    chosen = body if ok else raw
    if note:
        chosen["comment"] = note
    return kind.key, name, chosen, ok


def emit_document(
    objects: list[tuple[dict[str, Any], str]],
    templates: TemplateSet,
    *,
    show_inactive: bool = False,
) -> dict[str, dict[str, Any]]:
    from uac_compose.yamlio import SECTIONS

    found: dict[str, dict[str, Any]] = {}
    for obj, type_name in objects:
        section, name, body, _ = emit_object(obj, type_name, templates, show_inactive=show_inactive)
        found.setdefault(section, {})[name] = body
    return {s: found[s] for s in SECTIONS if s in found}
