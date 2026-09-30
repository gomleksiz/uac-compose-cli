"""`--style uac`: UAC's own field names and nesting, defaults removed. Nothing else.

The section says what kind an object is; the key is its name. `type:` is
written only where it says something the section does not: a task's or a
trigger's discriminator, or a credential's / virtual resource's / email
connection's own `type`.
"""

from __future__ import annotations

from typing import Any

from uac_compose import inactive, registry
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.routing import UAC_TYPE_KEY
from uac_compose.transport import Kind, kind_for_type, object_name


def body_of(small: dict[str, Any], kind: Kind) -> dict[str, Any]:
    """A simplified (stamped) object as it sits under its section key."""
    drop = {kind.name_key, UAC_TYPE_KEY}
    if kind.routing is not None:
        drop.add("type")
    body = {k: v for k, v in small.items() if k not in drop}
    if kind.routing is not None and small.get(UAC_TYPE_KEY) is not None:
        body = {"type": small[UAC_TYPE_KEY], **body}
    elif "type" in body:
        body = {"type": body.pop("type"), **body}
    return body


REDACTED_NOTE = (
    "providerParameters not exported: the controller redacts their values, and "
    "an import of blanks would clear the secret (--update replaces lists)"
)


def without_redacted(small: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """SPEC §6.7: never emit a providerParameters list holding a blank value."""
    rows = small.get("providerParameters")
    if isinstance(rows, list) and any(
        not isinstance(r, dict) or r.get("value") in (None, "") for r in rows
    ):
        return {k: v for k, v in small.items() if k != "providerParameters"}, REDACTED_NOTE
    return small, None


def emit_object(
    obj: dict[str, Any], type_name: str, templates: TemplateSet, *, show_inactive: bool = False
) -> tuple[str, str, dict[str, Any]]:
    """(section, name, body) for one stored object."""
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
    body = body_of(small, kind)
    if note:
        body["comment"] = note
    return kind.key, object_name(stamped), body


def emit_document(
    objects: list[tuple[dict[str, Any], str]],
    templates: TemplateSet,
    *,
    show_inactive: bool = False,
) -> dict[str, dict[str, Any]]:
    """A bundle document (section -> name -> body) for (object, type) pairs, sections in order."""
    from uac_compose.yamlio import SECTIONS

    found: dict[str, dict[str, Any]] = {}
    for obj, type_name in objects:
        section, name, body = emit_object(obj, type_name, templates, show_inactive=show_inactive)
        found.setdefault(section, {})[name] = body
    return {s: found[s] for s in SECTIONS if s in found}
