"""Milestone 6: the non-task sections, business services and --with-members."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from uac_compose import importer, validate, yamlio
from uac_compose.compile import compile_bundle, compile_object
from uac_compose.core.templates import TemplateSet
from uac_compose.emit import collect, export_text, find, kinds_for, list_services
from uac_compose.learn import Workspace
from uac_compose.style.uac import REDACTED_NOTE
from uac_compose.transport import KIND_BY_KEY, FakeTransport, Kind, TransportError

OBJECTS: dict[str, dict[str, Any]] = {
    "credentials": {
        "name": "svc",
        "_uacType": "Standard",
        "runtimeUser": "batch",
        "opswiseGroups": ["Ops"],
    },
    "variables": {"name": "REGION", "value": "eu", "description": "where"},
    "calendars": {
        "name": "Business",
        "businessDays": {"value": "Monday,Tuesday,Friday"},
        "customDays": [{"name": "Xmas"}],
        "firstDayOfWeek": "Monday",
    },
    "custom_days": {
        "name": "Xmas",
        "ctype": "Absolute Repeating Date",
        "month": "Dec",
        "day": 25,
        "observedRules": [{"actualDayOfWeek": "Sat", "observedDayOfWeek": "Fri"}],
    },
    "event_templates": {
        "name": "file-arrived",
        "label": "File arrived",
        "attributes": [{"name": "path", "label": "Path", "type": "Text"}],
    },
    "scripts": {"scriptName": "load.sh", "scriptType": "Script", "content": "echo hi\n"},
    "virtual_resources": {"name": "db-slots", "_uacType": "Renewable", "limit": 4},
    "database_connections": {
        "name": "orders-db",
        "dbType": "Oracle",
        "dbUrl": "jdbc:x",
        "credentials": "svc",
    },
    "email_connections": {"name": "smtp", "_uacType": "Outgoing", "smtp": "mail.example"},
    "business_services": {"name": "Ops", "description": "operations"},
}


def _controller() -> FakeTransport:
    fake = FakeTransport()
    for key, obj in OBJECTS.items():
        kind = KIND_BY_KEY[key]
        stamped = {**obj, "type": kind.routing}
        fake.objects[key] = [stamped]
    return fake


def _import(text: str, fake: FakeTransport, templates: TemplateSet, **kw: Any) -> list[str]:
    planned = compile_bundle(yamlio.read_bundle(text), templates)
    assert validate.errors(validate.static(planned, None, templates.extensions)) == []
    decisions = importer.decide_all(planned, fake, templates.extensions, services=[], **kw)
    return [d.action for d in decisions]


@pytest.mark.parametrize("style", ["uac", "compact"])
@pytest.mark.parametrize("section", list(OBJECTS))
def test_every_kind_exports_and_decides_reuse(
    tmp_path: Path, templates: TemplateSet, style: str, section: str
) -> None:
    fake = _controller()
    name = OBJECTS[section].get("scriptName") or OBJECTS[section]["name"]
    text, _ = export_text([name], section, Workspace(tmp_path), fake, templates, style=style)
    assert f"{section}:\n" in text
    # A listed business service is planned too, and is on the controller.
    assert set(_import(text, fake, templates, update=False)) == {"reuse"}


def test_own_type_written_as_type(tmp_path: Path, templates: TemplateSet) -> None:
    text, _ = export_text(["svc"], "credential", Workspace(tmp_path), _controller(), templates)
    assert "credentials:\n  svc:\n    type: Standard\n" in text


def test_calendar_idioms(tmp_path: Path, templates: TemplateSet) -> None:
    text, _ = export_text(["Business"], "calendar", Workspace(tmp_path), _controller(), templates)
    doc = yamlio.load(text)["calendars"]["Business"]
    assert doc["businessDays"] == ["Monday", "Tuesday", "Friday"]
    assert doc["customDays"] == ["Xmas"]
    text, _ = export_text(["Xmas"], "custom-day", Workspace(tmp_path), _controller(), templates)
    assert yamlio.load(text)["custom_days"]["Xmas"]["observed"] == {"Sat": "Fri"}


def test_secret_field_refused() -> None:
    planned = compile_bundle(
        yamlio.read_bundle(
            "credentials:\n  c: {type: Standard, runtimeUser: u, runtimePassword: hunter2}\n"
        )
    )
    errors = validate.errors(validate.static(planned))
    assert len(errors) == 1 and "runtimePassword is a secret" in str(errors[0])


def test_blank_provider_parameter_not_emitted(tmp_path: Path, templates: TemplateSet) -> None:
    fake = FakeTransport(
        objects={
            "credentials": [
                {
                    "name": "vault",
                    "type": "credential",
                    "_uacType": "Resolvable",
                    "providerParameters": [
                        {"name": "REGION", "value": "eu"},
                        {"name": "SECRET", "value": ""},
                    ],
                }
            ]
        }
    )
    text, _ = export_text(["vault"], "credential", Workspace(tmp_path), fake, templates)
    assert "providerParameters" not in yamlio.load(text)["credentials"]["vault"]
    assert REDACTED_NOTE.split(":")[0] in text  # said why, as a comment the loader strips
    assert _import(text, fake, templates, update=False) == ["reuse"]


def test_full_provider_parameters_emitted_as_pairs(tmp_path: Path, templates: TemplateSet) -> None:
    fake = FakeTransport(
        objects={
            "credentials": [
                {
                    "name": "vault",
                    "type": "credential",
                    "_uacType": "Resolvable",
                    "providerParameters": [{"name": "REGION", "value": "eu"}],
                }
            ]
        }
    )
    text, _ = export_text(["vault"], "credential", Workspace(tmp_path), fake, templates)
    assert yamlio.load(text)["credentials"]["vault"]["providerParameters"] == [{"REGION": "eu"}]


# --- business services ------------------------------------------------------------


def test_business_service_mapping_does_not_join() -> None:
    text = "business_services:\n  Ops: {description: x}\ntasks:\n  T: {type: Unix}\n"
    planned = compile_bundle(yamlio.read_bundle(text))
    task = next(p for p in planned if p.name == "T")
    assert "opswiseGroups" not in task.obj
    assert [p.section for p in planned] == ["business_services", "tasks"]


def test_no_services_on_types_without_opswise_groups() -> None:
    text = "business_services: [Ops]\nvariables:\n  V: {value: '1'}\ncustom_days:\n  D: {}\n"
    for p in compile_bundle(yamlio.read_bundle(text)):
        assert "opswiseGroups" not in p.obj


def test_service_list_defines_and_joins_nothing() -> None:
    text = (
        "business_services:\n- M\n- Q: my description\n"
        "tasks:\n  A: {type: Unix}\n  B: {type: Unix, business_services: [M]}\n"
        "  C: {type: Unix, business_services: []}\n"
    )
    planned = {p.name: p.obj for p in compile_bundle(yamlio.read_bundle(text))}
    assert planned["M"] == {"type": "businessService", "name": "M"}
    assert planned["Q"]["description"] == "my description"
    assert "opswiseGroups" not in planned["A"]  # none is the default
    assert planned["B"]["opswiseGroups"] == ["M"]
    assert "opswiseGroups" not in planned["C"]


def test_services_exported_as_list_objects_keep_their_own() -> None:
    q = ({"type": "businessService", "name": "Q", "description": "d"}, "businessService")
    both = ({"name": "A", "type": "taskUnix", "opswiseGroups": ["M", "Q"]}, "taskUnix")
    none = ({"name": "C", "type": "taskUnix"}, "taskUnix")
    v = ({"name": "V", "type": "variable", "opswiseGroups": ["X"]}, "variable")
    items, rest = list_services([q, both, none, v])
    assert items == [{"Q": "d"}, "M"]  # named only: listed, so import creates it
    assert rest == [both, none, v]
    wider = ({"type": "businessService", "name": "W", "owner": "x"}, "businessService")
    items, rest = list_services([wider, both])
    assert items == [] and [o["name"] for o, t in rest if t == "businessService"] == ["W", "M", "Q"]


def test_update_fence_lets_unowned_objects_through() -> None:
    assert importer.in_services({"type": "taskUnix"}, ["Ops"])
    assert importer.in_services({"type": "taskUnix", "opswiseGroups": ["Ops"]}, ["Ops"])
    assert not importer.in_services({"type": "taskUnix", "opswiseGroups": ["Other"]}, ["Ops"])


def test_unknown_service_refused_before_import() -> None:
    text = "business_services: [Ops]\ntasks:\n  T: {type: Unix, business_services: [Gone]}\n"
    planned = compile_bundle(yamlio.read_bundle(text))
    findings = validate.references_(planned, {"business_services": {"Ops"}})
    assert [f.level for f in findings] == ["error"] and "'Gone'" in str(findings[0])


# --- weekdays ------------------------------------------------------------


def _static(section: str, name: str, body: dict[str, Any]) -> list[str]:
    from uac_compose.compile import Planned

    planned = [Planned(section, name, compile_object(section, name, body))]
    return [str(f) for f in validate.errors(validate.static(planned))]


def test_calendar_rejects_short_weekday() -> None:
    errors = _static("calendars", "C", {"businessDays": ["Mon", "Tuesday"]})
    assert len(errors) == 1 and "'Mon'" in errors[0]


def test_custom_day_rejects_long_weekday() -> None:
    errors = _static("custom_days", "D", {"dayofweek": "Monday", "observed": {"Saturday": "Fri"}})
    assert len(errors) == 2


def test_script_type_is_closed() -> None:
    assert _static("scripts", "s", {"scriptType": "Shell"})
    assert not _static("scripts", "s", {"scriptType": "Script"})


# --- with members ------------------------------------------------------------


def test_extension_slots_name_credentials_and_scripts() -> None:
    from uac_compose import references

    task = {
        "credentialField1": {"label": "Cred", "name": "cred", "value": "svc"},
        "scriptField2": {"label": "Prompt", "name": "prompt", "value": "prompt.md"},
        "credentialVarField1": {"label": "Cred", "name": "cred_var", "value": "${c}"},
        "textField1": {"label": "Path", "name": "path", "value": "svc"},
    }
    assert references.named("taskUniversal", task) == [
        ("credentialField1", "credentials", "svc"),
        ("scriptField2", "scripts", "prompt.md"),
    ]


def test_universal_monitor_needs_its_task_first() -> None:
    from uac_compose import references
    from uac_compose.compile import Planned, creation_order

    monitor = {"type": "taskUniversalMonitor", "name": "Watch", "taskUniversal": "Pub"}
    assert references.named("taskUniversalMonitor", monitor) == [
        ("taskUniversal", "tasks", "Pub")
    ]
    watch = Planned("tasks", "Watch", monitor)
    pub = Planned("tasks", "Pub", {"type": "taskUniversal", "name": "Pub"})
    assert [p.name for p in creation_order([watch, pub])] == ["Pub", "Watch"]


def test_variablize_agents() -> None:
    from uac_compose.emit import variablize_agents

    a = ({"type": "taskUnix", "name": "A", "agent": "AGT-01"}, "taskUnix")
    b = ({"type": "taskUnix", "name": "B", "agent": "AGT-01", "agentVar": None}, "taskUnix")
    c = ({"type": "taskUnix", "name": "C", "agentVar": "${mine}"}, "taskUnix")
    taken = ({"type": "variable", "name": "agent_agt_01", "value": "other"}, "variable")
    out = variablize_agents([a, b, c, taken])
    assert out[0][0] == {"type": "taskUnix", "name": "A", "agentVar": "${agent_agt_01_2}"}
    assert out[1][0]["agentVar"] == "${agent_agt_01_2}" and "agent" not in out[1][0]
    assert out[2] == c  # already a variable
    assert out[3] == taken
    assert out[4] == ({"type": "variable", "name": "agent_agt_01_2", "value": "AGT-01"}, "variable")
    assert a[0]["agent"] == "AGT-01"  # the collected object is not changed


def _workflow_controller() -> FakeTransport:
    fake = _controller()
    fake.objects["tasks"] = [
        {
            "type": "taskWorkflow",
            "name": "WF",
            "workflowVertices": [
                {"task": {"value": "A"}, "vertexId": "2"},
                {"task": {"value": "Gone"}, "vertexId": "3"},
            ],
        },
        {
            "type": "taskUnix",
            "name": "A",
            "credentials": "svc",
            "script": "load.sh",
            "agentVar": "${agent}",
            "virtualResources": [{"resource": "db-slots", "amount": 1}],
        },
        {"type": "taskUnix", "name": "Unrelated"},
    ]
    fake.objects["triggers"] = [
        {"type": "triggerTime", "name": "Nightly", "tasks": ["WF"], "calendar": "Business"},
        {"type": "triggerTime", "name": "Other", "tasks": ["Unrelated"]},
    ]
    return fake


def test_with_members_brings_ftp_credentials_and_watched_services(tmp_path: Path) -> None:
    fake = _controller()
    fake.objects["credentials"].append({"type": "credential", "name": "remote", "runtimeUser": "u"})
    fake.objects["business_services"].append({"type": "businessService", "name": "UDMG_Primary"})
    fake.objects["tasks"] = [
        {
            "type": "taskUniversalMonitor",
            "name": "Mon",
            "eventBusinessServiceCriteria": "Member of Specific Business Services",
            "eventBusinessServices": ["UDMG_Primary"],
        },
        {
            "type": "taskFtp",
            "name": "Ftp",
            "primaryCredentials": "svc",
            "secondaryCredentials": "remote",
        },
    ]
    found = collect(["Mon", "Ftp"], kinds_for("task"), Workspace(tmp_path), fake, with_members=True)
    names = [(t, o.get("name")) for o, t in found.objects]
    assert ("businessService", "UDMG_Primary") in names
    assert ("credential", "svc") in names and ("credential", "remote") in names
    assert found.warnings == []


def test_with_members(tmp_path: Path, templates: TemplateSet) -> None:
    fake = _workflow_controller()
    lines: list[str] = []
    found = collect(
        ["WF"], kinds_for("task"), Workspace(tmp_path), fake, with_members=True, echo=lines.append
    )
    names = [(t, o.get("name") or o.get("scriptName")) for o, t in found.objects]
    assert names == [
        ("taskWorkflow", "WF"),
        ("taskUnix", "A"),
        ("triggerTime", "Nightly"),
        ("credential", "svc"),
        ("script", "load.sh"),
        ("virtualResource", "db-slots"),
        ("calendar", "Business"),
        ("businessService", "Ops"),
        ("customDay", "Xmas"),
    ]
    assert found.warnings == ["taskWorkflow 'WF' names task 'Gone', which was not found"]
    # A line per object as it is read, with the time it took. The total is what
    # has been found so far: it grows a generation of members at a time.
    assert [re.sub(r"  \d+\.\ds$", "", line) for line in lines] == [
        "[1/1] taskWorkflow 'WF'",
        "   no triggers in the cache; listing them from the controller...",
        "[2/4] taskUnix 'A'",
        "[3/4] no object named 'Gone'",
        "[4/4] triggerTime 'Nightly'",
        "[5/8] credential 'svc'",
        "[6/8] script 'load.sh'",
        "[7/8] virtualResource 'db-slots'",
        "[8/8] calendar 'Business'",
        "[9/10] businessService 'Ops'",
        "[10/10] customDay 'Xmas'",
    ]
    text, _ = export_text(["WF"], "task", Workspace(tmp_path), fake, templates, with_members=True)
    order = [k for k in yamlio.load(text) if k not in ("version", "style", "create_only")]
    assert order == [
        "business_services",
        "credentials",
        "virtual_resources",
        "custom_days",
        "calendars",
        "scripts",
        "tasks",
        "triggers",
    ]


# --- the cache says which kind, never what the object holds ---------------------------


def _cached(ws: Workspace, key: str, obj: dict[str, Any]) -> None:
    ws.cache.mkdir(parents=True, exist_ok=True)
    ws.cache_file(key).write_text(json.dumps([obj]))


def test_export_reads_the_controller_not_the_stale_cache(
    tmp_path: Path, templates: TemplateSet
) -> None:
    """Found live: `export` wrote a task as it was at the last `learn`.

    The task had been changed on the controller since. The cache still held
    the version it was copied from, so the file looked perfectly normal and
    said the wrong thing. A bundle file is a promise about a controller, so
    the content comes from that controller.
    """
    ws = Workspace(tmp_path)
    fake = _controller()
    fake.objects["tasks"] = [
        {"type": "taskUnix", "name": "T", "command": "the one on the controller", "agent": "a"}
    ]
    _cached(ws, "tasks", {"type": "taskUnix", "name": "T", "command": "stale", "agent": "a"})

    text, warnings = export_text(["T"], "task", ws, fake, templates)
    assert "the one on the controller" in text
    assert "stale" not in text
    assert warnings == []


def test_an_object_only_in_the_cache_is_written_but_reported(
    tmp_path: Path, templates: TemplateSet
) -> None:
    """Deleted on the controller since the last learn: say so, do not pretend."""
    ws = Workspace(tmp_path)
    fake = _controller()
    fake.objects["tasks"] = []
    _cached(ws, "tasks", {"type": "taskUnix", "name": "T", "command": "ls", "agent": "a"})

    text, warnings = export_text(["T"], "task", ws, fake, templates)
    assert "command: ls" in text
    assert warnings == ["tasks/T: not on the controller; wrote the cached copy"]


def test_the_cache_is_parsed_once_per_version_of_the_file(tmp_path: Path) -> None:
    """Every lookup parsed the whole file: 0.13 s each for a 25.6 MB tasks file."""
    ws = Workspace(tmp_path)
    _cached(ws, "tasks", {"type": "taskUnix", "name": "T", "command": "one"})
    first = ws.by_name("tasks")
    assert ws.by_name("tasks") is first
    _cached(ws, "tasks", {"type": "taskUnix", "name": "T", "command": "two, after learn"})
    assert ws.by_name("tasks")["T"]["command"] == "two, after learn"
    # The parsed file is shared: what `find` hands out from it is a copy.
    obj, _ = find("T", kinds_for("task"), ws, FakeTransport())
    obj["command"] = "changed"
    assert ws.by_name("tasks")["T"]["command"] == "two, after learn"


# --- reading in parallel ----------------------------------------------------------


class _Late(FakeTransport):
    """The credential answers last."""

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None:
        if name == "svc":
            time.sleep(0.1)
        return super().get_in(kind, name)


def test_parallel_reading_writes_the_same_file(tmp_path: Path, templates: TemplateSet) -> None:
    """The answers come in any order; what is kept comes in the order asked."""
    ws = Workspace(tmp_path)
    one = collect(["WF"], kinds_for("task"), ws, _workflow_controller(), with_members=True)
    fake = _workflow_controller()
    late = _Late(objects=fake.objects, raw=fake.raw)
    lines: list[str] = []
    four = collect(
        ["WF"], kinds_for("task"), ws, late, with_members=True, echo=lines.append, parallel=4
    )
    assert four.objects == one.objects and four.warnings == one.warnings
    # Asked first in its generation of four, it answered last: its line says so.
    assert any(line.startswith("[8/8] credential 'svc'  ") for line in lines)
    text, _ = export_text(["WF"], "task", ws, _workflow_controller(), templates, with_members=True)
    assert export_text(
        ["WF"], "task", ws, late, templates, with_members=True, parallel=4
    ) == (text, one.warnings)


class _Broken(FakeTransport):
    """Every `get` takes 10 ms, and the one for T1 fails."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(objects={"tasks": [{"type": "taskUnix", "name": n} for n in names]})
        self.asked: list[str] = []

    def get_in(self, kind: Kind, name: str) -> dict[str, Any] | None:
        self.asked.append(name)
        if name == "T1":
            raise TransportError("uac task get 'T1': no answer after 900s")
        time.sleep(0.01)
        return super().get_in(kind, name)


def test_a_failed_call_stops_the_reading(tmp_path: Path) -> None:
    """The export fails as it did one by one, and the reads not yet started never are."""
    names = [f"T{i}" for i in range(40)]
    fake = _Broken(names)
    with pytest.raises(TransportError, match="no answer after 900s"):
        collect(names, kinds_for("task"), Workspace(tmp_path), fake, parallel=2)
    assert len(set(fake.asked)) < 10
