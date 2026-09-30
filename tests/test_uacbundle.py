"""Exporting a UAC Bundle: the controller decides what belongs, not us."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import uacbundle, yamlio
from uac_compose.cli import cli
from uac_compose.core.templates import TemplateSet
from uac_compose.emit import LookupError_, export_text, from_uac_bundle
from uac_compose.learn import Workspace
from uac_compose.transport import BUNDLE_GROUP, FakeTransport

# Shaped as a real `bundle get-report` answers (measured on the MyBundle bundle).
REPORT: dict[str, Any] = {
    "name": "MyBundle",
    "followReferences": True,
    "excludeOnExistence": (
        "Triggers, Calendars, Custom Days, Variables, Credentials, Agent Clusters, "
        "Virtual Resources, Scripts, Email Templates"
    ),
    "bundleTasks": [{"name": "MyBundle iSeries Example 1"}],
    "bundleTriggers": [{"name": "MyBundle iSeries Example #TRIGGER#"}],
    "bundleCredentials": [{"name": "MyBundle Creds"}],
    "bundleBusinessServices": [{"name": "JobAsCode_DemoWorkflow"}],
    "bundleCalendars": [{"name": "System Default"}],
}


def _controller(report: dict[str, Any] | None = None) -> FakeTransport:
    fake = FakeTransport(raw={BUNDLE_GROUP: [report or REPORT]})
    fake.objects = {
        "tasks": [
            {
                "type": "taskUnix",
                "name": "MyBundle iSeries Example 1",
                "command": "ls",
                "agent": "a",
                "exitCodes": "0",
            }
        ],
        "triggers": [
            {
                "type": "triggerTime",
                "name": "MyBundle iSeries Example #TRIGGER#",
                "tasks": ["MyBundle iSeries Example 1"],
            }
        ],
        "credentials": [
            {
                "type": "credential",
                "name": "MyBundle Creds",
                "_uacType": "Resolvable",
                "runtimeUser": "support1",
            }
        ],
        "business_services": [{"type": "businessService", "name": "JobAsCode_DemoWorkflow"}],
        "calendars": [{"type": "calendar", "name": "System Default"}],
    }
    return fake


# --- reading the report ---------------------------------------------------------


def test_members_come_back_in_creation_order() -> None:
    assert uacbundle.members(REPORT) == [
        ("business_services", "JobAsCode_DemoWorkflow"),
        ("credentials", "MyBundle Creds"),
        ("calendars", "System Default"),
        ("tasks", "MyBundle iSeries Example 1"),
        ("triggers", "MyBundle iSeries Example #TRIGGER#"),
    ]


def test_a_member_named_twice_is_one_member() -> None:
    twice = {"bundleTasks": [{"name": "T"}, {"name": "T"}, "T"]}
    assert uacbundle.members(twice) == [("tasks", "T")]


def test_unsupported_members_are_named_and_counted() -> None:
    report = {
        **REPORT,
        "bundleReports": [{"name": "r"}],
        "bundleUniversalTemplates": [{"name": "a"}, {"name": "b"}],
    }
    assert uacbundle.unsupported(report) == ["1 report(s)", "2 universal template(s)"]
    assert uacbundle.unsupported(REPORT) == []


def test_exclude_on_existence_becomes_create_only() -> None:
    sections = uacbundle.create_only(REPORT)
    assert sections == [
        "triggers",
        "calendars",
        "custom_days",
        "variables",
        "credentials",
        "agent_clusters",
        "virtual_resources",
        "scripts",
        "business_services",
    ]
    # "Email Templates" names a kind this format has no section for, so there
    # is nothing here for it to protect. "Agent Clusters" now has one.


def test_business_services_are_always_create_only() -> None:
    """UAC has no label for them, and a promotion must never rewrite one."""
    assert uacbundle.create_only({"excludeOnExistence": "None"}) == ["business_services"]
    assert uacbundle.create_only({}) == ["business_services"]


def test_every_create_only_section_is_a_real_section() -> None:
    assert set(uacbundle.EXCLUDE_SECTIONS.values()) <= set(yamlio.SECTIONS)
    assert set(uacbundle.ALWAYS_CREATE_ONLY) <= set(yamlio.SECTIONS)


def test_every_member_section_is_a_real_section() -> None:
    from uac_compose.transport import KIND_BY_KEY

    assert set(uacbundle.MEMBER_SECTIONS.values()) <= set(yamlio.SECTIONS)
    assert set(uacbundle.MEMBER_SECTIONS.values()) <= set(KIND_BY_KEY)


def test_member_sections_are_in_creation_order() -> None:
    order = list(uacbundle.MEMBER_SECTIONS.values())
    assert order == [s for s in yamlio.SECTIONS if s in order]


# --- collecting ------------------------------------------------------------------


def test_from_uac_bundle_collects_what_the_report_resolves_to(tmp_path: Path) -> None:
    found = from_uac_bundle("MyBundle", Workspace(tmp_path), _controller())
    assert [o.get("name") for o, _ in found.objects] == [
        "JobAsCode_DemoWorkflow",
        "MyBundle Creds",
        "System Default",
        "MyBundle iSeries Example 1",
        "MyBundle iSeries Example #TRIGGER#",
    ]
    assert found.warnings == []
    assert "credentials" in found.create_only


def test_a_member_that_is_gone_is_reported_not_fatal(tmp_path: Path) -> None:
    """`bundleUniversalEventTemplates` lists a Universal Template's own events
    under a qualified name (`SQL ODBC: Duration`); those are created with the
    extension. Measured live on the OtherBundle bundle."""
    report = {**REPORT, "bundleUniversalEventTemplates": [{"name": "SQL ODBC: Duration"}]}
    lines: list[str] = []
    found = from_uac_bundle("MyBundle", Workspace(tmp_path), _controller(report), lines.append)
    assert found.warnings == ["not exported: no event_template named 'SQL ODBC: Duration'"]
    assert len(found.objects) == 5  # the rest are still exported
    # A line per member as it is read, with the time it took; the missing one
    # has its line too, so the count reaches its total.
    assert [re.sub(r"  \d+\.\ds$", "", line) for line in lines] == [
        "   it holds 6 object(s); reading them 1 at a time",
        "[1/6] businessService 'JobAsCode_DemoWorkflow'",
        "[2/6] credential 'MyBundle Creds'",
        "[3/6] calendar 'System Default'",
        "[4/6] no object named 'SQL ODBC: Duration'",
        "[5/6] taskUnix 'MyBundle iSeries Example 1'",
        "[6/6] triggerTime 'MyBundle iSeries Example #TRIGGER#'",
    ]


def test_unsupported_kinds_are_warned_about(tmp_path: Path) -> None:
    report = {**REPORT, "bundleWidgets": [{"name": "w"}]}
    found = from_uac_bundle("MyBundle", Workspace(tmp_path), _controller(report))
    assert found.warnings == ["not exported (1 widget(s)): this format has no section for it"]


def test_an_unknown_bundle_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LookupError_, match="no UAC Bundle named 'Nope'"):
        from_uac_bundle("Nope", Workspace(tmp_path), _controller())


def test_a_bundle_holding_nothing_we_can_write_is_an_error(tmp_path: Path) -> None:
    empty = {"name": "April17 Demo", "bundleReports": [{"name": "r"}]}
    with pytest.raises(LookupError_, match="resolves to nothing this format can hold"):
        from_uac_bundle("April17 Demo", Workspace(tmp_path), _controller(empty))


# --- the file -------------------------------------------------------------------


def test_create_only_is_written_at_the_top_on_one_line(
    tmp_path: Path, templates: TemplateSet
) -> None:
    """Where a reader meets it before the objects it governs, as UAC states it."""
    text, _ = export_text(
        [], None, Workspace(tmp_path), _controller(), templates, uac_bundle="MyBundle"
    )
    head = text.split("\n\n", 1)[0].splitlines()
    assert head[0] == "version: 1"
    assert head[1] == "style: compact"
    assert head[2].startswith("create_only: [") and head[2].endswith("]")
    assert text.count("create_only:") == 1  # said once, in the header


def test_create_only_keeps_the_whole_rule_not_just_what_was_exported(
    tmp_path: Path, templates: TemplateSet
) -> None:
    """The Bundle's standing rule about those kinds, so an object added to the
    file later is covered too. This export has no variables and no scripts."""
    text, _ = export_text(
        [], None, Workspace(tmp_path), _controller(), templates, uac_bundle="MyBundle"
    )
    document = yamlio.load(text)
    assert "variables" not in document and "scripts" not in document
    assert {"variables", "scripts"} <= set(document["create_only"])


def test_the_file_reloads_with_those_objects_marked_create_only(
    tmp_path: Path, templates: TemplateSet
) -> None:
    text, _ = export_text(
        [], None, Workspace(tmp_path), _controller(), templates, uac_bundle="MyBundle"
    )
    bundle = yamlio.read_bundle(text)
    assert ("credentials", "MyBundle Creds") in bundle.create_only
    assert ("calendars", "System Default") in bundle.create_only
    # `Tasks` is not in this Bundle's excludeOnExistence, so its tasks are not seeds
    assert not any(section == "tasks" for section, _ in bundle.create_only)


def test_a_seed_that_drifted_is_kept_not_overwritten(
    tmp_path: Path, templates: TemplateSet
) -> None:
    """What `create_only` is for: the Bundle says create it there if missing,
    never overwrite what is already there -- even under `--update`."""
    from uac_compose import importer
    from uac_compose.compile import compile_bundle

    fake = _controller()
    text, _ = export_text([], None, Workspace(tmp_path), fake, templates, uac_bundle="MyBundle")
    # somebody changed the credential on the target since the Bundle was made
    fake.objects["credentials"][0]["runtimeUser"] = "somebody-else"
    planned = compile_bundle(yamlio.read_bundle(text), templates)
    decided = {
        d.planned.name: d.action
        for d in importer.decide_all(planned, fake, templates.extensions, update=True, services=[])
    }
    assert decided["MyBundle Creds"] == "keep"
    assert decided["MyBundle iSeries Example 1"] == "reuse"


# --- the command ------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = _controller()
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    return controller


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_export_bundle_command(wired: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "b.uac.yaml"
    code, _ = _run("export", "--bundle", "MyBundle", "--out", str(out))
    assert code == 0
    document = yamlio.load(out.read_text())
    assert "MyBundle iSeries Example 1" in document["tasks"]
    assert document["create_only"]


def test_names_and_bundle_together_are_refused(wired: FakeTransport) -> None:
    code, text = _run("export", "T", "--bundle", "MyBundle")
    assert code == 1 and "not both" in text


def test_bundle_with_members_is_refused(wired: FakeTransport) -> None:
    code, text = _run("export", "--bundle", "MyBundle", "--with-members")
    assert code == 1 and "followReferences" in text


def test_export_needs_a_name_or_a_bundle(wired: FakeTransport) -> None:
    code, text = _run("export")
    assert code == 1 and "name something to export, or pass --bundle" in text
