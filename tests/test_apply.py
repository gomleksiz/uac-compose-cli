"""`uac-compose apply`: ordered update, rename and delete steps; never create."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import cli as cli_module
from uac_compose.apply import ApplyError, read
from uac_compose.cli import cli
from uac_compose.transport import FakeTransport


def _unix(name: str, sys_id: str, **fields: Any) -> dict[str, Any]:
    return {"type": "taskUnix", "name": name, "sysId": sys_id, "agent": "a", **fields}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = FakeTransport(
        objects={
            "tasks": [
                _unix("Load", "1", command="load.sh"),
                _unix("tmp_a", "2", command="x"),
                _unix("tmp_b", "3", command="x"),
                {"type": "taskWindows", "name": "tmp_win", "sysId": "4", "command": "x"},
                _unix(
                    "Act",
                    "5",
                    command="y",
                    actions={
                        "setVariableActions": [
                            {"status": "Success", "variableName": "A", "variableValue": "1"},
                            {"status": "Failed", "variableName": "B", "variableValue": "2"},
                        ]
                    },
                ),
            ],
            "variables": [{"type": "variable", "name": "DB_HOST", "sysId": "9", "value": "old"}],
        }
    )
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    return controller


def _apply(tmp_path: Path, text: str, *args: str) -> tuple[int, str]:
    path = tmp_path / "c.apply.yaml"
    path.write_text(text)
    result = CliRunner().invoke(cli, ["apply", str(path), *args])
    return result.exit_code, result.output


def _task(fake: FakeTransport, name: str) -> dict[str, Any] | None:
    return next((o for o in fake.objects["tasks"] if o["name"] == name), None)


# --- reading the file ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("steps:\n  - create task: X\n", "apply never creates"),
        ("steps:\n  - delete widget: X\n", "unknown kind 'widget'"),
        ("steps:\n  - update task: X\n", "update needs `set:`"),
        ("steps:\n  - rename task: X\n", "rename needs one of"),
        ("steps:\n  - delete task: X\n    set: {a: 1}\n", "delete takes no set"),
        ("steps:\n  - update task: X\n    remove: {actions.x: [1]}\n", "give the fields"),
        ("steps: []\n", "must be a list of steps"),
    ],
)
def test_bad_files_are_refused_before_anything_runs(text: str, said: str) -> None:
    with pytest.raises(ApplyError, match=said.replace("(", r"\(").replace("`", ".")):
        read(text)


# --- plan and run -------------------------------------------------------------------


def test_update_one_field(fake: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - update task: Load\n    set: {command: load.sh --fast}\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert "[1] update    tasks/Load" in out and "done: 1 change(s) to 1 object(s)" in out
    task = _task(fake, "Load")
    assert task is not None and task["command"] == "load.sh --fast" and task["sysId"] == "1"


def test_dry_run_writes_nothing_and_no_terminal_needs_yes(
    fake: FakeTransport, tmp_path: Path
) -> None:
    text = "steps:\n  - delete task: Load\n"
    code, out = _apply(tmp_path, text, "-n")
    assert code == 0 and "[1] delete    tasks/Load" in out and "nothing was written" in out
    code, out = _apply(tmp_path, text)
    assert code == 1 and "add -y to run them" in out
    assert fake.writes == []


def test_the_question_on_a_terminal(
    fake: FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "_ask", lambda _: "n")
    code, out = _apply(tmp_path, "steps:\n  - delete task: Load\n")
    assert code == 1 and fake.writes == []
    monkeypatch.setattr(cli_module, "_ask", lambda _: "y")
    code, out = _apply(tmp_path, "steps:\n  - delete task: Load\n")
    assert code == 0 and _task(fake, "Load") is None


def test_delete_by_list_and_by_pattern_with_type(fake: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - delete task: 'TMP_*'\n    type: linux\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert _task(fake, "tmp_a") is None and _task(fake, "tmp_b") is None
    assert _task(fake, "tmp_win") is not None  # a Windows task: not `linux`
    code, out = _apply(tmp_path, "steps:\n  - delete task: [tmp_win]\n", "-y")
    assert code == 0 and _task(fake, "tmp_win") is None


def test_a_missing_object_is_an_error(fake: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - update task: Gone\n    set: {command: x}\n", "-y")
    assert code == 1 and "not on the controller: 'Gone' (apply never creates)" in out
    assert fake.writes == []


def test_a_misspelt_field_is_an_error(fake: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - update task: Load\n    set: {comand: x}\n", "-y")
    assert code == 1 and "'comand' is not a field of taskUnix" in out and fake.writes == []


def test_rename_to_then_update_the_new_name(fake: FakeTransport, tmp_path: Path) -> None:
    text = (
        "steps:\n"
        "  - rename task: Load\n    to: DEV_Load\n"
        "  - update task: DEV_Load\n    set: {command: dev.sh}\n"
    )
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert "[1] rename    tasks/Load -> DEV_Load" in out
    task = _task(fake, "DEV_Load")
    assert task is not None and task["sysId"] == "1" and task["command"] == "dev.sh"
    assert _task(fake, "Load") is None


@pytest.mark.parametrize(
    ("how", "names"),
    [
        ("prefix: DEV_", ["DEV_tmp_a", "DEV_tmp_b"]),
        ("suffix: _old", ["tmp_a_old", "tmp_b_old"]),
        ("replace: {from: '^TMP', to: work}", ["work_a", "work_b"]),
    ],
)
def test_rename_many(fake: FakeTransport, tmp_path: Path, how: str, names: list[str]) -> None:
    code, out = _apply(tmp_path, f"steps:\n  - rename task: 'tmp_?'\n    {how}\n", "-y")
    assert code == 0, out
    assert all(_task(fake, n) is not None for n in names)


def test_rename_onto_an_existing_name_is_refused(fake: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - rename task: tmp_a\n    to: tmp_b\n", "-y")
    assert code == 1 and "'tmp_b' exists already" in out and fake.writes == []


def test_child_rows_add_remove_change(fake: FakeTransport, tmp_path: Path) -> None:
    text = """steps:
  - update task: Act
    remove:
      actions.setVariableActions: {variableName: B}
    change:
      actions.setVariableActions:
        where: {variableName: A}
        set: {variableValue: '10'}
    add:
      actions.setVariableActions:
        - {status: Success, variableName: C, variableValue: '3'}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert "-1 setVariableActions" in out and "+1 setVariableActions" in out
    task = _task(fake, "Act")
    assert task is not None
    rows = task["actions"]["setVariableActions"]
    assert [(r["variableName"], r["variableValue"]) for r in rows] == [("A", "10"), ("C", "3")]


