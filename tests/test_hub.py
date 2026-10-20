"""Composer Hub: install by reference with the checksum checked, publish with a token."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from uac_compose import cli as cli_module
from uac_compose import hub
from uac_compose.cli import cli
from uac_compose.transport import FakeTransport

BUNDLE = "tasks:\n  T: {type: Unix, agent: linux-01, command: ls}\n"
SHA = hashlib.sha256(BUNDLE.encode()).hexdigest()


class FakeHub(BaseHTTPRequestHandler):
    content = BUNDLE
    kind = "uac"
    published: list[dict[str, Any]] = []
    tokens: list[str] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/api/v1/resolve/"):
            info = {"name": "t", "version": "1.0", "kind": self.kind, "sha256": SHA,
                    "url": f"{base}/d/t==1.0"}  # fmt: skip
            self._send(200, json.dumps(info).encode())
        elif self.path.startswith("/d/"):
            self._send(200, self.content.encode(),
                       {"X-Checksum-SHA256": SHA, "X-Package": "t", "X-Version": "1.0",
                        "X-Kind": self.kind})  # fmt: skip
        else:
            self._send(404, b'{"error": "no such thing"}')

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHub.tokens.append(self.headers.get("Authorization", ""))
        if body["version"] == "1.0":
            self._send(
                409, b'{"error": "t==1.0 was published; a version number is never used twice"}'
            )
            return
        FakeHub.published.append(body)
        answer = {"name": "t", "version": body["version"], "status": "published", "sha256": SHA,
                  "page": "https://hub/p/t", "url": "https://hub/d/t==1.1"}  # fmt: skip
        self._send(201, json.dumps(answer).encode())


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    FakeHub.content, FakeHub.kind = BUNDLE, "uac"
    FakeHub.published, FakeHub.tokens = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    monkeypatch.setenv("UAC_COMPOSE_HUB_URL", url)
    monkeypatch.setenv("UAC_COMPOSE_HUB_TOKEN", "cmp_test")
    yield url
    httpd.shutdown()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTransport:
    controller = FakeTransport(objects={"tasks": [{"type": "taskUnix", "name": "T", "sysId": "1",
                                                   "agent": "linux-01", "command": "ls",
                                                   "exitCodes": "0"}]})  # fmt: skip
    monkeypatch.setattr("uac_compose.transport.UacCli", lambda profile: controller)
    monkeypatch.setenv("UAC_COMPOSE_WORKSPACE", str(tmp_path / "ws"))
    assert cli_module
    return controller


def test_fetch_checks_the_hubs_checksum(server: str) -> None:
    got = hub.fetch("hub:t==1.0")
    assert got.text == BUNDLE and got.sha256 == SHA and got.version == "1.0"
    FakeHub.content = BUNDLE + "# changed on the way\n"
    with pytest.raises(hub.HubError, match="does not match"):
        hub.fetch("hub:t==1.0")
    with pytest.raises(hub.HubError, match="does not match"):
        hub.fetch(f"{server}/d/t==1.0")


def test_a_pin_is_checked_too(server: str) -> None:
    assert hub.fetch("hub:t", SHA.upper()).sha256 == SHA
    with pytest.raises(hub.HubError, match="does not match"):
        hub.fetch("hub:t", "0" * 64)
    with pytest.raises(hub.HubError, match="64 hexadecimal"):
        hub.fetch("hub:t", "abc")


def test_another_kind_is_refused(server: str) -> None:
    FakeHub.kind = "udmg"
    with pytest.raises(hub.HubError, match="udmg bundle"):
        hub.fetch("hub:t")


def test_import_from_the_hub(server: str, fake: FakeTransport) -> None:
    result = CliRunner().invoke(cli, ["import", "-n", "hub:t==1.0", "--sha256", SHA])
    assert result.exit_code == 0, result.output
    assert (
        "reuse    tasks/T" in result.output and f"t==1.0: sha256 {SHA} (checked)" in result.output
    )
    bad = CliRunner().invoke(cli, ["import", "-n", "hub:t==1.0", "--sha256", "f" * 64])
    assert bad.exit_code == 1 and "does not match" in bad.output and fake.writes == []


def test_a_missing_file_says_how_to_name_a_hub_bundle(fake: FakeTransport) -> None:
    result = CliRunner().invoke(cli, ["import", "nope.uac.yaml"])
    assert result.exit_code == 1 and "hub:NAME==VERSION" in result.output


def test_publish(server: str, tmp_path: Path) -> None:
    f = tmp_path / "t.uac.yaml"
    f.write_text(BUNDLE)
    args = ["publish", str(f), "t==1.1", "-d", "Lists.", "--tag", "demo"]
    args += ["--require", "Agent linux-01", "--require", "Python 3"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "published: t==1.1" in result.output and f"--sha256 {SHA}" in result.output
    assert FakeHub.published[0] == {"version": "1.1", "content": BUNDLE, "publish": True,
                                    "description": "Lists.", "tags": ["demo"],
                                    "requirements": ["Agent linux-01", "Python 3"]}  # fmt: skip
    assert FakeHub.tokens == ["Bearer cmp_test"]
    again = CliRunner().invoke(cli, ["publish", str(f), "t==1.0"])
    assert again.exit_code == 1 and "never used twice" in again.output


def test_publish_refuses_a_secret_before_upload(server: str, tmp_path: Path) -> None:
    f = tmp_path / "c.uac.yaml"
    f.write_text("credentials:\n  c: {user: u, runtimePassword: hunter2}\n")
    result = CliRunner().invoke(cli, ["publish", str(f), "c==1.0"])
    assert result.exit_code == 1 and "secret" in result.output and FakeHub.published == []


@pytest.mark.parametrize("bad", ["t", "t>=1.0", "t==1.0,<2"])
def test_publish_names_one_version(bad: str) -> None:
    with pytest.raises(hub.HubError, match="NAME==VERSION"):
        hub.split_requirement(bad)


def test_the_environment_token_wins_over_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved = tmp_path / "hub-token"
    saved.write_text("cmp_from_file\n")
    monkeypatch.setattr(hub, "TOKEN_FILE", saved)
    monkeypatch.setenv("UAC_COMPOSE_HUB_TOKEN", "cmp_from_env")
    assert hub.token() == "cmp_from_env"
    monkeypatch.setenv("UAC_COMPOSE_HUB_TOKEN", " ")  # empty: the file is used
    assert hub.token() == "cmp_from_file"


def test_no_token_says_where_to_put_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UAC_COMPOSE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(hub, "TOKEN_FILE", tmp_path / "none")
    with pytest.raises(hub.HubError, match="no hub token"):
        hub.token()
