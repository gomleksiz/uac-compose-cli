"""Remove every value equal to the type's default (SPEC §5.2).

One generic recursive differ for all types; per-type behavior comes from the
template. `inflate` is the exact inverse and imports `should_descend` from here.
"""

from __future__ import annotations

from typing import Any

from uac_compose.core.extensions import ExtensionMap, collapse
from uac_compose.core.normalize import ALWAYS_KEEP, Exclusions, is_empty, normalize


def should_descend(default: Any, path: str, sub_templates: dict[str, dict[str, Any]]) -> bool:
    """Recurse into a dict when there is a nested default, or a sub-template below it."""
    if isinstance(default, dict):
        return True
    prefix = f"{path}."
    return any(candidate.startswith(prefix) for candidate in sub_templates)


def simplify(
    obj: dict[str, Any],
    template: dict[str, Any],
    sub_templates: dict[str, dict[str, Any]] | None = None,
    *,
    extensions: ExtensionMap | None = None,
    reserved: frozenset[str] = frozenset(),
    excl: Exclusions | None = None,
) -> dict[str, Any]:
    """`normalize`, drop defaults, then (optionally) collapse extension slots.

    `reserved` is the type's real UAC field names: a parameter never collapses
    onto one of them.
    """
    small = _diff(normalize(obj, excl), obj, template, sub_templates or {}, "", root=True)
    if extensions is not None:
        small = collapse(small, extensions, reserved)
    return small


def _diff(
    obj: dict[str, Any],
    raw: Any,
    template: dict[str, Any],
    subs: dict[str, dict[str, Any]],
    path: str,
    *,
    root: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        child = f"{path}.{key}" if path else key
        default = template.get(key)
        if root and key in ALWAYS_KEEP:
            out[key] = value
            continue
        if key in template and value == default:
            continue
        if isinstance(value, dict) and should_descend(default, child, subs):
            nested = _diff(
                value, _at(raw, key), default if isinstance(default, dict) else {}, subs, child
            )
            if nested:
                out[key] = nested
            continue
        if isinstance(value, list) and child in subs:
            raw_items = _at(raw, key)
            out[key] = [
                _diff(item, _nth(raw_items, i), subs[child], subs, child)
                if isinstance(item, dict)
                else item
                for i, item in enumerate(value)
            ]
            continue
        out[key] = value
    # A field the template defaults but the object stores empty travels as an
    # explicit null, or `inflate` would fill the default back in. A field the
    # object does not carry at all stays out. Measured on ps1: 24 taskSap with
    # outputReturnNline null, 2 triggerTime with startingAt null; corpus: 0.
    if isinstance(raw, dict):
        for key in template:
            if key not in obj and key in raw and is_empty(raw[key]):
                out[key] = None
    return out


def _at(raw: Any, key: str) -> Any:
    return raw.get(key) if isinstance(raw, dict) else None


def _nth(raw: Any, index: int) -> Any:
    return raw[index] if isinstance(raw, list) and index < len(raw) else None