def test_like_from_templates(fake: FakeTransport, tmp_path: Path) -> None:
    text = """templates:
  fast: {command: fast.sh, agent: AGT-2}
steps:
  - update task: [Load, tmp_a]
    set: {like: fast, agent: AGT-3}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    for name in ("Load", "tmp_a"):
        task = _task(fake, name)
        assert task is not None and (task["command"], task["agent"]) == ("fast.sh", "AGT-3")


def test_update_a_variable(fake: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(
        tmp_path, "steps:\n  - update variable: 'db_*'\n    set: {value: new}\n", "-y"
    )
    assert code == 0, out
    assert fake.objects["variables"][0]["value"] == "new"


def test_no_change_writes_nothing(fake: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - update task: Load\n    set: {command: load.sh}\n")
    assert code == 0 and "no change" in out and "nothing to change" in out


def test_the_first_failed_write_stops_the_rest(fake: FakeTransport, tmp_path: Path) -> None:
    def refuse(kind: Any, name: str) -> None:
        from uac_compose.transport import TransportError

        raise TransportError(f"uac task delete {name!r}: HTTP 400: in use by a workflow")

    text = (
        "steps:\n"
        "  - update task: Load\n    set: {command: a.sh}\n"
        "  - delete task: tmp_a\n"
        "  - update task: tmp_b\n    set: {command: b.sh}\n"
    )
    fake.delete = refuse  # type: ignore[method-assign]
    code, out = _apply(tmp_path, text, "-y")
    assert code == 1 and "in use by a workflow" in out
    assert "step 2 (delete task) failed on tasks/tmp_a; 1 of 3 change(s) ran" in out
    load, b = _task(fake, "Load"), _task(fake, "tmp_b")
    assert load is not None and load["command"] == "a.sh"
    assert b is not None and b["command"] == "x"  # never reached


def test_the_example_file_reads() -> None:
    example = Path(__file__).parent.parent / "examples" / "changes.apply.yaml"
    changes = read(example.read_text())
    assert [s.action for s in changes.steps] == [
        "delete",
        "delete",
        "delete",
        "rename",
        "update",
        "update",
        "update",
        "rename",
        "rename",
        "replace",
        "update",
        "delete",
        "update",
        "update",
        "delete",
    ]
    assert "night-agent" in changes.templates


# --- replace a task in workflows ----------------------------------------------------


def _wf(name: str, sys_id: str, *members: str) -> dict[str, Any]:
    vertices = [
        {"task": {"value": m}, "vertexId": str(i), "vertexX": "10", "vertexY": str(i * 50)}
        for i, m in enumerate(members, 1)
    ]
    return {"type": "taskWorkflow", "name": name, "sysId": sys_id, "workflowVertices": vertices}


@pytest.fixture
def flows(fake: FakeTransport) -> FakeTransport:
    fake.objects["tasks"] += [
        _wf("Nightly A", "11", "Load", "tmp_a"),
        _wf("Nightly B", "12", "Load", "Load"),
        _wf("Weekly", "13", "Load"),
    ]
    return fake


def _members(fake: FakeTransport, name: str) -> list[str]:
    wf = _task(fake, name)
    assert wf is not None
    return [v["task"]["value"] for v in wf["workflowVertices"]]


def test_replace_a_task_in_matching_workflows(flows: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - replace task: Load\n    with: tmp_b\n    in: 'nightly*'\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert "[1] replace   tasks/Nightly B" in out and "vertex 'Load' -> 'tmp_b' (2 places)" in out
    assert _members(flows, "Nightly A") == ["tmp_b", "tmp_a"]
    assert _members(flows, "Nightly B") == ["tmp_b", "tmp_b"]
    assert _members(flows, "Weekly") == ["Load"]  # not in `in:`
    wf = _task(flows, "Nightly A")
    assert wf is not None and wf["workflowVertices"][0]["vertexId"] == "1"  # shape kept


def test_replace_in_every_workflow_by_default(flows: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - replace task: Load\n    with: tmp_b\n", "-y")
    assert code == 0, out
    assert _members(flows, "Weekly") == ["tmp_b"]
    assert "done: 3 change(s) to 3 object(s)" in out


def test_replace_then_delete_the_old_task(flows: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - replace task: Load\n    with: tmp_b\n  - delete task: Load\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert _task(flows, "Load") is None and _members(flows, "Weekly") == ["tmp_b"]


def test_replace_needs_the_new_task_and_a_workflow_that_uses_the_old(
    flows: FakeTransport, tmp_path: Path
) -> None:
    code, out = _apply(tmp_path, "steps:\n  - replace task: Load\n    with: Nope\n", "-y")
    assert code == 1 and "'Nope' is not on the controller" in out
    code, out = _apply(tmp_path, "steps:\n  - replace task: tmp_b\n    with: Load\n", "-y")
    assert code == 1 and "no workflow in ['*'] runs 'tmp_b'" in out
    with pytest.raises(ApplyError, match="replace needs `with:`"):
        read("steps:\n  - replace task: Load\n")
    with pytest.raises(ApplyError, match="replace names one task"):
        read("steps:\n  - replace variable: X\n    with: Y\n")


# --- action rows as objects -----------------------------------------------------------


def _mailer(name: str, sys_id: str, *to: str) -> dict[str, Any]:
    rows = [{"status": "Failed", "to": t, "subject": "x"} for t in to]
    return _unix(name, sys_id, command="z", actions={"emailNotifications": rows})


@pytest.fixture
def mail(fake: FakeTransport) -> FakeTransport:
    fake.objects["tasks"] += [
        _mailer("M1", "21", "jane.doe@example.com, ops@example.com", "dev@x.com"),
        _mailer("M2", "22", "JANE.DOE@example.com"),
        _mailer("M3", "23", "nobody@x.com"),
    ]
    return fake


def _to(fake: FakeTransport, name: str) -> list[str]:
    task = _task(fake, name)
    assert task is not None
    return [r["to"] for r in task["actions"]["emailNotifications"]]


def test_edit_email_actions_that_send_to_someone(mail: FakeTransport, tmp_path: Path) -> None:
    text = """steps:
  - update email action: '*'
    where: {to: {contains: jane.doe}}
    edit:
      to: {from: 'jane\\.doe@example\\.com', to: 'team@example.com'}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert _to(mail, "M1") == ["team@example.com, ops@example.com", "dev@x.com"]
    assert _to(mail, "M2") == ["team@example.com"]
    assert _to(mail, "M3") == ["nobody@x.com"]
    assert "done: 2 change(s) to 2 object(s)" in out  # tasks without such a row: untouched


