"""Composer Hub: install a bundle by name and version, publish one with a token.

The hub (https://composer.husey.in by default) keeps bundles like PyPI keeps
packages. A published version never changes, and every download carries its
SHA-256, which is checked here before the text is used.

- `hub:NAME`, `hub:NAME==1.0`, `hub:NAME@1.0`, `hub:NAME>=1.0,<2`: pip-style.
- An `https://` URL works too; its `X-Checksum-SHA256` header is checked.
- `--sha256` pins the file, like pip's `--require-hashes`.

The token comes from `UAC_COMPOSE_HUB_TOKEN`; when it is not set, from the file
`~/.config/uac-compose/hub-token`. Never from the command line (it would stay in the shell history).
Standard library only: no new dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from uac_compose import __version__
from uac_compose.validate import SECRETS
from uac_compose.yamlio import BundleError, load, read_bundle

DEFAULT_URL = "https://composer.husey.in"
TOKEN_FILE = Path("~/.config/uac-compose/hub-token")
PREFIX = "hub:"
TIMEOUT = 30
MAX_BYTES = 5 * 1024 * 1024
KIND = "uac"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HubError(Exception):
    """A hub call failed, or a file did not match its checksum."""


@dataclass(frozen=True)
class Fetched:
    text: str
    sha256: str
    url: str
    name: str = ""
    version: str = ""


def hub_url() -> str:
    return os.environ.get("UAC_COMPOSE_HUB_URL", DEFAULT_URL).rstrip("/")


def token() -> str:
    value = os.environ.get("UAC_COMPOSE_HUB_TOKEN", "").strip()
    if not value:
        path = TOKEN_FILE.expanduser()
        if path.is_file():
            value = path.read_text().strip()
    if not value:
        raise HubError(
            f"no hub token: create one on {hub_url()}/tokens, then save it in "
            f"{TOKEN_FILE} (chmod 600) or set UAC_COMPOSE_HUB_TOKEN"
        )
    return value


def is_reference(text: str) -> bool:
    """A hub reference or an http(s) URL, rather than a local file."""
    return text.startswith(PREFIX) or text.startswith(("https://", "http://"))


def _request(
    url: str,
    *,
    data: bytes | None = None,
    method: str = "GET",
    auth: str | None = None,
    accept: str = "application/json",
) -> tuple[bytes, dict[str, str]]:
    if urlsplit(url).scheme not in ("https", "http"):
        raise HubError(f"{url}: only http(s) URLs")
    headers = {"Accept": accept, "User-Agent": f"uac-compose/{__version__}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - http(s) only, checked above
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            if urlsplit(response.geturl()).scheme not in ("https", "http"):
                raise HubError(f"{url}: redirected away from http(s)")
            body: bytes = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise HubError(f"{url}: larger than {MAX_BYTES:,} bytes")
            return body, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        raise HubError(f"{url}: HTTP {error.code}: {_reason(error)}") from error
    except urllib.error.URLError as error:
        raise HubError(f"{url}: {error.reason}") from error
    except TimeoutError as error:
        raise HubError(f"{url}: no answer in {TIMEOUT} s") from error


def _reason(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read(65536) or b"{}")
    except ValueError:
        return str(error.reason)
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return str(error.reason)


def check(data: bytes, expected: str | None, what: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if expected and digest != expected.strip().lower():
        raise HubError(
            f"{what}: SHA-256 {digest} does not match {expected.strip().lower()}; "
            "the file is refused (nothing was read)"
        )
    return digest


def check_pin(sha256: str | None) -> str | None:
    if sha256 is None:
        return None
    pin = sha256.strip().lower()
    if not SHA256.match(pin):
        raise HubError(f"--sha256 {sha256!r}: expected 64 hexadecimal characters")
    return pin


def fetch(reference: str, sha256: str | None = None, *, url: str | None = None) -> Fetched:
    """The text of `hub:NAME[spec]` or of a URL, its checksum checked.

    `url`: the hub to ask, when not `hub_url()` (a Universal Extension's field).
    """
    pin = check_pin(sha256)
    if reference.startswith(PREFIX):
        requirement = reference[len(PREFIX) :].strip()
        base = (url or hub_url()).rstrip("/")
        body, _ = _request(f"{base}/api/v1/resolve/{quote(requirement, safe='=@')}")
        info = json.loads(body)
        if info.get("kind", KIND) != KIND:
            raise HubError(f"{reference} is a {info.get('kind')} bundle, not a uac one")
        data, _ = _request(str(info["url"]), accept="text/plain")
        check(data, str(info["sha256"]), f"{info['name']}=={info['version']}")
        digest = check(data, pin, f"{info['name']}=={info['version']}")
        return Fetched(_text(data), digest, str(info["url"]), info["name"], info["version"])
    data, headers = _request(reference, accept="text/plain, application/yaml, */*")
    if headers.get("x-kind", KIND) != KIND:
        raise HubError(f"{reference} is a {headers['x-kind']} bundle, not a uac one")
    check(data, headers.get("x-checksum-sha256"), reference)
    digest = check(data, pin, reference)
    return Fetched(
        _text(data), digest, reference, headers.get("x-package", ""), headers.get("x-version", "")
    )


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HubError("the file is not UTF-8 text") from error


def split_requirement(requirement: str) -> tuple[str, str]:
    """`name==1.0` or `name@1.0` -> (name, version): a publish names one version."""
    match = re.match(
        r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==|@)\s*([^\s,;<>=!~]+)\s*$", requirement
    )
    if not match:
        raise HubError(f"{requirement!r}: give NAME==VERSION, e.g. nightly-transfer==1.0")
    return match.group(1), match.group(2)


def secrets_in(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if key in SECRETS and value not in (None, ""):
                found.append(where)
            found.extend(secrets_in(value, where))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(secrets_in(item, f"{path}[{i}]"))
    return found


def check_local(text: str, source: str) -> None:
    """What the hub refuses, refused here first: an unreadable file or a secret."""
    try:
        read_bundle(text)
    except BundleError as error:
        raise HubError(f"{source}: {error}") from error
    found = secrets_in(load(text))
    if found:
        raise HubError(f"{source}: {', '.join(found[:5])}: a secret never belongs in a bundle")


def publish(
    text: str,
    requirement: str,
    *,
    description: str | None = None,
    tags: list[str] | None = None,
    requirements: list[str] | None = None,
    draft: bool = False,
    source: str = "bundle",
    url: str | None = None,
    auth: str | None = None,
) -> dict[str, Any]:
    """Upload a version; published unless `draft`. Returns the hub's answer.

    `requirements`: the owner's list for the package page (None keeps it); the
    hub adds the extensions the file uses by itself.

    `url` and `auth`: the hub and the token, when not from the environment.
    """
    name, version = split_requirement(requirement)
    check_local(text, source)
    payload: dict[str, Any] = {"version": version, "content": text, "publish": not draft}
    if description is not None:
        payload["description"] = description
    if tags:
        payload["tags"] = tags
    if requirements is not None:
        payload["requirements"] = requirements
    body, _ = _request(
        f"{(url or hub_url()).rstrip('/')}/api/v1/packages/{quote(name, safe='')}/versions",
        data=json.dumps(payload).encode(),
        method="POST",
        auth=auth or token(),
    )
    answer: dict[str, Any] = json.loads(body)
    return answer
