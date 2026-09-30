"""Milestone 8: `uac-compose freeze` and `import --extensions`."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import freeze as freeze_module
from uac_compose.cli import cli
from uac_compose.freeze import (
    LOCK_NAME,
    MANIFEST_NAME,
    Busy,
    Frozen,
    Manifest,
    definition_checksum,
    differences,
    fingerprint,
    freeze,
    install,
    plan_sync,
    row_for,
    safe_stem,
    scripts_checksum,
    template_names,
)
from uac_compose.transport import TEMPLATE_GROUP, FakeTransport, TransportError, UacCli

S3 = {
    "name": "AWS-S3",
    "sysId": "abc",
    "extension": "AwsS3",
    "exportReleaseLevel": "8.0.2.0",
    "minReleaseLevel": "7.0.0.0",
    "fields": [{"name": "bucket"}],
}
GPG = {"name": "GnuPG", "sysId": "def", "fields": [{"name": "key"}]}


def _controller(*templates: dict[str, Any]) -> FakeTransport:
    return FakeTransport(raw={TEMPLATE_GROUP: [dict(t) for t in templates]})


def _quiet(_: str) -> None:
    return None


# --- checksum ------------------------------------------------------------------


def test_checksum_stable_across_exports() -> None:
    """Bookkeeping the reading controller stamps on is not content."""
    other = {
        **S3,
        "sysId": "zzz",
        "exportReleaseLevel": "8.1.0.0",
        "version": 9,
        "iconFilename": "s3.png",
        "minReleaseLevel": "7.5.0.0",
    }
    assert fingerprint(S3) == fingerprint(other)
    assert fingerprint({**S3, "fields": []}) != fingerprint(S3)


def test_safe_stem_keeps_distinct_names_distinct() -> None:
    assert safe_stem("AWS-S3") == "AWS-S3"
    assert safe_stem("a/b") != safe_stem("a:b")
    assert "/" not in safe_stem("a/b")


# --- the script checksum: what "the same extension" means ---------------------

SCRIPTED = {
    "name": "GnuPG",
    "sysId": "b",
    "extension": "GnuPg",
    "exportReleaseLevel": "8.0.2.0",
    "label": "GnuPG",
    "fields": [{"name": "key", "label": "Key"}],
    "scriptUnix": "IyEvYmluL3No",
    "scriptWindows": "QGVjaG8gb2Zm",
}


def test_scripts_checksum_is_the_code_alone() -> None:
    other_code = {**SCRIPTED, "scriptUnix": "IyEvYmluL2Jhc2g="}
    other_label = {**SCRIPTED, "fields": [{"name": "key", "label": "Private key"}]}
    assert scripts_checksum(SCRIPTED) != scripts_checksum(other_code)
    assert scripts_checksum(SCRIPTED) == scripts_checksum(other_label)
    assert definition_checksum(SCRIPTED) == definition_checksum(other_code)
    assert definition_checksum(SCRIPTED) != definition_checksum(other_label)


def test_a_compiled_extension_has_no_code_checksum() -> None:
    """166 of one controller's 271 carry no script; one shared hash would say nothing."""
    assert scripts_checksum({"name": "AWS-Lambda"}) is None
    assert scripts_checksum({"name": "x", "scriptUnix": ""}) is None
    row = row_for("AWS-Lambda.zip", {"name": "AWS-Lambda", "fields": []})
    assert row.scripts_checksum is None
    assert differences({"name": "AWS-Lambda", "fields": []}, row) == []
    assert differences({"name": "AWS-Lambda", "fields": [], "scriptUnix": "eA=="}, row) == ["code"]


def test_differences_names_which_half_moved() -> None:
    row = row_for("GnuPG.zip", SCRIPTED)
    assert differences(SCRIPTED, row) == []
    assert differences({**SCRIPTED, "scriptUnix": "b3RoZXI="}, row) == ["code"]
    assert differences({**SCRIPTED, "label": "Other"}, row) == ["definition"]
    both = {**SCRIPTED, "scriptUnix": "b3RoZXI=", "label": "Other"}
    assert differences(both, row) == ["code", "definition"]
    # bookkeeping the reading controller stamps on is neither
    assert differences({**SCRIPTED, "sysId": "zz", "version": 9}, row) == []