def test_set_and_delete_rows_of_a_kind(mail: FakeTransport, tmp_path: Path) -> None:
    text = """steps:
  - update email action: [M1, M3]
    where: {to: 'dev@*'}
    set: {subject: moved}
  - delete email action: 'M*'
    where: {to: {regex: '^nobody'}}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    m1 = _task(mail, "M1")
    assert m1 is not None and m1["actions"]["emailNotifications"][1]["subject"] == "moved"
    assert _to(mail, "M3") == []


def test_row_kinds_need_where_and_a_matching_row(mail: FakeTransport, tmp_path: Path) -> None:
    with pytest.raises(ApplyError, match="say which rows with `where:`"):
        read("steps:\n  - delete email action: '*'\n")
    with pytest.raises(ApplyError, match="can be updated or deleted"):
        read("steps:\n  - rename variable action: X\n    where: {a: b}\n    to: y\n")
    text = "steps:\n  - delete email action: '*'\n    where: {to: '*@nowhere*'}\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 1 and "no task here has a actions.emailNotifications row" in out
    assert mail.writes == []


def test_change_rows_match_with_wildcards_now(fake: FakeTransport, tmp_path: Path) -> None:
    text = """steps:
  - update task: Act
    change:
      actions.setVariableActions:
        where: {variableName: {not: 'a*'}}
        set: {variableValue: '99'}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    task = _task(fake, "Act")
    assert task is not None
    rows = task["actions"]["setVariableActions"]
    assert [(r["variableName"], r["variableValue"]) for r in rows] == [("A", "1"), ("B", "99")]


