"""Milestone 5: workflow members, edges, joins and run criteria."""

from __future__ import annotations

from typing import Any

import pytest

from uac_compose import runcriteria, yamlio
from uac_compose.compile import CompileError, compile_object
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.idioms import without_labels
from uac_compose.workflow import canonical, emit_members, parse_join, render_join


def _wf(members: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return compile_object("tasks", "WF", {"type": "Workflow", "tasks": members, **extra})


def _vertex(task: str, vid: str, **extra: Any) -> dict[str, Any]:
    return {"task": {"value": task}, "vertexId": vid, **extra}


def _edge(src: tuple[str, str], dst: tuple[str, str], cond: Any = "Success") -> dict[str, Any]:
    return {
        "condition": cond if isinstance(cond, dict) else {"value": cond},
        "sourceId": {"taskName": src[0], "value": src[1]},
        "targetId": {"taskName": dst[0], "value": dst[1]},
    }


def test_members_become_vertices_and_edges() -> None:
    obj = _wf({"Extract": None, "Load": {"after": "Extract", "position": [120, 40.5]}})
    assert obj["workflowVertices"] == [
        _vertex("Extract", "2"),
        _vertex("Load", "3", vertexX="120", vertexY="40.5"),
    ]
    assert obj["workflowEdges"] == [_edge(("Extract", "2"), ("Load", "3"))]


def test_repeated_task_keeps_both_vertices() -> None:
    body = {
        "workflowVertices": [_vertex("Load", "2"), _vertex("Load", "3")],
        "workflowEdges": [_edge(("Load", "2"), ("Load", "3"))],
    }
    members = emit_members(body)["tasks"]
    assert members == {"Load": None, "Load (2)": {"task": "Load", "after": "Load"}}
    back = _wf(members)
    assert canonical(back) == canonical({**back, **body})


def test_variable_condition_edge_falls_back_to_raw() -> None:
    cond = {"type": "Variable", "firstValue": "${x}", "operator": "=", "secondValue": "1"}
    body = {
        "workflowVertices": [_vertex("A", "2"), _vertex("B", "3")],
        "workflowEdges": [_edge(("A", "2"), ("B", "3"), cond)],
    }
    assert emit_members(body) == body


def test_edge_conditions() -> None:
    obj = _wf(
        {"A": None, "B": {"after": [{"A": "failure"}]}, "C": {"after": {"B": "exit code 0-9"}}}
    )
    conditions = [e["condition"] for e in obj["workflowEdges"]]
    assert conditions == [{"value": "Failure"}, {"type": "Exit Code", "value": "0-9"}]


def test_edge_extras_kept() -> None:
    obj = _wf({"A": None, "B": {"after": [{"A": {"condition": "success", "straightEdge": True}}]}})
    assert obj["workflowEdges"][0]["straightEdge"] is True


def test_join_name_with_space_is_quoted() -> None:
    vertex_of = {"APPROVAL - IT": "2", "B": "3", "C": "4"}
    expr = parse_join('"APPROVAL - IT" OR (B AND C)', vertex_of, "x")
    handles = {v: k for k, v in vertex_of.items()}
    assert render_join(expr, handles) == '"APPROVAL - IT" OR (B AND C)'
    assert expr == {
        "logicalOperator": "Or",
        "conditions": [
            {"type": "condition", "vertexId": "2"},
            {
                "type": "compoundCondition",
                "logicalOperator": "And",
                "conditions": [
                    {"type": "condition", "vertexId": "3"},
                    {"type": "condition", "vertexId": "4"},
                ],
            },
        ],
    }


def test_any_is_or_and_all_is_the_default() -> None:
    assert parse_join("any", {}, "x") == {"logicalOperator": "Or"}
    assert parse_join("all", {}, "x") is None
    with pytest.raises(ValueError, match="not a member"):
        parse_join("A OR Z", {"A": "2"}, "x")


def test_vertex_id_one_rejected() -> None:
    with pytest.raises(CompileError, match="2 or more"):
        _wf({"A": {"vertexId": 1}, "B": {"vertexId": 3}})


def test_vertex_id_on_all_or_none() -> None:
    with pytest.raises(CompileError, match="some members but not all"):
        _wf({"A": {"vertexId": 5}, "B": None})


def test_after_unknown_member_rejected() -> None:
    with pytest.raises(CompileError, match="not a member"):
        _wf({"A": {"after": "Z"}})


def test_members_emitted_in_execution_order() -> None:
    body = {
        "workflowVertices": [_vertex("C", "2"), _vertex("B", "3"), _vertex("A", "4")],
        "workflowEdges": [_edge(("A", "4"), ("B", "3")), _edge(("B", "3"), ("C", "2"))],
    }
    members = emit_members(body)["tasks"]
    assert list(members) == ["A", "B", "C"]
    assert members["A"] == {"vertexId": 4}  # ids are not 2, 3, 4 in that order


def test_run_if_line() -> None:
    obj = _wf({"A": {"run_if": "business day"}, "B": {"skip_if": "mon, fri at run time"}})
    rows = obj["runCriteria"]
    assert rows[0]["businessDay"] is True and rows[0]["task"] == "A" and rows[0]["vertexId"] == "2"
    assert rows[1]["type"] == "Skip Criteria" and rows[1]["evaluateAt"] == "Run Time"
    assert rows[1]["specificDayMon"] and rows[1]["specificDayFri"]


def test_run_criteria_grammar() -> None:
    row = runcriteria.parse("1st business day of month")
    assert row["complexAdjective"] == "1st" and row["complexNouns"] == [{"value": "Business Day"}]
    assert runcriteria.render(row) == ("run_if", "1st business day of month")
    row = runcriteria.parse("${REGION} != EU and weekdays")
    assert row["variableName"] == "REGION" and row["variableOp"] == "!=" and row["specificDayWed"]
    row = runcriteria.parse("last business day of month minus 1 day")
    assert row["complexAdjustment"] == "Minus" and row["complexAdjustmentAmount"] == 1
    with pytest.raises(runcriteria.CriteriaError):
        runcriteria.parse("whenever")


def test_unrenderable_criterion_stays_raw() -> None:
    row = {"businessDay": True, "description": "keep me"}
    assert runcriteria.line_for(row, {}) is None


# --- the corpus ------------------------------------------------------------


@pytest.mark.corpus
def test_workflows_emit_members_and_compile_to_the_same_graph(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    from uac_compose.style import compact

    workflows = [e for e in corpus if e.type == "taskWorkflow"]
    with_members = 0
    for e in workflows:
        section, name, body, ok = compact.emit_object(e.obj, e.type, templates, show_inactive=True)
        with_members += "tasks" in body
        back = yamlio.load(yamlio.dump({section: {name: body}}))[section][name]
        got = compile_object(section, name, back, templates)
        want = simplify(e.obj, templates.get(e.type), templates.sub_templates)
        assert canonical(without_labels(got)) == canonical(without_labels(want)), e.name
    assert with_members > 0.9 * len([e for e in workflows if e.obj.get("workflowVertices")])


@pytest.mark.corpus
def test_run_criteria_render_and_parse_back(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    default = templates.sub_templates.get("runCriteria", {})
    total = rendered = 0
    for e in corpus:
        if e.type != "taskWorkflow":
            continue
        small = simplify(e.obj, templates.get(e.type), templates.sub_templates)
        for row in small.get("runCriteria") or []:
            row = {k: v for k, v in row.items() if k not in ("task", "vertexId")}
            total += 1
            rendered += runcriteria.line_for(row, default) is not None
    assert total > 700
    assert rendered >= total - 8  # measured: 766 / 774; the 8 carry residue a line cannot say
