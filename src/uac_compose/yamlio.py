"""Reading and writing .uac.yaml files (SPEC §6.3).

- The bool resolver accepts only true/false: YAML 1.1 reads `on: success` as
  `True: success`.
- `comment:` and `create_only:` are stripped at every depth on load, after
  `create_only` has been read.
- The dump is deterministic (insertion order), quotes any string that would
  reload as another type, writes `position: [x, y]` and any `Flow` list in
  flow style, multi-line text as a `|` block, and an empty member as `NAME:`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

COMMENT = "comment"
CREATE_ONLY = "create_only"
#: The name an object has on the controller now, when the key is its new name.
WAS = "was"
VERSION = "version"
STYLE = "style"

#: The .uac.yaml format itself. Bumped when the format changes in a way an older
#: uac-compose would read wrongly; a file that states a higher one is refused rather
#: than half-understood.
FORMAT_VERSION = 1

#: `compact` is the readable form; `standard` is UAC's own field names and
#: nesting with defaults removed, for anyone who would rather not learn the
#: other one. `uac` is the name `standard` used to have.
STYLES = ("compact", "standard")
STYLE_ALIASES = {"uac": "standard"}


def style_named(written: str) -> str:
    name = STYLE_ALIASES.get(written.strip().lower(), written.strip().lower())
    if name not in STYLES:
        raise BundleError(f"unknown style {written!r}; use {' or '.join(STYLES)}")
    return name


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class Dumper(yaml.SafeDumper):
    pass


class Flow(list[Any]):
    """A list the dump writes on one line: `[Monday, Friday]`. Equal to a plain list."""


def _flow(dumper: yaml.SafeDumper, values: Flow) -> yaml.Node:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", values, flow_style=True)


def _none(dumper: yaml.SafeDumper, _: Any) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _list(dumper: yaml.SafeDumper, values: list[Any]) -> yaml.Node:
    flow = bool(values) and all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in values
    )
    return dumper.represent_sequence("tag:yaml.org,2002:seq", values, flow_style=flow)


def _block_safe(text: str) -> bool:
    """A `|` block cannot hold trailing spaces on a line or leading indentation."""
    return (
        "\n" in text
        and text.strip() != ""
        and not any(line != line.rstrip() for line in text.split("\n"))
        and not text.startswith((" ", "\t"))
    )


def _str(dumper: yaml.SafeDumper, text: str) -> yaml.Node:
    style = "|" if _block_safe(text) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


def _float(dumper: yaml.SafeDumper, value: float) -> yaml.Node:
    """`120.0` is written `120`: the same number, and what a person writes."""
    if value.is_integer() and abs(value) < 2**53:
        return dumper.represent_int(int(value))
    return dumper.represent_float(value)


Dumper.add_representer(type(None), _none)
Dumper.add_representer(list, _list)
Dumper.add_representer(Flow, _flow)
Dumper.add_representer(str, _str)
Dumper.add_representer(float, _float)


def dump(document: Any) -> str:
    text: str = yaml.dump(
        document, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100, indent=2
    )
    return text


def load(text: str) -> Any:
    return yaml.load(text, Loader=Loader)  # noqa: S506 - Loader is a SafeLoader


def strip_annotations(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: strip_annotations(v) for k, v in node.items() if k not in (COMMENT, CREATE_ONLY)}
    if isinstance(node, list):
        return [strip_annotations(item) for item in node]
    return node


# Sections in creation order (SPEC §6.2). `templates` is copied from, never created.
SECTIONS = (
    "business_services",
    "user_groups",
    "agent_clusters",
    "credentials",
    "variables",
    "virtual_resources",
    "database_connections",
    "peoplesoft_connections",
    "sap_connections",
    "email_connections",
    "custom_days",
    "calendars",
    "scripts",
    "event_templates",
    "tasks",
    "triggers",
    "webhooks",  # a webhook launches a task: after tasks
)
TEMPLATES = "templates"


class BundleError(ValueError):
    """The file cannot be read as a bundle."""


@dataclass
class Bundle:
    version: int = FORMAT_VERSION
    """The .uac.yaml format the file states; `FORMAT_VERSION` when it states none."""
    style: str | None = None
    """What the file says it is written in. Import reads either whichever it says."""
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Section -> name -> body, annotations stripped, in file order."""
    templates: dict[str, Any] = field(default_factory=dict)
    create_only: set[tuple[str, str]] = field(default_factory=set)
    """(section, name) of every object marked create-only."""
    was: dict[tuple[str, str], str] = field(default_factory=dict)
    """(section, name) -> the name it has on the controller now (`was:`): a rename."""
    joins: list[str] = field(default_factory=list)
    """The names in a `business_services:` LIST (`- Name` / `- Name: description`).

    Each item is a definition; no object joins them. `import --update` rewrites
    only objects in one of these services, or in none.
    """


