"""Milestone 4: the compact style compiles to exactly the objects the uac style does."""

from __future__ import annotations

from typing import Any

import pytest

from uac_compose import groups, idioms, registry, typealias, yamlio
from uac_compose.compile import CompileError, compile_bundle, compile_object
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.style import compact
from uac_compose.transport import kind_for_type
from uac_compose.workflow import canonical


def _task(body: dict[str, Any]) -> dict[str, Any]:
    return compile_object("tasks", "T", body)


# --- types ------------------------------------------------------------


def test_type_alias_resolved_per_section() -> None:
    assert typealias.to_uac("tasks", "File Monitor") == "taskFileMonitor"
    assert typealias.to_uac("triggers", "File Monitor") == "triggerFm"
    assert typealias.to_uac("tasks", "unix") == "taskUnix"
    assert typealias.to_uac("tasks", "taskUnix") == "taskUnix"
    assert typealias.to_uac("tasks", "Timer") == "taskSleep"
    assert typealias.alias_for("taskUniversalMonitor") == "Universal Monitor"
    assert typealias.alias_for("triggerCron") == "Cron"


def test_unknown_alias_names_the_known_ones() -> None:
    with pytest.raises(CompileError, match="Unix"):
        _task({"type": "Unx"})


# --- sla, the one block that is left ------------------------------------------


def test_sla_block_equals_the_flat_form() -> None:
    flat = {
        "type": "Unix",
        "agent": "a",
        "lfEnabled": True,
        "lfType": "Duration",
        "lsEnabled": True,
        "userEstimatedDuration": "00:01:00:00",
    }
    grouped = {
        "type": "Unix",
        "agent": "a",
        "sla": {
            "userEstimatedDuration": "00:01:00:00",
            "late_start": {"enabled": True},
            "late_finish": {"type": "Duration"},
        },
    }
    assert _task(grouped) == _task(flat)


def test_everything_else_is_written_flat() -> None:
    body = groups.regroup(
        "taskUnix",
        {"type": "Unix", "agent": "a", "retryMaximum": 3, "exitCodes": "0", "lfEnabled": True},
    )
    assert body == {
        "type": "Unix",
        "agent": "a",
        "retryMaximum": 3,
        "exitCodes": "0",
        "sla": {"late_finish": {"enabled": True}},
    }


def test_sla_is_written_last() -> None:
    body = groups.regroup("taskUnix", {"lfEnabled": True, "command": "ls"})
    assert list(body) == ["command", "sla"]


def test_an_object_with_no_sla_field_gets_no_block() -> None:
    assert groups.regroup("taskUnix", {"type": "Unix", "command": "ls"}) == {
        "type": "Unix",
        "command": "ls",
    }


def test_sla_block_rejects_a_foreign_field() -> None:
    with pytest.raises(CompileError, match="'retryMaximum' is not part of sla"):
        _task({"type": "Unix", "sla": {"retryMaximum": 3}})
    with pytest.raises(CompileError, match="'lsEnabled' is not part of sla.late_finish"):
        _task({"type": "Unix", "sla": {"late_finish": {"lsEnabled": True}}})
    with pytest.raises(CompileError, match="'nope' is not part of sla.late_finish"):
        _task({"type": "Unix", "sla": {"late_finish": {"nope": 1}}})


def test_a_field_flat_and_in_sla_is_rejected() -> None:
    with pytest.raises(CompileError, match="written both on its own and inside sla"):
        _task({"type": "Unix", "lfEnabled": True, "sla": {"late_finish": {"lfEnabled": False}}})


def test_a_type_with_a_field_called_sla_keeps_it() -> None:
    """The block is display sugar; a real field of that name always wins."""
    assert all(
        not groups.blocks(t) or "sla" not in registry.get(t).fields for t in registry.types()
    )


# --- like: ---------------------------------------------------------------------

LIKE = """
templates:
  standard-retry:
    retryMaximum: 3
    retryInterval: 300
  batch-target:
    agent: linux-batch-01
    credentials: etl-svc
tasks:
  Load A:
    type: Unix
    like: [standard-retry, batch-target]
    command: /opt/a.sh
  Load B:
    like: Load A
    command: /opt/b.sh
    retryMaximum: 5
"""


def _planned(text: str) -> dict[str, dict[str, Any]]:
    return {p.name: p.obj for p in compile_bundle(yamlio.read_bundle(text))}


def test_like_copies_a_whole_template() -> None:
    planned = _planned(LIKE)
    a = planned["Load A"]
    assert a["retryMaximum"] == 3 and a["retryInterval"] == 300
    assert a["agent"] == "linux-batch-01" and a["credentials"] == "etl-svc"
    assert a["command"] == "/opt/a.sh"


