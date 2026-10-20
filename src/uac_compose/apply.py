"""`uac-compose apply FILE`: ordered steps that update, rename or delete (SPEC §9a).

A change file creates only users (`create user`, with the password from an
environment variable). Each step names objects -- exact names, a list,
or a wildcard / regex as `export` reads them -- and says what to do with them:

    version: 1
    templates:
      night: {agent: AGT-02}
    steps:
      - delete task: '*_tmp'
      - update task: Nightly Load
        set: {like: night, command: load.sh --fast}
        add: {actions.setVariableActions: [{status: Success, variableName: X}]}
      - rename task: Nightly Load
        to: DEV_Nightly Load

Every step is planned first, in order and reading live, over an overlay of
what the steps before it do, so `rename A to B` then `update B` plans right.
Nothing is written until the whole plan holds; then the writes run one at a
time and stop at the first error.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

from uac_compose import commands, emit, importer, references, validate, where, yamlio
from uac_compose.compile import CompileError, Planned, apply_like, compile_object
from uac_compose.core.extensions import ExtensionMap
from uac_compose.core.normalize import normalize
from uac_compose.core.templates import TemplateSet
from uac_compose.paths import data_root
from uac_compose.transport import (
    KIND_BY_KEY,
    Kind,
    Transport,
    TransportError,
    object_name,
    server_filters,
)

Echo = Callable[[str], None]

FORMAT_VERSION = 1
ACTIONS = ("delete", "update", "rename", "replace")
#: Keys a step may hold beside its `<action> <kind>` key, per action.
ALLOWED = {
    "delete": {"type", "regex", "comment", "where", "from"},
    "update": {"type", "regex", "comment", "where", "from", "set", "add", "remove", "change"},
    "rename": {"type", "regex", "comment", "where", "from", "set", "add", "remove", "change"}
    | {"to", "prefix", "suffix", "replace"},
    "replace": {"regex", "comment", "with", "in", "where"},
}
RENAMES = ("to", "prefix", "suffix", "replace")
#: Rows inside a task that a step can treat as objects of their own:
#: `update email action: '*'` + `where: {to: {contains: jane.doe}}`.
ROW_KINDS = {
    "email action": "actions.emailNotifications",
    "email notification": "actions.emailNotifications",
    "variable action": "actions.setVariableActions",
    "set variable action": "actions.setVariableActions",
    "system action": "actions.systemOperations",
    "system operation": "actions.systemOperations",
    "abort action": "actions.abortActions",
    "snmp action": "actions.snmpNotifications",
    "snmp notification": "actions.snmpNotifications",
}
#: The actions a step may write as `<action>: Kind.find(...)`.
QUERY_ACTIONS = ("delete", "update", "rename")
ROWS = ("remove", "change", "add")  # the order they apply in, after `set`

#: Users are not a bundle section: `create user` is the one step that creates.
USERS = Kind("users", "user")
USER_KEYS = {"comment", "password_env", "set", "groups"}
#: UserWsData fields a `set:` may not hold: the controller's own, the name
#: (the step's head says it) and secrets (rule 8: the password comes from
#: `password_env`).
USER_NOT_SET = {
    "sysId",
    "excludeRelated",
    "exportReleaseLevel",
    "exportTable",
    "retainSysIds",
    "userName",
    "userPassword",
    "tokens",
}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ApplyError(ValueError):
    """The change file cannot be read, or its plan does not hold."""


@dataclass
class Step:
    number: int
    action: str
    kind: Kind
    names: list[str]
    regex: bool = False
    type: str | None = None
    source: dict[str, str] = field(default_factory=dict)
    """`from:` list-advanced filters: the candidates the names and `where` narrow."""
    set: dict[str, Any] = field(default_factory=dict)
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    """`add` / `remove` / `change` -> dotted list path -> what to do."""
    rename: dict[str, Any] = field(default_factory=dict)
    """One of `to`, `prefix`, `suffix`, `replace`."""
    replace_with: str = ""
    """`replace task: OLD` / `with: NEW`: the task the workflows get instead."""
    within: list[str] = field(default_factory=lambda: ["*"])
    """`in:` the workflows to look in, by name or pattern; all by default."""
    where: list[Any] = field(default_factory=list)
    """Field tests every object must pass (`where.Test`); for replace, every workflow;
    for a row kind, every row."""
    row_path: str = ""
    """A row kind's list (`actions.emailNotifications`): the step works on its rows."""
    edit: dict[str, Any] = field(default_factory=dict)
    """Row kind updates: field -> {from: REGEX, to: TEXT}, a part of the value."""
    password_env: str = ""
    """`create user`: the environment variable that holds the password."""
    groups: list[str] = field(default_factory=list)
    """`create user`: the user groups the new user joins."""

    @property
    def label(self) -> str:
        what = next((w for w, p in ROW_KINDS.items() if p == self.row_path), self.kind.key[:-1])
        return f"step {self.number} ({self.action} {what})"


@dataclass
class ChangeFile:
    steps: list[Step]
    templates: dict[str, Any]


# --- read ---------------------------------------------------------------------------


