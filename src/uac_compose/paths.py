"""Where uac-compose reads its shipped data and writes its per-profile workspace.

Environment variables are read on every call, never captured at import: a
`.env` loaded after import must still reach them.
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


class ConfigError(Exception):
    """A configuration problem the CLI reports as one line, not a traceback."""


def data_root() -> Path:
    """The read-only seed data: `UAC_COMPOSE_DATA`, else `uac-compose/_data` in a
    wheel, else `data/` in a checkout.

    The variable is for a package that is imported from a zip (a Universal
    Extension): its `_data` is not a folder there, so the caller copies it out
    and names the copy.
    """
    env = os.environ.get("UAC_COMPOSE_DATA")
    if env:
        given = Path(env).expanduser()
        if given.is_dir():
            return given
        raise ConfigError(f"UAC_COMPOSE_DATA={env}: no such folder")
    installed = _PACKAGE / "_data"
    if installed.is_dir():
        return installed
    checkout = _PACKAGE.parent.parent / "data"
    if checkout.is_dir():
        return checkout
    raise ConfigError(f"uac-compose data not found (looked in {installed} and {checkout})")


def workspace_root() -> Path:
    """`UAC_COMPOSE_WORKSPACE`, else `~/.uac/compose`."""
    env = os.environ.get("UAC_COMPOSE_WORKSPACE")
    return Path(env).expanduser() if env else Path.home() / ".uac" / "compose"


def profile_name(explicit: str | None = None) -> str:
    """`--profile`, else `UAC_COMPOSE_PROFILE`, else `default`."""
    return explicit or os.environ.get("UAC_COMPOSE_PROFILE") or "default"


def profile_dir(explicit: str | None = None) -> Path:
    return workspace_root() / profile_name(explicit)


def corpus_dir() -> Path:
    """The real export corpus used by tests; `UAC_COMPOSE_CORPUS` overrides."""
    env = os.environ.get("UAC_COMPOSE_CORPUS")
    return Path(env).expanduser() if env else Path.home() / "SE/scripts/uac_model/SampleExports"
