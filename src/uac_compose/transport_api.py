"""Talking to a controller through its REST API, in this process (`uac-api`).

The same calls `UacCli` makes as `uac <group> <verb>`, sent with
`uac_api.UniversalController`'s own `get` / `post` / `put` / `delete`: the `uac`
CLI is `uac_cli` on top of `uac_api`, so each verb is one REST path. Nothing
else needs to be installed, which is what a Universal Extension needs.

Kept the same as `UacCli` (see `transport.py`):
- absent only on HTTP 404 or "does not exist"; anything else raises
  `TransportError` with the controller's own reason;
- the routing key never reaches the controller;
- `retainSysIds: "true"` goes in the body where the CLI puts it: on every
  create, and on the updates whose `uac_api` method maps it.

`uac_api` builds a query string by joining `key=value` as given, so every
value is URL-encoded here: a name like `Start #2 & check` would otherwise cut
the query short.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from uac_compose import routing
from uac_compose.transport import (
    BUNDLE_GROUP,
    TEMPLATE_GROUP,
    Kind,
    TransportError,
    kind_for_type,
    name_arg,
    object_name,
)

#: `uac` group -> REST resource under `/resources/`.
RESOURCES: dict[str, str] = {
    "task": "task",
    "trigger": "trigger",
    "variable": "variable",
    "virtual-resource": "virtual",
    "credential": "credential",
    "business-service": "businessservice",
    "agent": "agent",
    "agent-cluster": "agentcluster",
    "user": "user",
    "user-group": "usergroup",
    "calendar": "calendar",
    "custom-day": "customday",
    "script": "script",
    "universal-event-template": "universaleventtemplate",
    "connection database": "databaseconnection",
    "connection peoplesoft": "peoplesoftconnection",
    "connection sap": "sapconnection",
    "connection email": "emailconnection",
    "webhook": "webhook",
    TEMPLATE_GROUP: "universaltemplate",
    BUNDLE_GROUP: "bundle",
}

#: `uac` list verb -> path suffix.
LIST_PATHS = {"list": "list", "list-advanced": "listadv"}

#: Groups whose plain `list` is a POST of filters (`{}`: all), not a GET.
POST_LISTS = frozenset({"task", "trigger", "variable"})

#: Groups whose `uac <group> update` sends `retainSysIds` (its `uac_api` method
#: maps the field). Every create does.
RETAIN_ON_UPDATE = frozenset({"task", "script", "calendar", "user-group"})

#: A `uac` CLI filter name whose REST query name differs.
QUERY_NAMES = {
    "business_services": "businessServices",
    "updated_time": "updatedTime",
    "updated_time_type": "updatedTimeType",
}

NOT_FOUND = "does not exist"


class Rest(Protocol):
    """The part of `uac_api.UniversalController` used here."""

    def get(
        self,
        resource: str,
        query: Any = "",
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any: ...

    def post(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any: ...

    def put(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any: ...

    def delete(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any: ...

    def post_data(
        self,
        resource: str,
        query: Any = "",
        data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any: ...


def resource(group: str, suffix: str = "") -> str:
    try:
        path = f"/resources/{RESOURCES[group]}"
    except KeyError as error:
        raise TransportError(f"no REST resource for uac group {group!r}") from error
    return f"{path}/{suffix}" if suffix else path


def query(**params: str) -> list[str]:
    """`uac_api`'s query: `key=value` strings, joined by `&` as they are."""
    return [f"{QUERY_NAMES.get(k, k)}={quote(str(v), safe='*')}" for k, v in params.items()]


def _status_and_text(error: Exception) -> tuple[int | None, str]:
    response = getattr(error, "response", None)
    if response is None:
        return None, str(error)
    return getattr(response, "status_code", None), str(getattr(response, "text", "") or "")


def _reason(error: Exception) -> str:
    """The controller's words, as `transport.reason` gives them for the CLI."""
    status, text = _status_and_text(error)
    text = " ".join(text.split())
    if status is None:
        return text or type(error).__name__
    return f"HTTP {status}: {text}" if text else f"HTTP {status}"