def read(text: str) -> ChangeFile:
    try:
        document = yamlio.load(text)
    except Exception as error:  # yaml.YAMLError and friends
        raise ApplyError(f"not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ApplyError("a change file is a mapping with `steps:`")
    unknown = set(document) - {"version", "templates", "steps", "comment"}
    if unknown:
        raise ApplyError(f"unknown key(s): {', '.join(sorted(map(str, unknown)))}")
    version = document.get("version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise ApplyError(
            f"version: this uac-compose reads change files of version {FORMAT_VERSION}"
        )
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ApplyError("`steps:` must be a list of steps")
    templates = document.get("templates") or {}
    if not isinstance(templates, dict):
        raise ApplyError("`templates:` must be a mapping of name -> fields")
    errors: list[str] = []
    steps: list[Step] = []
    for number, raw in enumerate(raw_steps, 1):
        try:
            steps.append(_step(number, raw))
        except ApplyError as error:
            errors.append(f"step {number}: {error}")
    if errors:
        raise ApplyError("\n".join(errors))
    return ChangeFile(steps, templates)


def _step(number: int, raw: Any) -> Step:
    if not isinstance(raw, dict):
        raise ApplyError("a step is a mapping like `delete task: NAME`")
    words = {k: k.split(None, 1) for k in raw if isinstance(k, str) and k.strip()}
    heads = [k for k, w in words.items() if len(w) == 2 and w[0] in (*ACTIONS, "create")]
    if not heads:  # `update: Task.find(...)`; not `replace`, which is a rename key too
        heads = [k for k, w in words.items() if len(w) == 1 and w[0] in QUERY_ACTIONS]
    if len(heads) != 1:
        raise ApplyError(
            "needs one `<delete|update|rename|replace> <kind>: NAME` key, "
            "or `<action>: Kind.find(...)`"
        )
    head = heads[0]
    query = None
    if len(head.split(None, 1)) == 1:  # `update: Task.find(name='*x*').where(agent=y)`
        if not isinstance(raw[head], str):
            raise ApplyError(f"`{head}:` alone takes a query: Task.find(...).where(...)")
        try:
            query = where.parse_query(raw[head])
        except where.WhereError as error:
            raise ApplyError(str(error)) from error
        action, word = head, query.kind.lower()
    else:
        action, word = head.split(None, 1)
    if action == "create":
        if word.strip().lower() == "user":
            return _user_step(number, head, raw)
        raise ApplyError("apply creates only users; use `uac-compose import` for the rest")
    row_word = " ".join(word.lower().replace("_", " ").replace("-", " ").split())
    if row_word in ROW_KINDS:
        return _row_step(number, action, head, raw, ROW_KINDS[row_word])
    key = emit.TYPE_WORDS.get(word.strip().replace("-", "_")) or emit.TYPE_WORDS.get(word.strip())
    if key is None:
        raise ApplyError(f"unknown kind {word!r}; use one of {', '.join(sorted(emit.TYPE_WORDS))}")
    rest = {k: v for k, v in raw.items() if k != head}
    extra = set(rest) - ALLOWED[action]
    if extra:
        raise ApplyError(f"{action} takes no {', '.join(sorted(map(str, extra)))}")
    names = (query.names or ["*"]) if query is not None else raw[head]
    names = [names] if isinstance(names, str) else names
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ApplyError("name one object, a list of names, or a pattern")
    step = Step(number, action, KIND_BY_KEY[key], list(names), bool(rest.get("regex")))
    step.type = rest.get("type")
    step.source = _source(key, rest.get("from"))
    try:
        step.where = (query.tests if query is not None else []) + (
            where.parse(rest["where"]) if "where" in rest else []
        )
    except where.WhereError as error:
        raise ApplyError(str(error)) from error
    step.set = rest.get("set") or {}
    if not isinstance(step.set, dict):
        raise ApplyError("`set:` is a mapping of field -> value")
    step.rows = {op: rest[op] for op in ROWS if op in rest}
    for op, paths in step.rows.items():
        if not isinstance(paths, dict):
            raise ApplyError(f"`{op}:` is a mapping of list path -> rows")
        for path, spec in paths.items():
            _check_rows(op, str(path), spec)
    step.rename = {k: rest[k] for k in RENAMES if k in rest}
    if action == "replace":
        if key != "tasks" or len(step.names) != 1 or emit.is_pattern(step.names[0], step.regex):
            raise ApplyError("replace names one task: `replace task: OLD` with `with: NEW`")
        if not isinstance(rest.get("with"), str) or not rest["with"]:
            raise ApplyError("replace needs `with:` the task to put in its place")
        within = rest.get("in", "*")
        within = [within] if isinstance(within, str) else within
        if not isinstance(within, list) or not all(isinstance(w, str) for w in within):
            raise ApplyError("`in:` is a workflow name, a list, or a pattern")
        step.replace_with, step.within = rest["with"], within
    if action == "update" and not step.set and not step.rows:
        raise ApplyError("update needs `set:`, `add:`, `remove:` or `change:`")
    if action == "rename" and len(step.rename) != 1:
        raise ApplyError("rename needs one of `to:`, `prefix:`, `suffix:` or `replace:`")
    replace = step.rename.get("replace")
    if replace is not None and not (isinstance(replace, dict) and set(replace) == {"from", "to"}):
        raise ApplyError("`replace:` is `{from: REGEX, to: TEXT}`")
    return step


def _row_step(number: int, action: str, head: str, raw: dict[str, Any], path: str) -> Step:
    """`update email action: TASKS` + `where:` (rows) + `set:` / `edit:`; or delete."""
    if action not in ("update", "delete"):
        raise ApplyError(f"{head.split()[1:]} rows can be updated or deleted")
    rest = {k: v for k, v in raw.items() if k != head}
    allowed = {"type", "regex", "comment", "where", "from"} | (
        {"set", "edit"} if action == "update" else set()
    )
    extra = set(rest) - allowed
    if extra:
        raise ApplyError(f"{action} of rows takes no {', '.join(sorted(map(str, extra)))}")
    if "where" not in rest:
        raise ApplyError("say which rows with `where:`, e.g. {to: {contains: name}}")
    names = raw[head]
    names = [names] if isinstance(names, str) else names
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ApplyError("name the tasks to look in: a name, a list, or a pattern ('*')")
    step = Step(number, action, KIND_BY_KEY["tasks"], list(names), bool(rest.get("regex")))
    step.type, step.row_path = rest.get("type"), path
    step.source = _source("tasks", rest.get("from"))
    step.set = rest.get("set") or {}
    step.edit = rest.get("edit") or {}
    if action == "update" and not step.set and not step.edit:
        raise ApplyError("an update of rows needs `set:` or `edit:`")
    try:
        step.where = where.parse(rest["where"])
    except where.WhereError as error:
        raise ApplyError(str(error)) from error
    _check_edit(step.edit)
    change: dict[str, Any] = {"where": rest["where"]}
    if action == "delete":
        step.rows = {"remove": {path: rest["where"]}}
    else:
        change.update({k: v for k, v in (("set", step.set), ("edit", step.edit)) if v})
        step.rows = {"change": {path: change}}
    step.set = {}
    return step


def _user_step(number: int, head: str, raw: dict[str, Any]) -> Step:
    """`create user: NAME` + `password_env:` + `set:` (user fields) + `groups:`."""
    rest = {k: v for k, v in raw.items() if k != head}
    fields = rest.get("set") or {}
    if {"password", "userPassword"} & set(rest) or (
        isinstance(fields, dict) and "userPassword" in fields
    ):
        raise ApplyError("a password never belongs in a file; name a variable: `password_env: X`")
    extra = set(rest) - USER_KEYS
    if extra:
        raise ApplyError(f"create user takes no {', '.join(sorted(map(str, extra)))}")
    name = raw[head]
    if not isinstance(name, str) or not name.strip():
        raise ApplyError("create user names one user: `create user: jdoe`")
    env = rest.get("password_env")
    if not isinstance(env, str) or not ENV_NAME.match(env):
        raise ApplyError("create user needs `password_env:` the environment variable to read")
    if not isinstance(fields, dict):
        raise ApplyError("`set:` is a mapping of user field -> value")
    unknown = sorted(str(k) for k in fields if k not in user_fields())
    if unknown:
        raise ApplyError(
            f"a user has no field {', '.join(map(repr, unknown))}; "
            f"use {', '.join(sorted(user_fields()))}"
        )
    groups = rest.get("groups") or []
    groups = [groups] if isinstance(groups, str) else groups
    if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
        raise ApplyError("`groups:` is a user group name or a list of them")
    step = Step(number, "create", USERS, [name])
    step.set, step.password_env, step.groups = fields, env, list(dict.fromkeys(groups))
    return step


@cache
def user_fields() -> frozenset[str]:
    """What `set:` may hold on a new user: UAC's UserWsData, from data/openapi.json."""
    spec = json.loads((data_root() / "openapi.json").read_text())
    schemas = (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}
    props = (schemas.get("UserWsData") or {}).get("properties") or {}
    return frozenset(props) - USER_NOT_SET


def _source(kind_key: str, raw: Any) -> dict[str, str]:
    """`from: {workflowname: X}`: filters `uac <group> list-advanced` takes."""
    if raw is None:
        return {}
    known = server_filters(kind_key)
    if not known:
        raise ApplyError(f"`from:` needs list-advanced filters; {kind_key} has none")
    if not isinstance(raw, dict) or not raw or any(
        isinstance(v, (dict, list)) or v is None for v in raw.values()
    ):
        raise ApplyError("`from:` is a mapping of filter -> value, e.g. {workflowname: X}")
    unknown = [str(k) for k in raw if k not in known]
    if unknown:
        raise ApplyError(
            f"`from:` {kind_key} has no filter {', '.join(map(repr, unknown))}; "
            f"use one of {', '.join(known)}"
        )
    return {str(k): str(v).lower() if isinstance(v, bool) else str(v) for k, v in raw.items()}


def _check_edit(edit: Any) -> None:
    if not isinstance(edit, dict):
        raise ApplyError("`edit:` is a mapping of field -> {from: REGEX, to: TEXT}")
    for name, how in edit.items():
        if not (isinstance(how, dict) and set(how) == {"from", "to"}):
            raise ApplyError(f"edit {name}: give {{from: REGEX, to: TEXT}}")
        try:
            re.compile(str(how["from"]))
        except re.error as error:
            raise ApplyError(
                f"edit {name}: {how['from']!r} is not a valid regex: {error}"
            ) from error


def _check_rows(op: str, path: str, spec: Any) -> None:
    if op == "add" and not (
        isinstance(spec, list) and spec and all(isinstance(r, dict) for r in spec)
    ):
        raise ApplyError(f"add {path}: give a list of rows")
    if op == "remove" and not isinstance(spec, dict):
        raise ApplyError(f"remove {path}: give the fields a row must match")
    if op == "change" and not (
        isinstance(spec, dict)
        and "where" in spec
        and set(spec) - {"where"} <= {"set", "edit"}
        and set(spec) - {"where"}
        and all(isinstance(v, dict) for v in spec.values())
    ):
        raise ApplyError(f"change {path}: give `where:` and `set:` or `edit:`")
    try:
        where.parse(spec if op == "remove" else spec["where"]) if op != "add" else None
    except where.WhereError as error:
        raise ApplyError(f"{op} {path}: {error}") from error
    if op == "change":
        _check_edit(spec.get("edit") or {})


# --- plan ---------------------------------------------------------------------------


@dataclass
class Change:
    step: Step
    kind: Kind
    name: str
    """The name the object has when this change runs."""
    action: str
    """delete, update, rename, or `no change`."""
    new_name: str | None = None
    payload: dict[str, Any] | None = None
    """What an update or rename sends: the whole object, sysId included."""
    lines: list[str] = field(default_factory=list)
    """What changes, and warnings, one per line."""
    origin: str = ""
    """The name the object has on the controller before the file runs."""
    groups: list[str] = field(default_factory=list)
    """`create user`: the groups to add the user to, after it is made."""
    password_env: str = ""
    """`create user`: read again when the write runs; the password is never kept."""
    commands: list[Any] = field(default_factory=list)
    """Run after the update: fields only a command sets (`webhook enable`)."""


class Planner:
    """The steps in order, over an overlay of what the steps before them do."""

    def __init__(
        self,
        transport: Transport,
        templates: TemplateSet,
        ext: ExtensionMap | None,
        *,
        parallel: int = 1,
        echo: Echo = lambda _: None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.transport = transport
        self.environ = os.environ if environ is None else environ
        self.templates = templates
        self.ext = ext
        self.parallel = parallel
        self.echo = echo
        self.state: dict[tuple[str, str], dict[str, Any] | None] = {}
        """(kind, name) -> the object as the steps before leave it; None: gone."""
        self.listed: dict[str, dict[str, emit.Listed]] = {}
        """kind -> name -> listing, kept up to date with deletes and renames."""
        self.live: dict[tuple[str, str], dict[str, Any]] = {}
        """(kind, name) -> the object as the controller had it: what a backup saves."""
        self.origin: dict[tuple[str, str], str] = {}
        """(kind, name now) -> its name on the controller, for renamed objects."""

    def plan(self, changes: ChangeFile) -> list[Change]:
        out: list[Change] = []
        errors: list[str] = []
        for step in changes.steps:
            try:
                out += self._step(step, changes.templates)
            except (ApplyError, CompileError) as error:
                errors.append(f"{step.label}: {error}")
        if errors:
            raise ApplyError("\n".join(errors))
        return out

    # -- names and objects

    def _names(self, step: Step) -> list[str]:
        patterns = [n for n in step.names if emit.is_pattern(n, step.regex)]
        exact = [n for n in step.names if n not in patterns]
        if step.source:
            return self._from(step, patterns, exact)
        if not patterns:
            if step.type is not None:
                raise ApplyError("`type:` narrows a pattern; exact names need none")
            return exact
        listing = self._listing(step.kind)
        allowed = self._types(step)
        tests = [emit.matcher(p, step.regex) for p in patterns]
        found = [
            name
            for name, item in listing.items()
            if (allowed is None or item.type in allowed) and any(t(name) for t in tests)
        ]
        if not found and not exact:
            raise ApplyError(f"nothing matches {', '.join(repr(p) for p in patterns)}")
        return list(dict.fromkeys([*exact, *found]))

    def _from(self, step: Step, patterns: list[str], exact: list[str]) -> list[str]:
        """`from:`: the candidates are what list-advanced answers, in one call.

        The answer is the controller before the file runs: an object an
        earlier step deleted is dropped, one it renamed is not found under
        its new name. The objects are complete, so none is read again.
        """
        rows = self.transport.list_kind(step.kind, step.source)
        shown = ", ".join(f"{k}={v}" for k, v in step.source.items())
        self.echo(f"   from {shown}: {len(rows)} {step.kind.key}")
        found: dict[str, dict[str, Any]] = {}
        for obj in rows:
            key = (step.kind.key, object_name(obj))
            if key not in self.state:
                self.state[key] = obj
                self.live.setdefault(key, obj)
            now = self.state[key]
            if now is not None:
                found[key[1]] = now
        missing = [n for n in exact if n not in found]
        if missing:
            raise ApplyError(f"not in from ({shown}): {', '.join(map(repr, missing))}")
        allowed = self._types(step)
        tests = [emit.matcher(p, step.regex) for p in patterns]
        picked = [
            name
            for name, obj in found.items()
            if (allowed is None or obj.get("type") in allowed)
            and (name in exact or any(t(name) for t in tests))
        ]
        if not picked:
            raise ApplyError(f"nothing from ({shown}) matches {', '.join(map(repr, step.names))}")
        return picked

    def _types(self, step: Step) -> frozenset[str] | None:
        if step.type is None:
            return None
        allowed = emit.type_filter(step.type)
        if step.kind.key not in allowed:
            raise ApplyError(f"type {step.type!r} is not a type of {step.kind.key}")
        return allowed[step.kind.key]

    def _listing(self, kind: Kind) -> dict[str, emit.Listed]:
        if kind.key not in self.listed:
            items = emit.list_names({kind.key: None}, self.transport, parallel=1, echo=self.echo)
            listing = {i.name: i for i in items}
            for (key, name), obj in self.state.items():  # what earlier steps did
                if key == kind.key and obj is None:
                    listing.pop(name, None)
                elif key == kind.key and name not in listing and obj is not None:
                    listing[name] = emit.Listed(kind, name, str(obj.get("type") or ""))
            self.listed[kind.key] = listing
        return self.listed[kind.key]

    def _read(self, kind: Kind, names: list[str]) -> dict[str, dict[str, Any] | None]:
        missing = [n for n in names if (kind.key, n) not in self.state]
        if missing:
            found = emit.read_objects(
                kind, missing, self.transport, parallel=self.parallel, echo=self.echo
            )
            for name, obj in found.items():
                self.state[(kind.key, name)] = obj
                if obj is not None:
                    self.live.setdefault((kind.key, name), obj)
        return {n: self.state[(kind.key, n)] for n in names}

    def _origin(self, kind: Kind, name: str) -> str:
        return self.origin.get((kind.key, name), name)

    def _gone(self, kind: Kind, name: str) -> None:
        self.state[(kind.key, name)] = None
        self.listed.get(kind.key, {}).pop(name, None)

    def _put(self, kind: Kind, name: str, obj: dict[str, Any]) -> None:
        self.state[(kind.key, name)] = obj
        if kind.key in self.listed:
            self.listed[kind.key][name] = emit.Listed(kind, name, str(obj.get("type") or ""))

    # -- one step

    def _step(self, step: Step, shared: dict[str, Any]) -> list[Change]:
        if step.action == "replace":
            return self._replace(step)
        if step.action == "create":
            return [self._create_user(step)]
        names = self._names(step)
        stored = self._read(step.kind, names)
        if step.where and not step.row_path:  # a row kind's where tests rows
            passing = [
                n
                for n in names
                if stored[n] is None or where.holds(step.where, stored[n] or {}, step.kind.key)
            ]
            self.echo(f"   {len(passing)} of {len(names)} pass the where filter")
            if not passing:
                raise ApplyError(f"nothing passes where: {', '.join(map(str, step.where))}")
            names = passing
        if step.rename.get("to") is not None and len(names) != 1:
            raise ApplyError(f"`to:` renames one object; this step names {len(names)}")
        absent = [n for n in names if stored[n] is None]
        if absent:
            raise ApplyError(
                f"not on the controller: {', '.join(map(repr, absent))} (apply never creates)"
            )
        out: list[Change] = []
        taken: set[str] = set()
        for name in names:
            obj = stored[name]
            assert obj is not None
            if step.row_path:  # rows as objects: only the tasks that hold one
                change = self._update(step, name, name, obj, shared)
                if not any(NO_ROW in line for line in change.lines):
                    out.append(change)
                continue
            if step.action == "delete":
                if step.kind.key == "tasks":
                    self._not_in_a_workflow(name, deleting=set(names))
                origin = self.origin.pop((step.kind.key, name), name)
                out.append(Change(step, step.kind, name, "delete", origin=origin))
                self._gone(step.kind, name)
                continue
            new = self._new_name(step, name) if step.action == "rename" else name
            if new != name:
                if new in taken or self._read(step.kind, [new])[new] is not None:
                    raise ApplyError(f"cannot rename {name!r}: {new!r} exists already")
                taken.add(new)
            out.append(self._update(step, name, new, obj, shared))
        if step.row_path and not out:
            tests = ", ".join(map(str, step.where))
            raise ApplyError(f"no task here has a {step.row_path} row where {tests}")
        return out

    def _replace(self, step: Step) -> list[Change]:
        """Every workflow `in:` names that runs the old task gets the new one.

        Only the vertex's `task` changes: its id, place, alias and the edges
        (which join vertex ids) stay, so the workflow keeps its shape.
        """
        old, new = step.names[0], step.replace_with
        if self._read(step.kind, [new])[new] is None:
            raise ApplyError(f"{new!r} is not on the controller (apply never creates)")
        tests = [emit.matcher(w, step.regex) for w in step.within if emit.is_pattern(w, step.regex)]
        exact = {w for w in step.within if not emit.is_pattern(w, step.regex)}
        workflows = [n for n in self._running(old) if n in exact or any(t(n) for t in tests)]
        self.echo(f"   {len(workflows)} workflow(s) in `in:` run {old!r}")
        out: list[Change] = []
        for name, stored in self._read(step.kind, workflows).items():
            if stored is None or not where.holds(step.where, stored, step.kind.key):
                continue
            # From the stored object, not normalize(): rows keep their sysId, so the
            # controller updates its run criteria rather than seeing new ones.
            fields, hits, follow = references.swap_task(stored, old, new)
            if not hits:
                continue
            obj = {"type": "taskWorkflow", "name": name, **fields}
            planned = Planned(step.kind.key, name, obj)
            decision = importer.Decision(planned, "update", [], stored)
            payload = importer.update_payload(decision, self.templates)
            self._put(step.kind, name, payload)
            line = f"vertex {old!r} -> {new!r}" + (f" ({hits} places)" if hits > 1 else "")
            said = [line] + [f"{field}: {n} follow" for field, n in follow.items() if n]
            origin = self._origin(step.kind, name)
            out.append(Change(step, step.kind, name, "replace", name, payload, said, origin))
        if not out:
            raise ApplyError(f"no workflow in {step.within} runs {old!r}")
        return out

    def _create_user(self, step: Step) -> Change:
        """A new user: not on the controller, its password set, its groups there."""
        name = step.names[0]
        made = (USERS.key, name) in self.state
        if made or self.transport.get_user(name) is not None:
            raise ApplyError(f"user {name!r} exists already (create user makes new users)")
        if not self.environ.get(step.password_env):
            raise ApplyError(f"set the password in ${step.password_env} first; it is empty")
        groups = KIND_BY_KEY["user_groups"]
        absent = [g for g, obj in self._read(groups, step.groups).items() if obj is None]
        if absent:
            raise ApplyError(f"no user group {', '.join(map(repr, absent))} on the controller")
        # A new user can log in (active) and must change the password first.
        user = {"userName": name, "active": True, "passwordNeedsReset": True, **step.set}
        self.state[(USERS.key, name)] = user
        lines = [f"password from ${step.password_env}"]
        lines += [f"{k}: {v}" for k, v in user.items() if k != "userName"]
        lines += [f"+ group {g}" for g in step.groups]
        change = Change(step, USERS, name, "create", payload=user, lines=lines, origin=name)
        change.groups, change.password_env = step.groups, step.password_env
        return change

    def _running(self, task: str) -> list[str]:
        """The workflows that run `task` once the steps before this one ran.

        The controller answers for itself (`workflows_of`, one call); the
        workflows an earlier step deleted or changed are read from the overlay.
        """
        kind = KIND_BY_KEY["tasks"]
        live = self.transport.workflows_of(task)
        touched = [
            name
            for (key, name), obj in self.state.items()
            if key == "tasks" and (obj is None or obj.get("type") == "taskWorkflow")
        ]
        out = []
        for name in dict.fromkeys([*live, *touched]):
            if (kind.key, name) not in self.state:
                out.append(name)  # untouched: the controller's answer stands
                continue
            wf = self.state[(kind.key, name)]
            if wf is not None and any(
                isinstance(v, dict)
                and isinstance(v.get("task"), dict)
                and v["task"].get("value") == task
                for v in wf.get("workflowVertices") or []
            ):
                out.append(name)
        return out

    def _not_in_a_workflow(self, task: str, deleting: set[str]) -> None:
        """A task a workflow still runs is not deleted.

        Over the overlay: a workflow an earlier step deleted, or where a
        `replace` step already swapped the task out, does not count; nor does
        one this same step deletes too.
        """
        users = [w for w in self._running(task) if w not in deleting]
        if users:
            shown = ", ".join(map(repr, users[:5])) + (
                f" and {len(users) - 5} more" if len(users) > 5 else ""
            )
            raise ApplyError(
                f"cannot delete {task!r}: workflow(s) {shown} run it "
                "(replace it there first, or delete those workflows)"
            )

    def _new_name(self, step: Step, name: str) -> str:
        how, value = next(iter(step.rename.items()))
        if how == "to":
            return str(value)
        if how == "prefix":
            return f"{value}{name}"
        if how == "suffix":
            return f"{name}{value}"
        try:
            return re.sub(str(value["from"]), str(value["to"]), name, flags=re.IGNORECASE)
        except re.error as error:
            raise ApplyError(f"replace: {value['from']!r} is not a valid regex: {error}") from error

    def _update(
        self, step: Step, name: str, new: str, stored: dict[str, Any], shared: dict[str, Any]
    ) -> Change:
        section = step.kind.key
        body: dict[str, Any] = dict(step.set)
        if section in emit.TYPED:
            body = {"type": stored.get("type"), **body}
        obj = compile_object(section, new, body, self.templates, shared.get)
        lines: list[str] = []
        full = normalize(stored)
        for op in ROWS:
            for path, written in step.rows.get(op, {}).items():
                spec = self._as_bundle(op, section, stored, path, written, shared)
                rows = _rows_at(full, path)
                changed, said = _apply_rows(op, path, rows, spec)
                lines += said
                _put_path(obj, path, changed)
                _put_path(full, path, changed)
        planned = Planned(section, new, obj, was=name if new != name else None)
        findings = validate.errors(validate.static([planned], None, self.ext))
        if findings:
            raise ApplyError("; ".join(f"{name!r}: {f.message}" for f in findings))
        differences = importer.compare(obj, stored, self.ext)
        # A removed last row leaves an empty list, which compare reads as unstated:
        # the row count lines (+1 / -1 / ~1) are the change then.
        touched = bool(differences) or any(line[:1] in "+-~" for line in lines)
        action = "rename" if new != name else ("update" if touched else "no change")
        decision = importer.Decision(planned, "update", differences, stored)
        payload = importer.update_payload(decision, self.templates) if touched else None
        origin = self._origin(step.kind, name)
        if payload is not None:
            self._check_names(name, stored, payload)
            if new != name:
                self._gone(step.kind, name)
                self.origin.pop((step.kind.key, name), None)
                self.origin[(step.kind.key, new)] = origin
            self._put(step.kind, new, payload)
        needed = commands.needed(obj, stored) if payload is not None else []
        lines += [c.note(step.kind.group) for c in needed]
        change = Change(step, step.kind, name, action, new, payload, differences + lines, origin)
        change.commands = needed
        return change

    def _as_bundle(
        self,
        op: str,
        section: str,
        stored: dict[str, Any],
        path: str,
        spec: Any,
        shared: dict[str, Any],
    ) -> Any:
        """Rows as a bundle writes them, in UAC's own form.

        An `add:` row, and the `set:` of a changed row, may say `like:` (a
        `templates:` entry) and use the short forms export writes, such as
        `variables: [- name: value]`; each goes through `compile_object` as
        a row of a bundle does. `remove:` and `where:` test stored fields.
        """
        if op == "remove":
            return spec
        rows = spec if op == "add" else [spec.get("set") or {}]
        compiled: list[dict[str, Any]] = []
        for row in rows:
            body: dict[str, Any] = {}
            if section in emit.TYPED:
                body["type"] = stored.get("type")
            _put_path(body, path, [apply_like(row, shared.get)])
            obj = compile_object(section, str(stored.get("name") or ""), body, self.templates)
            compiled += _rows_at(obj, path) or [{}]
        if op == "add":
            return compiled
        return {**spec, "set": compiled[0]} if spec.get("set") else spec

    def _check_names(self, name: str, stored: dict[str, Any], payload: dict[str, Any]) -> None:
        """Every object the change makes this one name must exist.

        Only the names the change adds are read: a name the object already
        held is the controller's business. Agents and users are not objects
        a change file can check; the controller still refuses a wrong one.
        """
        from uac_compose import references

        type_name = str(payload.get("type", ""))
        before = {(k, n) for _, k, n in references.named(type_name, normalize(stored))}
        after = {(k, n) for _, k, n in references.named(type_name, normalize(payload))}
        missing = [
            f"{kind[:-1]} {ref!r}"
            for kind, ref in sorted(after - before)
            if kind in KIND_BY_KEY and self._read(KIND_BY_KEY[kind], [ref])[ref] is None
        ]
        if missing:
            raise ApplyError(
                f"{name!r} would name {', '.join(missing)}, which is not on the controller"
            )


# --- rows of a child list -------------------------------------------------------------


def _rows_at(obj: dict[str, Any], path: str) -> list[dict[str, Any]]:
    node: Any = obj
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if node is None:
        return []
    if not isinstance(node, list):
        raise ApplyError(f"{path} is not a list of rows")
    return [dict(r) for r in node if isinstance(r, dict)]


def _put_path(obj: dict[str, Any], path: str, value: Any) -> None:
    *head, last = path.split(".")
    node = obj
    for part in head:
        node = node.setdefault(part, {})
    node[last] = value


def _edited(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    out = {**row, **(spec.get("set") or {})}
    for name, how in (spec.get("edit") or {}).items():
        if out.get(name) is not None:
            out[name] = re.sub(str(how["from"]), str(how["to"]), str(out[name]), flags=re.I)
    return out


def _apply_rows(
    op: str, path: str, rows: list[dict[str, Any]], spec: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows match with the `where` tests: a wildcard, a list, not, regex, contains."""
    list_name = path.split(".")[-1]
    if op == "add":
        return rows + [copy.deepcopy(r) for r in spec], [f"+{len(spec)} {list_name}"]
    written = spec if op == "remove" else spec["where"]
    tests = where.parse(written)
    hits = [i for i, r in enumerate(rows) if where.holds(tests, r, "")]
    if not hits:
        return rows, [f"?? {op} {list_name}: {NO_ROW} {written}"]
    if op == "remove":
        return [r for i, r in enumerate(rows) if i not in hits], [f"-{len(hits)} {list_name}"]
    changed = [_edited(r, spec) if i in hits else r for i, r in enumerate(rows)]
    return changed, [f"~{len(hits)} {list_name}"]


NO_ROW = "no row matches"


# --- backup ---------------------------------------------------------------------------

BACKUP_FORMATS = ("compose", "json")


@dataclass
class Saved:
    """One object the plan writes, as the controller has it before the file runs."""

    kind: Kind
    name: str
    obj: dict[str, Any]
    now: str | None
    """Its name once the file has run; None: deleted."""
    final: dict[str, Any] | None = None
    """The object once the file has run; None: deleted."""

    @property
    def after(self) -> str:
        if self.now is None:
            return "deleted"
        return "updated" if self.now == self.name else f"renamed to {self.now}"


def saved(plan: list[Change], planner: Planner) -> list[Saved]:
    """Every object the plan writes, once, in the order the plan first meets it."""
    now: dict[tuple[str, str], str | None] = {}
    kinds: dict[tuple[str, str], Kind] = {}
    for change in plan:
        if change.action in ("no change", "create"):  # a new user has no old state
            continue
        key = (change.kind.key, change.origin or change.name)
        kinds[key] = change.kind
        now[key] = None if change.action == "delete" else (change.new_name or change.name)
    return [
        Saved(
            kinds[key],
            key[1],
            planner.live[key],
            name,
            None if name is None else planner.state.get((key[0], name)),
        )
        for key, name in now.items()
    ]


def backup_format(path: str, chosen: str | None) -> str:
    """The format asked for, or from the file name: `.json` is json, else compose."""
    if chosen:
        return chosen
    return "json" if path.lower().endswith(".json") else "compose"


def backup_json(items: list[Saved], profile: str, source: str) -> str:
    """The objects as the controller returned them, secrets taken out (rule 8)."""
    document = {
        "profile": profile,
        "file": source,
        "written": datetime.now().isoformat(timespec="seconds"),
        "objects": [
            {"kind": i.kind.key, "name": i.name, "after": i.after, "object": _no_secrets(i.obj)}
            for i in items
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _no_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _no_secrets(v) for k, v in value.items() if k not in validate.SECRETS}
    if isinstance(value, list):
        return [_no_secrets(v) for v in value]
    return value


def backup_compose(
    items: list[Saved],
    ws: Any,
    transport: Transport,
    templates: TemplateSet,
    ext: ExtensionMap | None,
    *,
    parallel: int = 1,
    echo: Echo = lambda _: None,
) -> tuple[str, list[str]]:
    """An `export --purpose update` bundle of the objects, each `was:` its name
    after the change: `import -u` of it undoes the file. An updated object
    is updated back, a renamed one renamed back (same sysId), and a deleted
    one, whose `was:` is gone from the controller, made again.

    Import compares only what a file states (rule 6), and an export leaves
    defaults out. So every field the plan changes is stated with its old
    value, default or not, in the standard style (UAC's own field names, so
    a stated field never meets an idiom). What import still cannot undo -- a
    row added to a list, since import never removes rows -- is a warning.
    """
    text, warnings = emit.export_text(
        [],
        None,
        ws,
        transport,
        templates,
        style="standard",
        targets=[(i.name, [i.kind]) for i in items],
        purpose="update",
        was={(i.kind.key, i.name): i.now for i in items if i.now is not None},
        parallel=parallel,
        echo=echo,
    )
    document = yamlio.load(text)
    for item in items:
        body = (document.get(item.kind.key) or {}).get(item.name)
        if item.final is None or not isinstance(body, dict):
            continue  # deleted: import makes it again, whole
        before = normalize(item.obj)
        changed = [
            k
            for k in dict.fromkeys(_top(importer.compare(item.final, item.obj, ext)))
            if k not in ("sysId", "name")
        ]
        stated = _compiled(item, body, templates)
        for key in changed:
            if key not in stated and key in before:
                body[key] = before[key]
        undone = set(_top(importer.compare(_compiled(item, body, templates), item.final, ext)))
        # A scalar the controller did not return has no old value to state;
        # a real controller returns every field, so only a list empty before is left.
        after = normalize(item.final)
        left = [
            k
            for k in changed
            if k not in undone and (k in before or isinstance(after.get(k), (list, dict)))
        ]
        if left:
            warnings.append(
                f"backup: `import -u` cannot undo {', '.join(left)} of "
                f"{item.kind.key}/{item.name}: import never removes a row the file "
                "does not state (a .json backup keeps the old object)"
            )
    return yamlio.dump(document), warnings


def _top(lines: list[str]) -> list[str]:
    """The top-level field of each `compare` line: `actions.x[0].to: ...` -> actions."""
    return [re.split(r"[.:\[ ]", line, maxsplit=1)[0] for line in lines]


def _compiled(item: Saved, body: dict[str, Any], templates: TemplateSet) -> dict[str, Any]:
    written = {k: v for k, v in body.items() if k != yamlio.WAS}
    return compile_object(item.kind.key, item.name, written, templates)


# --- print and run --------------------------------------------------------------------


def describe(change: Change) -> list[str]:
    head = f"[{change.step.number}] {change.action:9s} {change.kind.key}/{change.name}"
    if change.action == "rename":
        head += f" -> {change.new_name}"
    return [head, *(f"      {line}" for line in change.lines if not line.startswith("name:"))]


def run(
    changes: list[Change],
    transport: Transport,
    echo: Echo,
    environ: Mapping[str, str] | None = None,
) -> None:
    """One write at a time, in order; the first error stops the rest."""
    env = os.environ if environ is None else environ
    todo = [c for c in changes if c.action != "no change"]
    for n, change in enumerate(todo, 1):
        try:
            if change.action == "create":
                _create_user(change, transport, env)
            elif change.action == "delete":
                transport.delete(change.kind, change.name)
            else:
                assert change.payload is not None
                transport.update(change.payload)
                for command in change.commands:
                    transport.command(
                        change.kind, change.new_name or change.name, command.verb, command.args
                    )
        except TransportError as error:
            ran = f"{n - 1} of {len(todo)} change(s) ran" if n > 1 else "nothing ran"
            raise ApplyError(
                f"{error}\n{change.step.label} failed on {change.kind.key}/{change.name}; "
                f"{ran}; the rest were not run"
            ) from error
        target = f" -> {change.new_name}" if change.action == "rename" else ""
        echo(f"[{n}/{len(todo)}] {change.action}d {change.kind.key}/{change.name}{target}")


def _create_user(change: Change, transport: Transport, env: Mapping[str, str]) -> None:
    """The password is read here, sent once, and kept nowhere."""
    password = env.get(change.password_env)
    if not password:
        raise TransportError(f"${change.password_env} is empty; the user was not made")
    assert change.payload is not None
    transport.create_user({**change.payload, "userPassword": password})
    for group in change.groups:
        transport.add_user_to_group(change.name, group)


def object_count(changes: list[Change]) -> int:
    return len({(c.kind.key, c.name) for c in changes if c.action != "no change"})
