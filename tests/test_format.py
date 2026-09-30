"""The compact format's own rules, one test per rule.

Each names the thing it protects. The corpus tests prove the invariant
(`compile(emit(x)) == simplify(x)`); these prove the *shape* is the one asked
for, which the invariant alone would not notice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from uac_compose import inactive, yamlio
from uac_compose.compile import compile_object
from uac_compose.core.normalize import normalize
from uac_compose.core.templates import TemplateSet
from uac_compose.emit import export_text
from uac_compose.learn import Workspace
from uac_compose.style import compact, uac
from uac_compose.transport import FakeTransport


def _emit(obj: dict[str, Any], type_name: str, templates: TemplateSet) -> dict[str, Any]:
    _, _, body, ok = compact.emit_object(obj, type_name, templates)
    assert ok, f"{type_name} fell back to the standard style"
    return body


def _task(body: dict[str, Any]) -> dict[str, Any]:
    return compile_object("tasks", "T", body)


def _roundtrip(obj: dict[str, Any], type_name: str, templates: TemplateSet) -> dict[str, Any]:
    section, name, body, _ = compact.emit_object(obj, type_name, templates)
    back = yamlio.load(yamlio.dump({section: {name: body}}))[section][name]
    return compile_object(section, name, back, templates)


# --- directives -------------------------------------------------------------------


def test_a_file_states_its_format_version_and_style(tmp_path: Path, templates: TemplateSet) -> None:
    fake = FakeTransport(objects={"tasks": [{"type": "taskUnix", "name": "T", "command": "ls"}]})
    text, _ = export_text(["T"], "task", Workspace(tmp_path), fake, templates)
    assert text.startswith("version: 1\nstyle: compact\n")
    standard, _ = export_text(["T"], "task", Workspace(tmp_path), fake, templates, style="standard")
    assert standard.startswith("version: 1\nstyle: standard\n")


def test_uac_is_the_old_name_for_standard() -> None:
    assert yamlio.style_named("uac") == "standard"
    assert yamlio.style_named("Compact") == "compact"
    with pytest.raises(yamlio.BundleError, match="unknown style"):
        yamlio.style_named("terse")


def test_a_file_from_a_newer_uac_compose_is_refused_not_half_read() -> None:
    with pytest.raises(yamlio.BundleError, match="Upgrade uac-compose"):
        yamlio.read_bundle("version: 99\ntasks:\n  T: {type: Unix}\n")
    assert yamlio.read_bundle("version: 1\ntasks:\n  T: {type: Unix}\n").version == 1
    assert yamlio.read_bundle("tasks:\n  T: {type: Unix}\n").version == yamlio.FORMAT_VERSION


def test_the_directives_are_not_sections() -> None:
    bundle = yamlio.read_bundle("version: 1\nstyle: compact\ntasks:\n  T: {type: Unix}\n")
    assert list(bundle.sections) == ["tasks"] and bundle.style == "compact"


# --- 1. credential aliases ----------------------------------------------------------


def test_a_credential_says_user_and_business_services(templates: TemplateSet) -> None:
    stored = {
        "type": "credential",
        "name": "svc",
        "_uacType": "Standard",
        "runtimeUser": "batch",
        "opswiseGroups": ["Ops"],
    }
    body = _emit(stored, "credential", templates)
    assert body["user"] == "batch" and body["business_services"] == ["Ops"]
    assert "runtimeUser" not in body and "opswiseGroups" not in body
    back = compile_object("credentials", "svc", body, templates)
    assert back["runtimeUser"] == "batch" and back["opswiseGroups"] == ["Ops"]


def test_every_type_says_business_services(templates: TemplateSet) -> None:
    body = _emit({"type": "taskUnix", "name": "T", "opswiseGroups": ["Ops"]}, "taskUnix", templates)
    assert body["business_services"] == ["Ops"] and "opswiseGroups" not in body
    back = compile_object("tasks", "T", body, templates)
    assert back["opswiseGroups"] == ["Ops"]


def test_a_real_extension_parameter_wins_over_the_alias(templates: TemplateSet) -> None:
    """Measured: one Universal task on a live controller carries a parameter
    called `business_services`. Two keys of one name could not be told apart,
    so on that object the UAC field keeps its own name."""
    ext = templates.extensions
    template = next(t for t, params in ext.templates.items() if "business_services" in params)
    slot = ext.templates[template]["business_services"].slot
    stored = {
        "type": "taskUniversal",
        "name": "T",
        "template": template,
        "opswiseGroups": ["Ops"],
        slot: {"label": "BS", "name": "business_services", "value": "CRITICAL"},
    }
    body = _emit(stored, "taskUniversal", templates)
    assert body["business_services"] == "CRITICAL"  # the parameter
    assert body["opswiseGroups"] == ["Ops"]  # the UAC field, unrenamed
    back = _roundtrip(stored, "taskUniversal", templates)
    assert back["business_services"] == "CRITICAL" and back["opswiseGroups"] == ["Ops"]


# --- 3. scripts ---------------------------------------------------------------------


def test_script_content_is_written_as_a_block(templates: TemplateSet) -> None:
    stored = {"type": "script", "scriptName": "load.sh", "content": "#!/bin/sh\necho hi\n"}
    _, _, body, _ = compact.emit_object(stored, "script", templates)
    text = yamlio.dump({"scripts": {"load.sh": body}})
    assert "content: |" in text
    assert yamlio.load(text)["scripts"]["load.sh"]["content"] == stored["content"]


def test_a_script_line_ending_in_a_space_is_quoted_not_silently_trimmed(
    templates: TemplateSet,
) -> None:
    """A `|` block cannot carry trailing whitespace: YAML strips it. Quoting is
    uglier and keeps the script byte for byte, which is the point of a script."""
    stored = {"type": "script", "scriptName": "q.sql", "content": "SELECT \n  user;"}
    _, _, body, _ = compact.emit_object(stored, "script", templates)
    text = yamlio.dump({"scripts": {"q.sql": body}})
    assert "content: |" not in text
    assert yamlio.load(text)["scripts"]["q.sql"]["content"] == stored["content"]


# --- 4. sla -------------------------------------------------------------------------


def test_the_sla_is_the_one_block(templates: TemplateSet) -> None:
    stored = {
        "type": "taskUnix",
        "name": "T",
        "agent": "a",
        "retryMaximum": 3,
        "lfEnabled": True,
        "lfType": "Duration",
        "lfDuration": "00:02:00:00",
    }
    body = _emit(stored, "taskUnix", templates)
    assert body["agent"] == "a" and body["retryMaximum"] == 3  # flat
    assert body["sla"] == {"late_finish": {"duration": "2h"}}


def test_a_time_constraint_shows_the_time_not_the_duration(templates: TemplateSet) -> None:
    """Measured: of the objects whose constraint is on and typed Time, every
    stored duration is 00:00:00:00 -- 65 late starts and 67 late finishes."""
    stored = {
        "type": "taskUnix",
        "name": "T",
        "lsEnabled": True,
        "lsTime": "07:00",
        "lsDuration": "00:00:00:00",
        "lfEnabled": True,
        "lfTime": "08:00",
        "lfDuration": "00:00:00:00",
    }
    assert _emit(stored, "taskUnix", templates)["sla"] == {
        "late_start": {"time": "07:00"},
        "late_finish": {"time": "08:00"},
    }


def test_a_duration_constraint_shows_the_duration_not_the_time(
    templates: TemplateSet,
) -> None:
    stored = {
        "type": "taskUnix",
        "name": "T",
        "lfEnabled": True,
        "lfType": "Duration",
        "lfDuration": "00:00:00:30",
        "lfTime": "00:00",
    }
    assert _emit(stored, "taskUnix", templates)["sla"] == {"late_finish": {"duration": "30s"}}


def test_a_block_drops_the_family_prefix() -> None:
    written = {
        "type": "Unix",
        "sla": {"late_finish": {"time": "08:00", "dayConstraint": "Next Day"}},
    }
    task = _task(written)
    assert task["lfTime"] == "08:00" and task["lfDayConstraint"] == "Next Day"


def test_the_full_uac_name_is_accepted_inside_a_block() -> None:
    short = {"type": "Unix", "sla": {"late_finish": {"time": "08:00"}}}
    full = {"type": "Unix", "sla": {"late_finish": {"lfTime": "08:00"}}}
    assert _task(short) == _task(full)


def test_a_stated_duration_says_the_type(templates: TemplateSet) -> None:
    stored = {
        "type": "taskUnix",
        "name": "T",
        "lfEnabled": True,
        "lfType": "Duration",
        "lfDuration": "00:00:00:30",
    }
    assert _emit(stored, "taskUnix", templates)["sla"] == {"late_finish": {"duration": "30s"}}
    assert _task({"type": "Unix", "sla": {"late_finish": {"duration": "30s"}}})["lfType"] == (
        "Duration"
    )


def test_average_duration_is_written_because_it_reads_the_offsets(
    templates: TemplateSet,
) -> None:
    """Only `Duration` is implied: Average Duration reads the offset fields."""
    stored = {
        "type": "taskUnix",
        "name": "T",
        "lfEnabled": True,
        "lfType": "Average Duration",
        "lfOffsetPercentage": 50,
    }
    block = _emit(stored, "taskUnix", templates)["sla"]["late_finish"]
    assert block == {"type": "Average Duration", "offsetPercentage": 50}


def test_a_stated_type_still_wins_over_the_implication() -> None:
    written = {
        "type": "Unix",
        "sla": {"late_finish": {"type": "Average Duration", "duration": "30s"}},
    }
    assert _task(written)["lfType"] == "Average Duration"


def test_a_short_name_is_never_two_fields_of_one_block() -> None:
    from uac_compose import groups, registry

    for type_name in registry.types():
        for block in groups.blocks(type_name):
            short = groups.short_names(type_name, block)
            assert len(set(short.values())) == len(short), f"{type_name}.{block}"


def test_writing_the_block_is_what_switches_it_on() -> None:
    written = {"type": "Unix", "sla": {"late_finish": {"time": "08:00"}}}
    task = _task(written)
    assert task["lfEnabled"] is True and task["lfTime"] == "08:00"


def test_a_flag_the_file_states_still_wins() -> None:
    written = {"type": "Unix", "sla": {"late_finish": {"enabled": False, "time": "08:00"}}}
    assert _task(written)["lfEnabled"] is False


def test_a_block_with_nothing_else_to_say_keeps_its_flag(templates: TemplateSet) -> None:
    stored = {"type": "taskUnix", "name": "T", "lfEnabled": True}
    assert _emit(stored, "taskUnix", templates)["sla"] == {"late_finish": {"enabled": True}}


def test_show_inactive_writes_the_fields_flat(templates: TemplateSet) -> None:
    """There a block appears for a constraint that is OFF, and `lfEnabled:
    false` is the type's default and so is absent -- a flagless block would
    read as the opposite of what is stored."""
    stored = {"type": "taskUnix", "name": "T", "lfEnabled": True, "lfTime": "08:00"}
    _, _, body, ok = compact.emit_object(stored, "taskUnix", templates, show_inactive=True)
    assert ok and "sla" not in body
    assert body["lfEnabled"] is True and body["lfTime"] == "08:00"


# --- 5, 6. system operations --------------------------------------------------------


def test_a_system_operation_says_status_then_operation_and_pairs_its_variables() -> None:
    written = {
        "type": "Unix",
        "actions": {
            "systemOperations": [
                {
                    "status": "Started",
                    "operation": "Launch Task",
                    "task": "B",
                    "variables": [{"run_id": "${ops_task_id}"}],
                }
            ]
        },
    }
    row = _task(written)["actions"]["systemOperations"][0]
    assert row["variables"] == [{"name": "run_id", "value": "${ops_task_id}"}]


def test_system_operation_row_order(templates: TemplateSet) -> None:
    stored = {
        "type": "taskUnix",
        "name": "T",
        "actions": {
            "systemOperations": [{"operation": "Launch Task", "task": "B", "status": "Started"}]
        },
    }
    row = _emit(stored, "taskUnix", templates)["actions"]["systemOperations"][0]
    assert list(row)[:2] == ["status", "operation"]


# --- 7. email monitor markers -------------------------------------------------------


def test_email_monitor_markers_survive_untouched(templates: TemplateSet) -> None:
    """`--start` is a plain string; nothing may read it as a list item or a flag."""
    stored = {
        "type": "taskEmailMonitor",
        "name": "M",
        "bodyBeginMarker": "--start",
        "bodyEndMarker": "--end",
    }
    body = _emit(stored, "taskEmailMonitor", templates)
    assert body["bodyBeginMarker"] == "--start" and body["bodyEndMarker"] == "--end"
    back = _roundtrip(stored, "taskEmailMonitor", templates)
    assert back["bodyBeginMarker"] == "--start" and back["bodyEndMarker"] == "--end"


# --- 8. exit codes ------------------------------------------------------------------


def test_the_exit_code_a_create_fills_anyway_is_not_written(templates: TemplateSet) -> None:
    stored = {"type": "taskUnix", "name": "T", "agent": "a", "exitCodes": "0"}
    assert "exitCodes" not in _emit(stored, "taskUnix", templates)


def test_any_other_exit_code_is_written(templates: TemplateSet) -> None:
    for codes in ("0,100", "1-4"):
        stored = {"type": "taskUnix", "name": "T", "agent": "a", "exitCodes": codes}
        assert _emit(stored, "taskUnix", templates)["exitCodes"] == codes


def test_a_zero_under_a_mode_no_create_would_fill_is_written(templates: TemplateSet) -> None:
    """Under `Failure Exitcode Range` nothing fills it, so leaving it out loses it."""
    stored = {
        "type": "taskUnix",
        "name": "T",
        "agent": "a",
        "exitCodes": "0",
        "exitCodeProcessing": "Failure Exitcode Range",
    }
    assert _emit(stored, "taskUnix", templates)["exitCodes"] == "0"


# --- 10. attachments that are not attached -------------------------------------------


def test_line_counts_for_an_attachment_that_is_off_are_not_written() -> None:
    row = {
        "status": "Failed",
        "attachFile": False,
        "fileNumLines": 100,
        "fileStartLine": 1,
        "attachStdOut": False,
        "stdoutNumLines": 100,
        "attachStdError": False,
        "stderrNumLines": 100,
        "to": "ops@example.com",
    }
    full = {"type": "taskUnix", "actions": {"emailNotifications": [row]}}
    shown = inactive.hide(full, normalize(full))["actions"]["emailNotifications"][0]
    assert set(shown) == {"status", "attachFile", "attachStdOut", "attachStdError", "to"}


def test_line_counts_are_written_when_the_attachment_is_on() -> None:
    row = {"attachFile": True, "fileName": "/tmp/out", "fileNumLines": 50}
    full = {"type": "taskUnix", "actions": {"emailNotifications": [row]}}
    shown = inactive.hide(full, normalize(full))["actions"]["emailNotifications"][0]
    assert shown == row


# --- 11. universal extension parameters -----------------------------------------------


def test_a_universal_task_says_its_parameters_by_name(templates: TemplateSet) -> None:
    stored = {
        "type": "taskUniversal",
        "name": "T",
        "template": "File Utility",
        "booleanField1": {"label": "Recursive", "name": "recursive", "value": True},
        "choiceField1": {"label": "Action", "name": "action", "value": "Find Files"},
    }
    body = _emit(stored, "taskUniversal", templates)
    assert body["recursive"] is True and body["action"] == "Find Files"
    assert not any(k.endswith(("Field1", "Field2")) for k in body)
    back = _roundtrip(stored, "taskUniversal", templates)
    assert back["recursive"] is True  # compiled form keeps the name


def test_both_spellings_compile_to_one_object(templates: TemplateSet) -> None:
    """A slot and its parameter name are the same statement."""
    slot = {
        "type": "Universal",
        "template": "File Utility",
        "booleanField1": {"label": "Recursive", "name": "recursive", "value": True},
    }
    named = {"type": "Universal", "template": "File Utility", "recursive": True}
    assert compile_object("tasks", "T", slot, templates) == compile_object(
        "tasks", "T", named, templates
    )


def test_the_slots_come_back_on_the_way_to_the_controller(templates: TemplateSet) -> None:
    from uac_compose.compile import Planned
    from uac_compose.importer import create_payload

    obj = compile_object(
        "tasks",
        "T",
        {"type": "Universal", "template": "File Utility", "recursive": True},
        templates,
    )
    payload, _ = create_payload(Planned("tasks", "T", obj), templates)
    assert payload["booleanField1"] == {"label": "Recursive", "name": "recursive", "value": True}
    assert "recursive" not in payload


# --- 12. notify aliases ----------------------------------------------------------------


def test_an_action_that_is_not_a_notification_says_on_late_finish(
    templates: TemplateSet,
) -> None:
    stored = {
        "type": "taskUnix",
        "name": "T",
        "actions": {
            "abortActions": [{"status": "Failed", "notifyOnLateFinish": True}],
            # a recipient puts it beyond the `email_on` idiom, so it stays raw
            "emailNotifications": [
                {"status": "Failed", "notifyOnLateFinish": True, "to": "ops@example.com"}
            ],
        },
    }
    actions = _emit(stored, "taskUnix", templates)["actions"]
    assert actions["abortActions"][0]["on_late_finish"] is True
    assert actions["emailNotifications"][0]["notifyOnLateFinish"] is True


# --- 13. workflow edges ------------------------------------------------------------------


def test_success_is_the_default_edge_condition() -> None:
    written = {"type": "Workflow", "tasks": {"A": None, "B": {"after": "A"}}}
    edge = _task(written)["workflowEdges"][0]
    assert edge["condition"] == {"value": "Success"}


def test_a_success_edge_is_written_as_a_bare_name(templates: TemplateSet) -> None:
    stored = {
        "type": "taskWorkflow",
        "name": "WF",
        "workflowVertices": [
            {"task": {"value": "A"}, "vertexId": "2"},
            {"task": {"value": "B"}, "vertexId": "3"},
        ],
        "workflowEdges": [
            {
                "condition": {"value": "Success"},
                "sourceId": {"taskName": "A", "value": "2"},
                "targetId": {"taskName": "B", "value": "3"},
            }
        ],
    }
    members = _emit(stored, "taskWorkflow", templates)["tasks"]
    assert members["B"]["after"] == "A"  # not {A: success}


def test_any_other_condition_is_written(templates: TemplateSet) -> None:
    written = {"type": "Workflow", "tasks": {"A": None, "B": {"after": {"A": "failure"}}}}
    assert _task(written)["workflowEdges"][0]["condition"] == {"value": "Failure"}


# --- 14. event template attributes -------------------------------------------------------

ATTRIBUTES = [
    {"name": "file_name", "label": "File Name", "type": "Text"},
    {"name": "file_size", "label": "File Size", "type": "Integer"},
    {"name": "type", "label": "Kind", "type": "Text"},
]


def _event_template(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "universalEventTemplate", "name": "file-arrived", "attributes": rows}


def test_an_attribute_is_its_name_and_label(templates: TemplateSet) -> None:
    body = _emit(_event_template(ATTRIBUTES[:1]), "universalEventTemplate", templates)
    assert body["attributes"] == [{"file_name": "File Name"}]


def test_an_attribute_type_is_written_only_when_it_is_not_text(templates: TemplateSet) -> None:
    body = _emit(_event_template(ATTRIBUTES[:2]), "universalEventTemplate", templates)
    assert body["attributes"][1] == {"file_size": "File Size", "type": "Integer"}


def test_an_attribute_called_like_a_row_field_keeps_uac_shape(templates: TemplateSet) -> None:
    body = _emit(_event_template(ATTRIBUTES), "universalEventTemplate", templates)
    assert body["attributes"][2] == {"name": "type", "label": "Kind", "type": "Text"}
    back = _roundtrip(_event_template(ATTRIBUTES), "universalEventTemplate", templates)
    assert back["attributes"] == ATTRIBUTES


def test_an_attribute_with_no_type_keeps_uac_shape(templates: TemplateSet) -> None:
    rows = [{"name": "file_name", "label": "File Name"}]
    assert _emit(_event_template(rows), "universalEventTemplate", templates)["attributes"] == rows


def test_attributes_in_uac_shape_still_compile() -> None:
    back = compile_object("event_templates", "E", {"attributes": ATTRIBUTES})
    assert back["attributes"] == ATTRIBUTES


# --- the standard style renames nothing ---------------------------------------------------


def test_standard_style_writes_uac_names_only(templates: TemplateSet) -> None:
    stored = {
        "type": "credential",
        "name": "svc",
        "_uacType": "Standard",
        "runtimeUser": "batch",
    }
    _, _, body = uac.emit_object(stored, "credential", templates)
    assert body["runtimeUser"] == "batch" and "user" not in body
