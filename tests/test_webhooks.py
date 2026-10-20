"""Webhooks: a bundle section, and the two fields only a command sets.

The names here are made up. A webhook's `status` and `executionUser` are not
stored by create or update (measured live, data/rules/commands.yaml): import
and apply write the object, then run `webhook enable` / `disable` /
`assign-execution-user`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from uac_compose import commands, service, validate
from uac_compose.compile import compile_bundle
from uac_compose.core.templates import TemplateSet
from uac_compose.learn import Workspace
from uac_compose.service import ServiceError
from uac_compose.transport import KIND_BY_KEY, FakeTransport
from uac_compose.transport_api import ApiTransport
from uac_compose.yamlio import read_bundle

STORED = {
    "type": "webhook",
    "name": "Notify Ops",
    "sysId": "w1",
    "action": "Url",
    "event": {"label": "UAC - Task Status Changed", "value": "uac_task_status_changed"},
    "eventBusinessServiceCriteria": "Member of Any Business Service or Unassigned",
    "filter": {
        "logicalOperator": "And",
        "criteria": [
            {"type": "criterion", "name": "status", "operator": "EQUALS", "value": "Failed"}
        ],
    },
    "httpAuth": "None",
    "httpHeaders": [],
    "urlParameters": [],
    "status": "Enabled",
    "executionUser": "ops",
    "url": "https://hooks.example.com/ops",
    "enabledBy": "ann",
    "enabledTime": "2026-01-01 00:00:00",
}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")


def test_export_is_short(ws: Workspace) -> None:
    fake = FakeTransport(objects={"webhooks": [dict(STORED)]})
    text = service.export_bundle(fake, ws, ["Notify Ops"], kind="webhook").text
    body = read_bundle(text).sections["webhooks"]["Notify Ops"]
    assert body["event"] == "uac_task_status_changed"  # the controller owns the label
    assert body["status"] == "Enabled" and body["executionUser"] == "ops"
    assert "httpAuth" not in body and "eventBusinessServiceCriteria" not in body  # defaults
    assert "enabledBy" not in body and "type" not in body["filter"]["criteria"][0]


def test_import_creates_then_enables_and_assigns(ws: Workspace) -> None:
    source = FakeTransport(objects={"webhooks": [dict(STORED)]})
    text = service.export_bundle(source, ws, ["Notify Ops"], kind="webhook").text
    target = FakeTransport()
    out: list[str] = []
    service.import_bundle(target, ws, text, dry_run=True, out=out.append)
    assert "   ~ status: Enabled is set by `webhook enable` after the write" in out
    assert (
        "   ~ executionUser: ops is set by `webhook assign-execution-user` after the write" in out
    )
    assert target.writes == []
    service.import_bundle(target, ws, text)
    verbs = [(kind, obj.get("verb", "")) for kind, obj in target.writes]
    assert verbs == [("create", ""), ("command", "assign-execution-user"), ("command", "enable")]
    created = target.objects["webhooks"][0]
    assert created["status"] == "Enabled" and created["executionUser"] == "ops"
    again: list[str] = []
    service.import_bundle(target, ws, text, dry_run=True, out=again.append)
    assert "reuse    webhooks/Notify Ops" in again


def test_a_disabled_webhook_is_created_with_no_command(ws: Workspace) -> None:
    """A create leaves it Disabled: no `disable` call is needed."""
    text = "webhooks:\n  W: {action: Url, event: uac_agent_changed, url: x, status: Disabled}\n"
    target = FakeTransport()
    service.import_bundle(target, ws, text)
    assert [kind for kind, _ in target.writes] == ["create"]


def test_status_differs_is_an_update_that_runs_the_command(ws: Workspace) -> None:
    target = FakeTransport(objects={"webhooks": [{**STORED, "status": "Disabled"}]})
    text = "webhooks:\n  Notify Ops: {status: Enabled}\n"
    with pytest.raises(ServiceError, match="1 conflict"):
        service.import_bundle(target, ws, text)
    service.import_bundle(target, ws, text, update=True)
    assert target.objects["webhooks"][0]["status"] == "Enabled"


def test_a_status_no_command_sets_is_refused() -> None:
    bundle = read_bundle("webhooks:\n  W: {action: Url, event: e, status: Paused}\n")
    planned = compile_bundle(bundle, TemplateSet("8.0.2.0"))
    findings = validate.static(planned)
    assert any("status: 'Paused' cannot be set" in str(f) for f in findings)


def test_apply_runs_the_command_after_the_update(ws: Workspace) -> None:
    target = FakeTransport(objects={"webhooks": [{**STORED, "status": "Enabled"}]})
    text = "steps:\n  - update webhook: Notify Ops\n    set: {status: Disabled}\n"
    out: list[str] = []
    service.apply_changes(target, ws, text, dry_run=True, out=out.append)
    assert any("status: Disabled is set by `webhook disable`" in line for line in out)
    service.apply_changes(target, ws, text, yes=True)
    assert target.objects["webhooks"][0]["status"] == "Disabled"
    assert ("command", {"verb": "disable", "name": "Notify Ops"}) in target.writes


def test_needed_compares_with_what_a_create_leaves() -> None:
    obj = {"type": "webhook", "name": "W", "status": "Disabled", "executionUser": "ops"}
    assert [c.verb for c in commands.needed(obj, None)] == ["assign-execution-user"]
    assert commands.needed(obj, {**obj}) == []


class Rest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    def post(
        self,
        resource: str,
        query: Any = "",
        json_data: Any = None,
        headers: Any = None,
        parse_response: bool = True,
    ) -> Any:
        self.calls.append((resource, query, json_data))


def test_api_commands_are_posts() -> None:
    rest = Rest()
    api = ApiTransport(rest)  # type: ignore[arg-type]
    kind = KIND_BY_KEY["webhooks"]
    api.command(kind, "Notify Ops", "enable", {})
    api.command(kind, "Notify Ops", "assign-execution-user", {"username": "ops"})
    assert rest.calls == [
        ("/resources/webhook/enable", ["webhookname=Notify%20Ops"], None),
        (
            "/resources/webhook/assignexecutionuser",
            ["webhookname=Notify%20Ops"],
            {"username": "ops"},
        ),
    ]


def test_cli_command_reads_the_json_answer() -> None:
    """`webhook enable` answers `"errors": ""` on success: not a failure."""
    import subprocess

    from uac_compose.transport import TransportError, UacCli

    def answer(stdout: str, code: int = 0) -> UacCli:
        done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
        return UacCli("p", runner=lambda argv: done)

    kind = KIND_BY_KEY["webhooks"]
    answer('{"success": true, "errors": "", "info": "Enabled"}').command(kind, "W", "enable", {})
    with pytest.raises(TransportError, match="no such webhook"):
        answer('{"success": false, "errors": "no such webhook"}').command(kind, "W", "enable", {})
    assigned = '{"response": "Successfully assigned execution user"}'
    answer(assigned).command(kind, "W", "assign-execution-user", {"username": "u"})


def test_an_execution_user_is_assigned_while_disabled() -> None:
    """The controller refuses assign-execution-user on an enabled webhook."""
    obj = {"type": "webhook", "name": "W", "status": "Enabled", "executionUser": "ops"}
    verbs = [c.verb for c in commands.needed(obj, None)]
    assert verbs == ["assign-execution-user", "enable"]  # a create is Disabled: no disable
    stored = {**obj, "executionUser": "ann"}
    verbs = [c.verb for c in commands.needed(obj, stored)]
    assert verbs == ["disable", "assign-execution-user", "enable"]
    unstated = {"type": "webhook", "name": "W", "executionUser": "ops"}  # no status in the file
    verbs = [c.verb for c in commands.needed(unstated, stored)]
    assert verbs == ["disable", "assign-execution-user", "enable"]  # it was Enabled: back on


def test_the_fake_refuses_what_the_controller_refuses(ws: Workspace) -> None:
    """An enabled webhook gets a new execution user: disable, assign, enable."""
    target = FakeTransport(objects={"webhooks": [{**STORED, "executionUser": "ann"}]})
    from uac_compose.transport import TransportError

    with pytest.raises(TransportError, match="while status is 'Enabled'"):
        target.command(
            KIND_BY_KEY["webhooks"], "Notify Ops", "assign-execution-user", {"username": "x"}
        )
    service.import_bundle(
        target, ws, "webhooks:\n  Notify Ops: {executionUser: ops}\n", update=True
    )
    stored = target.objects["webhooks"][0]
    assert stored["executionUser"] == "ops" and stored["status"] == "Enabled"
