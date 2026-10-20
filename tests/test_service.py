"""export, import and apply as functions, with no CLI: what a Universal Extension runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from uac_compose import service
from uac_compose.learn import Workspace
from uac_compose.service import ServiceError
from uac_compose.transport import FakeTransport


def _unix(name: str, sys_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "taskUnix",
        "name": name,
        "sysId": sys_id,
        "agent": "linux-01",
        "exitCodes": "0",
        **fields,
    }


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport(objects={"tasks": [_unix("T", "1", command="ls")]})


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")


class Lines:
    """Collects what a callback is given."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_export_then_import_is_reuse(fake: FakeTransport, ws: Workspace) -> None:
    exported = service.export_bundle(fake, ws, ["T"])
    assert "type: Unix" in exported.text and exported.warnings == []
    out = Lines()
    result = service.import_bundle(fake, ws, exported.text, dry_run=True, out=out)
    assert [d.action for d in result.decisions] == ["reuse"]
    assert "dry run: nothing was written" in out.lines


def test_import_creates_and_says_so(fake: FakeTransport, ws: Workspace) -> None:
    text = "tasks:\n  New: {type: Unix, agent: linux-01, command: pwd}\n"
    out, say = Lines(), Lines()
    result = service.import_bundle(fake, ws, text, out=out, say=say, profile_label="atl")
    assert result.written == 1 and fake.writes[0][0] == "create"
    assert any(line.startswith("[1/1] created  tasks/New") for line in out.lines)
    assert say.lines[0] == "profile atl: bundle"  # `source` names it in messages


def test_a_conflict_raises_and_writes_nothing(fake: FakeTransport, ws: Workspace) -> None:
    text = "tasks:\n  T: {type: Unix, agent: linux-01, command: pwd}\n"
    with pytest.raises(ServiceError, match=r"1 conflict\(s\); nothing was written \(--update"):
        service.import_bundle(fake, ws, text)
    assert fake.writes == []
    assert service.import_bundle(fake, ws, text, update=True).written == 1


def test_a_bad_file_names_its_source(fake: FakeTransport, ws: Workspace) -> None:
    with pytest.raises(ServiceError, match=r"^https://hub/x\.uac\.yaml:\n"):
        service.import_bundle(fake, ws, "tasks: [not, a, map]\n", source="https://hub/x.uac.yaml")


def test_export_of_many_needs_yes_or_a_chooser(ws: Workspace) -> None:
    fake = FakeTransport(objects={"tasks": [_unix(f"T{i}", str(i)) for i in range(20)]})
    with pytest.raises(ServiceError, match="20 objects match 'T\\*'; add -y"):
        service.export_bundle(fake, ws, ["T*"])
    picked: list[int] = []

    def choose(found: list[Any], patterns: list[str], say: Any) -> list[Any]:
        picked.append(len(found))
        return found[:2]

    result = service.export_bundle(fake, ws, ["T*"], choose=choose)
    assert picked == [20] and result.text.count("type: Unix") == 2
    assert service.export_bundle(fake, ws, ["T*"], yes=True).text.count("type: Unix") == 20


def test_export_list_only_lists_and_reads_nothing(fake: FakeTransport, ws: Workspace) -> None:
    result = service.export_bundle(fake, ws, ["T*"], list_only=True)
    assert result.text == "" and len(result.listed) == 1 and result.listed[0].endswith(" T")


def test_apply_needs_yes_or_a_confirm(fake: FakeTransport, ws: Workspace) -> None:
    text = "steps:\n  - update task: T\n    set: {command: ls -l}\n"
    with pytest.raises(ServiceError, match="1 change\\(s\\) to 1 object\\(s\\); add -y"):
        service.apply_changes(fake, ws, text)
    with pytest.raises(ServiceError, match="not run; nothing was written"):
        service.apply_changes(fake, ws, text, confirm=lambda summary: False)
    assert fake.writes == []
    out = Lines()
    result = service.apply_changes(fake, ws, text, yes=True, out=out)
    assert result.done and result.changes == 1 and "done: 1 change(s) to 1 object(s)" in out.text
    assert fake.objects["tasks"][0]["command"] == "ls -l"


def test_apply_dry_run_shows_the_plan(fake: FakeTransport, ws: Workspace) -> None:
    out = Lines()
    text = "steps:\n  - delete task: T\n"
    result = service.apply_changes(fake, ws, text, dry_run=True, out=out)
    assert not result.done and result.changes == 1
    assert "[1] delete    tasks/T" in out.lines and fake.writes == []


def test_apply_backup_is_written_before_the_change(
    fake: FakeTransport, ws: Workspace, tmp_path: Path
) -> None:
    backup = tmp_path / "before.uac.yaml"
    text = "steps:\n  - update task: T\n    set: {command: ls -l}\n"
    service.apply_changes(fake, ws, text, yes=True, backup=str(backup))
    assert "command: ls\n" in backup.read_text()
    with pytest.raises(ServiceError, match="exists; give a new file name"):
        service.apply_changes(fake, ws, text, yes=True, backup=str(backup))


def test_a_refused_list_is_not_no_match(ws: Workspace) -> None:
    """Every list refused (a wrong token): say the controller's reason."""
    from uac_compose.transport import TransportError

    class Refusing(FakeTransport):
        def list_raw(self, group: str) -> list[dict[str, Any]]:
            raise TransportError(f"{group} list: HTTP 401: Unauthorized")

    with pytest.raises(ServiceError, match="nothing could be listed: .*HTTP 401: Unauthorized"):
        service.export_bundle(Refusing(), ws, ["T*"])
