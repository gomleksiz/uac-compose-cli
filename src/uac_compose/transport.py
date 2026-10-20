"""Talking to a controller through the `uac` CLI (SPEC §3).

Rules that each cost a bug in uac-ai:
- the name argument of `get` is a table, not a rule (`NAME_ARGS`);
- connections sit two levels deep (`uac connection database get`);
- absent only on `Response Code : 404` / "does not exist"; `No usage found
  for argument` is a malformed call; anything else raises with the FIRST
  stderr line (the rest is a usage listing);
- `business-service create` needs `name=` beside `--input`;
- the routing key never reaches the controller.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from uac_compose import routing
from uac_compose.paths import data_root


class TransportError(Exception):
    """A call to the controller failed (not: the object is absent)."""


@dataclass(frozen=True)
class Kind:
    """One kind of object and the `uac` group that answers for it."""

    key: str
    """Cache file and bundle section name (`tasks`, `credentials`...)."""
    group: str
    """`uac` group; two words for connections."""
    verb: str = "list"
    """The list verb that returns complete definitions."""
    routing: str | None = None
    """Routing `type` to stamp; None where the object carries its own discriminator."""
    own_type: bool = False
    """The object's `type` is its own data: move it to `_uacType` before stamping."""
    name_key: str = "name"

    def stamp(self, obj: dict[str, Any]) -> dict[str, Any]:
        if self.routing is None:
            return obj
        if self.own_type:
            return routing.stamp(obj, self.routing)
        return {**obj, "type": self.routing}


KINDS: tuple[Kind, ...] = (
    Kind("tasks", "task", verb="list-advanced"),
    Kind("triggers", "trigger", verb="list-advanced"),
    Kind("variables", "variable", "list-advanced", "variable", own_type=True),
    Kind("virtual_resources", "virtual-resource", "list-advanced", "virtualResource", True),
    Kind("credentials", "credential", routing="credential", own_type=True),
    Kind("business_services", "business-service", routing="businessService"),
    # `list-advanced` answers in the export shape and stamps each cluster's own
    # type (unixAgentCluster...), as it does for tasks.
    Kind("agent_clusters", "agent-cluster", verb="list-advanced"),
    # `user-group list` is complete but carries no type at all.
    Kind("user_groups", "user-group", routing="userGroup"),
    Kind("calendars", "calendar", routing="calendar"),
    Kind("custom_days", "custom-day", routing="customDay"),
    Kind("scripts", "script", routing="script", name_key="scriptName"),
    Kind("event_templates", "universal-event-template", routing="universalEventTemplate"),
    Kind("database_connections", "connection database", routing="databaseConnection"),
    Kind("peoplesoft_connections", "connection peoplesoft", routing="peoplesoftConnection"),
    Kind("sap_connections", "connection sap", routing="sapConnection"),
    Kind("email_connections", "connection email", routing="emailConnection", own_type=True),
    # `webhook list` is complete and carries no type.
    Kind("webhooks", "webhook", routing="webhook"),
)
KIND_BY_KEY = {k.key: k for k in KINDS}
_KIND_BY_ROUTING = {k.routing: k for k in KINDS if k.routing}

# `uac <group> get` name argument, where it is not `<group without dashes>name`.
NAME_ARGS: dict[str, str] = {
    "universal-event-template": "templatename",
    "virtual-resource": "resourcename",
    "business-service": "busservicename",
    "connection database": "connectionname",
    "connection peoplesoft": "connectionname",
    "connection sap": "connectionname",
    "connection email": "connectionname",
    "universal-template": "templatename",
    "bundle": "bundlename",
    "user-group": "groupname",
}

# The extension group is not a bundle section: an extension is the schema
# behind a taskUniversal, not an object a bundle creates.
TEMPLATE_GROUP = "universal-template"

#: UAC's own Bundle: a promotion unit, not an object a `.uac.yaml` file creates.
BUNDLE_GROUP = "bundle"


def name_arg(group: str) -> str:
    return NAME_ARGS.get(group) or f"{group.replace('-', '')}name"


def kind_for_type(type_name: str) -> Kind:
    """The kind of a routing type: `taskUnix` and `taskWorkflow` are tasks."""
    if type_name in _KIND_BY_ROUTING:
        return _KIND_BY_ROUTING[type_name]
    if type_name.endswith("AgentCluster"):
        return KIND_BY_KEY["agent_clusters"]
    if type_name.startswith("trigger"):
        return KIND_BY_KEY["triggers"]
    if type_name.startswith("task"):
        return KIND_BY_KEY["tasks"]
    raise TransportError(f"no uac group for type {type_name!r}")