def test_a_change_that_names_a_missing_object_is_refused(
    fake: FakeTransport, tmp_path: Path
) -> None:
    text = """steps:
  - update task: Act
    add:
      actions.systemOperations:
        - {status: Failed, operation: Launch Task, task: Nope}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 1 and "'Act' would name task 'Nope', which is not on the controller" in out
    assert fake.writes == []
    code, out = _apply(tmp_path, text.replace("Nope", "Load"), "-y")
    assert code == 0, out


def test_a_name_a_rename_step_made_counts(fake: FakeTransport, tmp_path: Path) -> None:
    text = """steps:
  - rename task: tmp_a
    to: Fresh
  - update task: Act
    add:
      actions.systemOperations:
        - {status: Failed, operation: Launch Task, task: Fresh}
"""
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out


def test_every_cookbook_change_file_reads() -> None:
    """A recipe that no longer parses is worse than none."""
    import re

    cookbook = (Path(__file__).parent.parent / "docs" / "COOKBOOK.md").read_text()
    blocks = [
        b
        for b in re.findall(r"```yaml\n(.*?)```", cookbook, re.S)
        if "steps:" in b and "<action>" not in b
    ]
    assert len(blocks) >= 12
    for block in blocks:
        read(block)


def test_a_task_a_workflow_runs_is_not_deleted(flows: FakeTransport, tmp_path: Path) -> None:
    code, out = _apply(tmp_path, "steps:\n  - delete task: Load\n", "-n")
    assert code == 1
    assert "cannot delete 'Load': workflow(s) 'Nightly A', 'Nightly B', 'Weekly' run it" in out
    assert flows.writes == []


def test_deleting_the_workflows_first_frees_the_task(flows: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - delete task: ['Nightly*', Weekly]\n  - delete task: Load\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert _task(flows, "Load") is None


def test_one_step_may_delete_a_workflow_and_its_task(flows: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - delete task: [Load, tmp_a, 'Nightly*', Weekly]\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out


def test_uac_workflows_of_reads_the_answer_and_a_404() -> None:
    import subprocess

    from uac_compose.transport import UacCli

    def answer(stdout: str, stderr: str = "", code: int = 0) -> Any:
        return lambda argv: subprocess.CompletedProcess(argv, code, stdout, stderr)

    rows = '[{"name": "WF1", "vertexId": "1"}, {"name": "WF1", "vertexId": "2"}, {"name": "WF2"}]'
    assert UacCli("p", answer(rows)).workflows_of("T") == ["WF1", "WF2"]
    gone = (
        "ERROR:root:https://x Response Code : 404\n"
        'ERROR:root:Failed with reason : Task with name "T" not found.'
    )
    assert UacCli("p", answer("", gone, 1)).workflows_of("T") == []


def _wired_wf() -> dict[str, Any]:
    """Load -> tmp_a, and a run criteria row on Load's vertex: all name the task."""
    wf = _wf("Wired", "31", "Load", "tmp_a")
    wf["workflowEdges"] = [
        {
            "condition": {"value": "Success"},
            "sourceId": {"taskName": "Load", "value": "1"},
            "targetId": {"taskName": "tmp_a", "value": "2"},
        }
    ]
    wf["runCriteria"] = [
        {"sysId": "rc1", "type": "Run Criteria", "task": "Load", "vertexId": "1", "holiday": True}
    ]
    return wf


