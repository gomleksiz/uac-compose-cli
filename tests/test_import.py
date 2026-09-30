"""Milestone 3: YAML I/O, the uac style, compile, and import decisions."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from uac_compose import importer, registry, validate, yamlio
from uac_compose.compile import CompileError, Planned, compile_bundle, compile_object
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.emit import LookupError_, export_text, find, kinds_for
from uac_compose.envelope import ObjectEnvelope
from uac_compose.learn import Workspace
from uac_compose.style import compact
from uac_compose.style.uac import emit_object
from uac_compose.transport import FakeTransport, kind_for_type

RELEASE = "8.0.2.0"

# --- YAML ------------------------------------------------------------


def test_on_is_not_true() -> None:
    assert yamlio.load("on: success\nyes: no\n") == {"on": "success", "yes": "no"}
    assert yamlio.load("a: true\nb: False\n") == {"a": True, "b": False}


def test_equals_sign_quoted() -> None:
    text = yamlio.dump({"condition": "=", "code": "100", "time": "10:30", "flag": "on"})
    assert yamlio.load(text) == {"condition": "=", "code": "100", "time": "10:30", "flag": "on"}


def test_position_is_flow_style_and_float_is_int() -> None:
    text = yamlio.dump({"position": [120.0, 40], "n": 5.0})
    assert "position: [120, 40]" in text and "n: 5\n" in text


def test_multiline_text_is_a_block() -> None:
    text = yamlio.dump({"command": "echo a\necho b"})
    assert "command: |" in text
    assert yamlio.load(text) == {"command": "echo a\necho b"}


def test_comment_and_create_only_stripped() -> None:
    bundle = yamlio.read_bundle(
        "create_only: [variables]\n"
        "variables:\n  v1: {value: '1', comment: seed}\n"
        "tasks:\n  T: {type: taskUnix, create_only: true, comment: hi, command: ls}\n"
        "  U: {type: taskUnix, command: ls}\n"
    )
    assert bundle.sections["tasks"]["T"] == {"type": "taskUnix", "command": "ls"}
    assert bundle.create_only == {("variables", "v1"), ("tasks", "T")}


def test_unknown_section_rejected() -> None:
    with pytest.raises(yamlio.BundleError, match="unknown section"):
        yamlio.read_bundle("task:\n  T: {type: taskUnix}\n")


def test_create_only_names_a_section() -> None:
    with pytest.raises(yamlio.BundleError, match="credential"):
        yamlio.read_bundle("create_only: [credential]\n")


# --- compile ------------------------------------------------------------


def test_type_resolved_per_section() -> None:
    assert compile_object("tasks", "T", {"type": "taskFileMonitor"})["type"] == "taskFileMonitor"
    assert compile_object("triggers", "T", {"type": "triggerFm"})["type"] == "triggerFm"
    with pytest.raises(CompileError):
        compile_object("tasks", "T", {"type": "triggerFm"})
    with pytest.raises(CompileError):
        compile_object("tasks", "T", {"command": "ls"})


def test_own_type_moves_aside() -> None:
    obj = compile_object("credentials", "c", {"type": "Standard", "runtimeUser": "u"})
    assert obj == {"type": "credential", "_uacType": "Standard", "name": "c", "runtimeUser": "u"}
    with pytest.raises(CompileError):
        compile_object("business_services", "b", {"type": "x"})


def test_script_key_is_script_name() -> None:
    assert compile_object("scripts", "a.sh", {"content": "x"})["scriptName"] == "a.sh"


def test_number_in_string_field_coerced() -> None:
    obj = compile_object("tasks", "T", {"type": "taskUnix", "outputReturnNline": 100})
    assert obj["outputReturnNline"] == "100"
    obj = compile_object("tasks", "T", {"type": "taskUnix", "retryMaximum": "3"})
    assert obj["retryMaximum"] == 3


def test_bool_never_coerced() -> None:
    obj = compile_object("tasks", "T", {"type": "taskUnix", "summary": True})
    assert obj["summary"] is True


def test_enum_id_and_ui_label_normalized() -> None:
    obj = compile_object(
        "tasks", "T", {"type": "taskUnix", "outputType": "Standard Output", "twWaitType": 2}
    )
    assert obj["outputType"] == "STDOUT" and obj["twWaitType"] == "Duration"


def _planned(*items: tuple[str, dict[str, Any]]) -> list[Planned]:
    doc = {"tasks": {name: body for name, body in items}}
    return compile_bundle(yamlio.read_bundle(yamlio.dump(doc)))


def test_workflow_members_created_first() -> None:
    wf = {"type": "taskWorkflow", "workflowVertices": [{"task": {"value": "A"}, "vertexId": "2"}]}
    order = [p.name for p in _planned(("WF", wf), ("A", {"type": "taskUnix"}))]
    assert order == ["A", "WF"]


def test_launched_task_created_first() -> None:
    launcher = {
        "type": "taskUnix",
        "actions": {"systemOperations": [{"operation": "Launch Task", "task": "B"}]},
    }
    order = [p.name for p in _planned(("L", launcher), ("B", {"type": "taskUnix"}))]
    assert order == ["B", "L"]


def test_sections_in_creation_order() -> None:
    bundle = yamlio.read_bundle(
        "triggers:\n  Tr: {type: triggerManual}\ntasks:\n  T: {type: taskUnix}\n"
        "variables:\n  v: {value: '1'}\n"
    )
    assert [p.section for p in compile_bundle(bundle)] == ["variables", "tasks", "triggers"]


def _errors(planned: list[Planned]) -> list[str]:
    return [str(f) for f in validate.errors(validate.static(planned))]


# --- validate ------------------------------------------------------------


def test_secret_refused() -> None:
    errors = _errors(
        _planned(("T", {"type": "taskUnix"}))
        + [
            Planned(
                "credentials", "c", compile_object("credentials", "c", {"runtimePassword": "x"})
            )
        ]
    )
    assert any("runtimePassword is a secret" in e for e in errors)


def test_unknown_field_rejected() -> None:
    errors = _errors(_planned(("T", {"type": "taskUnix", "retryIntervl": 3})))
    assert errors == [
        "!! tasks/T: 'retryIntervl' is not a field of taskUnix (did you mean retryInterval?)"
    ]


# --- decide ------------------------------------------------------------

STORED = {
    "type": "taskUnix",
    "name": "T",
    "sysId": "abc",
    "command": "ls",
    "retryMaximum": 0,
    "agentVar": "${agent}",
    "exitCodes": "0",
    "actions": {
        "emailNotifications": [
            {"status": "Failed", "emailTemplate": "e1", "sysId": "1"},
            {"status": "Success", "emailTemplate": "e2", "sysId": "2"},
        ]
    },
}


def _one(body: dict[str, Any], create_only: bool = False) -> Planned:
    return Planned("tasks", "T", compile_object("tasks", "T", body), create_only)


def test_restated_default_is_reuse() -> None:
    d = importer.decide(_one({"type": "taskUnix", "command": "ls", "retryMaximum": 0}), STORED)
    assert d.action == "reuse"


def test_unstated_field_is_not_a_difference() -> None:
    assert importer.decide(_one({"type": "taskUnix"}), STORED).action == "reuse"


def test_list_order_ignored() -> None:
    swapped = {
        "emailNotifications": [
            {"status": "Success", "emailTemplate": "e2"},
            {"status": "Failed", "emailTemplate": "e1"},
        ]
    }
    d = importer.decide(_one({"type": "taskUnix", "actions": swapped}), STORED)
    assert d.action == "reuse"


def test_difference_is_conflict_unless_update() -> None:
    planned = _one({"type": "taskUnix", "command": "pwd"})
    d = importer.decide(planned, STORED)
    assert d.action == "conflict"
    assert d.differences == ["command: file says 'pwd', controller has 'ls'"]
    assert importer.decide(planned, STORED, update=True).action == "update"


def test_create_only_beats_update() -> None:
    d = importer.decide(_one({"type": "taskUnix", "command": "pwd"}, True), STORED, update=True)
    assert d.action == "keep"


def test_absent_is_create() -> None:
    assert importer.decide(_one({"type": "taskUnix"}), None).action == "create"


def test_update_fenced_by_business_services() -> None:
    stored = {**STORED, "opswiseGroups": ["Other"]}
    d = importer.decide(
        _one({"type": "taskUnix", "command": "pwd"}), stored, update=True, services=["Mine"]
    )
    assert d.action == "conflict" and "business service" in d.differences[-1]


def test_update_clears_other_twin() -> None:
    merged = importer.merge_update(
        {"type": "taskUnix", "name": "T", "agentVar": "${agent}", "command": "ls"},
        {"type": "taskUnix", "name": "T", "agent": "linux-01"},
    )
    assert merged == {"type": "taskUnix", "name": "T", "agent": "linux-01", "command": "ls"}


def test_update_merges_dicts_replaces_lists() -> None:
    merged = importer.merge_update({"a": {"x": 1, "y": 2}, "l": [1, 2]}, {"a": {"y": 3}, "l": [9]})
    assert merged == {"a": {"x": 1, "y": 3}, "l": [9]}


def test_oncreate_exit_codes_only_in_exit_code_mode() -> None:
    obj: dict[str, Any] = {"type": "taskUnix", "name": "T"}
    assert importer.fill_on_create(obj) == ["exitCodes"] and obj["exitCodes"] == "0"
    obj = {"type": "taskUnix", "name": "T", "exitCodeProcessing": "Success Output Contains"}
    assert importer.fill_on_create(obj) == []
    obj = {"type": "taskUnix", "name": "T", "exitCodes": "0,1"}
    assert importer.fill_on_create(obj) == [] and obj["exitCodes"] == "0,1"


def test_oncreate_not_on_update_not_in_compare(templates: TemplateSet) -> None:
    stored = {k: v for k, v in STORED.items() if k != "exitCodes"}
    planned = _one({"type": "taskUnix", "command": "pwd"})
    assert importer.decide(_one({"type": "taskUnix"}), stored).action == "reuse"
    decision = importer.decide(planned, stored, update=True)
    payload = importer.update_payload(decision, templates)
    assert "exitCodes" not in payload
    assert payload["sysId"] == "abc" and payload["command"] == "pwd"


# --- export -> import, end to end with a fake controller ---------------------------------------


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def _controller() -> FakeTransport:
    return FakeTransport(
        objects={
            "tasks": [dict(STORED)],
            "credentials": [{"name": "T", "type": "credential", "_uacType": "Standard"}],
        }
    )


def test_ambiguous_name_needs_type(tmp_path: Path) -> None:
    with pytest.raises(LookupError_, match="--type"):
        find("T", kinds_for(None), _ws(tmp_path), _controller())
    obj, type_name = find("T", kinds_for("task"), _ws(tmp_path), _controller())
    assert type_name == "taskUnix"


def _import(text: str, fake: FakeTransport, templates: TemplateSet, **kw: Any) -> list[str]:
    planned = compile_bundle(yamlio.read_bundle(text), templates)
    assert validate.errors(validate.static(planned, None, templates.extensions)) == []
    decisions = importer.decide_all(planned, fake, templates.extensions, services=[], **kw)
    if not any(d.action == "conflict" for d in decisions):
        importer.write(decisions, fake, templates, lambda _: None)
    return [d.action for d in decisions]


@pytest.mark.parametrize("style", ["uac", "compact"])
def test_export_import_loop(tmp_path: Path, templates: TemplateSet, style: str) -> None:
    fake = _controller()
    text, _ = export_text(["T"], "task", _ws(tmp_path), fake, templates, style=style)
    head = "type: taskUnix" if style == "uac" else "type: Unix"
    assert f"tasks:\n  T:\n    {head}\n" in text
    assert _import(text, fake, templates, update=False) == ["reuse"]

    changed = text.replace("command: ls", "command: pwd")
    assert _import(changed, fake, templates, update=False) == ["conflict"]
    assert fake.writes == []
    assert _import(changed, fake, templates, update=True) == ["update"]
    assert _import(changed, fake, templates, update=False) == ["reuse"]
    assert fake.writes[0][1]["sysId"] == "abc"
    assert fake.writes[0][1]["agentVar"] == "${agent}" and "agent" not in fake.writes[0][1]

    fresh = changed.replace("  T:\n", "  New:\n")
    assert _import(fresh, fake, templates, update=False) == ["create"]
    created = fake.writes[-1][1]
    assert created["name"] == "New" and created["exitCodes"] == "0"


# --- the corpus ------------------------------------------------------------


@pytest.mark.corpus
def test_emit_compile_identity_uac(corpus: list[ObjectEnvelope], templates: TemplateSet) -> None:
    failures = []
    for e in corpus:
        stamped = kind_for_type(e.type).stamp(e.obj)
        want = simplify(
            stamped,
            templates.get(e.type),
            templates.sub_templates,
            extensions=templates.extensions,
            reserved=frozenset(registry.get(e.type).fields),
        )
        section, name, body = emit_object(e.obj, e.type, templates, show_inactive=True)
        back = yamlio.load(yamlio.dump({section: {name: body}}))
        got = compile_object(section, name, back[section][name], templates)
        if got != want:
            failures.append(f"{e.type}/{e.name}")
    assert not failures, f"{len(failures)} of {len(corpus)}: {failures[:5]}"


def _is_pruning_of(small: Any, full: Any, path: str = "") -> list[str]:
    """Where `small` differs from `full` by anything other than removed keys."""
    if isinstance(full, dict):
        if not isinstance(small, dict) or set(small) - set(full):
            return [path or "object"]
        return [w for k, v in small.items() for w in _is_pruning_of(v, full[k], f"{path}.{k}")]
    if isinstance(full, list):
        if not isinstance(small, list) or len(small) != len(full):
            return [path]
        return [
            w
            for i, (s, f) in enumerate(zip(small, full, strict=True))
            for w in _is_pruning_of(s, f, f"{path}[{i}]")
        ]
    return [] if small == full else [path]


@pytest.mark.corpus
def test_hiding_only_removes_never_rewrites(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    """Inactive rules are display only, at every depth: a value that is written
    is the value that is stored."""
    for e in corpus:
        _, _, shown = emit_object(e.obj, e.type, templates)
        _, _, full = emit_object(e.obj, e.type, templates, show_inactive=True)
        assert _is_pruning_of(shown, full) == [], f"{e.name}: rewrote {_is_pruning_of(shown, full)}"


@pytest.mark.corpus
def test_hidden_top_level_fields_are_named_by_a_rule(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    from uac_compose import inactive
    from uac_compose.core.normalize import normalize

    for e in corpus:
        _, _, shown = emit_object(e.obj, e.type, templates)
        _, _, full = emit_object(e.obj, e.type, templates, show_inactive=True)
        stamped = kind_for_type(e.type).stamp(e.obj)
        assert set(full) - set(shown) <= inactive.inactive_fields(normalize(stamped)), e.name


@pytest.mark.corpus
@pytest.mark.parametrize("style", ["uac", "compact"])
def test_every_exported_object_decides_reuse(
    corpus: list[ObjectEnvelope], templates: TemplateSet, style: str
) -> None:
    """Export each object, import it back against a controller holding the corpus."""
    by_kind: dict[str, dict[str, dict[str, Any]]] = {}
    names = Counter((kind_for_type(e.type).key, e.name) for e in corpus)
    for e in corpus:
        kind = kind_for_type(e.type)
        by_kind.setdefault(kind.key, {})[e.name] = kind.stamp(e.obj)
    fake = FakeTransport(objects={k: list(v.values()) for k, v in by_kind.items()})

    checked, failures, invalid = 0, [], []
    for e in corpus:
        kind = kind_for_type(e.type)
        if names[(kind.key, e.name)] > 1:
            continue  # two versions of one name: the controller holds only one
        if style == "uac":
            section, name, body = emit_object(e.obj, e.type, templates)
        else:
            section, name, body, _ = compact.emit_object(e.obj, e.type, templates)
        text = yamlio.dump({section: {name: body}})
        planned = compile_bundle(yamlio.read_bundle(text), templates)
        invalid += [
            str(f) for f in validate.errors(validate.static(planned, None, templates.extensions))
        ]
        (decision,) = importer.decide_all(
            planned, fake, templates.extensions, update=False, services=[]
        )
        checked += 1
        if decision.action != "reuse":
            failures.append(f"{e.type}/{e.name}: {decision.differences[:2]}")
    assert not invalid, invalid[:5]
    assert not failures, f"{len(failures)} of {checked}: {failures[:3]}"
    assert checked > 5000


# --- live (opt-in, read-only) ------------------------------------------------------------------


@pytest.mark.live
def test_export_import_live_is_reuse(tmp_path: Path) -> None:
    """UAC_COMPOSE_LIVE_PROFILE + UAC_COMPOSE_LIVE_TASK: export it, decide it again: reuse."""
    import os

    from uac_compose.transport import UacCli

    profile = os.environ.get("UAC_COMPOSE_LIVE_PROFILE")
    task = os.environ.get("UAC_COMPOSE_LIVE_TASK")
    if not (profile and task):
        pytest.skip("set UAC_COMPOSE_LIVE_PROFILE and UAC_COMPOSE_LIVE_TASK")
    cli = UacCli(profile)
    templates = TemplateSet(RELEASE)
    text, _ = export_text([task], "task", _ws(tmp_path), cli, templates)
    planned = compile_bundle(yamlio.read_bundle(text), templates)
    (decision,) = importer.decide_all(planned, cli, templates.extensions, update=False, services=[])
    assert decision.action == "reuse", decision.differences


# --- a switch that is off on the target (data/rules/guards.yaml) -----------------------


def _monitor(**fields: Any) -> dict[str, Any]:
    return {"type": "taskEmailMonitor", "name": "Monitor for Approval", **fields}


def test_a_value_its_target_switch_kills_is_a_note_not_a_conflict() -> None:
    """Measured live: the file was right, the target was right, the conflict was for ever.

    The file states `body` and leaves `bodyFilter` out, because `Contains` is
    the default. Someone set `bodyFilter` to `None` on the target, so UAC
    discards `body` on the way in and the difference comes back on every run.
    The switch belongs to the target, so uac-compose says it once and moves on.
    """
    planned = Planned("tasks", "Monitor for Approval", _monitor(body="Approved"))
    stored = _monitor(bodyFilter="None", subjectFilter="Contains", body=None)
    decision = importer.decide(planned, stored)
    assert decision.action == "reuse"
    assert decision.differences == []
    assert decision.notes == [
        "body not stored: bodyFilter is 'None' on the controller, "
        "and that switches it off"
    ]


def test_a_value_whose_switch_is_on_is_still_a_conflict() -> None:
    planned = Planned("tasks", "Monitor for Approval", _monitor(body="Approved"))
    stored = _monitor(bodyFilter="Contains", body="Rejected")
    decision = importer.decide(planned, stored)
    assert decision.action == "conflict" and decision.notes == []
    assert decision.differences == ["body: file says 'Approved', controller has 'Rejected'"]


def test_the_switch_itself_is_never_changed_by_an_import() -> None:
    """The target keeps its own switch: with --update the object is not written at all."""
    planned = Planned("tasks", "Monitor for Approval", _monitor(body="Approved"))
    stored = _monitor(bodyFilter="None", body=None)
    assert importer.decide(planned, stored, update=True).action == "reuse"


def test_a_type_change_is_a_conflict_even_with_update() -> None:
    """Measured live: an update cannot change a task's type, and the controller
    says so only after the objects before it in the file were written."""
    planned = Planned("tasks", "Approval Task", {"type": "taskApproval", "name": "Approval Task"})
    stored = {"type": "taskSleep", "name": "Approval Task", "sleepAmount": 10}
    decision = importer.decide(planned, stored, update=True)
    assert decision.action == "conflict"
    assert decision.differences == [
        "type: Sleep on the controller, Approval in the file. An update cannot change "
        "a type. Rename one of them, or delete it on the controller and import again"
    ]


def test_suggested_templates_export_and_decide_reuse(
    tmp_path: Path, templates: TemplateSet
) -> None:
    tasks = [{**STORED, "name": n, "sysId": n, "command": f"run {n}"} for n in "ABC"]
    fake = FakeTransport(objects={"tasks": tasks})
    report: list[str] = []
    text, _ = export_text(
        list("ABC"), "task", _ws(tmp_path), fake, templates, suggest_templates=True, report=report
    )
    assert "\ntemplates:\n" in text and "    like: " in text
    assert report[-1].startswith("1 template(s) suggested")
    assert _import(text, fake, templates, update=False) == ["reuse"] * 3
