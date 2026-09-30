"""Milestone 7: one test per validation rung, named for the failure it catches."""

from __future__ import annotations

from typing import Any

import pytest

from uac_compose import importer, validate, yamlio
from uac_compose.compile import Planned, compile_bundle, compile_object
from uac_compose.core.extensions import ExtensionMap, Slot
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.transport import FakeTransport


def _p(body: dict[str, Any], name: str = "T", section: str = "tasks") -> Planned:
    return Planned(section, name, compile_object(section, name, body))


def _errors(*planned: Planned) -> list[str]:
    return [f.message for f in validate.errors(validate.static(list(planned)))]


def _warnings(*planned: Planned) -> list[str]:
    return [f.message for f in validate.static(list(planned)) if f.level == "warning"]


def test_unknown_type_rejected() -> None:
    bad = Planned("tasks", "T", {"type": "taskNope", "name": "T"})
    assert _errors(bad) == ["unknown type 'taskNope'"]


def test_misspelt_field_rejected_with_close_match() -> None:
    assert _errors(_p({"type": "Unix", "comand": "ls"})) == [
        "'comand' is not a field of taskUnix (did you mean command?)"
    ]


def test_var_check_toggle_refused() -> None:
    (message,) = _errors(_p({"type": "Unix", "agentVar": "${a}", "agentVarCheck": True}))
    assert "agentVarCheck is a UI toggle" in message and "agentVar" in message


def test_unknown_extension_parameter_rejected() -> None:
    (message,) = _errors(_p({"type": "Universal", "template": "Nope", "bucket": "b"}))
    assert "neither a field of taskUniversal nor a parameter" in message


def test_a_missing_extension_is_one_error_not_one_per_parameter() -> None:
    """Measured on a live import: an extension neither on the target nor frozen
    gave 13 lines, one per parameter, and none said the extension was missing."""
    ext = ExtensionMap({"Known": {"bucket": Slot("textField")}}, frozenset({"Known", "Bare"}))
    body = {"type": "Universal", "template": "Gone", "bucket": "b", "region": "r", "mode": "m"}
    found = validate.errors(validate.static([_p(body)], None, ext))
    assert [f.message for f in found] == [
        "extension 'Gone' is not on this controller and not frozen; "
        "run `uac-compose freeze` on the controller it came from"
    ]
    # An extension the map holds still has each unknown parameter named.
    known = {"type": "Universal", "template": "Known", "bucket": "b", "regoin": "r"}
    (message,) = [f.message for f in validate.errors(validate.static([_p(known)], None, ext))]
    assert message == (
        "'regoin' is neither a field of taskUniversal nor a parameter of extension 'Known'"
    )
    # Declared with no parameters is still there: nothing to say about it.
    bare = {"type": "Universal", "template": "Bare", "agent": "a"}
    assert validate.errors(validate.static([_p(bare)], None, ext)) == []
    # A map read from a file never saw the declarations, so it cannot say what
    # is absent: each parameter is named, as before.
    from_file = ExtensionMap(dict(ext.templates))
    assert len(validate.errors(validate.static([_p(body)], None, from_file))) == 3


def test_sub_object_field_typo_rejected() -> None:
    body = {
        "type": "Unix",
        "actions": {"abortActions": [{"status": "Failed", "haltOnFinsh": True}]},
    }
    (message,) = _errors(_p(body))
    assert "'haltOnFinsh' is not a field of actions.abortActions" in message


def test_workflow_edge_condition_exempt_from_sub_field_check() -> None:
    body = {"type": "Workflow", "tasks": {"A": None, "B": {"after": {"A": "exit code 1"}}}}
    assert _errors(_p(body)) == []


def test_both_var_halves_rejected() -> None:
    (message,) = _errors(_p({"type": "Unix", "agent": "a", "agentVar": "${b}"}))
    assert "agent and agentVar are both stated" in message


def test_system_operation_without_operation_rejected() -> None:
    body = {"type": "Unix", "actions": {"systemOperations": [{"status": "Failed", "task": "X"}]}}
    assert _errors(_p(body)) == ["actions.systemOperations[0] has no operation"]


def test_enum_outside_docs_is_a_warning_not_an_error() -> None:
    planned = _p({"type": "Unix", "twWaitType": "Sometimes"})
    assert _errors(planned) == []
    assert _warnings(planned) == [
        "twWaitType 'Sometimes' is not one the docs list (None, Time, Relative Time, Duration, "
        "Seconds)"
    ]


def test_three_part_duration_rejected() -> None:
    (message,) = _errors(_p({"type": "Unix", "lfDuration": "00:01:49"}))
    assert "lfDuration '00:01:49' is not in the format DD:HH:MM:SS" in message


def test_bad_time_and_exit_codes_rejected() -> None:
    errors = _errors(_p({"type": "Unix", "lsTime": "9:5", "exitCodes": "0,a"}))
    assert len(errors) == 2


def test_variable_value_skips_format_check() -> None:
    assert _errors(_p({"type": "Unix", "exitCodes": "${codes}"})) == []


def test_broadcast_with_agent_rejected() -> None:
    errors = _errors(_p({"type": "Unix", "agent": "a", "broadcastCluster": "c"}))
    assert errors == ["a broadcast cluster cannot be combined with an agent or cluster"]


