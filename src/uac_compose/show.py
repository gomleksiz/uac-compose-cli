"""`uac-compose show`: the parameters of one Universal Extension, on one controller.

Why it exists. An import validates a Universal task against the extension map
of the profile it is writing to. When the target has never had that extension
installed, every parameter of the task is refused, one line each:

    'sourcefile' is neither a field of taskUniversal nor a parameter of
    extension 'CS AWS S3'

Seventeen lines like that say the same one thing -- *this controller has no
CS AWS S3* -- and nothing in them says it. `show` answers the question the
lines raise: what does this controller know about this extension?

It reads the profile's learned map first, and asks the controller when the map
has never heard the name. It writes nothing, anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from uac_compose.core.extensions import ExtensionMap, Slot, declared_map


@dataclass
class Description:
    name: str
    parameters: dict[str, Slot] = field(default_factory=dict)
    source: str = ""
    """Where the answer came from: the profile's map, or the controller."""

    near: list[str] = field(default_factory=list)
    """Names on this controller that are spelt nearly the same."""

    @property
    def found(self) -> bool:
        return bool(self.parameters)


def _slot_order(slot: str) -> tuple[str, int]:
    """`choiceField12` sorts after `choiceField2`, not between 1 and 2."""
    digits = "".join(c for c in slot if c.isdigit())
    return slot.rstrip("0123456789"), int(digits) if digits else 0


def describe(name: str, ext: ExtensionMap, transport: Any = None) -> Description:
    """What this controller knows about the extension called `name`."""
    params = ext.for_template(name)
    source = "the profile's extension map"
    if not params and transport is not None:
        definition = transport.get_template(name)
        if definition is not None:
            params = declared_map([definition]).for_template(name)
            source = "the controller"
    if not params:
        return Description(name, near=get_close_matches(name, sorted(ext.templates), n=5))
    ordered = dict(sorted(params.items(), key=lambda kv: (_slot_order(kv[1].slot), kv[0])))
    return Description(name, ordered, source)


def lines(described: Description) -> list[str]:
    """The report, as lines to print."""
    if not described.found:
        out = [f"no Universal Extension named {described.name!r} on this controller"]
        if described.near:
            out.append("did you mean: " + ", ".join(repr(n) for n in described.near))
        out.append("`learn --refresh` re-reads the controller; `freeze` copies extensions.")
        return out
    width = max(len(n) for n in described.parameters)
    out = [
        f"{described.name}: {len(described.parameters)} parameter(s), "
        f"from {described.source}",
        f"{'parameter'.ljust(width)}  {'slot'.ljust(14)}  values",
    ]
    for param, slot in described.parameters.items():
        values = ", ".join(slot.values) if slot.values else ""
        out.append(f"{param.ljust(width)}  {slot.slot.ljust(14)}  {values}".rstrip())
    return out
