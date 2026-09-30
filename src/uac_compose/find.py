"""`uac-compose find`: build and test a change-file filter, one line at a time.

The session starts from a server filter (`workflowname=Nightly WF`): one
`uac <group> list-advanced` call, whose answer is complete. Every test after
that runs on those objects, at once, in the syntax a change file and
`export -W` use. `step delete` prints the filter as a change-file step, with
the server filter as `from:`, so `apply` selects what the session showed.
"""

from __future__ import annotations

import difflib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from uac_compose import emit, typealias, where, yamlio
from uac_compose.core.normalize import normalize
from uac_compose.transport import (
    KIND_BY_KEY,
    Kind,
    Transport,
    TransportError,
    object_name,
    server_filters,
)

Echo = Callable[[str], None]

SHOWN = 15
STEP_ACTIONS = ("delete", "update", "rename")
HELP = """\
A test keeps the objects it holds for; every test must hold:
  agent=AGT-01          equal, or a wildcard: agent=AGT*
  agent=[A, B]          one of a list
  agent!=AGT-01         not
  command~sleep \\d+     a regular expression (found anywhere; case ignored)
  command*=sleep        contains the text
  where: {agent: 'AGT*', command: {regex: 'sleep'}}   the change-file form
Help to write one:
  fields [TEXT]         the fields of the matches (only those holding TEXT):
                        how many matches have one, and the most common value
  values FIELD          each value of a field, with its count (dotted paths: actions.x.y)
  show NAME             one object's fields and values
  list | list all       the matches (15 / all)
The session:
  tests | undo | clear  the tests now; drop the last one; drop them all
  from FILTER=VALUE     one more server filter: the controller lists again
  filters               the server filters of this kind
  step ACTION           the filter as a change-file step (delete, update, rename)
  save FILE [ACTION]    add that step to a change file (made if new)
  help | quit"""


class FindError(ValueError):
    """A line the session cannot do; it says why and goes on."""


