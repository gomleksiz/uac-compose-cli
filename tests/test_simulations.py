"""Simulations: a bundle section for an object with no name of its own.

The names here are made up. A simulation is keyed by its task, the workflow it
is limited to, and the vertex (simulation.py); the file keys it by a handle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from uac_compose import idioms, service, simulation
from uac_compose.compile import compile_bundle
from uac_compose.learn import Workspace
from uac_compose.service import ServiceError
from uac_compose.transport import FakeTransport, object_name
from uac_compose.yamlio import read_bundle

TASK = {"type": "taskUnix", "name": "Load Prices", "agent": "A1", "command": "echo real"}

PLAIN: dict[str, Any] = {
    "type": "simulation",
    "sysId": "s1",
    "task": "Load Prices",
    "workflow": None,
    "vertexId": "-1",
    "status": "FAILED",
    "exitCode": "3",
    "outputs": [
        {"outputType": "STDOUT", "outputData": "ORA-01403: no data found\n", "sysId": "o1"},
        {"outputType": "STDERR", "outputData": "failed", "outputFileName": None, "sysId": "o2"},
    ],
    "variables": [{"name": "rows", "value": "0"}],
    "abortActions": False,
    "emailNotificationActions": False,
    "publishStatus": False,
    "exportTable": "ops_simulation",
}

IN_WORKFLOW: dict[str, Any] = {
    "type": "simulation",
    "sysId": "s2",
    "task": "Load Prices",
    "workflow": "Daily Close",
    "vertexId": "7",
    "status": "SUCCESS",
    "exitCode": "0",
    "outputs": [{"outputType": "FILE", "outputData": "x", "outputFileName": "/tmp/x.log"}],
    "variables": [],
    "exportTable": "ops_simulation",
}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")


def test_handle_and_query() -> None:
    assert simulation.handle(PLAIN) == "Load Prices"
    assert simulation.handle(IN_WORKFLOW) == "Load Prices in Daily Close (vertex 7)"
    assert simulation.handle({**IN_WORKFLOW, "vertexId": "-1"}) == "Load Prices in Daily Close"
    assert simulation.query(PLAIN) == {"taskname": "Load Prices"}
    assert simulation.query(IN_WORKFLOW) == {
        "taskname": "Load Prices",
        "workflowname": "Daily Close",
        "vertexid": "7",
    }
    # As the controller answers it: no type, the export table only.
    assert object_name({k: v for k, v in IN_WORKFLOW.items() if k != "type"}) == (
        "Load Prices in Daily Close (vertex 7)"
    )


def test_outputs_idiom_is_lossless_only() -> None:
    rows = [{"outputType": "STDOUT", "outputData": "a"}, {"outputType": "STDERR"}]
    assert idioms.emit_outputs(rows) == {"STDOUT": "a", "STDERR": None}
    assert idioms.parse_outputs({"STDOUT": "a", "STDERR": None}) == rows
    # A file name, or a type twice, keeps UAC's own rows.
    assert idioms.emit_outputs([{"outputType": "FILE", "outputFileName": "f"}]) is None
    assert idioms.emit_outputs([{"outputType": "STDOUT"}, {"outputType": "STDOUT"}]) is None


def test_export_is_short(ws: Workspace) -> None:
    fake = FakeTransport(objects={"simulations": [dict(PLAIN), dict(IN_WORKFLOW)]})
    text = service.export_bundle(fake, ws, ["Load*"], kind="simulation", yes=True).text
    sims = read_bundle(text).sections["simulations"]
    plain = sims["Load Prices"]
    assert plain["outputs"] == {"STDOUT": "ORA-01403: no data found\n", "STDERR": "failed"}
    assert plain["variables"] == [{"rows": "0"}]  # the name/value idiom
    assert "task" not in plain and "vertexId" not in plain and "publishStatus" not in plain
    scoped = sims["Load Prices in Daily Close (vertex 7)"]
    assert scoped["task"] == "Load Prices"  # the key is not the task: it stays
    assert scoped["workflow"] == "Daily Close" and scoped["vertexId"] == "7"
    assert scoped["outputs"][0]["outputFileName"] == "/tmp/x.log"  # raw rows


def test_round_trip_is_reuse(ws: Workspace) -> None:
    fake = FakeTransport(objects={"simulations": [dict(PLAIN), dict(IN_WORKFLOW)]})
    for style in ("compact", "standard"):
        text = service.export_bundle(
            fake, ws, ["Load*"], kind="simulation", yes=True, style=style
        ).text
        out: list[str] = []
        service.import_bundle(fake, ws, text, dry_run=True, out=out.append)
        assert "reuse    simulations/Load Prices" in out, style
        assert "reuse    simulations/Load Prices in Daily Close (vertex 7)" in out, style


def test_status_takes_the_ui_word() -> None:
    text = "simulations:\n  Load Prices: {status: Failed}\n"
    (planned,) = compile_bundle(read_bundle(text))
    assert planned.obj["status"] == "FAILED"
    assert planned.obj["task"] == "Load Prices"


def test_create_after_its_task_and_update(ws: Workspace) -> None:
    text = (
        "tasks:\n"
        "  Load Prices: {type: Unix, agent: A1, command: echo real}\n"
        "simulations:\n"
        "  Load Prices:\n"
        "    status: Failed\n"
        "    exitCode: '2'\n"
        "    outputs: {STDOUT: simulated}\n"
        "    variables:\n"
        "    - rows: '0'\n"
    )
    target = FakeTransport()
    service.import_bundle(target, ws, text)
    assert [obj["type"] for _, obj in target.writes] == ["taskUnix", "simulation"]
    sent = target.writes[1][1]
    assert sent["outputs"] == [{"outputType": "STDOUT", "outputData": "simulated"}]
    # A task variable's defaults (allowEmptyOption...) are not a simulation's.
    assert sent["variables"] == [{"name": "rows", "value": "0"}]
    changed = text.replace("exitCode: '2'", "exitCode: '3'")
    with pytest.raises(ServiceError):
        service.import_bundle(target, ws, changed)
    service.import_bundle(target, ws, changed, update=True)
    assert target.objects["simulations"][0]["exitCode"] == "3"
    again: list[str] = []
    service.import_bundle(target, ws, changed, dry_run=True, out=again.append)
    assert "reuse    simulations/Load Prices" in again


def test_one_simulation_twice_is_refused(ws: Workspace) -> None:
    text = (
        "simulations:\n"
        "  Load Prices: {status: Failed}\n"
        "  Second handle: {task: Load Prices, status: Success}\n"
    )
    out: list[str] = []
    with pytest.raises(ServiceError):
        service.import_bundle(FakeTransport(), ws, text, dry_run=True, out=out.append)
    assert any("the same simulation as 'Load Prices'" in line for line in out), out


def test_a_bad_output_type_is_refused(ws: Workspace) -> None:
    text = "simulations:\n  Load Prices: {outputs: {STDIN: x}}\n"
    out: list[str] = []
    with pytest.raises(ServiceError):
        service.import_bundle(FakeTransport(), ws, text, dry_run=True, out=out.append)
    assert any("'STDIN' is not one of" in line for line in out), out


def test_with_simulations_brings_those_of_the_exported_tasks(ws: Workspace) -> None:
    other = {**PLAIN, "sysId": "s3", "task": "Something Else"}
    fake = FakeTransport(
        objects={
            "tasks": [dict(TASK)],
            "simulations": [dict(PLAIN), dict(IN_WORKFLOW), other],
        }
    )
    text = service.export_bundle(fake, ws, ["Load Prices"], kind="task", with_simulations=True).text
    sims = read_bundle(text).sections["simulations"]
    assert list(sims) == ["Load Prices", "Load Prices in Daily Close (vertex 7)"]
