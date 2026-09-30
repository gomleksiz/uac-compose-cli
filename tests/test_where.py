"""Field filters (`where:` and Task.find(...).where(...)) for apply and export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import where, yamlio
from uac_compose.cli import cli
from uac_compose.transport import FakeTransport

TASK: dict[str, Any] = {
    "type": "taskUnix",
    "name": "Nightly Sleep",
    "agent": "stonebranch-01",
    "command": "sleep 30",
    "actions": {"setVariableActions": [{"variableName": "A"}, {"variableName": "B"}]},
}


@pytest.mark.parametrize(
    ("spec", "held"),
    [
        ({"agent": "stone*"}, True),
        ({"agent": "STONEBRANCH-01"}, True),
        ({"agent": "other"}, False),
        ({"agent": ["x", "stone*"]}, True),
        ({"agent": {"not": "stone*"}}, False),
        ({"command": {"regex": r"sleep \d+"}}, True),
        ({"command": {"contains": "EEP"}}, True),
        ({"type": "linux"}, True),
        ({"type": "Unix"}, True),
        ({"type": "windows"}, False),
        ({"actions.setVariableActions.variableName": "B"}, True),
        ({"actions.setVariableActions.variableName": "C"}, False),
        ({"credentials": {"not": "*"}}, False),  # absent is "" and * matches it
        ({"name": "*sleep"}, True),
        ({"agent": "stone*", "command": "x"}, False),  # every test must hold
    ],
)
def test_yaml_form(spec: dict[str, Any], held: bool) -> None:
    assert where.holds(where.parse(spec), TASK, "tasks") is held


def test_chain_form_is_the_same_filter() -> None:
    q = where.parse_query(
        "Task.find(name='*sleep*').where(type=linux).where(agent='stone*', command~'\\d+')"
        ".where(agent!=x, agent=[a, 'stone*'], command*=sle)"
    )
    assert q.kind == "Task" and q.names == ["*sleep*"]
    assert [t.op for t in q.tests] == ["match", "match", "regex", "not", "any", "contains"]
    assert where.holds(q.tests, TASK, "tasks")
    assert where.parse_query("Variable.find('DB_*')").names == ["DB_*"]


@pytest.mark.parametrize(
    "bad", ["Task.find(", "Task.select(x=1)", "Task.where(agent)", "Task.where(c~'(')"]
)
def test_bad_chains(bad: str) -> None:
    with pytest.raises(where.WhereError):
        where.parse_query(bad)


# --- in apply and export ---------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = FakeTransport(
        objects={
            "tasks": [
                {**TASK, "sysId": "1"},
                {**TASK, "name": "Other Sleep", "agent": "agt-02", "sysId": "2"},
                {"type": "taskWindows", "name": "Win Sleep", "agent": "stone-w", "sysId": "3"},
            ]
        }
    )
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    return controller


def _agents(fake: FakeTransport) -> dict[str, str]:
    return {o["name"]: o.get("agent", "") for o in fake.objects["tasks"]}


def test_apply_with_a_chained_query(fake: FakeTransport, tmp_path: Path) -> None:
    path = tmp_path / "c.apply.yaml"
    path.write_text(
        "steps:\n"
        "  - update: Task.find(name='*sleep*').where(type=linux).where(agent='stone*')\n"
        "    set: {agent: stonebranch-02}\n"
    )
    result = CliRunner().invoke(cli, ["apply", str(path), "-y"])
    assert result.exit_code == 0, result.output
    assert _agents(fake) == {
        "Nightly Sleep": "stonebranch-02",
        "Other Sleep": "agt-02",
        "Win Sleep": "stone-w",
    }


def test_apply_with_a_where_key(fake: FakeTransport, tmp_path: Path) -> None:
    path = tmp_path / "c.apply.yaml"
    path.write_text("steps:\n  - delete task: '*'\n    where: {agent: {not: 'stone*'}}\n")
    result = CliRunner().invoke(cli, ["apply", str(path), "-y"])
    assert result.exit_code == 0, result.output
    assert sorted(_agents(fake)) == ["Nightly Sleep", "Win Sleep"]


def test_apply_where_that_nothing_passes(fake: FakeTransport, tmp_path: Path) -> None:
    path = tmp_path / "c.apply.yaml"
    path.write_text("steps:\n  - delete task: '*'\n    where: {agent: nobody}\n")
    result = CliRunner().invoke(cli, ["apply", str(path), "-y"])
    assert result.exit_code == 1 and "nothing passes where" in result.output
    assert fake.writes == []


def test_export_where(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "x.uac.yaml"
    result = CliRunner().invoke(
        cli, ["export", "-t", "task", "-W", "agent=stone*", "-W", "type!=windows", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert list(yamlio.load(out.read_text())["tasks"]) == ["Nightly Sleep"]
    assert "1 of 3 pass the where filter" in result.output


@pytest.mark.parametrize(
    "spec",
    [
        {"agent": "stone*"},
        {"agent": ["a", "b"]},
        {"agent": {"not": "x"}, "command": {"regex": r"sleep \d+"}},
        {"command": {"contains": "sleep"}, "type": "linux"},
        {"agent": {"not": ["a", "b"]}},
    ],
)
def test_to_yaml_is_parse_reversed(spec: dict[str, Any]) -> None:
    assert where.to_yaml(where.parse(spec)) == spec


def test_to_yaml_refuses_two_tests_on_one_field() -> None:
    tests = where.parse_terms(["agent=a*", "agent!=ab"])
    with pytest.raises(where.WhereError, match="two tests on 'agent'"):
        where.to_yaml(tests)


def test_a_term_is_one_test_so_a_regex_may_hold_brackets_and_commas() -> None:
    (test,) = where.parse_terms([r"name~(19|20)\d{2,4}"])
    assert (test.field, test.op, test.value) == ("name", "regex", r"(19|20)\d{2,4}")
    assert where.parse_terms(["agent=[a, b]"])[0].value == ["a", "b"]
