"""The export and import commands end to end, against a fake controller."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import cli as cli_module
from uac_compose import yamlio
from uac_compose.cli import cli
from uac_compose.transport import FakeTransport

TASK = {
    "type": "taskUnix",
    "name": "T",
    "sysId": "1",
    "agent": "linux-01",
    "command": "ls",
    "exitCodes": "0",
}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = FakeTransport(objects={"tasks": [dict(TASK)]})
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    assert cli_module  # the commands import UacCli lazily, from uac_compose.transport
    return controller


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_export_then_import_is_reuse(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "t.uac.yaml"
    code, _ = _run("export", "T", "--out", str(out))
    assert code == 0 and "type: Unix" in out.read_text()
    code, text = _run("import", str(out), "--dry-run")
    assert code == 0 and "reuse    tasks/T" in text


def test_conflict_writes_nothing_and_exits_1(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "t.uac.yaml"
    _run("export", "T", "--out", str(out))
    out.write_text(out.read_text().replace("command: ls", "command: pwd"))
    code, text = _run("import", str(out))
    assert code == 1 and "conflict tasks/T" in text and "nothing was written" in text
    assert fake.writes == []
    code, text = _run("import", str(out), "--update")
    assert code == 0 and "updated  tasks/T" in text


def test_validation_error_stops_before_the_controller(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "t.uac.yaml"
    out.write_text("tasks:\n  New: {type: Unix, agent: a, comand: ls}\n")
    code, text = _run("import", str(out))
    assert code == 1 and "!! tasks/New: 'comand' is not a field of taskUnix" in text
    assert fake.writes == []


def test_errors_are_listed_before_warnings(fake: FakeTransport, tmp_path: Path) -> None:
    """In file order, the error that stopped a live import was 16th of 18 lines."""
    out = tmp_path / "t.uac.yaml"
    out.write_text(
        "tasks:\n"
        "  A: {type: Unix, agent: a, command: ls, twWaitType: Sometimes}\n"
        "  B: {type: Unix, agent: a, comand: ls}\n"
    )
    code, text = _run("import", str(out))
    assert code == 1 and fake.writes == []
    lines = [line for line in text.splitlines() if line[:3] in ("!! ", "?? ")]
    assert lines[0].startswith("!! tasks/B: 'comand' is not a field")
    assert lines[1].startswith("?? tasks/A: twWaitType 'Sometimes'")
    assert "1 error(s)" in text.splitlines()[-1]


def test_create_needs_an_execution_target(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "t.uac.yaml"
    out.write_text("tasks:\n  New: {type: Unix, command: ls}\n")
    code, text = _run("import", str(out))
    assert code == 1 and "no execution target" in text and fake.writes == []


def test_create(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "t.uac.yaml"
    out.write_text("tasks:\n  New: {type: Unix, agent: a, command: ls}\n")
    code, text = _run("import", str(out))
    assert code == 0, text
    assert "exitCodes not stated, creating with '0'" in text and "created  tasks/New" in text
    assert fake.writes[-1][1]["agent"] == "a"


def test_export_says_what_it_reads_on_stderr(fake: FakeTransport) -> None:
    """Each object is one call to the controller, and an export of a few hundred
    took minutes with nothing on the screen. The lines go to stderr: without
    --out, stdout is the file."""
    result = CliRunner().invoke(cli, ["export", "T"])
    assert result.exit_code == 0
    assert yamlio.load(result.stdout)["tasks"]["T"]["command"] == "ls"
    lines = result.stderr.splitlines()
    assert lines[0].startswith("profile ")
    assert lines[0].endswith(": reading 1 object(s), up to 4 at a time")
    assert re.fullmatch(r"\[1/1\] taskUnix 'T'  \d+\.\ds", lines[1])
    assert re.fullmatch(r"read 1 object\(s\) in \d+\.\ds", lines[2])
    assert len(lines) == 3


def test_import_says_what_it_reads_and_writes_on_stderr(
    fake: FakeTransport, tmp_path: Path
) -> None:
    """Import reads every object live too: a line per read and per write."""
    out = tmp_path / "t.uac.yaml"
    out.write_text(
        "tasks:\n"
        "  T: {type: Unix, agent: linux-01, command: ls}\n"
        "  New: {type: Unix, agent: a, command: pwd}\n"
    )
    result = CliRunner().invoke(cli, ["import", str(out), "--parallel", "2"])
    assert result.exit_code == 0, result.output
    assert "created  tasks/New" in result.stdout  # the decisions stay on stdout
    lines = [re.sub(r"  \d+\.\ds$", "", line) for line in result.stderr.splitlines()]
    assert lines[0].endswith(f"{out}, 2 object(s)")
    assert "reading 2 object(s) from the controller, up to 2 at a time" in lines
    # Read two at a time: they may answer in either order, a line each.
    reads = [line for line in lines if re.match(r"\[\d/2\] tasks/", line)]
    assert sorted(line[:5] for line in reads) == ["[1/2]", "[2/2]"]
    assert sorted(line[6:] for line in reads) == ["tasks/New", "tasks/T"]
    assert "[1/1] created  tasks/New" in result.stdout
    assert re.search(r"wrote 1 object\(s\); \d+\.\ds in all", result.stderr)


def test_export_parallel_must_be_at_least_one(fake: FakeTransport) -> None:
    code, text = _run("export", "T", "--parallel", "0")
    assert code == 2 and "--parallel" in text


def test_ambiguous_export_needs_type(fake: FakeTransport) -> None:
    fake.objects["credentials"] = [{"name": "T", "type": "credential"}]
    code, text = _run("export", "T")
    assert code == 1 and "--type" in text


EXAMPLES = sorted(Path(__file__).parent.parent.glob("examples/*.uac.yaml"))


@pytest.mark.parametrize("example", EXAMPLES)
def test_examples_compile_and_validate_clean(example: Path) -> None:
    from uac_compose import validate, yamlio
    from uac_compose.compile import compile_bundle
    from uac_compose.core.templates import TemplateSet

    planned = compile_bundle(yamlio.read_bundle(example.read_text()), TemplateSet("8.0.2.0"))
    assert planned and validate.static(planned) == []


# --- names with wildcards and regular expressions -----------------------------------


@pytest.fixture
def sleepy(fake: FakeTransport) -> FakeTransport:
    fake.objects["tasks"] += [
        {"type": "taskSleep", "name": "Sleep 10", "sleepAmount": 10},
        {"type": "taskUnix", "name": "Nightly SLEEP", "agent": "a", "command": "sleep 5"},
        {
            "type": "taskWorkflow",
            "name": "WF",
            "workflowVertices": [{"task": {"value": "T"}, "vertexId": "2"}],
        },
    ]
    fake.objects["credentials"] = [{"type": "credential", "name": "sleepy", "runtimeUser": "u"}]
    fake.objects["variables"] = [{"type": "variable", "name": "other", "value": "1"}]
    return fake


def _exported(tmp_path: Path, *args: str) -> tuple[int, str, dict[str, Any]]:
    out = tmp_path / "x.uac.yaml"
    code, text = _run("export", *args, "-o", str(out))
    return code, text, (yamlio.load(out.read_text()) if out.exists() else {})


def test_a_wildcard_takes_every_kind_and_ignores_case(
    sleepy: FakeTransport, tmp_path: Path
) -> None:
    code, text, doc = _exported(tmp_path, "*sleep*")
    assert code == 0, text
    assert sorted(doc["tasks"]) == ["Nightly SLEEP", "Sleep 10"]
    assert list(doc["credentials"]) == ["sleepy"] and "variables" not in doc
    assert "3 of 6 listed object(s) match" in text


def test_type_narrows_a_pattern(sleepy: FakeTransport, tmp_path: Path) -> None:
    code, _, doc = _exported(tmp_path, "*sleep*", "-t", "linux")
    assert code == 0 and list(doc["tasks"]) == ["Nightly SLEEP"] and "credentials" not in doc
    code, _, doc = _exported(tmp_path, "*sleep*", "-t", "timer,credential")
    assert (
        code == 0 and list(doc["tasks"]) == ["Sleep 10"] and list(doc["credentials"]) == ["sleepy"]
    )


def test_regex_flag(sleepy: FakeTransport, tmp_path: Path) -> None:
    code, _, doc = _exported(tmp_path, "-E", r"^sleep \d+$")
    assert code == 0 and list(doc["tasks"]) == ["Sleep 10"]
    code, text = _run("export", "-E", "(")
    assert code == 1 and "not a valid regular expression" in text


def test_exact_names_and_patterns_mix(sleepy: FakeTransport, tmp_path: Path) -> None:
    code, _, doc = _exported(tmp_path, "T", "Sleep*")
    assert code == 0 and sorted(doc["tasks"]) == ["Sleep 10", "T"]


def test_one_name_in_two_kinds_is_taken_from_both(sleepy: FakeTransport, tmp_path: Path) -> None:
    sleepy.objects["credentials"].append({"type": "credential", "name": "T", "runtimeUser": "u"})
    code, _, doc = _exported(tmp_path, "T*")
    assert code == 0 and "T" in doc["tasks"] and "T" in doc["credentials"]


def test_with_members_follows_from_a_pattern(sleepy: FakeTransport, tmp_path: Path) -> None:
    code, _, doc = _exported(tmp_path, "W?", "-w")
    assert code == 0 and sorted(doc["tasks"]) == ["T", "WF"]


def test_list_shows_the_matches_and_writes_nothing(sleepy: FakeTransport, tmp_path: Path) -> None:
    code, text, doc = _exported(tmp_path, "*sleep*", "-l")
    assert code == 0 and doc == {}
    lines = [line.split() for line in text.splitlines() if line.startswith(("tasks", "cred"))]
    assert ["tasks", "Unix", "Nightly", "SLEEP"] in lines
    assert ["credentials", "sleepy"] in lines


def _many(fake: FakeTransport) -> None:
    fake.objects["variables"] = [
        {"type": "variable", "name": f"v{i:02d}", "value": "x"} for i in range(20)
    ]


def test_many_matches_need_a_choice(sleepy: FakeTransport, tmp_path: Path) -> None:
    """No terminal, no one to ask: nothing is read."""
    _many(sleepy)
    code, text, doc = _exported(tmp_path, "*")
    assert code == 1 and "add -y to export them all" in text and doc == {}
    code, _, doc = _exported(tmp_path, "*", "-y")
    assert code == 0 and len(doc["variables"]) == 20


def test_many_matches_on_a_terminal_ask_which(
    sleepy: FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _many(sleepy)
    answers = iter(["list", "9", "3"])
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "_ask", lambda _: next(answers))
    code, text, doc = _exported(tmp_path, "*")
    assert code == 0, text
    assert "3) credentials" in text and "     variable               v07" in text
    assert "'9': answer all, none, list, or numbers from 1 to 3" in text
    assert list(doc) == ["version", "style", "create_only", "credentials"]


# --- renaming through was: ----------------------------------------------------------


def _for_update(tmp_path: Path, *names: str) -> Path:
    out = tmp_path / "u.uac.yaml"
    code, text = _run("export", *names, "-P", "update", "-o", str(out))
    assert code == 0, text
    return out


def test_purpose_update_writes_was_and_imports_as_reuse(
    fake: FakeTransport, tmp_path: Path
) -> None:
    out = _for_update(tmp_path, "T")
    doc = yamlio.load(out.read_text())
    assert doc["tasks"]["T"]["was"] == "T" and "create_only" not in doc
    code, text = _run("import", str(out), "-n")
    assert code == 0 and "reuse    tasks/T" in text


def test_a_changed_key_renames_in_place(fake: FakeTransport, tmp_path: Path) -> None:
    out = _for_update(tmp_path, "T")
    out.write_text(out.read_text().replace("\n  T:\n", "\n  DEV_T:\n"))
    code, text = _run("import", str(out))
    assert code == 1 and "conflict tasks/DEV_T  (was 'T')" in text and fake.writes == []
    code, text = _run("import", str(out), "-u")
    assert code == 0 and "rename   tasks/DEV_T  (was 'T')" in text, text
    assert [(o["name"], o["sysId"]) for o in fake.objects["tasks"]] == [("DEV_T", "1")]
    code, text = _run("import", str(out), "-n")  # a second run finds it done
    assert code == 0 and "reuse    tasks/DEV_T" in text and "'T' is already called 'DEV_T'" in text


def test_a_rename_is_refused_when_both_names_exist(fake: FakeTransport, tmp_path: Path) -> None:
    fake.objects["tasks"].append({**fake.objects["tasks"][0], "name": "DEV_T", "sysId": "2"})
    out = tmp_path / "r.uac.yaml"
    out.write_text("tasks:\n  DEV_T: {was: T, type: Unix, agent: linux-01, command: ls}\n")
    code, text = _run("import", str(out), "-u")
    assert code == 1 and "both 'T' and 'DEV_T' exist; rename refused" in text
    assert fake.writes == []


def test_was_naming_nothing_creates(fake: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "r.uac.yaml"
    out.write_text("tasks:\n  New: {was: Gone, type: Unix, agent: a, command: ls}\n")
    code, text = _run("import", str(out))
    assert (
        code == 0
        and "created  tasks/New" in text
        and "was: 'Gone' is not on the controller" in text
    )


def test_the_file_follows_its_own_rename(fake: FakeTransport, tmp_path: Path) -> None:
    """A workflow in the same file that still names the old task."""
    fake.objects["tasks"].append(
        {
            "type": "taskWorkflow",
            "name": "WF",
            "sysId": "9",
            "workflowVertices": [{"task": {"value": "T"}, "vertexId": "2"}],
        }
    )
    out = _for_update(tmp_path, "T", "WF")
    out.write_text(out.read_text().replace("\n  T:\n", "\n  DEV_T:\n"))
    code, text = _run("import", str(out), "-u")
    assert code == 0, text
    assert "workflow vertex: 'T' -> 'DEV_T' (renamed in this file)" in text
    wf = next(o for o in fake.objects["tasks"] if o["name"] == "WF")
    assert wf["workflowVertices"][0]["task"]["value"] == "DEV_T"


def test_a_create_only_object_is_not_renamed(fake: FakeTransport, tmp_path: Path) -> None:
    fake.objects["credentials"] = [
        {"type": "credential", "name": "c", "sysId": "5", "runtimeUser": "u"}
    ]
    out = tmp_path / "r.uac.yaml"
    out.write_text("create_only: [credentials]\ncredentials:\n  c2: {was: c, runtimeUser: u}\n")
    code, text = _run("import", str(out), "-u")
    assert code == 1 and "rename refused: it is create_only" in text


def test_was_never_changes_what_compiles(fake: FakeTransport, tmp_path: Path) -> None:
    """Rule 2: `was:` is an annotation, like `comment:`; it never reaches an object."""
    from uac_compose.compile import compile_bundle

    plain = tmp_path / "p.uac.yaml"
    _run("export", "T", "-o", str(plain))
    marked = _for_update(tmp_path, "T")

    def objects(path: Path) -> list[dict[str, Any]]:
        return [p.obj for p in compile_bundle(yamlio.read_bundle(path.read_text()))]

    assert objects(marked) == objects(plain)
    assert "was" not in objects(marked)[0]


def test_a_file_rename_moves_edges_and_run_criteria(fake: FakeTransport, tmp_path: Path) -> None:
    fake.objects["tasks"] += [
        {"type": "taskUnix", "name": "U", "sysId": "7", "agent": "a", "command": "x"},
        {
            "type": "taskWorkflow",
            "name": "WF",
            "sysId": "9",
            "workflowVertices": [
                {"task": {"value": "T"}, "vertexId": "1"},
                {"task": {"value": "U"}, "vertexId": "2"},
            ],
            "workflowEdges": [
                {
                    "condition": {"value": "Success"},
                    "sourceId": {"taskName": "T", "value": "1"},
                    "targetId": {"taskName": "U", "value": "2"},
                }
            ],
        },
    ]
    out = _for_update(tmp_path, "T", "U", "WF")
    out.write_text(out.read_text().replace("\n  T:\n", "\n  DEV_T:\n"))
    code, text = _run("import", str(out), "-u")
    assert code == 0, text
    wf = next(o for o in fake.objects["tasks"] if o["name"] == "WF")
    assert [v["task"]["value"] for v in wf["workflowVertices"]] == ["DEV_T", "U"]
    assert wf["workflowEdges"][0]["sourceId"]["taskName"] == "DEV_T"


def test_plain_uac_compose_says_the_environment_variables() -> None:
    """Every UAC_COMPOSE_* the code reads is in the help, but the tests' own."""
    source = Path(__file__).parent.parent / "src" / "uac_compose"
    read = {
        name
        for path in source.rglob("*.py")
        for name in re.findall(r"environ\.get\(\"(UAC_COMPOSE_\w+)\"", path.read_text())
    }
    read.discard("UAC_COMPOSE_CORPUS")  # the test corpus, for `make test-corpus`
    assert read == {"UAC_COMPOSE_PROFILE", "UAC_COMPOSE_WORKSPACE", "UAC_COMPOSE_UAC"}
    _, text = _run()
    assert "Environment variables:" in text
    for name in read:
        assert name in text
