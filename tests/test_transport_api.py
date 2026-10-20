"""`ApiTransport`: the same calls as `UacCli`, sent as REST through `uac_api`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from uac_compose.transport import KIND_BY_KEY, TransportError
from uac_compose.transport_api import ApiTransport, query


@dataclass
class Response:
    status_code: int
    text: str


class HttpError(Exception):
    """What `requests.HTTPError` carries: the response."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"{status}")
        self.response = Response(status, text)


@dataclass
class FakeRest:
    """Answers by (method, resource); records every call."""

    answers: dict[tuple[str, str], Any] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def _answer(self, method: str, resource: str, **kw: Any) -> Any:
        self.calls.append((method, resource, kw))
        answer = self.answers.get((method, resource))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(
        self, resource: str, query: Any = "", headers: Any = None, parse_response: bool = True
    ) -> Any:
        return self._answer("GET", resource, query=query, headers=headers)

    def post(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any:
        return self._answer("POST", resource, query=query, json_data=json_data)

    def put(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any:
        return self._answer("PUT", resource, query=query, json_data=json_data)

    def delete(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any:
        return self._answer("DELETE", resource, query=query)

    def post_data(
        self,
        resource: str,
        query: Any = "",
        data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any:
        return self._answer("POST", resource, data=data, headers=headers)


TASKS = KIND_BY_KEY["tasks"]


def test_a_name_is_url_encoded() -> None:
    """`uac_api` joins `key=value` as given: `#` or `&` would cut the query."""
    assert query(taskname="Start #2 & check") == ["taskname=Start%20%232%20%26%20check"]
    assert query(taskname="Nightly*") == ["taskname=Nightly*"]  # a wildcard stays one


def test_a_filter_takes_its_rest_name() -> None:
    assert query(business_services="Ops") == ["businessServices=Ops"]


def test_list_kind_uses_listadv_and_stamps() -> None:
    rest = FakeRest({("GET", "/resources/task/listadv"): [{"type": "taskUnix", "name": "T"}]})
    rows = ApiTransport(rest).list_kind(TASKS, {"taskname": "T*"})
    assert rows == [{"type": "taskUnix", "name": "T"}]
    assert rest.calls[0][2]["query"] == ["taskname=T*"]


def test_a_kind_without_its_own_type_is_stamped() -> None:
    kind = KIND_BY_KEY["calendars"]
    rest = FakeRest({("GET", "/resources/calendar/list"): [{"name": "C"}]})
    assert ApiTransport(rest).list_kind(kind) == [{"name": "C", "type": "calendar"}]


def test_a_connection_has_its_own_resource() -> None:
    kind = KIND_BY_KEY["database_connections"]
    rest = FakeRest({("GET", "/resources/databaseconnection"): {"name": "DB"}})
    assert ApiTransport(rest).get_in(kind, "DB") == {"name": "DB", "type": "databaseConnection"}
    assert rest.calls[0][2]["query"] == ["connectionname=DB"]


def test_404_is_absent() -> None:
    rest = FakeRest({("GET", "/resources/task"): HttpError(404, "Task not found")})
    assert ApiTransport(rest).get_in(TASKS, "Nope") is None


def test_does_not_exist_is_absent() -> None:
    error = HttpError(400, "A Task with name Nope does not exist.")
    rest = FakeRest({("GET", "/resources/task"): error})
    assert ApiTransport(rest).get_in(TASKS, "Nope") is None


def test_another_error_raises_with_the_controllers_reason() -> None:
    rest = FakeRest({("GET", "/resources/task"): HttpError(401, "Invalid credentials")})
    with pytest.raises(TransportError, match="task get 'T': HTTP 401: Invalid credentials"):
        ApiTransport(rest).get_in(TASKS, "T")


def test_a_refused_write_raises_with_the_reason() -> None:
    error = HttpError(400, "Create task failed. Agent 'x' not found.")
    rest = FakeRest({("POST", "/resources/task"): error})
    with pytest.raises(TransportError, match=r"HTTP 400: Create task failed\. Agent 'x'"):
        ApiTransport(rest).create({"type": "taskUnix", "name": "T"})


def test_the_routing_key_never_reaches_the_controller() -> None:
    rest = FakeRest()
    ApiTransport(rest).create({"type": "calendar", "name": "C"})
    method, resource, kw = rest.calls[0]
    assert (method, resource) == ("POST", "/resources/calendar")
    assert "type" not in kw["json_data"] and kw["json_data"]["name"] == "C"


def test_a_credential_keeps_its_own_type() -> None:
    rest = FakeRest()
    ApiTransport(rest).create({"type": "credential", "_uacType": "Standard", "name": "K"})
    assert rest.calls[0][2]["json_data"]["type"] == "Standard"


def test_retain_sys_ids_where_the_cli_sends_it() -> None:
    """Every create; on update only where `uac_api` maps it (task, script...)."""
    rest = FakeRest()
    api = ApiTransport(rest)
    api.create({"type": "taskUnix", "name": "T"})
    api.update({"type": "taskUnix", "name": "T", "sysId": "1"})
    api.update({"type": "customDay", "name": "D", "sysId": "2"})
    bodies = [kw["json_data"] for _, _, kw in rest.calls]
    assert bodies[0]["retainSysIds"] == "true"
    assert bodies[1]["retainSysIds"] == "true"
    assert "retainSysIds" not in bodies[2]
    assert rest.calls[1][0] == "PUT"


def test_delete_names_the_object() -> None:
    rest = FakeRest()
    ApiTransport(rest).delete(KIND_BY_KEY["scripts"], "a.sh")
    assert rest.calls[0][:2] == ("DELETE", "/resources/script")
    assert rest.calls[0][2]["query"] == ["scriptname=a.sh"]


def test_workflows_of_an_unknown_task_is_none() -> None:
    rest = FakeRest({("GET", "/resources/task/parent/list"): HttpError(404, "not found")})
    assert ApiTransport(rest).workflows_of("Nope") == []


def test_workflows_of_lists_each_name_once() -> None:
    rows = [{"name": "W1"}, {"name": "W2"}, {"name": "W1"}]
    rest = FakeRest({("GET", "/resources/task/parent/list"): rows})
    assert ApiTransport(rest).workflows_of("T") == ["W1", "W2"]


def test_list_raw_of_the_extensions() -> None:
    rest = FakeRest({("GET", "/resources/universaltemplate/list"): [{"name": "X"}]})
    assert ApiTransport(rest).list_raw("universal-template") == [{"name": "X"}]


def test_the_plain_list_of_tasks_is_a_post() -> None:
    """`uac task list` (names and types, what a pattern searches) is a POST of
    filters; `{}` lists them all. A GET of the same path answers nothing."""
    rows = [{"name": "T", "type": "Linux/Unix"}]
    rest = FakeRest({("POST", "/resources/task/list"): rows})
    assert ApiTransport(rest).list_raw("task") == rows
    assert rest.calls[0][2]["json_data"] == {}


def test_a_list_that_is_not_a_list_raises() -> None:
    rest = FakeRest({("GET", "/resources/agent/list"): "<html>login</html>"})
    with pytest.raises(TransportError, match="not a list"):
        ApiTransport(rest).list_raw("agent")


def test_bundle_report() -> None:
    rest = FakeRest({("GET", "/resources/bundle/report"): {"tasks": []}})
    assert ApiTransport(rest).bundle_report("B") == {"tasks": []}
    assert rest.calls[0][2]["query"] == ["bundlename=B"]


def test_export_and_import_an_extension(tmp_path: Path) -> None:
    zipped = b"PK\x03\x04zip"
    rest = FakeRest({("GET", "/resources/universaltemplate/exporttemplate"): zipped})
    api = ApiTransport(rest)
    target = tmp_path / "x" / "AWS.zip"
    api.export_template("AWS", target)
    assert target.read_bytes() == zipped
    api.import_template(target)
    method, resource, kw = rest.calls[1]
    assert (method, resource) == ("POST", "/resources/universaltemplate/importtemplate")
    assert kw["data"] == zipped and kw["headers"] == {"Content-Type": "application/zip"}


def test_an_empty_export_raises(tmp_path: Path) -> None:
    rest = FakeRest({("GET", "/resources/universaltemplate/exporttemplate"): b""})
    with pytest.raises(TransportError, match="not a zip"):
        ApiTransport(rest).export_template("AWS", tmp_path / "AWS.zip")


def test_users() -> None:
    rest = FakeRest({("GET", "/resources/user"): {"userName": "jdoe"}})
    api = ApiTransport(rest)
    assert api.get_user("jdoe") == {"userName": "jdoe"}
    api.create_user({"userName": "ann", "userPassword": "x"})
    api.add_user_to_group("ann", "Ops Team")
    assert rest.calls[1][2]["json_data"]["retainSysIds"] == "true"
    assert rest.calls[2][2]["query"] == ["username=ann", "groupname=Ops%20Team"]


def test_connect_needs_a_token_or_a_user_and_password() -> None:
    pytest.importorskip("uac_api")
    with pytest.raises(TransportError, match="token, or a user and a password"):
        ApiTransport.connect("https://uc.example.com", user="ann")


def test_live_api_reads_what_the_cli_reads(tmp_path: Path) -> None:
    """The same export through both transports. The profile's url and token (or
    user and password) come from the `uac` CLI's own config, never printed."""
    import os

    import yaml

    from uac_compose.emit import export_text
    from uac_compose.learn import Workspace, current_extensions
    from uac_compose.transport import UacCli

    profile = os.environ.get("UAC_COMPOSE_LIVE_PROFILE")
    if not profile:
        pytest.skip("set UAC_COMPOSE_LIVE_PROFILE to run against a controller")
    pytest.importorskip("uac_api")
    config = yaml.safe_load((Path.home() / ".uac" / "config.yml").read_text())
    settings = config["profiles"][profile]
    api = ApiTransport.connect(
        settings["url"],
        token=settings.get("token"),
        user=settings.get("username"),
        password=settings.get("password"),
        ssl_verify=settings.get("ssl_verify", True),
    )
    cli = UacCli(profile)
    names = [t["name"] for t in cli.list_kind(KIND_BY_KEY["tasks"], {"taskname": "*"})[:5]]
    ws = Workspace(tmp_path)
    templates = ws.template_set(current_extensions(ws, cli))
    via_api, _ = export_text(names, "task", ws, api, templates, with_members=True)
    via_cli, _ = export_text(names, "task", ws, cli, templates, with_members=True)
    assert via_api == via_cli
