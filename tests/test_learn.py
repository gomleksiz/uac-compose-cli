"""Milestone 2: transport rules, template derivation gates, and `learn` end to end."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from uac_compose.core.derive import derive
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.learn import Workspace, learn
from uac_compose.transport import (
    KIND_BY_KEY,
    FakeTransport,
    TransportError,
    UacCli,
    kind_for_type,
    name_arg,
    reason,
)

# --- transport ------------------------------------------------------------


def test_name_arg_table() -> None:
    assert name_arg("task") == "taskname"
    assert name_arg("custom-day") == "customdayname"
    assert name_arg("credential") == "credentialname"
    assert name_arg("virtual-resource") == "resourcename"
    assert name_arg("business-service") == "busservicename"
    assert name_arg("universal-event-template") == "templatename"
    assert name_arg("connection database") == "connectionname"


class Recorder:
    """A fake `uac` process: records argv, answers with fixed output."""

    def __init__(self, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        self.stdout, self.stderr, self.code = stdout, stderr, code
        self.calls: list[list[str]] = []
        self.inputs: list[Any] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "--input" in argv:
            self.inputs.append(json.loads(Path(argv[argv.index("--input") + 1]).read_text()))
        return subprocess.CompletedProcess(list(argv), self.code, self.stdout, self.stderr)


USAGE = "Usage: uac task get [OPTIONS]\n  --log-level [DEBUG|INFO|WARNING|ERROR]\n"


def test_404_is_absent() -> None:
    run = Recorder(stderr="Response Code : 404\nTask x does not exist.\n", code=1)
    assert UacCli("p", run).get("taskUnix", "x") is None
    assert run.calls[0] == ["uac", "--profile", "p", "task", "get", "taskname=x"]


def test_no_usage_found_is_not_absent() -> None:
    run = Recorder(stderr="No usage found for argument foo\nResponse Code : 404\n" + USAGE, code=1)
    with pytest.raises(TransportError, match="No usage found"):
        UacCli("p", run).get("taskUnix", "x")


def test_error_reason_is_first_line() -> None:
    assert reason("Connection refused\n" + USAGE) == "Connection refused"
    run = Recorder(stderr="Connection refused\n" + USAGE, code=1)
    with pytest.raises(TransportError, match="Connection refused$"):
        UacCli("p", run).get("taskUnix", "x")


def test_connections_are_two_levels_deep() -> None:
    run = Recorder(stdout='{"name": "db"}')
    got = UacCli("p", run).get("databaseConnection", "db")
    assert run.calls[0][3:] == ["connection", "database", "get", "connectionname=db"]
    assert got == {"name": "db", "type": "databaseConnection"}


def test_credential_type_moved_not_overwritten() -> None:
    run = Recorder(stdout='{"name": "c", "type": "Standard"}')
    got = UacCli("p", run).get("credential", "c")
    assert got == {"name": "c", "type": "credential", "_uacType": "Standard"}


def test_routing_key_stripped_on_write() -> None:
    run = Recorder(stdout="created with id 123")
    UacCli("p", run).update({"name": "c", "type": "credential", "_uacType": "Standard"})
    assert run.inputs == [{"name": "c", "type": "Standard"}]
    run = Recorder(stdout="created with id 123")
    UacCli("p", run).create({"name": "v", "type": "variable", "value": "1"})
    assert run.inputs == [{"name": "v", "value": "1"}]


def test_task_type_is_sent() -> None:
    run = Recorder(stdout="created with id 123")
    UacCli("p", run).create({"name": "t", "type": "taskUnix"})
    assert run.inputs == [{"name": "t", "type": "taskUnix"}]


def test_business_service_create_names_itself() -> None:
    run = Recorder(stdout="created with id 123")
    UacCli("p", run).create({"name": "BS", "type": "businessService"})
    argv = run.calls[0]
    assert argv[3:6] == ["business-service", "create", "name=BS"]


def test_failed_write_raises() -> None:
    run = Recorder(stdout="", stderr="Duplicate value\n" + USAGE, code=1)
    with pytest.raises(TransportError, match="Duplicate value"):
        UacCli("p", run).create({"name": "t", "type": "taskUnix"})


def test_kind_for_type() -> None:
    assert kind_for_type("taskWorkflow").key == "tasks"
    assert kind_for_type("triggerCron").key == "triggers"
    assert kind_for_type("script").name_key == "scriptName"


# --- derivation gates ------------------------------------------------------------


def _env(obj: dict[str, Any]) -> ObjectEnvelope:
    return ObjectEnvelope(
        type=obj["type"], name=obj["name"], obj=obj, release="8.0.2.0", kind="task"
    )


def _unix(n: int, **fields: Any) -> dict[str, Any]:
    return {"type": "taskUnix", "name": f"t{n}", "exportTable": "ops_task_unix", **fields}


def test_reference_never_defaulted() -> None:
    envs = [_env(_unix(i, agent="linux-01", credentials="c1", command="ls")) for i in range(5)]
    template = derive(envs).templates["taskUnix"]
    assert "agent" not in template and "credentials" not in template
    assert template["command"] == "ls"


def test_partial_field_gets_no_default() -> None:
    envs = [_env(_unix(i, exitCodes="0")) for i in range(5)] + [_env(_unix(9))]
    assert "exitCodes" not in derive(envs).templates["taskUnix"]


def test_varying_shape_gets_no_default() -> None:
    plain = {"condition": {"value": "Success"}}
    variable = {"condition": {"value": "x", "operator": "=", "variable": "v", "type": "var"}}
    edges = [plain] * 4 + [variable]
    envs = [
        _env({"type": "taskWorkflow", "name": f"w{i}", "workflowEdges": [edge]})
        for i, edge in enumerate(edges)
    ]
    assert "condition" not in derive(envs).sub_templates["workflowEdges"]


def test_low_support_gets_no_default() -> None:
    envs = [_env(_unix(i, command=f"c{i % 3}")) for i in range(6)]
    assert "command" not in derive(envs).templates["taskUnix"]


def test_documented_default_wins_where_present_and_observed() -> None:
    # 3 of 5 say Duration (the statistic); the docs say Time, seen on 2.
    types = ["Duration"] * 3 + ["Time"] * 2
    envs = [_env(_unix(i, lfType=t)) for i, t in enumerate(types)]
    derived = derive(envs)
    assert derived.templates["taskUnix"]["lfType"] == "Time"
    assert ("lfType", "Duration", "Time") in derived.report["taskUnix"].overruled


def test_disputed_documented_default_withheld() -> None:
    envs = [_env(_unix(i, lfType="Duration")) for i in range(5)]
    derived = derive(envs)
    assert derived.templates["taskUnix"]["lfType"] == "Duration"
    assert any(f == "lfType" for f, _ in derived.report["taskUnix"].withheld)


def test_never_default_sub_field() -> None:
    headers = [{"name": f"h{i}", "value": "same"} for i in range(5)]
    envs = [
        _env({"type": "taskWebService", "name": f"w{i}", "httpHeaders": [h]})
        for i, h in enumerate(headers)
    ]
    assert "value" not in derive(envs).sub_templates["httpHeaders"]


def test_a_shape_the_profile_never_saw_falls_back_to_the_shipped_one(
    tmp_path: Path,
) -> None:
    """A controller with no Approval task derives no `approvalCriteria.criteria`,
    so `inflate` could not put back the `type: criterion` the controller
    demands. Measured on a real promotion: the target had 12 of the 34 shapes
    and the create was refused with "missing type id property 'type'".
    """
    from uac_compose.core.templates import TemplateSet

    release = "8.0.2.0"
    thin = tmp_path / release
    (thin / "_sub").mkdir(parents=True)
    (thin / "_sub" / "variables.json").write_text('{"required": false}')
    templates = TemplateSet(release, root=tmp_path)
    assert templates.sub_templates["variables"] == {"required": False}  # its own wins
    assert templates.sub_templates["approvalCriteria.criteria"] == {"type": "criterion"}
    assert len(templates.sub_templates) > 30


def test_the_shipped_set_does_not_fall_back_to_itself() -> None:
    from uac_compose.core.templates import TemplateSet, shipped_sub_templates

    shipped = TemplateSet("8.0.2.0")
    assert set(shipped.sub_templates) >= set(shipped_sub_templates("8.0.2.0"))


# --- learn end to end ------------------------------------------------------------


def _fake_from(objs: list[dict[str, Any]]) -> FakeTransport:
    fake = FakeTransport(raw={"agent": [{"name": "linux-01", "type": "Linux/Unix", "ip": "x"}]})
    for obj in objs:
        kind = KIND_BY_KEY["tasks"] if obj["type"].startswith("task") else None
        assert kind is not None
        fake.objects.setdefault(kind.key, []).append(kind.stamp(obj))
    return fake


# A real controller hands back every field, so a stub that carries only four
# would "lose" the rest on the way back. These stubs carry what UAC would.
SHIPPED_UNIX = TemplateSet("8.0.2.0").get("taskUnix")
SMALL = [
    _unix(i, **SHIPPED_UNIX, command="ls", exportReleaseLevel="8.0.2.0", agent="a")
    for i in range(4)
]


def test_learn_writes_workspace(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    result = learn(ws, _fake_from(SMALL), refresh=False, echo=lambda _: None)
    assert result.check.ok and result.check.total == 4
    assert result.release == "8.0.2.0"
    assert json.loads(ws.source.read_text())["counts"]["tasks"] == 4
    refs = json.loads(ws.references.read_text())
    assert refs["agents"] == [{"name": "linux-01", "type": "Linux/Unix"}]  # identity only
    # The extension map is what a profile learns. Defaults are not learned.
    written = sorted(p.name for p in ws.templates("8.0.2.0").iterdir())
    assert written == ["_extensions.json"]


def test_defaults_are_the_shipped_ones_whatever_the_profile_saw(tmp_path: Path) -> None:
    """The bug this rule exists for: one controller's habit must not decide a file.

    A promotion moved a task from a controller that mostly used one `sapView`
    to one that mostly used the other. Export dropped the field against the
    first default and import filled it from the second, so the object changed
    with no conflict and no warning. Both ends now default from the same file.
    """
    # Every task on this controller holds a value the shipped default disputes.
    assert SHIPPED_UNIX["exitCodeProcessing"] == "Success Exitcode Range"
    fields = {**SHIPPED_UNIX, "exitCodeProcessing": "Success", "exportReleaseLevel": "8.0.2.0"}
    odd = [_unix(i, **fields) for i in range(9)]
    ws = Workspace(tmp_path)
    result = learn(ws, _fake_from(odd), refresh=False, echo=lambda _: None)
    assert result.check.ok
    assert ws.template_set().get("taskUnix") == SHIPPED_UNIX
    # It is not hidden, though: learn says where the controller differs, and a
    # file from it states the field rather than leaving it out.
    assert any(line.startswith("taskUnix.exitCodeProcessing:") for line in result.disagreements)


def test_learn_reuses_cache(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    learn(ws, _fake_from(SMALL), refresh=False, echo=lambda _: None)
    again = learn(ws, FakeTransport(), refresh=False, echo=lambda _: None)
    assert again.fetched == [] and again.counts["tasks"] == 4
    refreshed = learn(ws, FakeTransport(), refresh=True, echo=lambda _: None)
    assert refreshed.counts["tasks"] == 0


def test_learn_writes_nothing_on_failed_self_check(tmp_path: Path) -> None:
    """A controller whose objects do not fit the shipped defaults is told so.

    Here the objects lack fields the shipped template defaults, so `inflate`
    would put back what the object never had.
    """
    ws = Workspace(tmp_path)
    stubs = [_unix(i, command="ls", exportReleaseLevel="8.0.2.0") for i in range(4)]
    result = learn(ws, _fake_from(stubs), refresh=False, echo=lambda _: None)
    assert not result.check.ok
    assert not ws.templates("8.0.2.0").exists()
    assert not ws.source.exists()
    assert (tmp_path / "learn-report.txt").read_text().count("taskUnix/") == 4


# --- the corpus as a profile ------------------------------------------------------------


@pytest.mark.corpus
def test_learn_corpus_as_profile(corpus: list[ObjectEnvelope], tmp_path: Path) -> None:
    fake = FakeTransport()
    for e in corpus:
        kind = kind_for_type(e.type)
        fake.objects.setdefault(kind.key, []).append(kind.stamp(e.obj))
    result = learn(Workspace(tmp_path), fake, refresh=False, echo=lambda _: None)
    assert result.check.total == len(corpus) == 5286
    assert result.check.ok, result.check.failures[:5]


# --- live (opt-in) -----------------------------------------------------------------


@pytest.mark.live
def test_learn_live(tmp_path: Path) -> None:
    import os

    profile = os.environ.get("UAC_COMPOSE_LIVE_PROFILE")
    if not profile:
        pytest.skip("set UAC_COMPOSE_LIVE_PROFILE to run against a controller")
    result = learn(Workspace(tmp_path), UacCli(profile), refresh=True, echo=print)
    assert result.check.ok, result.check.failures[:5]


# The two lines the `uac` CLI writes for a refused write, as measured live.
REFUSED = (
    "ERROR:root:https://host/resources/task Response Code : 400\n"
    "ERROR:root:Failed with reason : Unable to map JSON input. "
    'Unrecognized property: "sleepAmount".\n'
)


def test_the_controllers_reason_is_shown_not_just_the_status() -> None:
    """Measured live: only "Response Code : 400" reached the reader; the cause was dropped."""
    assert reason(REFUSED) == (
        'HTTP 400: Unable to map JSON input. Unrecognized property: "sleepAmount".'
    )


def test_a_long_reason_is_kept_whole() -> None:
    """A user group refusal names every missing user; cutting it hides the ones at the end."""
    names = ", ".join(f"Cannot find user: u{i}." for i in range(40))
    said = f"ERROR:root:x Response Code : 400\nERROR:root:Failed with reason : {names}\n"
    assert reason(said).endswith("Cannot find user: u39.")


def test_a_refused_write_raises_with_the_reason() -> None:
    run = Recorder(stderr=REFUSED, code=1)
    with pytest.raises(TransportError, match='Unrecognized property: "sleepAmount"'):
        UacCli("p", run).update({"type": "taskUnix", "name": "x"})


# --- finding the uac CLI --------------------------------------------------------------


def test_uac_command_from_the_environment_then_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import stat

    from uac_compose.transport import TransportError, uac_command

    monkeypatch.delenv("UAC_COMPOSE_UAC", raising=False)
    exe = tmp_path / ("uac.exe" if os.name == "nt" else "uac")
    exe.write_text("")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert Path(uac_command()) == exe  # PATHEXT on Windows: uac.exe for "uac"
    monkeypatch.setenv("UAC_COMPOSE_UAC", str(exe))
    monkeypatch.setenv("PATH", "")
    assert Path(uac_command()) == exe  # a full path needs no PATH
    monkeypatch.setenv("UAC_COMPOSE_UAC", str(tmp_path / "nope"))
    with pytest.raises(TransportError, match="UAC_COMPOSE_UAC=.*no such program"):
        uac_command()


def test_no_uac_cli_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from uac_compose.transport import TransportError

    monkeypatch.delenv("UAC_COMPOSE_UAC", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(TransportError, match="uac CLI was not found on PATH"):
        UacCli("p").get("taskUnix", "x")


def test_the_runner_starts_the_found_cli_and_reads_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uac_compose import transport

    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"], seen["kwargs"] = command, kwargs
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(transport, "uac_command", lambda: "/opt/uac/bin/uac")
    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    transport._run(["uac", "--profile", "p", "task", "list"])
    assert seen["command"] == ["/opt/uac/bin/uac", "--profile", "p", "task", "list"]
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"