def test_a_template_may_hold_anything_and_needs_no_type() -> None:
    """A template is a bag of fields, not an object: the definition decides the type."""
    assert _planned(LIKE)["Load A"]["type"] == "taskUnix"


def test_like_copies_another_object() -> None:
    planned = _planned(LIKE)
    assert planned["Load B"]["agent"] == "linux-batch-01"
    assert planned["Load B"]["command"] == "/opt/b.sh"


def test_the_definition_overrides_what_it_copied() -> None:
    assert _planned(LIKE)["Load B"]["retryMaximum"] == 5


def test_several_sources_apply_in_the_order_written() -> None:
    text = (
        "templates:\n"
        "  first: {command: /opt/a.sh, retryMaximum: 1}\n"
        "  second: {command: /opt/b.sh}\n"
        "tasks:\n"
        "  T: {type: Unix, like: [first, second]}\n"
        "  U: {type: Unix, like: [second, first]}\n"
    )
    planned = _planned(text)
    assert planned["T"]["command"] == "/opt/b.sh"  # the later one wins
    assert planned["U"]["command"] == "/opt/a.sh"
    assert planned["T"]["retryMaximum"] == planned["U"]["retryMaximum"] == 1


def test_a_copied_mapping_merges_and_a_copied_list_replaces() -> None:
    text = (
        "templates:\n"
        "  base:\n"
        "    sla: {late_finish: {lfEnabled: true, lfType: Duration}}\n"
        "    variables: [{a: '1'}, {b: '2'}]\n"
        "tasks:\n"
        "  T:\n"
        "    type: Unix\n"
        "    like: base\n"
        "    sla: {late_finish: {lfDuration: 2h}}\n"
        "    variables: [{c: '3'}]\n"
    )
    task = _planned(text)["T"]
    assert task["lfEnabled"] is True and task["lfDuration"] == "00:02:00:00"
    assert task["variables"] == [{"name": "c", "value": "3"}]


def test_templates_are_never_created() -> None:
    assert "standard-retry" not in _planned(LIKE)


def test_like_loop_rejected() -> None:
    with pytest.raises(CompileError, match="loops"):
        _planned("tasks:\n  A: {like: B, type: Unix}\n  B: {like: A, type: Unix}\n")


def test_like_unknown_source_rejected() -> None:
    with pytest.raises(CompileError, match="names nothing"):
        _planned("tasks:\n  A: {type: Unix, like: nope}\n")


def test_like_takes_a_name_or_a_list() -> None:
    with pytest.raises(CompileError, match="a name, or a list of names"):
        _planned("tasks:\n  A: {type: Unix, like: {a: 1}}\n")


# --- idioms ------------------------------------------------------------


def test_var_pair_idiom_roundtrip() -> None:
    assert _task({"type": "Unix", "agent": "${ag}"})["agentVar"] == "${ag}"
    assert _task({"type": "Unix", "agent": "linux-01"})["agent"] == "linux-01"
    assert idioms.emit("taskUnix", {"agentVar": "${ag}"}) == {"agent": "${ag}"}
    # a literal in the *Var half stays where it is
    assert idioms.emit("taskUnix", {"agentVar": "plain"}) == {"agentVar": "plain"}


def test_var_pair_idiom_not_on_scalar_pair() -> None:
    assert _task({"type": "Unix", "resPriority": "${p}"})["resPriority"] == "${p}"


def test_duration_idiom_four_parts() -> None:
    assert idioms.parse_duration("1h 49m") == "00:01:49:00"
    assert idioms.parse_duration("90s") == "00:00:01:30"
    assert idioms.parse_duration("2d 6h") == "02:06:00:00"
    assert idioms.render_duration("00:01:49:00") == "1h 49m"
    assert idioms.render_duration("00:00:00:00") == "0s"
    assert idioms.render_duration("00:25:00:00") is None  # not canonical: stays raw
    assert _task({"type": "Unix", "lfDuration": "2h"})["lfDuration"] == "00:02:00:00"


def test_duration_idiom_not_on_offset_fields() -> None:
    assert "lfOffsetDuration" not in idioms.duration_fields("taskUnix")
    assert _task({"type": "Unix", "lfOffsetDuration": "2h"})["lfOffsetDuration"] == "2h"