def test_wait_for_output_and_failure_only_rejected() -> None:
    errors = _errors(_p({"type": "Unix", "waitForOutput": True, "outputFailureOnly": True}))
    assert errors == ["waitForOutput and outputFailureOnly cannot both be true"]


def test_failure_edge_from_timer_rejected() -> None:
    wf = _p({"type": "Workflow", "tasks": {"Wait": None, "B": {"after": {"Wait": "failure"}}}})
    timer = _p({"type": "Timer"}, name="Wait")
    assert _errors(wf, timer) == ["edge from 'Wait': a taskSleep cannot end in failure"]


def test_approved_edge_from_non_approval_rejected() -> None:
    wf = _p({"type": "Workflow", "tasks": {"A": None, "B": {"after": {"A": "approved"}}}})
    assert _errors(wf, _p({"type": "Unix"}, name="A")) == [
        "edge from 'A': Approved is only for an Approval task"
    ]


def test_edge_check_skips_unknown_source_types() -> None:
    wf = _p({"type": "Workflow", "tasks": {"A": None, "B": {"after": {"A": "failure"}}}})
    assert _errors(wf) == []


# --- create rungs ------------------------------------------------------------


def test_required_when_missing_on_create(templates: TemplateSet) -> None:
    planned = _p({"type": "Unix", "agent": "a", "twWaitType": "Seconds"})
    (finding,) = validate.for_create(planned, templates)
    assert finding.message == "twWaitAmount is required when twWaitType='Seconds'"
    scan = _p({"type": "Unix", "agent": "a", "exitCodeProcessing": "Failure Output Contains"})
    assert [f.message for f in validate.for_create(scan, templates)] == [
        "exitCodeText is required when exitCodeProcessing='Failure Output Contains'"
    ]


def test_no_execution_target_on_create(templates: TemplateSet) -> None:
    (finding,) = validate.for_create(_p({"type": "Unix"}), templates)
    assert "no execution target" in finding.message
    assert validate.for_create(_p({"type": "Unix", "agent": "${a}"}), templates) == []
    assert validate.for_create(_p({"type": "Workflow"}), templates) == []  # no agent to have


# --- warnings ------------------------------------------------------------


def test_inactive_field_stated_warns(templates: TemplateSet) -> None:
    planned = _p({"type": "Unix", "lfEnabled": False, "lfTime": "10:00"})
    (finding,) = validate.inactive_stated(planned, templates)
    assert finding.level == "warning" and finding.message.startswith("lfTime is stated but")


def test_warning_knows_objects_defined_in_same_file() -> None:
    text = (
        "credentials:\n  svc: {type: Standard}\n"
        "tasks:\n  T: {type: Unix, agent: linux-01, credentials: svc}\n"
    )
    planned = compile_bundle(yamlio.read_bundle(text))
    known = {"agents": {"linux-01"}, "credentials": set()}
    assert validate.references_(planned, known) == []


def test_unknown_reference_warns_with_close_match() -> None:
    planned = compile_bundle(yamlio.read_bundle("tasks:\n  T: {type: Unix, agent: linux-1}\n"))
    (finding,) = validate.references_(planned, {"agents": {"linux-01", "win-01"}})
    assert finding.level == "warning"
    assert "agent names agent 'linux-1'" in finding.message and "'linux-01'" in finding.message


def test_variable_reference_not_checked() -> None:
    planned = compile_bundle(yamlio.read_bundle("tasks:\n  T: {type: Unix, agent: '${a}'}\n"))
    assert validate.references_(planned, {"agents": set()}) == []


def test_finding_printed_in_full_on_one_line() -> None:
    long = "x" * 300
    line = str(validate.Finding("warning", "tasks/T", long))
    assert "\n" not in line and line.endswith(long) and line.startswith("?? tasks/T: ")


def test_missing_workflow_member_is_found() -> None:
    wf = _p({"type": "Workflow", "tasks": {"Here": None, "Elsewhere": None, "Nowhere": None}})
    here = _p({"type": "Unix"}, name="Here")
    fake = FakeTransport(objects={"tasks": [{"type": "taskUnix", "name": "Elsewhere"}]})
    assert importer.missing_members([wf, here], fake) == [
        "tasks/T: member 'Nowhere' is not in this file or on the controller"
    ]


# --- the corpus ------------------------------------------------------------


@pytest.mark.corpus
def test_every_exported_object_validates_clean(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    """A rung that rejects a real object gets turned off (LESSONS #25)."""
    from uac_compose.style import compact

    type_of = {e.name: e.type for e in corpus if e.kind in ("task", "workflow")}
    errors: list[str] = []
    for e in corpus:
        section, name, body, _ = compact.emit_object(e.obj, e.type, templates)
        planned = compile_bundle(
            yamlio.read_bundle(yamlio.dump({section: {name: body}})), templates
        )
        errors += [
            str(f) for f in validate.errors(validate.static(planned, type_of, templates.extensions))
        ]
        for p in planned:
            errors += [str(f) for f in validate.for_create(p, templates)]
    assert errors == [], errors[:5]