def test_replace_also_moves_edges_and_run_criteria(fake: FakeTransport, tmp_path: Path) -> None:
    """Measured live: changing only the vertex is refused ("Task Run Criteria is invalid")."""
    fake.objects["tasks"].append(_wired_wf())
    text = "steps:\n  - replace task: Load\n    with: tmp_b\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert "workflowEdges: 1 follow" in out and "runCriteria: 1 follow" in out
    wf = _task(fake, "Wired")
    assert wf is not None
    assert [v["task"]["value"] for v in wf["workflowVertices"]] == ["tmp_b", "tmp_a"]
    edge = wf["workflowEdges"][0]
    assert (edge["sourceId"]["taskName"], edge["targetId"]["taskName"]) == ("tmp_b", "tmp_a")
    assert wf["runCriteria"][0]["task"] == "tmp_b" and wf["runCriteria"][0]["sysId"] == "rc1"


def test_swap_task_leaves_other_vertices_alone() -> None:
    from uac_compose.references import swap_task

    wf = _wired_wf()
    fields, hits, follow = swap_task(wf, "tmp_a", "X")
    assert hits == 1 and follow == {"workflowEdges": 1, "runCriteria": 0}
    assert fields["workflowEdges"][0]["sourceId"]["taskName"] == "Load"
    assert fields["runCriteria"][0]["task"] == "Load"
    assert wf["workflowEdges"][0]["targetId"]["taskName"] == "tmp_a"  # the input is not changed


# --- backup ---------------------------------------------------------------------------

CHANGES = (
    "steps:\n"
    "  - update task: Load\n    set: {command: new.sh}\n"
    "  - rename task: tmp_a\n    to: tmp_a2\n"
    "  - update task: tmp_a2\n    set: {command: renamed.sh}\n"
    "  - delete task: tmp_b\n"
    "  - update variable: DB_HOST\n    set: {value: new}\n"
)


def test_backup_json_saves_each_object_once_as_it_was(
    fake: FakeTransport, tmp_path: Path
) -> None:
    import json

    fake.objects["credentials"] = [
        {"type": "credential", "name": "c", "sysId": "c1", "runtimePassword": "s3cret"}
    ]
    path = tmp_path / "before.json"
    text = CHANGES + "  - update credential: c\n    set: {runtimeUser: u}\n"
    code, out = _apply(tmp_path, text, "-y", "--backup", str(path))
    assert code == 0, out
    assert out.index("wrote backup") < out.index("done:")
    saved = json.loads(path.read_text())
    got = {(o["kind"], o["name"]): o for o in saved["objects"]}
    assert list(got) == [
        ("tasks", "Load"),
        ("tasks", "tmp_a"),
        ("tasks", "tmp_b"),
        ("variables", "DB_HOST"),
        ("credentials", "c"),
    ]
    assert got[("tasks", "Load")]["object"]["command"] == "load.sh"
    assert got[("tasks", "tmp_a")]["after"] == "renamed to tmp_a2"
    assert got[("tasks", "tmp_a")]["object"]["command"] == "x"
    assert got[("tasks", "tmp_b")]["after"] == "deleted"
    assert got[("variables", "DB_HOST")]["object"]["value"] == "old"
    assert "runtimePassword" not in got[("credentials", "c")]["object"]  # rule 8
    assert "s3cret" not in path.read_text()


