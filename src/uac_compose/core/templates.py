"""Per-type default templates for one UAC release, plus sub-object item templates.

Layout: `<root>/<release>/<type>.json`, `_sub/<dotted.path>.json`,
`_extensions.json`. Keys starting with `_` are provenance and are stripped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

import yaml

from uac_compose import registry
from uac_compose.core.extensions import ExtensionMap
from uac_compose.paths import data_root

# Used only where no sub-template was derived for the path: a virtual resource
# without `amount` fails create with "Amount: Field is required".
FALLBACK_SUB_TEMPLATES: dict[str, dict[str, Any]] = {"virtualResources": {"amount": 1}}


def _read_sub(sub_dir: Path) -> dict[str, dict[str, Any]]:
    if not sub_dir.is_dir():
        return {}
    return {
        p.stem: allowed_sub(p.stem, _strip_meta(json.loads(p.read_text())))
        for p in sorted(sub_dir.glob("*.json"))
    }


@cache
def shipped_sub_templates(release: str) -> dict[str, dict[str, Any]]:
    """The sub-object shapes that ship with uac-compose, for a release.

    A profile derives a shape only from objects it *has*. A controller with no
    Approval task derives no `approvalCriteria.criteria`, so `inflate` cannot
    put back the `type: criterion` the controller demands -- measured on a real
    promotion, where the target had 12 of the 34 shapes and the create was
    refused with "missing type id property 'type'". A shape the target has
    never seen is exactly the one a promotion carries to it.
    """
    return _read_sub(data_root() / "templates" / release / "_sub")


def _strip_meta(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


@dataclass(frozen=True)
class NeverDefault:
    """Fields no template may default (data/rules/never_default.yaml + references)."""

    top_level: frozenset[str]
    sub: frozenset[str]

    def top(self, type_name: str) -> frozenset[str]:
        refs = registry.reference_fields(type_name) if type_name in registry.load() else frozenset()
        return self.top_level | refs | {"name"}

    def sub_path(self, path: str, field_name: str) -> bool:
        return f"{path}.{field_name}" in self.sub


@cache
def never_default() -> NeverDefault:
    raw = yaml.safe_load((data_root() / "rules" / "never_default.yaml").read_text())
    return NeverDefault(
        frozenset(raw.get("top_level") or ()),
        frozenset(raw.get("sub") or ()) | registry.reference_sub_fields(),
    )


def allowed(type_name: str, template: dict[str, Any]) -> dict[str, Any]:
    """The template without the fields that may never be defaulted."""
    banned = never_default().top(type_name)
    return {k: v for k, v in template.items() if k not in banned}


def allowed_sub(path: str, template: dict[str, Any]) -> dict[str, Any]:
    nd = never_default()
    return {k: v for k, v in template.items() if not nd.sub_path(path, k)}


def shipped_release(release: str | None) -> str:
    """The shipped release directory to default from: the named one, else the newest."""
    shipped = sorted(p.name for p in (data_root() / "templates").iterdir() if p.is_dir())
    return release if release in shipped else shipped[-1]


class TemplateSet:
    """Defaults for one release, plus the extension map they are read with.

    `root` holds the defaults and is normally the shipped `data/templates`:
    **the same defaults on every controller**. A file that leaves a field out
    says "this field has its default"; if the two ends disagreed about what
    that default is, the object would change meaning on the way over. That is
    not a theory -- a real promotion moved `sapView` from Legacy View to
    Enhanced View, silently, because export dropped it against one default and
    import filled it from another.

    `extensions_path` is the one part that is *properly* per-controller: the
    Universal Extension parameter map is what a given controller has installed,
    and it names slots rather than defaults them. It defaults to the map beside
    the templates, and is given on its own because a profile keeps its map
    under the release the *controller* runs, which need not be the release the
    shipped defaults were measured on.
    """

    def __init__(
        self,
        release: str,
        root: Path | None = None,
        extensions_path: Path | None = None,
        extensions: ExtensionMap | None = None,
    ) -> None:
        self.release = release
        self.root = (root or data_root() / "templates") / release
        self.extensions_path = extensions_path or (self.root / "_extensions.json")
        self._cache: dict[str, dict[str, Any]] = {}
        if extensions is not None:  # a map read live just now wins over the file
            self.__dict__["extensions"] = extensions

    def get(self, type_name: str) -> dict[str, Any]:
        """The type's template, or `{}` (safe: nothing matches, nothing is dropped)."""
        if type_name not in self._cache:
            path = self.root / f"{type_name}.json"
            raw = _strip_meta(json.loads(path.read_text())) if path.exists() else {}
            self._cache[type_name] = allowed(type_name, raw)
        return self._cache[type_name]

    @cached_property
    def sub_templates(self) -> dict[str, dict[str, Any]]:
        """Dotted field path -> default shape of one item of that list.

        A path this profile never derived falls back to the shipped shape, and
        then to `FALLBACK_SUB_TEMPLATES`. A profile's own shape always wins,
        however little data backed it.
        """
        derived = _read_sub(self.root / "_sub")
        for path, fallback in shipped_sub_templates(self.release).items():
            derived.setdefault(path, fallback)
        for path, fallback in FALLBACK_SUB_TEMPLATES.items():
            derived.setdefault(path, dict(fallback))
        return derived

    @cached_property
    def extensions(self) -> ExtensionMap:
        path = self.extensions_path
        return ExtensionMap.load(path) if path.exists() else ExtensionMap({})
