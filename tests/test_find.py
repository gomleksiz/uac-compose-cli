"""`uac-compose find`: build and test a change-file filter, one line at a time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import apply, find, where
from uac_compose.cli import cli
from uac_compose.transport import FakeTransport


def _unix(name: str, agent: str, command: str, **fields: Any) -> dict[str, Any]:
    return {"type": "taskUnix", "name": name, "sysId": name, "agent": agent, "command": command}


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport(
        objects={
            "tasks": [
                _unix("Nightly Sleep", "AGT-01", "sleep 30"),
                _unix("Nightly Load", "AGT-01", "load.sh"),
                _unix("Day Sleep", "AGT-02", "sleep 5"),
                {"type": "taskWindows", "name": "Win", "sysId": "w", "agent": "AGT-01"},
            ],
            "credentials": [{"type": "credential", "name": "c1", "sysId": "c"}],
        }
    )


def _session(fake: FakeTransport, word: str = "task") -> find.Session:
    return find.Session.open(word, fake, lambda _: None)


def test_tasks_start_from_a_server_filter(fake: FakeTransport) -> None:
    session = _session(fake)
    assert session.needs_source and "start with a server filter" in session.opening()[0]
    said = session.handle("agent=AGT-01")  # a local test cannot start the session
    assert said[0].startswith("?? 'agent=AGT-01': start with a server filter")
    said = session.handle("agentname=AGT-01")
    assert fake.filtered == [("tasks", {"agentname": "AGT-01"})]
    assert said[-1] == "  3 of 3 match" and not session.needs_source


def test_a_kind_without_server_filters_lists_all(fake: FakeTransport) -> None:
    session = _session(fake, "credential")
    assert not session.needs_source
    assert session.opening()[-1] == "  1 of 1 match"


def test_type_word_narrows_and_goes_into_the_step(fake: FakeTransport) -> None:
    session = _session(fake, "linux")
    session.handle("agentname=AGT-01")
    assert session.matches() == ["Nightly Sleep", "Nightly Load"]
    assert "type: linux" in session.step("delete")


def test_tests_undo_clear_and_a_bad_regex(fake: FakeTransport) -> None:
    session = _session(fake)
    session.handle("taskname=*")
    assert session.handle("command~sleep \\d+")[0] == "  2 of 4 match (1 test)"
    assert session.handle("agent=AGT-01")[0] == "  1 of 4 match (2 tests)"
    assert session.handle("undo")[-1] == "  2 of 4 match (1 test)"
    assert session.handle("where: {agent: [AGT-02]}")[0] == "  1 of 4 match (2 tests)"
    assert session.handle("clear") == ["  4 of 4 match"]
    assert session.handle("command~(")[0].startswith("?? command: '(' is not a valid regex")
    assert session.tests == []


def test_fields_values_and_show_help_to_write_a_test(fake: FakeTransport) -> None:
    session = _session(fake)
    session.handle("taskname=*")
    fields = "\n".join(session.handle("fields"))
    assert "agent" in fields and "command" in fields
    assert session.handle("fields comm") == [session.handle("fields comm")[0]]
    assert session.handle("values agent") == ["       3  AGT-01", "       1  AGT-02"]
    assert session.handle("values type")[0] == "       3  Unix"
    shown = "\n".join(session.handle("show nightly sleep"))
    assert "command: sleep 30" in shown
    assert "did you mean 'Nightly Sleep'" in session.handle("show Nightly Slep")[0]


def test_the_step_reads_back_as_the_same_filter(fake: FakeTransport) -> None:
    session = _session(fake)
    session.handle("agentname=AGT-01")
    session.handle("command~sleep \\d{1,3}")  # a comma inside a regex is not a split
    session.handle("agent!=AGT-02")
    text = session.step("delete")
    changes = apply.read("steps:\n" + "".join(f"  {line}\n" for line in text.splitlines()))
    step = changes.steps[0]
    assert step.action == "delete" and step.names == ["*"]
    assert step.source == {"agentname": "AGT-01"}
    assert where.to_yaml(step.where) == where.to_yaml(session.tests)
    assert "set: {}" in session.step("update") and "prefix:" in session.step("rename")
    assert session.handle("step create")[0].startswith("?? step ACTION: one of")


def test_save_makes_and_adds_to_a_change_file(fake: FakeTransport, tmp_path: Path) -> None:
    session = _session(fake)
    session.handle("taskname=*Sleep*")
    path = tmp_path / "c.apply.yaml"
    assert session.handle(f"save {path}") == [f"  wrote {path} with 1 step"]
    session.handle("agent=AGT-02")
    assert session.handle(f"save {path} delete") == [f"  added step 2 to {path}"]
    steps = apply.read(path.read_text()).steps
    assert [s.source for s in steps] == [{"taskname": "*Sleep*"}] * 2
    assert steps[1].where and not steps[0].where
    other = tmp_path / "other.apply.yaml"
    other.write_text("steps:\n  - delete task: x\ntemplates: {}\n")
    assert "`steps:` is not the last key" in session.handle(f"save {other}")[0]


def test_the_command_reads_lines_from_stdin(
    fake: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: fake)
    lines = "command~sleep\nlist\nstep delete\nquit\n"
    result = CliRunner().invoke(cli, ["find", "-t", "task", "agentname=AGT-01"], input=lines)
    assert result.exit_code == 0, result.output
    assert "uac task list-advanced agentname=AGT-01: 3 tasks" in result.output
    assert "1 of 3 match (1 test)" in result.output
    assert "- delete task: '*'" in result.output and "regex: sleep" in result.output


def test_the_command_refuses_more_than_one_kind(fake: FakeTransport) -> None:
    result = CliRunner().invoke(cli, ["find", "-t", "task,credential"])
    assert result.exit_code == 1 and "names 2 kinds" in result.output