def test_backup_compose_and_import_u_undoes_the_file(fake: FakeTransport, tmp_path: Path) -> None:
    from uac_compose.cli import cli as the_cli

    before = {k: [dict(o) for o in v] for k, v in fake.objects.items()}
    path = tmp_path / "before.uac.yaml"
    code, out = _apply(tmp_path, CHANGES, "-y", "-B", str(path))
    assert code == 0, out
    assert f"import {path} -u" in out
    assert "was: tmp_a2" in path.read_text()
    assert _task(fake, "tmp_b") is None and _task(fake, "tmp_a") is None
    result = CliRunner().invoke(the_cli, ["import", str(path), "-u"])
    assert result.exit_code == 0, result.output
    for key in ("tasks", "variables"):
        now = {o["name"]: o for o in fake.objects[key]}
        for old in before[key]:
            assert old["name"] in now, old["name"]
            for field_name in ("command", "value"):
                assert now[old["name"]].get(field_name) == old.get(field_name)
    tmp_a = _task(fake, "tmp_a")
    assert tmp_a is not None and tmp_a["sysId"] == "2"  # renamed back, not made again
    assert _task(fake, "tmp_a2") is None


def test_backup_compose_states_the_old_default_and_warns_for_added_rows(
    fake: FakeTransport, tmp_path: Path
) -> None:
    """Import compares only what the file states (rule 6), and an export leaves
    defaults out: the backup states every field the plan changes."""
    from uac_compose.cli import cli as the_cli

    row = {"status": "Failed", "variableName": "A", "variableValue": "1"}
    fake.objects["tasks"].append(
        _unix("Full", "f1", command="c", retryMaximum=0, logLevel="Inherited")
        | {"actions": {"setVariableActions": [row]}}
    )
    text = (
        "steps:\n  - update task: Full\n    set: {retryMaximum: 3, logLevel: Debug}\n"
        "    add:\n      actions.setVariableActions:\n"
        "        - {status: Success, variableName: X, variableValue: '1'}\n"
    )
    path = tmp_path / "before.uac.yaml"
    code, out = _apply(tmp_path, text, "-y", "-B", str(path))
    assert code == 0, out
    assert "retryMaximum: 0" in path.read_text()  # a default, stated on purpose
    assert "cannot undo actions of tasks/Full" in out  # import never removes a row
    result = CliRunner().invoke(the_cli, ["import", str(path), "-u"])
    assert result.exit_code == 0, result.output
    task = _task(fake, "Full")
    assert task is not None and task["retryMaximum"] == 0 and task["logLevel"] == "Inherited"


def test_backup_never_overwrites_and_is_not_written_by_a_dry_run(
    fake: FakeTransport, tmp_path: Path
) -> None:
    path = tmp_path / "before.uac.yaml"
    code, out = _apply(tmp_path, CHANGES, "-n", "-B", str(path))
    assert code == 0 and "no backup written" in out and not path.exists()
    path.write_text("an older backup\n")
    code, out = _apply(tmp_path, CHANGES, "-y", "-B", str(path))
    assert code == 1 and "exists; give a new file name" in out
    assert fake.writes == [] and path.read_text() == "an older backup\n"


def test_a_failed_backup_changes_nothing(fake: FakeTransport, tmp_path: Path) -> None:
    path = tmp_path / "no-such-dir" / "before.json"
    code, out = _apply(tmp_path, CHANGES, "-y", "-B", str(path))
    assert code == 1 and "backup failed, nothing was changed" in out
    assert fake.writes == []


def test_backup_format_from_the_file_name() -> None:
    from uac_compose.apply import backup_format

    assert backup_format("a.json", None) == "json"
    assert backup_format("a.uac.yaml", None) == "compose"
    assert backup_format("a.txt", "json") == "json"


# --- from: list-advanced filters ------------------------------------------------------


def test_from_narrows_with_one_list_advanced_call(fake: FakeTransport, tmp_path: Path) -> None:
    fake.objects["tasks"].append(_unix("Load2", "12", command="x", agent="b"))
    text = "steps:\n  - delete task: '*'\n    from: {agentname: b}\n"
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    assert fake.filtered == [("tasks", {"agentname": "b"})]
    assert _task(fake, "Load2") is None and _task(fake, "Load") is not None


def test_from_with_a_name_pattern_and_where(fake: FakeTransport, tmp_path: Path) -> None:
    text = (
        "steps:\n  - update task: 'tmp_*'\n    from: {type: Unix}\n"
        "    where: {command: x}\n    set: {command: y}\n"
    )
    code, out = _apply(tmp_path, text, "-n")
    assert code == 0, out
    assert "tasks/tmp_a" in out and "tasks/tmp_b" in out
    assert "tmp_win" not in out and "tasks/Load" not in out