def object_name(obj: dict[str, Any]) -> str:
    """A script keeps its name in `scriptName`; everything else in `name`."""
    return str(obj.get("scriptName") or obj.get("name") or "")


class Transport(Protocol):
    def list_kind(
        self, kind: Kind, filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Every object of the kind, complete, with the routing key stamped.
        `filters` go to `list-advanced` as `name=value` (`server_filters`)."""
        ...

    def list_raw(self, group: str) -> list[dict[str, Any]]:
        """`uac <group> list` as it answers (agents, clusters, universal templates)."""
        ...

    def get(self, type_name: str, name: str) -> dict[str, Any] | None:
        """The stored object, stamped; None only when the controller says it is absent."""
        ...

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None: ...

    def create(self, obj: dict[str, Any]) -> None: ...

    def update(self, obj: dict[str, Any]) -> None: ...

    def delete(self, kind: Kind, name: str) -> None:
        """Delete the object called `name` of this kind; absent is an error."""
        ...

    def workflows_of(self, task: str) -> list[str]:
        """The workflows that run `task` (none when there is no such task)."""
        ...

    def get_template(self, name: str) -> dict[str, Any] | None:
        """One Universal Extension as `universal-template get` answers, or None."""
        ...

    def export_template(self, name: str, path: Path) -> None:
        """Write the extension's export zip to `path`."""
        ...

    def import_template(self, path: Path) -> None:
        """Install or replace an extension from its zip. Upserts by name."""
        ...

    def bundle_report(self, name: str) -> dict[str, Any] | None:
        """What a UAC Bundle resolves to, or None when there is no such Bundle."""
        ...

    def get_user(self, name: str) -> dict[str, Any] | None:
        """The user called `name`, or None when the controller has no such user."""
        ...

    def create_user(self, user: dict[str, Any]) -> None:
        """Create a user. `user` holds its password: never print or keep it."""
        ...

    def add_user_to_group(self, user: str, group: str) -> None: ...

    def command(self, kind: Kind, name: str, verb: str, args: dict[str, str]) -> None:
        """`uac <group> <verb>` on one object: what sets a field create and update
        do not store (data/rules/commands.yaml), e.g. `webhook enable`."""
        ...


def in_parallel[T, R](
    items: Sequence[T],
    call: Callable[[T], R],
    parallel: int,
    done: Callable[[int, R, float], None],
) -> list[R]:
    """`call` on each item, `parallel` at a time; the results in the order given.

    Each `uac` call is a new process and a round trip (0.5 s on a real
    controller), and the calls of one step do not depend on each other.
    `done(index, result, seconds)` runs on this thread as each one finishes,
    so progress lines come as the answers do while what is kept stays in
    order. A failed call, or Ctrl-C, stops the ones not yet started.
    """

    def timed(item: T) -> tuple[R, float]:
        at = time.monotonic()
        result = call(item)
        return result, time.monotonic() - at

    results: dict[int, R] = {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        running = {pool.submit(timed, item): i for i, item in enumerate(items)}
        try:
            for future in as_completed(running):
                i = running[future]
                result, seconds = future.result()
                results[i] = result
                done(i, result, seconds)
        except BaseException:
            for future in running:
                future.cancel()
            raise
    return [results[i] for i in range(len(items))]


# --- the uac CLI -------------------------------------------------------------

NOT_FOUND = ("Response Code : 404", "does not exist")
MALFORMED = "No usage found for argument"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the `uac` CLI, found as `uac_command` says.

    Its answer is read as UTF-8, and it is asked to write UTF-8: on Windows a
    pipe otherwise gets the ANSI code page, and a name outside it fails.
    """
    command = [uac_command(), *argv[1:]] if argv and argv[0] == "uac" else list(argv)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=900,
    )


def uac_command() -> str:
    """The `uac` CLI: `UAC_COMPOSE_UAC` (a path), else `uac` found on PATH.

    Found with `shutil.which`, which on Windows tries each PATHEXT ending
    (uac.exe, uac.cmd, uac.bat). A bare "uac" is not enough there: Windows
    starts a program without PATHEXT and looks in Python's own folder first.
    """
    explicit = os.environ.get("UAC_COMPOSE_UAC")
    if explicit:
        found = shutil.which(explicit)  # a full path, or a name on PATH
        if found is None:
            raise TransportError(f"UAC_COMPOSE_UAC={explicit}: no such program")
        return found
    found = shutil.which("uac")
    if found is None:
        raise TransportError(
            "the uac CLI was not found on PATH; add its folder to PATH, or set "
            "UAC_COMPOSE_UAC to its full path, e.g. C:\\Tools\\uac.exe"
        )
    return found


#: The `uac` CLI logs through Python's logging: every line starts like this.
LOG_PREFIX = re.compile(r"^(ERROR|WARNING|INFO):[\w.]*:")
FAILED_WITH = re.compile(r"^Failed with reason\s*:\s*", re.IGNORECASE)
STATUS = re.compile(r"Response Code\s*:\s*(\d{3})")


def reason(said: str) -> str:
    """What went wrong, in the controller's own words.

    The `uac` CLI answers a refused write with two lines:

        ERROR:root:https://host/resources/task Response Code : 400
        ERROR:root:Failed with reason : <what the controller said>

    Only the first used to be shown, which names the URL and the status and
    never the cause. Measured on a live import: "Response Code : 400" and
    nothing else, while the second line said exactly which field was wrong.
    The cause is kept whole -- it is the one thing the reader needs -- and the
    status goes in front of it. Everything from `Usage:` on is a listing.
    """
    lines: list[str] = []
    for raw in said.splitlines():
        line = LOG_PREFIX.sub("", raw.strip()).strip()
        if line.startswith("Usage:"):
            break
        if line:
            lines.append(line)
    if not lines:
        return ""
    causes = [FAILED_WITH.sub("", line) for line in lines if FAILED_WITH.match(line)]
    status = next((m.group(1) for line in lines if (m := STATUS.search(line))), None)
    if causes:
        cause = " ".join(causes)
        return f"HTTP {status}: {cause}" if status else cause
    return lines[0]


def is_absent(said: str) -> bool:
    return MALFORMED not in said and any(marker in said for marker in NOT_FOUND)


@dataclass
class UacCli:
    profile: str
    runner: Runner = field(default=_run)

    def _call(self, group: str, verb: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one `uac` call. Every way it can fail comes back as TransportError.

        A call that never answers raises `subprocess.TimeoutExpired`, which is
        not an OSError and so used to travel straight past every caller: a
        single stuck export ended a freeze of 271 extensions with a traceback
        at 269, rather than costing that one extension a FAILED line.
        """
        argv = ["uac", "--profile", self.profile, *group.split(), verb, *args]
        try:
            return self.runner(argv)
        except subprocess.TimeoutExpired as error:
            seconds = f"{error.timeout:.0f}s" if error.timeout else "the timeout"
            raise TransportError(f"uac {group} {verb}: no answer after {seconds}") from error
        except OSError as error:
            raise TransportError(f"uac {group} {verb}: {error}") from error

    def _list(self, group: str, verb: str, *args: str) -> list[dict[str, Any]]:
        done = self._call(group, verb, *args, "-f", "json")
        text = (done.stdout or "").strip()
        if done.returncode != 0 or not text.startswith("["):
            said = (done.stderr or text).strip()
            raise TransportError(f"uac {group} {verb}: {reason(said) or f'exit {done.returncode}'}")
        rows: Any = json.loads(text)
        return [r for r in rows if isinstance(r, dict)]

    def list_kind(
        self, kind: Kind, filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        args = [f"{k}={v}" for k, v in (filters or {}).items()]
        return [kind.stamp(row) for row in self._list(kind.group, kind.verb, *args)]

    def list_raw(self, group: str) -> list[dict[str, Any]]:
        return self._list(group, "list")

    def get(self, type_name: str, name: str) -> dict[str, Any] | None:
        return self.get_in(kind_for_type(type_name), name)

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None:
        done = self._call(kind.group, "get", f"{name_arg(kind.group)}={name}")
        text = (done.stdout or "").strip()
        if not text.startswith("{"):
            said = (done.stderr or text).strip()
            if is_absent(said):
                return None
            raise TransportError(
                f"uac {kind.group} get {name!r}: {reason(said) or f'exit {done.returncode}'}"
            )
        try:
            payload: Any = json.loads(text)
        except ValueError as error:
            raise TransportError(f"uac {kind.group} get {name!r}: answer is not JSON") from error
        if not isinstance(payload, dict) or not object_name(payload):
            return None
        return kind.stamp(payload)

    def _write(self, verb: str, obj: dict[str, Any]) -> None:
        kind = kind_for_type(str(obj.get("type", "")))
        body = routing.restore(obj) if kind.routing else obj
        extra = (
            [f"name={object_name(obj)}"]
            if (kind.key, verb)
            == (
                "business_services",
                "create",
            )
            else []
        )
        self._send(kind.group, verb, body, object_name(obj), *extra)

    def _send(self, group: str, verb: str, body: dict[str, Any], name: str, *extra: str) -> None:
        """`uac <group> <verb> --input FILE`; the file is gone when this returns."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "object.json"
            path.write_text(json.dumps(body))
            done = self._call(group, verb, *extra, "--input", str(path))
        self._check(done, f"uac {group} {verb} {name!r}")

    @staticmethod
    def _check(done: subprocess.CompletedProcess[str], what: str) -> None:
        output = done.stdout or ""
        if done.returncode != 0 or "error" in output.lower():
            said = (done.stderr or output).strip()
            raise TransportError(f"{what}: {reason(said) or f'exit {done.returncode}'}")

    def get_user(self, name: str) -> dict[str, Any] | None:
        done = self._call("user", "get", f"username={name}")
        text = (done.stdout or "").strip()
        if not text.startswith("{"):
            said = (done.stderr or text).strip()
            if is_absent(said):
                return None
            raise TransportError(
                f"uac user get {name!r}: {reason(said) or f'exit {done.returncode}'}"
            )
        try:
            payload: Any = json.loads(text)
        except ValueError as error:
            raise TransportError(f"uac user get {name!r}: answer is not JSON") from error
        return payload if isinstance(payload, dict) and payload.get("userName") else None

    def create_user(self, user: dict[str, Any]) -> None:
        self._send("user", "create", user, str(user.get("userName", "")))

    def add_user_to_group(self, user: str, group: str) -> None:
        done = self._call("user", "add-group", f"username={user}", f"groupname={group}")
        self._check(done, f"uac user add-group {user!r} to {group!r}")

    def command(self, kind: Kind, name: str, verb: str, args: dict[str, str]) -> None:
        """Its answer is JSON that always holds `"errors"`, empty on success
        (`webhook enable`: `{"success": true, "errors": ""}`), so `_check`'s
        "error" in the text would fail every call."""
        extra = [f"{k}={v}" for k, v in args.items()]
        done = self._call(kind.group, verb, f"{name_arg(kind.group)}={name}", *extra)
        what = f"uac {kind.group} {verb} {name!r}"
        text = (done.stdout or "").strip()
        try:
            answer: Any = json.loads(text) if text.startswith("{") else None
        except ValueError:
            answer = None
        if isinstance(answer, dict):
            failed = answer.get("success") is False or bool(answer.get("errors"))
            if done.returncode == 0 and not failed:
                return
            said = str(answer.get("errors") or answer.get("info") or done.stderr or text)
            raise TransportError(f"{what}: {reason(said) or said}")
        self._check(done, what)

    def create(self, obj: dict[str, Any]) -> None:
        self._write("create", obj)

    def update(self, obj: dict[str, Any]) -> None:
        self._write("update", obj)

    def workflows_of(self, task: str) -> list[str]:
        """`uac task list-workflow-list`: 0.4 s, where reading every workflow of a
        large controller took 48 s (measured on 650 workflows)."""
        done = self._call("task", "list-workflow-list", f"taskname={task}", "-f", "json")
        text = (done.stdout or "").strip()
        if not text.startswith("["):
            said = (done.stderr or text).strip()
            if is_absent(said) or "not found" in said.lower():
                return []
            raise TransportError(
                f"uac task list-workflow-list {task!r}: {reason(said) or f'exit {done.returncode}'}"
            )
        rows: Any = json.loads(text)
        return list(
            dict.fromkeys(str(r["name"]) for r in rows if isinstance(r, dict) and r.get("name"))
        )

    def delete(self, kind: Kind, name: str) -> None:
        done = self._call(kind.group, "delete", f"{name_arg(kind.group)}={name}")
        output = done.stdout or ""
        if done.returncode != 0 or "error" in output.lower():
            said = (done.stderr or output).strip()
            raise TransportError(
                f"uac {kind.group} delete {name!r}: {reason(said) or f'exit {done.returncode}'}"
            )

    def get_template(self, name: str) -> dict[str, Any] | None:
        done = self._call(TEMPLATE_GROUP, "get", f"{name_arg(TEMPLATE_GROUP)}={name}")
        text = (done.stdout or "").strip()
        if not text.startswith("{"):
            said = (done.stderr or text).strip()
            if is_absent(said):
                return None
            raise TransportError(
                f"uac {TEMPLATE_GROUP} get {name!r}: {reason(said) or f'exit {done.returncode}'}"
            )
        payload: Any = json.loads(text)
        return payload if isinstance(payload, dict) and payload.get("name") else None

    def export_template(self, name: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        done = self._call(
            TEMPLATE_GROUP, "export", f"{name_arg(TEMPLATE_GROUP)}={name}", "-o", str(path)
        )
        if done.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            said = (done.stderr or done.stdout or "").strip()
            raise TransportError(
                f"uac {TEMPLATE_GROUP} export {name!r}: {reason(said) or f'exit {done.returncode}'}"
            )

    def bundle_report(self, name: str) -> dict[str, Any] | None:
        done = self._call(BUNDLE_GROUP, "get-report", f"{name_arg(BUNDLE_GROUP)}={name}")
        text = (done.stdout or "").strip()
        if not text.startswith("{"):
            said = (done.stderr or text).strip()
            if is_absent(said):
                return None
            raise TransportError(
                f"uac {BUNDLE_GROUP} get-report {name!r}: "
                f"{reason(said) or f'exit {done.returncode}'}"
            )
        try:
            payload: Any = json.loads(text)
        except ValueError as error:
            raise TransportError(
                f"bundle {name!r} answered with something that is not JSON"
            ) from error
        return payload if isinstance(payload, dict) else None

    def import_template(self, path: Path) -> None:
        done = self._call(TEMPLATE_GROUP, "import", "-i", str(path))
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip()
            raise TransportError(
                f"uac {TEMPLATE_GROUP} import {path.name}: "
                f"{reason(said) or f'exit {done.returncode}'}"
            )


# --- list-advanced filters (data/rules/list_filters.yaml) -------------------------


@cache
def _list_filters() -> dict[str, dict[str, dict[str, Any]]]:
    raw = yaml.safe_load((data_root() / "rules" / "list_filters.yaml").read_text())
    return {str(k): dict(v) for k, v in (raw or {}).items()}


def server_filters(kind_key: str) -> dict[str, dict[str, Any]]:
    """filter name -> {field, note} for `uac <group> list-advanced`; {} if none."""
    return _list_filters().get(kind_key, {})


def _fake_match(row: dict[str, Any], path: str, value: str) -> bool:
    got = row.get(path)
    if path == "type" and isinstance(got, str):
        return value.lower() in (got.lower(), got.lower().removeprefix("task"))
    return fnmatch.fnmatchcase(str(got).lower(), value.lower())


# --- in memory, for tests ------------------------------------------------------


def _as_stored(obj: dict[str, Any], before: dict[str, Any] | None) -> dict[str, Any]:
    """What the controller keeps of a write: a field only a command sets
    (data/rules/commands.yaml) is left at its `created` value, or as it was."""
    from uac_compose import commands

    stored = dict(obj)
    for name, how in commands.rules().get(str(obj.get("type", "")), {}).items():
        kept = before.get(name) if before is not None else how.get("created")
        if kept is None:
            stored.pop(name, None)
        else:
            stored[name] = kept
    return stored


@dataclass
class FakeTransport:
    """A controller as dicts: kind key -> list of stamped objects."""

    objects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    raw: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    filtered: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    """Each `list_kind` call with filters: (kind key, filters)."""

    def list_kind(
        self, kind: Kind, filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        rows = [dict(o) for o in self.objects.get(kind.key, [])]
        if not filters:
            return rows
        self.filtered.append((kind.key, dict(filters)))
        known = server_filters(kind.key)
        for name, value in filters.items():
            if name not in known:
                raise TransportError(f"uac {kind.group} list-advanced: no filter {name!r}")
            path = known[name].get("field")
            if path:  # a filter only the controller knows keeps every row here
                rows = [r for r in rows if _fake_match(r, str(path), value)]
        return rows

    def list_raw(self, group: str) -> list[dict[str, Any]]:
        if group in self.raw:
            return [dict(o) for o in self.raw[group]]
        # A kind's own group lists its objects, as `uac <group> list` does.
        keys = [k.key for k in KINDS if k.group == group]
        return [dict(o) for key in keys for o in self.objects.get(key, [])]

    def get(self, type_name: str, name: str) -> dict[str, Any] | None:
        return self.get_in(kind_for_type(type_name), name)

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None:
        for obj in self.objects.get(kind.key, []):
            if object_name(obj) == name:
                return dict(obj)
        return None

    def command(self, kind: Kind, name: str, verb: str, args: dict[str, str]) -> None:
        """Does what the controller does: sets the field the command sets."""
        from uac_compose import commands

        for obj in self.objects.get(kind.key, []):
            if object_name(obj) != name:
                continue
            for field_name, how in commands.rules().get(str(obj.get("type")), {}).items():
                if verb in (how.get("values") or {}).values():
                    obj[field_name] = next(v for v, c in how["values"].items() if c == verb)
                elif verb == how.get("command"):
                    for first, value in (how.get("while") or {}).items():
                        if obj.get(first) != value:  # as the controller refuses it
                            raise TransportError(
                                f"{kind.group} {verb} {name!r}: HTTP 400: cannot be executed "
                                f"while {first} is {obj.get(first)!r}"
                            )
                    obj[field_name] = args[how["argument"]]
            self.writes.append(("command", {"verb": verb, "name": name, **args}))
            return
        raise TransportError(f"{kind.group} {verb} {name!r}: no such object")

    def create(self, obj: dict[str, Any]) -> None:
        kind = kind_for_type(str(obj.get("type", "")))
        self.objects.setdefault(kind.key, []).append(_as_stored(obj, None))
        self.writes.append(("create", dict(obj)))

    def update(self, obj: dict[str, Any]) -> None:
        kind = kind_for_type(str(obj.get("type", "")))
        rows = self.objects.setdefault(kind.key, [])
        # By sysId when the update has one, as the controller does: that is
        # how an update with a new name renames the object in place.
        same = (
            (lambda o: o.get("sysId") == obj["sysId"])
            if obj.get("sysId")
            else (lambda o: object_name(o) == object_name(obj))
        )
        before = next((o for o in rows if same(o)), None)
        rows[:] = [o for o in rows if not same(o)] + [_as_stored(obj, before)]
        self.writes.append(("update", dict(obj)))

    def workflows_of(self, task: str) -> list[str]:
        return [
            str(o["name"])
            for o in self.objects.get("tasks", [])
            if o.get("type") == "taskWorkflow"
            and any(
                (v.get("task") or {}).get("value") == task for v in o.get("workflowVertices") or []
            )
        ]

    def delete(self, kind: Kind, name: str) -> None:
        rows = self.objects.get(kind.key, [])
        if not any(object_name(o) == name for o in rows):
            raise TransportError(f"uac {kind.group} delete {name!r}: does not exist")
        rows[:] = [o for o in rows if object_name(o) != name]
        self.writes.append(("delete", {"type": kind.key, "name": name}))

    def get_template(self, name: str) -> dict[str, Any] | None:
        for row in self.raw.get(TEMPLATE_GROUP, []):
            if row.get("name") == name:
                return dict(row)
        return None

    def bundle_report(self, name: str) -> dict[str, Any] | None:
        for row in self.raw.get(BUNDLE_GROUP, []):
            if row.get("name") == name:
                return dict(row)
        return None

    def get_user(self, name: str) -> dict[str, Any] | None:
        for user in self.objects.get("users", []):
            if user.get("userName") == name:
                return dict(user)
        return None

    def create_user(self, user: dict[str, Any]) -> None:
        if self.get_user(str(user.get("userName"))) is not None:
            raise TransportError(f"uac user create {user.get('userName')!r}: exists already")
        self.objects.setdefault("users", []).append(dict(user))
        self.writes.append(("create_user", dict(user)))

    def add_user_to_group(self, user: str, group: str) -> None:
        if self.get_user(user) is None:
            raise TransportError(f"uac user add-group {user!r}: no such user")
        self.writes.append(("add_user_to_group", {"user": user, "group": group}))

    def export_template(self, name: str, path: Path) -> None:
        template = self.get_template(name)
        if template is None:
            raise TransportError(f"no extension named {name!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("template.json", json.dumps(template))
        self.writes.append(("export_template", dict(template)))

    def import_template(self, path: Path) -> None:
        with zipfile.ZipFile(path) as archive:
            template = json.loads(archive.read("template.json"))
        rows = self.raw.setdefault(TEMPLATE_GROUP, [])
        rows[:] = [r for r in rows if r.get("name") != template.get("name")] + [template]
        self.writes.append(("import_template", dict(template)))
