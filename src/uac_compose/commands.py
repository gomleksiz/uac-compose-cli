"""Fields only a command sets (data/rules/commands.yaml).

A webhook's `status` and `executionUser` are not stored by create or update:
`webhook enable` and `webhook assign-execution-user` set them. Import and
apply write the object, then run what `needed` returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any

import yaml

from uac_compose.paths import data_root


@dataclass(frozen=True)
class Command:
    verb: str
    """The `uac <group>` command, e.g. `enable`."""
    field_name: str
    value: Any
    args: dict[str, str] = field(default_factory=dict)

    def note(self, group: str) -> str:
        return f"{self.field_name}: {self.value} is set by `{group} {self.verb}` after the write"


@cache
def rules() -> dict[str, dict[str, dict[str, Any]]]:
    raw = yaml.safe_load((data_root() / "rules" / "commands.yaml").read_text())
    return {str(t): dict(fields) for t, fields in (raw or {}).items()}


def needed(obj: dict[str, Any], stored: dict[str, Any] | None) -> list[Command]:
    """The commands, in order, that make the controller hold what `obj` states.

    `stored` is the object before the write; None for a create, which leaves
    each field at its `created` value. A field the file does not state keeps
    the value it had: when a `while` changes it on the way (a webhook is
    disabled to assign its execution user), it is set back afterwards.
    """
    spec = rules().get(str(obj.get("type", "")), {})
    now = {
        name: stored.get(name) if stored is not None else how.get("created")
        for name, how in spec.items()
    }
    goal = {name: obj.get(name) if obj.get(name) not in (None, "") else now[name] for name in spec}
    out: list[Command] = []

    def set_to(name: str, value: Any) -> None:
        command = _command(name, spec[name], value)
        if command is not None:
            out.append(command)
            now[name] = value

    for name, how in spec.items():
        if goal[name] == now[name] or goal[name] is None:
            continue
        for first, value in (how.get("while") or {}).items():
            if now.get(first) != value:
                set_to(first, value)
        set_to(name, goal[name])
    for name in spec:  # what a `while` changed goes back
        if now[name] != goal[name] and goal[name] is not None:
            set_to(name, goal[name])
    return out


def _command(name: str, how: dict[str, Any], value: Any) -> Command | None:
    if "values" in how:
        verb = how["values"].get(str(value))
        return None if verb is None else Command(str(verb), name, value)  # validation says why
    return Command(str(how["command"]), name, value, {how["argument"]: str(value)})


def unknown_values(obj: dict[str, Any]) -> list[str]:
    """Values no command can set, e.g. `status: Paused`."""
    out = []
    for name, how in rules().get(str(obj.get("type", "")), {}).items():
        want = obj.get(name)
        if "values" in how and want not in (None, "") and str(want) not in how["values"]:
            allowed = ", ".join(how["values"])
            out.append(f"{name}: {want!r} cannot be set; use one of {allowed}")
    return out
