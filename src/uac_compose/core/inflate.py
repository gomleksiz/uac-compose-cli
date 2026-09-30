"""The inverse of `simplify`: deep-merge the simplified object over its template."""

from __future__ import annotations

from typing import Any

from uac_compose.core.extensions import ExtensionMap, expand
from uac_compose.core.simplify import should_descend


def inflate(
    small: dict[str, Any],
    template: dict[str, Any],
    sub_templates: dict[str, dict[str, Any]] | None = None,
    *,
    extensions: ExtensionMap | None = None,
    reserved: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if extensions is not None:
        small = expand(small, extensions, reserved)
    return _merge(template, small, sub_templates or {}, "")


def _merge(
    template: dict[str, Any],
    overlay: dict[str, Any],
    subs: dict[str, dict[str, Any]],
    path: str,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(template)
    for key, value in overlay.items():
        child = f"{path}.{key}" if path else key
        default = template.get(key)
        if value is None:
            out.pop(key, None)  # the object leaves a defaulted field empty
        elif isinstance(value, dict) and should_descend(default, child, subs):
            out[key] = _merge(default if isinstance(default, dict) else {}, value, subs, child)
        elif isinstance(value, list) and child in subs:
            out[key] = [
                _merge(subs[child], item, subs, child) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[key] = value
    return out