def test_from_is_checked_before_anything_runs() -> None:
    with pytest.raises(ApplyError, match="no filter 'agent'; use one of taskname"):
        read("steps:\n  - delete task: '*'\n    from: {agent: b}\n")
    with pytest.raises(ApplyError, match="credentials has none"):
        read("steps:\n  - delete credential: '*'\n    from: {name: b}\n")
    with pytest.raises(ApplyError, match="mapping of filter"):
        read("steps:\n  - delete task: '*'\n    from: [agentname]\n")


def test_from_does_not_see_what_an_earlier_step_deleted(
    fake: FakeTransport, tmp_path: Path
) -> None:
    text = (
        "steps:\n  - delete task: tmp_a\n"
        "  - update task: '*'\n    from: {taskname: 'tmp_*'}\n    set: {command: z}\n"
    )
    code, out = _apply(tmp_path, text, "-n", "-v")
    assert code == 0, out
    assert "[2] update    tasks/tmp_a" not in out and "[2] update    tasks/tmp_b" in out


def test_from_exact_name_must_be_in_the_answer(fake: FakeTransport, tmp_path: Path) -> None:
    text = "steps:\n  - delete task: Load\n    from: {agentname: nobody}\n"
    code, out = _apply(tmp_path, text, "-n")
    assert code == 1 and "not in from (agentname=nobody): 'Load'" in out


# --- rows in bundle syntax: like: and the short forms export writes -------------------

SNOW = """templates:
  snow:
    status: Failed
    operation: Launch Task
    description: Create ServiceNow
    task: Load
    variables:
    - failed_task_name: ${ops_task_name}
    - failed_task_id: ${ops_task_id}
"""


ADD_SNOW = "steps:\n- update task: Act\n  add:\n    actions.systemOperations:\n    - like: snow\n"


def _sysops(fake: FakeTransport, name: str) -> list[dict[str, Any]]:
    task = _task(fake, name)
    assert task is not None
    return list(task["actions"]["systemOperations"])


def test_an_added_row_may_say_like_a_template(fake: FakeTransport, tmp_path: Path) -> None:
    text = SNOW + ADD_SNOW
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    (row,) = _sysops(fake, "Act")
    assert (row["status"], row["operation"], row["task"]) == ("Failed", "Launch Task", "Load")
    assert row["description"] == "Create ServiceNow" and "like" not in row
    assert row["variables"] == [
        {"name": "failed_task_name", "value": "${ops_task_name}"},
        {"name": "failed_task_id", "value": "${ops_task_id}"},
    ]


def test_own_fields_win_over_like_and_short_variables_work(
    fake: FakeTransport, tmp_path: Path
) -> None:
    text = SNOW + (
        "steps:\n- update task: Act\n  add:\n    actions.systemOperations:\n"
        "    - like: snow\n      status: Success\n      variables:\n      - only: one\n"
    )
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    (row,) = _sysops(fake, "Act")
    assert row["status"] == "Success" and row["task"] == "Load"
    assert row["variables"] == [{"name": "only", "value": "one"}]  # a list replaces


def test_a_changed_row_set_uses_the_short_forms_too(fake: FakeTransport, tmp_path: Path) -> None:
    first = SNOW + ADD_SNOW
    assert _apply(tmp_path, first, "-y")[0] == 0
    text = (
        "steps:\n- update system action: Act\n  where: {task: Load}\n"
        "  set:\n    variables:\n    - run: ${ops_task_id}\n"
    )
    code, out = _apply(tmp_path, text, "-y")
    assert code == 0, out
    (row,) = _sysops(fake, "Act")
    assert row["variables"] == [{"name": "run", "value": "${ops_task_id}"}]
    assert row["task"] == "Load"  # the other fields of the row stay


def test_like_in_a_row_names_a_template_and_never_loops(
    fake: FakeTransport, tmp_path: Path
) -> None:
    add = "steps:\n- update task: Act\n  add:\n    actions.systemOperations:\n    - like: {}\n"
    code, out = _apply(tmp_path, add.format("nope"), "-n")
    assert code == 1 and "`like: nope` names nothing in this file" in out
    loop = "templates:\n  a: {like: b}\n  b: {like: a}\n" + add.format("a")
    code, out = _apply(tmp_path, loop, "-n")
    assert code == 1 and "`like:` loops: a -> b -> a" in out