def test_a_manifest_from_before_script_checksums_still_compares() -> None:
    old = Frozen(file="GnuPG.zip", checksum=fingerprint(SCRIPTED))
    assert differences(SCRIPTED, old) == []
    assert differences({**SCRIPTED, "scriptUnix": "b3RoZXI="}, old) == ["content"]


def _exports(fake: FakeTransport) -> list[str]:
    return [w[1]["name"] for w in fake.writes if w[0] == "export_template"]


def test_unchanged_extension_is_not_downloaded_again(tmp_path: Path) -> None:
    fake = _controller(SCRIPTED)
    freeze(["GnuPG"], tmp_path, fake, "p", _quiet)
    assert _exports(fake) == ["GnuPG"]
    step = freeze(["GnuPG"], tmp_path, fake, "p", _quiet)[0]
    assert step.skipped and _exports(fake) == ["GnuPG"]  # no second download


def test_changed_code_is_downloaded_again_and_says_so(tmp_path: Path) -> None:
    fake = _controller(SCRIPTED)
    freeze(["GnuPG"], tmp_path, fake, "p", _quiet)
    fake.raw[TEMPLATE_GROUP] = [{**SCRIPTED, "scriptUnix": "IyEvYmluL2Jhc2g="}]
    lines: list[str] = []
    step = freeze(["GnuPG"], tmp_path, fake, "p", lines.append)[0]
    assert not step.skipped and step.changed == ("code",)
    assert "(code changed)" in "\n".join(lines)
    assert _exports(fake) == ["GnuPG", "GnuPG"]


def test_changed_definition_is_downloaded_again_and_says_so(tmp_path: Path) -> None:
    fake = _controller(SCRIPTED)
    freeze(["GnuPG"], tmp_path, fake, "p", _quiet)
    fake.raw[TEMPLATE_GROUP] = [{**SCRIPTED, "fields": [{"name": "key", "label": "Private"}]}]
    lines: list[str] = []
    step = freeze(["GnuPG"], tmp_path, fake, "p", lines.append)[0]
    assert step.changed == ("definition",)
    assert "(definition changed)" in "\n".join(lines)


def test_bookkeeping_alone_is_not_a_download(tmp_path: Path) -> None:
    """A re-export on the source bumps sysId and the release; the extension is the same."""
    fake = _controller(SCRIPTED)
    freeze(["GnuPG"], tmp_path, fake, "p", _quiet)
    fake.raw[TEMPLATE_GROUP] = [{**SCRIPTED, "sysId": "new", "exportReleaseLevel": "8.1.0.0"}]
    assert freeze(["GnuPG"], tmp_path, fake, "p", _quiet)[0].skipped
    assert _exports(fake) == ["GnuPG"]


def test_manifest_keeps_the_metadata(tmp_path: Path) -> None:
    freeze(["GnuPG"], tmp_path, _controller(SCRIPTED), "p", _quiet)
    row = Manifest.load(tmp_path / MANIFEST_NAME).templates["GnuPG"]
    assert row.metadata == {
        "extension": "GnuPg",
        "exportReleaseLevel": "8.0.2.0",
        "label": "GnuPG",
    }
    assert row.scripts_checksum == scripts_checksum(SCRIPTED)
    assert row.definition_checksum == definition_checksum(SCRIPTED)


def test_sync_says_which_half_differs(tmp_path: Path) -> None:
    freeze(["GnuPG"], tmp_path, _controller(SCRIPTED), "source", _quiet)
    theirs = _controller({**SCRIPTED, "scriptUnix": "b3RoZXI="})
    (step,) = plan_sync(["GnuPG"], tmp_path, theirs)
    assert step.action == "warn" and "its code differs" in step.message
    (step,) = plan_sync(["GnuPG"], tmp_path, theirs, update=True)
    assert step.action == "update" and step.message == "code differs"


# --- which extensions a file needs ---------------------------------------------


def test_email_template_is_not_an_extension() -> None:
    objects = [
        {"type": "taskEmail", "template": "Ops Mail"},
        {"type": "taskUniversal", "template": "AWS-S3"},
        {"type": "taskUniversalMonitor", "template": "GnuPG"},
        {"type": "taskUniversal", "template": "${chosen}"},
        {"type": "taskUniversal", "template": "AWS-S3"},
    ]
    assert template_names(objects) == ["AWS-S3", "GnuPG"]


