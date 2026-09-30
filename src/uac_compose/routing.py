"""Where UAC's own `type` goes while `type` carries the routing key.

A credential (`Standard`/`Resolvable`), a virtual resource
(`Renewable`/`Boundary`/`Depletable`) and an email connection (`Outgoing`...)
declare `type` as data. The routing key must not overwrite it, so the object's
own value moves to `_uacType` first and comes back on every write path. No
registry field begins with an underscore.
"""

from __future__ import annotations

from typing import Any

UAC_TYPE_KEY = "_uacType"


def stamp(obj: dict[str, Any], routing_type: str) -> dict[str, Any]:
    """The object with `type` = the routing key; its own `type` moved aside if it differs."""
    out = dict(obj)
    own = out.get("type")
    if own is not None and own != routing_type:
        out[UAC_TYPE_KEY] = own
    out["type"] = routing_type
    return out


def restore(obj: dict[str, Any]) -> dict[str, Any]:
    """The object as the controller takes it: routing key removed, own `type` back."""
    out = {k: v for k, v in obj.items() if k not in ("type", UAC_TYPE_KEY)}
    if obj.get(UAC_TYPE_KEY) is not None:
        out["type"] = obj[UAC_TYPE_KEY]
    return out