def _absent(error: Exception) -> bool:
    status, text = _status_and_text(error)
    return status == 404 or NOT_FOUND in text


def quiet_logger() -> logging.Logger:
    """`uac_api` logs every URL at INFO and every refusal at ERROR. The refusal
    reaches the caller as a `TransportError`; the rest is not wanted on a
    task's output."""
    log = logging.getLogger("uac_compose.uac_api")
    log.setLevel(logging.CRITICAL)
    log.propagate = False
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


@dataclass
class ApiTransport:
    """`Transport` over the controller's REST API (`uac_api`)."""

    rest: Rest

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        user: str | None = None,
        password: str | None = None,
        token: str | None = None,
        ssl_verify: bool = True,
    ) -> ApiTransport:
        """A controller at `url`, with a token or a user and password."""
        try:
            from uac_api import UniversalController
        except ImportError as error:
            raise TransportError(
                "uac-api is not installed: pip install 'uac-compose[api]'"
            ) from error
        if token:
            client = UniversalController(
                url, token=token, ssl_verify=ssl_verify, logger=quiet_logger()
            )
        elif user and password:
            client = UniversalController(
                url, credential=(user, password), ssl_verify=ssl_verify, logger=quiet_logger()
            )
        else:
            raise TransportError("give a token, or a user and a password")
        return cls(client)

    # --- reading -------------------------------------------------------------

    def _list(self, group: str, verb: str, **filters: str) -> list[dict[str, Any]]:
        what = f"{group} {verb}"
        try:
            path = resource(group, LIST_PATHS[verb])
            if verb == "list" and group in POST_LISTS:
                rows = self.rest.post(path, query=query(**filters), json_data={})
            else:
                rows = self.rest.get(path, query=query(**filters))
        except KeyError as error:
            raise TransportError(f"{what}: no REST path for {verb!r}") from error
        except Exception as error:
            raise TransportError(f"{what}: {_reason(error)}") from error
        if not isinstance(rows, list):
            raise TransportError(f"{what}: the answer is not a list")
        return [r for r in rows if isinstance(r, dict)]

    def _get(self, group: str, what: str, params: dict[str, str], suffix: str = "") -> Any:
        """One object, or None when the controller says it is absent."""
        try:
            return self.rest.get(resource(group, suffix), query=query(**params))
        except Exception as error:
            if _absent(error):
                return None
            raise TransportError(f"{what}: {_reason(error)}") from error

    def list_kind(
        self, kind: Kind, filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        return [kind.stamp(row) for row in self._list(kind.group, kind.verb, **(filters or {}))]

    def list_raw(self, group: str) -> list[dict[str, Any]]:
        return self._list(group, "list")

    def get(self, type_name: str, name: str) -> dict[str, Any] | None:
        return self.get_in(kind_for_type(type_name), name)

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None:
        payload = self._get(
            kind.group, f"{kind.group} get {name!r}", {name_arg(kind.group): name}
        )
        if not isinstance(payload, dict) or not object_name(payload):
            return None
        return kind.stamp(payload)

    def workflows_of(self, task: str) -> list[str]:
        """The workflows that run `task` (`/task/parent/list`)."""
        try:
            rows = self.rest.get(resource("task", "parent/list"), query=query(taskname=task))
        except Exception as error:
            _, text = _status_and_text(error)
            if _absent(error) or "not found" in text.lower():
                return []
            raise TransportError(f"task list-workflow-list {task!r}: {_reason(error)}") from error
        if not isinstance(rows, list):
            return []
        return list(
            dict.fromkeys(str(r["name"]) for r in rows if isinstance(r, dict) and r.get("name"))
        )

    def get_template(self, name: str) -> dict[str, Any] | None:
        payload = self._get(
            TEMPLATE_GROUP, f"{TEMPLATE_GROUP} get {name!r}", {name_arg(TEMPLATE_GROUP): name}
        )
        return payload if isinstance(payload, dict) and payload.get("name") else None

    def bundle_report(self, name: str) -> dict[str, Any] | None:
        payload = self._get(
            BUNDLE_GROUP,
            f"{BUNDLE_GROUP} get-report {name!r}",
            {name_arg(BUNDLE_GROUP): name},
            suffix="report",
        )
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise TransportError(f"bundle {name!r} answered with something that is not JSON")
        return payload

    def get_user(self, name: str) -> dict[str, Any] | None:
        payload = self._get("user", f"user get {name!r}", {"username": name})
        return payload if isinstance(payload, dict) and payload.get("userName") else None

    # --- writing -------------------------------------------------------------

    def _send(self, verb: str, group: str, body: dict[str, Any], name: str) -> None:
        retain = verb == "create" or group in RETAIN_ON_UPDATE
        if retain and "retainSysIds" not in body:
            body = {**body, "retainSysIds": "true"}
        call = self.rest.post if verb == "create" else self.rest.put
        try:
            call(resource(group), json_data=body, parse_response=False)
        except Exception as error:
            raise TransportError(f"{group} {verb} {name!r}: {_reason(error)}") from error

    def _write(self, verb: str, obj: dict[str, Any]) -> None:
        kind = kind_for_type(str(obj.get("type", "")))
        body = routing.restore(obj) if kind.routing else obj
        self._send(verb, kind.group, body, object_name(obj))

    def create(self, obj: dict[str, Any]) -> None:
        self._write("create", obj)

    def update(self, obj: dict[str, Any]) -> None:
        self._write("update", obj)

    def delete(self, kind: Kind, name: str) -> None:
        try:
            self.rest.delete(
                resource(kind.group),
                query=query(**{name_arg(kind.group): name}),
                parse_response=False,
            )
        except Exception as error:
            raise TransportError(f"{kind.group} delete {name!r}: {_reason(error)}") from error

    def create_user(self, user: dict[str, Any]) -> None:
        self._send("create", "user", user, str(user.get("userName", "")))

    def add_user_to_group(self, user: str, group: str) -> None:
        try:
            self.rest.post(
                resource("user", "groups"),
                query=query(username=user, groupname=group),
                parse_response=False,
            )
        except Exception as error:
            raise TransportError(
                f"user add-group {user!r} to {group!r}: {_reason(error)}"
            ) from error

    def command(self, kind: Kind, name: str, verb: str, args: dict[str, str]) -> None:
        """`uac <group> <verb>` is a POST to `/resources/<group>/<verb without
        dashes>`: the object named in the query, the arguments in the body
        (as `uac_api` sends `assignexecutionuser`)."""
        try:
            self.rest.post(
                resource(kind.group, verb.replace("-", "")),
                query=query(**{name_arg(kind.group): name}),
                json_data=dict(args) or None,
                parse_response=False,
            )
        except Exception as error:
            raise TransportError(f"{kind.group} {verb} {name!r}: {_reason(error)}") from error

    # --- extensions ----------------------------------------------------------

    def export_template(self, name: str, path: Path) -> None:
        what = f"{TEMPLATE_GROUP} export {name!r}"
        try:
            content = self.rest.get(
                resource(TEMPLATE_GROUP, "exporttemplate"),
                query=query(**{name_arg(TEMPLATE_GROUP): name}),
                headers={"Accept": "application/octet-stream"},
                parse_response=False,
            )
        except Exception as error:
            raise TransportError(f"{what}: {_reason(error)}") from error
        if not isinstance(content, bytes | bytearray) or not content:
            raise TransportError(f"{what}: the answer is not a zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(content))

    def import_template(self, path: Path) -> None:
        try:
            self.rest.post_data(
                resource(TEMPLATE_GROUP, "importtemplate"),
                data=path.read_bytes(),
                headers={"Content-Type": "application/zip"},
                parse_response=False,
            )
        except Exception as error:
            raise TransportError(
                f"{TEMPLATE_GROUP} import {path.name}: {_reason(error)}"
            ) from error