def test_the_manifest_is_written_as_each_export_lands(tmp_path: Path) -> None:
    """An interrupted run must keep what it already downloaded.

    The last export waits until the manifest actually holds a row. Saving only
    at the end -- which is why a long run that was stopped fetched everything
    again -- never gets there, so this times out on the old behaviour.
    """
    manifest = tmp_path / MANIFEST_NAME

    def rows() -> int:
        return len(json.loads(manifest.read_text())["templates"]) if manifest.exists() else 0

    class Waiting(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            deadline = time.monotonic() + 10
            while name == "Third" and rows() == 0:
                assert time.monotonic() < deadline, "nothing was recorded until the very end"
                time.sleep(0.02)
            super().export_template(name, path)

    fake = Waiting(raw={TEMPLATE_GROUP: [dict(S3), dict(GPG), {"name": "Third", "sysId": "c"}]})
    steps = freeze(["AWS-S3", "GnuPG", "Third"], tmp_path, fake, "p", _quiet, parallel=1)
    assert not any(s.error for s in steps)
    assert rows() == 3


def test_an_interrupted_run_resumes(tmp_path: Path) -> None:
    """Ctrl-C part way through keeps what was already downloaded."""

    class Interrupted(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            if name == "Third":
                raise KeyboardInterrupt
            super().export_template(name, path)

    rows = [dict(S3), dict(GPG), {"name": "Third", "sysId": "c"}]
    stopped = Interrupted(raw={TEMPLATE_GROUP: rows})
    with pytest.raises(KeyboardInterrupt):
        freeze(["AWS-S3", "GnuPG", "Third"], tmp_path, stopped, "p", _quiet, parallel=1)
    assert sorted(Manifest.load(tmp_path / MANIFEST_NAME).templates) == ["AWS-S3", "GnuPG"]
    assert not (tmp_path / LOCK_NAME).exists()  # the lock is released even then

    fake = FakeTransport(raw={TEMPLATE_GROUP: rows})
    steps = freeze(["AWS-S3", "GnuPG", "Third"], tmp_path, fake, "p", _quiet)
    assert [s.skipped for s in steps] == [True, True, False]
    assert _exports(fake) == ["Third"]  # only the one that never finished


def test_a_failed_extension_is_retried_next_run(tmp_path: Path) -> None:
    """152 of one live run's 271 failed on connection errors; the next run
    must fetch those and skip the ones that already worked."""
    rows = [dict(S3), dict(GPG)]
    flaky = _controller(*rows)
    calls: list[str] = []

    def export(name: str, path: Path) -> None:
        calls.append(name)
        if name == "GnuPG" and calls.count("GnuPG") == 1:
            raise TransportError("ConnectionError: the controller stopped answering")
        FakeTransport.export_template(flaky, name, path)

    flaky.export_template = export  # type: ignore[method-assign]
    steps = freeze(["AWS-S3", "GnuPG"], tmp_path, flaky, "p", _quiet)
    assert [bool(s.error) for s in steps] == [False, True]

    steps = freeze(["AWS-S3", "GnuPG"], tmp_path, flaky, "p", _quiet)
    assert [s.skipped for s in steps] == [True, False]
    assert not any(s.error for s in steps)
    assert calls == ["AWS-S3", "GnuPG", "GnuPG"]  # the good one was not fetched twice


def test_two_freezes_into_one_folder_are_refused(tmp_path: Path) -> None:
    """Two runs each hold the manifest in memory; the later save would drop the
    earlier one's rows -- measured live as 85 zips on disk and 35 rows left."""
    started = threading.Event()
    release = threading.Event()

    class Slow(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            started.set()
            assert release.wait(timeout=10)
            super().export_template(name, path)

    first = Slow(raw={TEMPLATE_GROUP: [dict(S3)]})
    result: list[object] = []

    def run_first() -> None:
        try:
            freeze(["AWS-S3"], tmp_path, first, "p", _quiet)
            result.append("done")
        except BaseException as error:  # noqa: BLE001 - reported through `result`
            result.append(error)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(timeout=10)
    with pytest.raises(Busy, match="another uac-compose freeze is writing"):
        freeze(["GnuPG"], tmp_path, _controller(GPG), "p", _quiet)
    release.set()
    worker.join(timeout=10)
    assert result == ["done"]
    # the lock is gone once the first run ends, so the next one may proceed
    assert not (tmp_path / LOCK_NAME).exists()
    assert freeze(["GnuPG"], tmp_path, _controller(GPG), "p", _quiet)[0].error is None


def test_a_lock_left_by_a_killed_run_is_taken_over(tmp_path: Path) -> None:
    (tmp_path / LOCK_NAME).write_text("999999\n")  # a pid that is not running
    steps = freeze(["AWS-S3"], tmp_path, _controller(S3), "p", _quiet)
    assert steps[0].error is None and not (tmp_path / LOCK_NAME).exists()


def test_an_unreadable_lock_does_not_block_for_ever(tmp_path: Path) -> None:
    (tmp_path / LOCK_NAME).write_text("not a pid")
    assert freeze(["AWS-S3"], tmp_path, _controller(S3), "p", _quiet)[0].error is None


def test_one_list_call_decides_every_extension(tmp_path: Path) -> None:
    """`universal-template list` answers with the same content `get` does."""
    calls: list[str] = []

    class Counting(FakeTransport):
        def list_raw(self, group: str) -> list[dict[str, Any]]:
            calls.append("list")
            return super().list_raw(group)

        def get_template(self, name: str) -> dict[str, Any] | None:
            calls.append(f"get {name}")
            return super().get_template(name)

    fake = Counting(raw={TEMPLATE_GROUP: [dict(S3), dict(GPG)]})
    freeze(["AWS-S3", "GnuPG"], tmp_path, fake, "p", _quiet)
    calls.clear()
    steps = freeze(["AWS-S3", "GnuPG"], tmp_path, fake, "p", _quiet)
    assert all(s.skipped for s in steps)
    assert calls == ["list"]  # not one `get` per extension


def test_refresh_does_not_bother_listing(tmp_path: Path) -> None:
    fake = _controller(S3)
    freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    steps = freeze(["AWS-S3"], tmp_path, fake, "p", _quiet, refresh=True)
    assert not steps[0].skipped


# --- freeze ---------------------------------------------------------------------


def test_freeze_writes_zips_and_manifest(tmp_path: Path) -> None:
    steps = freeze(["AWS-S3", "GnuPG"], tmp_path, _controller(S3, GPG), "demo", _quiet)
    assert [s.name for s in steps] == ["AWS-S3", "GnuPG"] and not any(s.error for s in steps)
    assert (tmp_path / "AWS-S3.zip").exists()
    manifest = Manifest.load(tmp_path / MANIFEST_NAME)
    assert manifest.profile == "demo"
    assert manifest.templates["AWS-S3"].checksum == fingerprint(S3)
    assert manifest.templates["AWS-S3"].extension == "AwsS3"
    assert manifest.templates["AWS-S3"].min_release == "7.0.0.0"
    assert manifest.templates["AWS-S3"].archive_checksum is None  # a scripted extension


def test_freeze_one_never_raises(tmp_path: Path) -> None:
    steps = freeze(["Nope"], tmp_path, _controller(S3), "demo", _quiet)
    assert steps[0].error and steps[0].row is None
    assert Manifest.load(tmp_path / MANIFEST_NAME).templates == {}


def test_a_stuck_export_costs_one_extension_not_the_run(tmp_path: Path) -> None:
    """Measured live: a freeze of 271 ended at 269 with a traceback.

    `subprocess.TimeoutExpired` is not an OSError, so it travelled past
    `freeze_one`'s "never raises" promise and out of the worker.
    """

    class Stuck(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            if name == "Cloud Data Monitor":
                raise subprocess.TimeoutExpired(["uac", "export"], 900)
            super().export_template(name, path)

    rows = [dict(S3), {"name": "Cloud Data Monitor", "sysId": "c"}, dict(GPG)]
    fake = Stuck(raw={TEMPLATE_GROUP: rows})
    steps = freeze(["AWS-S3", "Cloud Data Monitor", "GnuPG"], tmp_path, fake, "p", _quiet)
    assert [bool(s.error) for s in steps] == [False, True, False]
    assert sorted(Manifest.load(tmp_path / MANIFEST_NAME).templates) == ["AWS-S3", "GnuPG"]


def test_a_timeout_is_a_transport_error_not_a_traceback() -> None:
    def stuck(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 900)

    with pytest.raises(TransportError, match="no answer after 900s"):
        UacCli("p", stuck).get_template("AWS-S3")


def test_a_missing_uac_binary_is_a_transport_error() -> None:
    def gone(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", "uac")

    with pytest.raises(TransportError, match="No such file"):
        UacCli("p", gone).get_template("AWS-S3")


def test_one_failure_does_not_stop_the_batch(tmp_path: Path) -> None:
    steps = freeze(["AWS-S3", "Nope", "GnuPG"], tmp_path, _controller(S3, GPG), "p", _quiet)
    assert [bool(s.error) for s in steps] == [False, True, False]
    assert sorted(Manifest.load(tmp_path / MANIFEST_NAME).templates) == ["AWS-S3", "GnuPG"]


def test_unchanged_is_skipped_and_refresh_re_exports(tmp_path: Path) -> None:
    fake = _controller(S3)
    freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    exported = len([w for w in fake.writes if w[0] == "export_template"])
    again = freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    assert again[0].skipped
    assert len([w for w in fake.writes if w[0] == "export_template"]) == exported
    refreshed = freeze(["AWS-S3"], tmp_path, fake, "p", _quiet, refresh=True)
    assert not refreshed[0].skipped


def test_changed_extension_is_re_exported(tmp_path: Path) -> None:
    fake = _controller(S3)
    freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    fake.raw[TEMPLATE_GROUP] = [{**S3, "fields": [{"name": "bucket"}, {"name": "region"}]}]
    steps = freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    assert not steps[0].skipped
    assert steps[0].row is not None
    assert steps[0].row.checksum != fingerprint(S3)


def test_progress_says_where_what_and_how_much(tmp_path: Path) -> None:
    lines: list[str] = []
    freeze(["AWS-S3", "GnuPG"], tmp_path, _controller(S3, GPG), "p", lines.append)
    text = "\n".join(lines)
    assert "\r" not in text  # plain lines: a redrawn bar reads as a hang in a log
    assert lines[0] == f"profile p: 2 extension(s) into {tmp_path}"
    assert lines[1] == "   AWS-S3, GnuPG"
    assert "] exported  AWS-S3" in text
    assert "AWS-S3.zip" in text and "B  sha256:" in text
    assert "extension AwsS3; needs controller 7.0.0.0 or later" in text
    assert "2 exported, 0 unchanged, 0 failed" in text
    assert f"manifest {tmp_path / MANIFEST_NAME}" in text


def test_progress_arrives_while_the_run_is_going(tmp_path: Path) -> None:
    """Each extension prints when IT finishes, not after the whole batch has.

    The last export blocks until a line has actually been printed. Collecting
    every result first and printing afterwards -- which is what this used to do
    -- cannot satisfy that, so this test times out on the old behaviour.
    """
    printed = threading.Event()

    class Blocking(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            if name == "Last" and not printed.wait(timeout=10):
                raise AssertionError("nothing was printed while exports were still running")
            super().export_template(name, path)

    def echo(line: str) -> None:
        if line.startswith("["):
            printed.set()

    names = ["AWS-S3", "GnuPG", "Last"]
    fake = Blocking(raw={TEMPLATE_GROUP: [dict(S3), dict(GPG), {"name": "Last", "sysId": "c"}]})
    steps = freeze(names, tmp_path, fake, "p", echo, parallel=1)
    assert [s.name for s in steps] == names  # reported in file order, printed as they finish
    assert not any(s.error for s in steps)


def test_many_extensions_are_counted_not_all_listed(tmp_path: Path) -> None:
    names = [f"Ext-{i:02d}" for i in range(20)]
    fake = _controller(*({"name": n, "sysId": n} for n in names))
    lines: list[str] = []
    freeze(names, tmp_path, fake, "p", lines.append)
    assert lines[1].endswith(", and 8 more")
    assert "20 extension(s)" in lines[0]


def test_failed_extension_says_why(tmp_path: Path) -> None:
    lines: list[str] = []
    freeze(["Nope"], tmp_path, _controller(S3), "p", lines.append)
    assert any(line.startswith("[1/1] FAILED    Nope") for line in lines)
    assert any("no extension named 'Nope'" in line for line in lines)


def test_unchanged_is_named_as_unchanged(tmp_path: Path) -> None:
    fake = _controller(S3)
    freeze(["AWS-S3"], tmp_path, fake, "p", _quiet)
    lines: list[str] = []
    freeze(["AWS-S3"], tmp_path, fake, "p", lines.append)
    assert any(line.startswith("[1/1] unchanged AWS-S3") for line in lines)
    assert "0 exported, 1 unchanged, 0 failed" in "\n".join(lines)


def test_compiled_extension_records_archive_checksum(tmp_path: Path) -> None:
    class WithArchive(FakeTransport):
        def export_template(self, name: str, path: Path) -> None:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("template.json", json.dumps(S3))
                archive.writestr("extension_archive.zip", b"binary")

    steps = freeze(["AWS-S3"], tmp_path, WithArchive(), "p", _quiet)
    assert steps[0].row is not None and steps[0].row.archive_checksum is not None


# --- import --extensions --------------------------------------------------------


def _frozen(tmp_path: Path, *templates: dict[str, Any]) -> FakeTransport:
    source = _controller(*templates)
    freeze([t["name"] for t in templates], tmp_path, source, "source", _quiet)
    return source


def test_missing_extension_is_installed(tmp_path: Path) -> None:
    _frozen(tmp_path, S3)
    target = _controller()
    (step,) = plan_sync(["AWS-S3"], tmp_path, target)
    assert step.action == "install"
    assert target.get_template("AWS-S3") is None  # planning writes nothing
    install([step], tmp_path, target)
    assert fingerprint(target.get_template("AWS-S3") or {}) == fingerprint(S3)


def test_missing_with_no_frozen_copy_warns(tmp_path: Path) -> None:
    _frozen(tmp_path, S3)
    (step,) = plan_sync(["GnuPG"], tmp_path, _controller())
    assert step.action == "warn" and "no local copy frozen" in step.message


def test_same_extension_is_reused(tmp_path: Path) -> None:
    _frozen(tmp_path, S3)
    (step,) = plan_sync(["AWS-S3"], tmp_path, _controller(S3))
    assert step.action == "reuse"


def test_different_extension_warns_unless_update(tmp_path: Path) -> None:
    _frozen(tmp_path, S3)
    theirs = _controller({**S3, "fields": [{"name": "other"}]})
    (step,) = plan_sync(["AWS-S3"], tmp_path, theirs)
    assert step.action == "warn" and "--update-extensions" in step.message
    (step,) = plan_sync(["AWS-S3"], tmp_path, theirs, update=True)
    assert step.action == "update"
    install([step], tmp_path, theirs)
    assert fingerprint(theirs.get_template("AWS-S3") or {}) == fingerprint(S3)


# --- the commands -----------------------------------------------------------------

UNIVERSAL = "tasks:\n  T: {type: Universal, template: AWS-S3, agent: a}\n"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = _controller(S3, GPG)
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    assert freeze_module  # the commands import it lazily
    return controller


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_freeze_command_without_a_file_takes_every_extension(
    wired: FakeTransport, tmp_path: Path
) -> None:
    out = tmp_path / "ext"
    code, text = _run("freeze", "--out", str(out))
    assert code == 0 and "2 exported, 0 unchanged, 0 failed" in text
    assert sorted(Manifest.load(out / MANIFEST_NAME).templates) == ["AWS-S3", "GnuPG"]
    assert f"install them with: uac-compose import FILE --extensions {out}" in text


def test_freeze_command_with_a_file_takes_only_what_it_names(
    wired: FakeTransport, tmp_path: Path
) -> None:
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text(UNIVERSAL)
    out = tmp_path / "ext"
    code, text = _run("freeze", str(bundle), "--out", str(out))
    assert code == 0 and "1 exported, 0 unchanged, 0 failed" in text
    assert list(Manifest.load(out / MANIFEST_NAME).templates) == ["AWS-S3"]
    assert f"{bundle}: 1 object(s)" in text
    assert f"install them with: uac-compose import {bundle} --extensions {out}" in text


@pytest.mark.parametrize("flag", ["-d", "-dex", "--export-extensions", "--download-extensions"])
def test_export_can_freeze_what_it_exported(
    wired: FakeTransport, tmp_path: Path, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One command instead of `export` then `freeze FILE`."""
    monkeypatch.delenv("UAC_COMPOSE_PROFILE", raising=False)
    wired.objects["tasks"] = [
        {"type": "taskUniversal", "name": "T", "template": "AWS-S3", "agent": "a"}
    ]
    bundle = tmp_path / "b.uac.yaml"
    code, text = _run("export", "T", "-t", "task", "-o", str(bundle), flag)
    assert code == 0, text
    assert "template: AWS-S3" in bundle.read_text()
    target = tmp_path / "ws" / "default" / "extensions"
    assert list(Manifest.load(target / MANIFEST_NAME).templates) == ["AWS-S3"]
    assert f"install them with: uac-compose import {bundle} --extensions {target}" in text


def test_export_extensions_when_none_are_named(wired: FakeTransport, tmp_path: Path) -> None:
    wired.objects["tasks"] = [{"type": "taskUnix", "name": "U", "agent": "a", "command": "ls"}]
    code, text = _run("export", "U", "-o", str(tmp_path / "u.uac.yaml"), "-d")
    assert code == 0 and "name no Universal Extension; nothing to freeze" in text


def test_freeze_names_the_failures(wired: FakeTransport, tmp_path: Path) -> None:
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text("tasks:\n  T: {type: Universal, template: Nope, agent: a}\n")
    code, text = _run("freeze", str(bundle), "--out", str(tmp_path / "ext"))
    assert code == 1 and "could not be exported: Nope" in text


def test_freeze_command_says_when_a_file_needs_none(wired: FakeTransport, tmp_path: Path) -> None:
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text("tasks:\n  T: {type: Unix, command: ls}\n")
    code, text = _run("freeze", str(bundle), "--out", str(tmp_path / "ext"))
    assert code == 0 and "names no Universal Extension" in text


def test_import_extensions_installs_before_writing(wired: FakeTransport, tmp_path: Path) -> None:
    out = tmp_path / "ext"
    _run("freeze", "--out", str(out))
    wired.raw[TEMPLATE_GROUP] = []  # the target no longer has it
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text(UNIVERSAL)
    code, text = _run("import", str(bundle), "--extensions", str(out))
    assert code == 0, text
    assert f"-- extensions from {out}: AWS-S3" in text
    assert "install  extension AWS-S3" in text and "created  tasks/T" in text
    kinds = [w[0] for w in wired.writes]
    assert kinds.index("import_template") < kinds.index("create")


def test_import_extensions_writes_nothing_on_a_conflict(
    wired: FakeTransport, tmp_path: Path
) -> None:
    out = tmp_path / "ext"
    _run("freeze", "--out", str(out))
    wired.raw[TEMPLATE_GROUP] = []
    wired.objects["tasks"] = [
        {"type": "taskUniversal", "name": "T", "template": "AWS-S3", "agent": "b"}
    ]
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text(UNIVERSAL)
    wired.writes.clear()  # the freeze above is not what this is measuring
    code, text = _run("import", str(bundle), "--extensions", str(out))
    assert code == 1 and "conflict tasks/T" in text
    assert [w[0] for w in wired.writes] == []  # not even the extension


def test_import_extensions_needs_a_manifest(wired: FakeTransport, tmp_path: Path) -> None:
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text(UNIVERSAL)
    code, text = _run("import", str(bundle), "--extensions", str(tmp_path / "empty"))
    assert code == 1 and "run `uac-compose freeze` first" in text


def test_update_extensions_needs_extensions(wired: FakeTransport, tmp_path: Path) -> None:
    bundle = tmp_path / "b.uac.yaml"
    bundle.write_text(UNIVERSAL)
    code, text = _run("import", str(bundle), "--update-extensions")
    assert code == 1 and "--update-extensions needs --extensions" in text


# --- live (opt-in) ------------------------------------------------------------------


@pytest.mark.live
def test_freeze_live(tmp_path: Path) -> None:
    import os

    from uac_compose.transport import UacCli

    profile = os.environ.get("UAC_COMPOSE_LIVE_PROFILE")
    name = os.environ.get("UAC_COMPOSE_LIVE_EXTENSION")
    if not (profile and name):
        pytest.skip("set UAC_COMPOSE_LIVE_PROFILE and UAC_COMPOSE_LIVE_EXTENSION")
    cli_transport = UacCli(profile)
    (step,) = freeze([name], tmp_path, cli_transport, profile, print)
    assert step.error is None and step.row is not None
    assert plan_sync([name], tmp_path, cli_transport)[0].action == "reuse"


def test_transport_export_of_an_unknown_extension_raises() -> None:
    with pytest.raises(TransportError):
        _controller(S3).export_template("Nope", Path("/tmp/none.zip"))