def test_name_value_lists() -> None:
    assert idioms.emit_pairs([{"name": "a", "value": "1"}, {"name": "b"}]) == [
        {"a": "1"},
        {"b": None},
    ]
    assert idioms.emit_pairs([{"name": "a", "value": "1", "description": "d"}]) is None
    assert idioms.emit_pairs([{"name": "name", "value": "1"}]) is None
    rows = _task({"type": "Unix", "variables": [{"region": "eu"}, {"empty": None}]})["variables"]
    assert rows == [{"name": "region", "value": "eu"}, {"name": "empty"}]


def test_custom_field_value_only() -> None:
    assert _task({"type": "Unix", "customField1": "nightly"})["customField1"] == {
        "value": "nightly"
    }
    assert idioms.emit("taskUnix", {"customField1": {"label": "TAG", "value": "x"}}) == {
        "customField1": "x"
    }


def test_email_on_only_when_lossless() -> None:
    plain = {"emailNotifications": [{"status": "Failed", "emailTemplate": "ops"}]}
    assert idioms.emit_email_on(plain) == {"email_on": ["failure"], "email_template": "ops"}
    with_recipient = {"emailNotifications": [{"status": "Failed", "to": "a@b.c"}]}
    assert idioms.emit_email_on(with_recipient) == with_recipient
    two = {"emailNotifications": [{"status": "Failed"}, {"status": "Success"}]}
    assert idioms.emit_email_on(two) == two
    back = _task({"type": "Unix", "actions": {"email_on": ["failure"], "email_template": "ops"}})
    assert back["actions"] == plain


def test_set_variable_runs_joined_in_order() -> None:
    def row(name: str, scope: str = "Parent") -> dict[str, str]:
        return {"status": "Success", "variableName": name, "variableValue": f"${{{name}}}",
                "variableScope": scope}  # fmt: skip

    rows = [row("a"), row("b"), row("c", "Global"), row("d")]
    short = idioms.emit_set_variables({"setVariableActions": rows})["setVariableActions"]
    assert short == [
        {"status": "Success", "variableScope": "Parent",
         "variables": [{"a": "${a}"}, {"b": "${b}"}]},
        row("c", "Global"),
        row("d"),
    ]  # fmt: skip
    assert idioms.parse_set_variables(short) == rows
    back = _task({"type": "Unix", "actions": {"setVariableActions": short}})
    assert back["actions"]["setVariableActions"] == rows


def test_email_connection_without_email_on_rejected() -> None:
    with pytest.raises(CompileError, match="without email_on"):
        _task({"type": "Unix", "actions": {"email_connection": "smtp"}})


# --- the corpus ------------------------------------------------------------


def _expected(e: ObjectEnvelope, templates: TemplateSet, show_inactive: bool) -> dict[str, Any]:
    from uac_compose import inactive

    stamped = kind_for_type(e.type).stamp(e.obj)
    small = simplify(
        stamped,
        templates.get(e.type),
        templates.sub_templates,
        extensions=templates.extensions,
        reserved=frozenset(registry.get(e.type).fields),
    )
    return small if show_inactive else inactive.hide(small, normalize(stamped))


@pytest.mark.corpus
@pytest.mark.parametrize("show_inactive", [True, False])
def test_emit_compile_identity_compact(
    corpus: list[ObjectEnvelope], templates: TemplateSet, show_inactive: bool
) -> None:
    """compile(emit(x, compact)) == simplify(x), through YAML text.

    Up to the two things that are not the file's to state: controller-owned
    labels, and the order of a workflow's vertices, edges and criteria (sets to UAC).
    """
    failures, raw = [], 0
    for e in corpus:
        section, name, body, ok = compact.emit_object(
            e.obj, e.type, templates, show_inactive=show_inactive
        )
        raw += not ok
        back = yamlio.load(yamlio.dump({section: {name: body}}))[section][name]
        got = compile_object(section, name, back, templates)
        want = _expected(e, templates, show_inactive)
        if canonical(idioms.without_labels(got)) != canonical(idioms.without_labels(want)):
            failures.append(f"{e.type}/{e.name}")
    assert not failures, f"{len(failures)} of {len(corpus)}: {failures[:5]}"
    assert raw <= 5  # measured: 5 workflows whose edges carry stale aliases


@pytest.mark.corpus
def test_compact_is_smaller_than_uac(corpus: list[ObjectEnvelope], templates: TemplateSet) -> None:
    from uac_compose.style import uac

    objects = [(e.obj, e.type) for e in corpus]
    small = len(yamlio.dump(compact.emit_document(objects, templates)))
    plain = len(yamlio.dump(uac.emit_document(objects, templates)))
    assert small < plain