@dataclass
class Session:
    kind: Kind
    transport: Transport
    types: frozenset[str] | None = None
    """`-t linux`: the types kept; None: every type of the kind."""
    type_word: str | None = None
    echo: Echo = lambda _: None
    source: dict[str, str] = field(default_factory=dict)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    tests: list[where.Test] = field(default_factory=list)
    listed: bool = False
    done: bool = False

    @classmethod
    def open(cls, word: str, transport: Transport, echo: Echo) -> Session:
        allowed = emit.type_filter(word)
        if len(allowed) != 1:
            raise FindError(f"-t {word!r} names {len(allowed)} kinds; give one (task, trigger...)")
        key, types = next(iter(allowed.items()))
        kind = KIND_BY_KEY[key]
        typed = word if types is not None else None
        return cls(kind, transport, types, typed, echo)

    # -- what the session holds

    @property
    def filters(self) -> dict[str, dict[str, Any]]:
        return server_filters(self.kind.key)

    @property
    def needs_source(self) -> bool:
        return bool(self.filters) and not self.listed

    def matches(self) -> list[str]:
        return [
            name
            for name, obj in self.objects.items()
            if where.holds(self.tests, obj, self.kind.key)
        ]

    def opening(self) -> list[str]:
        """What to say first: the server filters to start from, or the listing."""
        if not self.filters:
            return self._list_again()
        return [
            f"{self.kind.key}: start with a server filter (one uac "
            f"{self.kind.group} {self.kind.verb} call), e.g. "
            f"{next(iter(self.filters))}=VALUE",
            *self._filter_lines(),
        ]

    # -- one line

    def handle(self, line: str) -> list[str]:
        text = line.strip()
        if not text:
            return []
        word, _, rest = text.partition(" ")
        word = word.lower()
        try:
            if word in ("quit", "exit", "q"):
                self.done = True
                return []
            if word in ("help", "?"):
                return HELP.splitlines()
            if word == "filters":
                return self._filter_lines()
            if word == "from":
                return self._from(rest)
            if self.needs_source:
                return self._from(text)
            return self._command(word, rest.strip(), text)
        except (FindError, where.WhereError, TransportError, yaml.YAMLError) as error:
            return [f"?? {error}"]

    def _command(self, word: str, rest: str, text: str) -> list[str]:
        if word == "list":
            return self._list_lines(everything=rest.lower() == "all")
        if word == "fields":
            return self._fields(rest.lower())
        if word == "values":
            return self._values(rest)
        if word == "show":
            return self._show(rest)
        if word == "tests":
            return [f"  {t}" for t in self.tests] or ["  no test: every listed object matches"]
        if word == "undo":
            if not self.tests:
                return ["  no test to drop"]
            dropped = self.tests.pop()
            return [f"  dropped: {dropped}", self._count()]
        if word == "clear":
            self.tests.clear()
            return [self._count()]
        if word == "step":
            return self.step(rest or "delete").splitlines()
        if word == "save":
            return self._save(rest)
        if word == "where:" or text.startswith("{"):
            loaded = yaml.safe_load(text if word != "where:" else rest)
            return self._add(where.parse(loaded))
        return self._add(where.parse_terms([text]))

    # -- server filters

    def _filter_lines(self) -> list[str]:
        if not self.filters:
            return [f"  {self.kind.key} has no server filters: every one is listed"]
        return [
            f"  {name:20s} {spec.get('note', '')}".rstrip() for name, spec in self.filters.items()
        ]

    def _from(self, text: str) -> list[str]:
        if not self.filters:
            raise FindError(f"{self.kind.key} has no server filters")
        added: dict[str, str] = {}
        for term in text.split(","):
            name, sep, value = term.partition("=")
            name, value = name.strip().lower(), value.strip().strip("'\"")
            if not sep or not value or name not in self.filters:
                raise FindError(
                    f"{term.strip()!r}: start with a server filter FILTER=VALUE, one of "
                    + ", ".join(self.filters)
                    + " (`filters` says what each takes)"
                )
            added[name] = value
        self.source.update(added)
        return self._list_again()

    def _list_again(self) -> list[str]:
        started = time.monotonic()
        rows = self.transport.list_kind(self.kind, self.source or None)
        kept = [r for r in rows if self.types is None or r.get("type") in self.types]
        self.objects = {object_name(r): r for r in kept}
        self.listed = True
        said = " ".join(f"{k}={v}" for k, v in self.source.items())
        call = f"uac {self.kind.group} {self.kind.verb} {said}".rstrip()
        typed = f", {len(kept)} of them {self.type_word}" if self.types is not None else ""
        seconds = time.monotonic() - started
        return [f"  {call}: {len(rows)} {self.kind.key}{typed} in {seconds:.1f}s", self._count()]

    # -- tests

    def _add(self, tests: list[where.Test]) -> list[str]:
        self.tests += tests  # `where.parse` already refused a bad regex
        found = self.matches()
        return [self._count(), *(f"    {n}" for n in found[:5])] + (
            [f"    ... {len(found) - 5} more (list)"] if len(found) > 5 else []
        )

    def _count(self) -> str:
        tests = f" ({len(self.tests)} test{'s' if len(self.tests) != 1 else ''})"
        return f"  {len(self.matches())} of {len(self.objects)} match" + (
            tests if self.tests else ""
        )

    # -- help to write a test

    def _list_lines(self, everything: bool) -> list[str]:
        found = self.matches()
        shown = found if everything else found[:SHOWN]
        lines = [
            f"  {typealias.alias_for(str(self.objects[n].get('type') or '')):14s} {n}"
            for n in shown
        ]
        if len(found) > len(shown):
            lines.append(f"  ... {len(found) - len(shown)} more (list all)")
        return lines or ["  nothing matches"]

    def _fields(self, part: str = "") -> list[str]:
        counts: Counter[str] = Counter()
        samples: dict[str, Counter[str]] = {}
        for name in self.matches():
            seen: set[str] = set()
            for path, value in _leaves(normalize(self.objects[name]), ""):
                if part not in path.lower():
                    continue
                if path not in seen:
                    seen.add(path)
                    counts[path] += 1
                samples.setdefault(path, Counter())[_short(value)] += 1
        if not counts:
            return [f"  no field holds {part!r}" if part else "  nothing matches"]
        width = max(len(p) for p in counts)
        return [
            f"  {path:{width}s} {counts[path]:6d}  {samples[path].most_common(1)[0][0]}"
            for path in sorted(counts)
        ]

    def _values(self, path: str) -> list[str]:
        if not path:
            raise FindError("values FIELD: e.g. values agent")
        counts: Counter[str] = Counter()
        for name in self.matches():
            obj = self.objects[name]
            found = [name] if path == "name" else where.values(obj, path.split("."))
            if path == "type":
                found = [typealias.alias_for(str(v)) for v in found]
            for value in found:
                counts[_short(value)] += 1
        if not counts:
            return [f"  no match has {path}; `fields` lists the fields"]
        top = counts.most_common(25)
        more = len(counts) - len(top)
        return [f"  {n:6d}  {v}" for v, n in top] + ([f"  ... {more} more"] if more else [])

    def _show(self, name: str) -> list[str]:
        if not name:
            raise FindError("show NAME: one of the listed objects")
        by_lower = {n.lower(): n for n in self.objects}
        found = by_lower.get(name.strip("'\"").lower())
        if found is None:
            close = difflib.get_close_matches(name, list(self.objects), n=5)
            hint = f"; did you mean {', '.join(map(repr, close))}?" if close else ""
            raise FindError(f"{name!r} is not listed{hint}")
        return ["  " + line for line in yamlio.dump(normalize(self.objects[found])).splitlines()]

    # -- the change-file step

    def step(self, action: str) -> str:
        action = action.strip().lower()
        if action not in STEP_ACTIONS:
            raise FindError(f"step ACTION: one of {', '.join(STEP_ACTIONS)}")
        body: dict[str, Any] = {}
        if self.type_word:
            body["type"] = self.type_word
        if self.source:
            body["from"] = dict(self.source)
        if self.tests:
            body["where"] = where.to_yaml(self.tests)
        head = f"{action} {self.kind.key[:-1]}"
        text = yamlio.dump([{head: "*", **body}])
        if action == "update":
            text += "  set: {}                   # the new values: field: value\n"
        elif action == "rename":
            text += "  prefix: NEW_              # or to:, suffix:, replace: {from: RE, to: T}\n"
        return text

    def _save(self, rest: str) -> list[str]:
        parts = rest.split()
        if not parts or len(parts) > 2:
            raise FindError("save FILE [ACTION]: e.g. save changes.apply.yaml delete")
        path = Path(parts[0])
        action = parts[1] if len(parts) > 1 else "delete"
        step = "".join(f"  {line}\n" for line in self.step(action).splitlines())
        if not path.exists():
            path.write_text(f"version: 1\nsteps:\n{step}")
            return [f"  wrote {path} with 1 step"]
        text = path.read_text()
        document = yaml.safe_load(text)
        if not isinstance(document, dict) or list(document)[-1:] != ["steps"]:
            raise FindError(
                f"{path}: `steps:` is not the last key, so a step cannot be added at the end; "
                "print it with `step` and paste it"
            )
        steps = document["steps"] or []
        path.write_text(text + ("" if text.endswith("\n") else "\n") + step)
        return [f"  added step {len(steps) + 1} to {path}"]


def _leaves(node: Any, path: str) -> list[tuple[str, Any]]:
    """(dotted path, value) for each plain value; rows of a list share one path."""
    if isinstance(node, dict):
        if "value" in node and not isinstance(node["value"], (dict, list)) and path:
            return [(path, node["value"])]  # a controller-labelled field
        out: list[tuple[str, Any]] = []
        for key, value in node.items():
            out += _leaves(value, f"{path}.{key}" if path else str(key))
        return out
    if isinstance(node, list):
        return [leaf for item in node for leaf in _leaves(item, path)]
    return [(path, node)] if path else []


def _short(value: Any, width: int = 60) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."