def service_list(items: list[Any]) -> tuple[list[str], dict[str, Any]]:
    """`[M, {Q: description}]` -> (the names, the definitions)."""
    names: list[str] = []
    defs: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict) and len(item) == 1:
            ((name, description),) = item.items()
            if description is not None and not isinstance(description, str):
                raise BundleError(
                    f"business_services: {name}: the value is the description, a text"
                )
            body = {"description": description} if description else {}
        elif isinstance(item, str | int | float) and not isinstance(item, bool):
            name, body = item, {}
        else:
            raise BundleError(f"business_services: expected `- Name` or `- Name: description`, "
                              f"not {item!r}")  # fmt: skip
        if str(name) in defs:
            raise BundleError(f"business_services: {name} is listed twice")
        names.append(str(name))
        defs[str(name)] = body
    return names, defs


def read_bundle(text: str) -> Bundle:
    try:
        document = load(text)
    except yaml.YAMLError as error:
        raise BundleError(f"not valid YAML: {error}") from error
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise BundleError("a bundle is a mapping of sections")
    directives = (CREATE_ONLY, COMMENT, VERSION, STYLE)
    unknown = [k for k in document if k not in (*SECTIONS, TEMPLATES, *directives)]
    if unknown:
        raise BundleError(
            f"unknown section(s): {', '.join(map(str, unknown))}; "
            f"sections are {', '.join(SECTIONS)}, {TEMPLATES}"
        )
    written_version = document.get(VERSION, FORMAT_VERSION)
    if not isinstance(written_version, int) or written_version < 1:
        raise BundleError(f"version: must be a whole number, not {written_version!r}")
    if written_version > FORMAT_VERSION:
        raise BundleError(
            f"this file states version {written_version}; this uac-compose reads up to "
            f"{FORMAT_VERSION}. Upgrade uac-compose rather than let it read the file half-way."
        )
    style = style_named(str(document[STYLE])) if document.get(STYLE) else None
    whole = document.get(CREATE_ONLY) or []
    whole = [whole] if isinstance(whole, str) else list(whole)
    bad = [s for s in whole if s not in SECTIONS]
    if bad:
        raise BundleError(f"create_only names {', '.join(map(str, bad))}, which is not a section")

    sections: dict[str, dict[str, Any]] = {}
    create_only: set[tuple[str, str]] = set()
    was: dict[tuple[str, str], str] = {}
    joins: list[str] = []
    for section in SECTIONS:
        body = document.get(section)
        if body is None:
            continue
        if section == "business_services" and isinstance(body, list):
            joins, body = service_list(body)
        if not isinstance(body, dict):
            raise BundleError(f"{section}: expected a mapping of name -> definition")
        for name, definition in body.items():
            if definition is not None and not isinstance(definition, dict):
                raise BundleError(f"{section}.{name}: expected a mapping")
            if section in whole or (
                isinstance(definition, dict) and definition.get(CREATE_ONLY) is True
            ):
                create_only.add((section, str(name)))
        cleaned: dict[str, Any] = {}
        for name, definition in body.items():
            own = dict(definition or {})
            if WAS in own:  # top level only: `was` names the object, not a field
                earlier = own.pop(WAS)
                if not isinstance(earlier, str) or not earlier:
                    raise BundleError(f"{section}.{name}: was: must be the current name")
                was[(section, str(name))] = earlier
            cleaned[str(name)] = strip_annotations(own)
        sections[section] = cleaned
    templates = strip_annotations(document.get(TEMPLATES) or {})
    return Bundle(written_version, style, sections, templates, create_only, was, joins)
