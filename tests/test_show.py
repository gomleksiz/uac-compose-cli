"""`uac-compose show`, and the stale extension map it was written to explain."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from uac_compose.core.extensions import ExtensionMap, Slot
from uac_compose.freeze import frozen_map
from uac_compose.learn import Workspace, current_extensions
from uac_compose.show import describe, lines
from uac_compose.transport import TEMPLATE_GROUP, FakeTransport

AWS = {
    "name": "CS AWS S3",
    "fields": [
        {"name": "action", "fieldMapping": "Choice Field 3"},
        {"name": "sourcefile", "fieldMapping": "Text Field 1"},
        {"name": "loglevel", "fieldMapping": "Choice Field 7"},
    ],
}


def _map() -> ExtensionMap:
    return ExtensionMap(
        {"CS AWS S3": {"action": Slot("choiceField3", "Action", ("upload-file", "list-objects"))}}
    )


def test_show_reads_the_profile_map() -> None:
    described = describe("CS AWS S3", _map())
    assert described.found and described.source == "the profile's extension map"
    assert described.parameters["action"].slot == "choiceField3"
    out = "\n".join(lines(described))
    assert "CS AWS S3: 1 parameter(s)" in out
    assert "upload-file, list-objects" in out


def test_show_asks_the_controller_when_the_map_has_not_heard_of_it() -> None:
    """The case that produced 17 error lines: installed since the last learn."""
    fake = FakeTransport(raw={TEMPLATE_GROUP: [AWS]})
    described = describe("CS AWS S3", ExtensionMap({}), fake)
    assert described.found and described.source == "the controller"
    assert set(described.parameters) == {"action", "sourcefile", "loglevel"}


def test_show_suggests_a_near_name() -> None:
    described = describe("CS AWS S4", _map(), FakeTransport())
    assert not described.found
    out = "\n".join(lines(described))
    assert "no Universal Extension named 'CS AWS S4'" in out
    assert "'CS AWS S3'" in out


def test_slots_sort_by_number_not_by_text() -> None:
    ext = ExtensionMap({"T": {f"p{i}": Slot(f"textField{i}") for i in (1, 2, 12)}})
    assert list(describe("T", ext).parameters) == ["p1", "p2", "p12"]


def test_frozen_map_reads_what_a_zip_declares(tmp_path: Path) -> None:
    """An extension this run installs is not on the target yet, so read the zip."""
    with zipfile.ZipFile(tmp_path / "CS AWS S3.zip", "w") as zf:
        zf.writestr("template.json", json.dumps(AWS))
    (tmp_path / "broken.zip").write_text("not a zip")  # skipped, never fatal
    assert set(frozen_map(tmp_path).for_template("CS AWS S3")) == {
        "action",
        "sourcefile",
        "loglevel",
    }


def test_the_map_is_re_read_from_the_controller(tmp_path: Path) -> None:
    """Measured live: `import --extensions` installed CS AWS S3 and left the map behind.

    The next import then refused all 17 parameters of a task that used it, and
    no line said the map was out of date.
    """
    ws = Workspace(tmp_path)
    ws.cache.mkdir(parents=True)
    ws.cache_file("tasks").write_text("[]")
    ws.source.write_text(json.dumps({"release": "8.0.2.0"}))
    stale = ws.templates("8.0.2.0")
    stale.mkdir(parents=True)
    ExtensionMap({}).save(stale / "_extensions.json")
    assert ws.template_set().extensions.for_template("CS AWS S3") == {}

    fake: Any = FakeTransport(raw={TEMPLATE_GROUP: [AWS]})
    assert "action" in current_extensions(ws, fake).for_template("CS AWS S3")
    # and it is written back, so the next run starts from the truth
    assert "action" in ws.template_set().extensions.for_template("CS AWS S3")
