"""Milestone 0: data/uacdocs.yaml agrees with the registry and documented.yaml."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from uac_compose import registry, uacdocs
from uac_compose.cli import cli
from uac_compose.paths import data_root


def test_cli_lists_the_four_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for command in ("learn", "export", "import", "freeze"):
        assert f"  {command} " in result.output


def test_every_type_field_is_in_the_registry() -> None:
    bad = [
        (type_name, name)
        for type_name, body in uacdocs.raw()["types"].items()
        for name in body.get("fields") or {}
        if not registry.get(type_name).declares(name)
    ]
    assert bad == []


def test_every_sub_object_field_is_in_the_registry() -> None:
    bad = [
        (type_name, path, name)
        for type_name, body in uacdocs.raw()["types"].items()
        for path, fields in (body.get("sub_objects") or {}).items()
        for name in fields
        if name not in registry.get(type_name).sub_fields.get(path, ())
    ]
    assert bad == []


def test_every_scope_field_is_declared_by_a_type_that_includes_it() -> None:
    data = uacdocs.raw()
    including: dict[str, list[str]] = {}
    for type_name, body in data["types"].items():
        for scope in body["include"]:
            including.setdefault(scope, []).append(type_name)
    bad = [
        (scope, name)
        for scope, fields in data["scopes"].items()
        for name in fields
        if not any(registry.get(t).declares(name) for t in including.get(scope, []))
    ]
    assert bad == []


def test_every_task_type_is_documented() -> None:
    tasks = {t for t in registry.types() if registry.get(t).kind in ("task", "workflow")}
    assert tasks <= set(uacdocs.documented_types())


def test_every_default_is_in_its_value_map() -> None:
    bad = []
    for type_name in uacdocs.documented_types():
        for doc in uacdocs.fields(type_name).values():
            if doc.values and doc.has_default and doc.default not in doc.values:
                bad.append((type_name, doc.name, doc.default))
    assert bad == []


def test_scope_field_only_where_declared() -> None:
    # Timer includes `common` but declares no retry fields.
    assert "retryMaximum" not in uacdocs.fields("taskSleep")
    assert uacdocs.default("taskUnix", "retryMaximum") == 0


def test_type_doc_overrides_scope() -> None:
    assert uacdocs.default("taskUnix", "outputReturnType") == "NONE"
    assert uacdocs.default("taskSap", "outputReturnType") == "OUTERR"


def _documented_defaults() -> list[tuple[str, str, Any]]:
    """(type, field, value) from rules/documented.yaml, common scoped by declaration."""
    doc = yaml.safe_load((data_root() / "rules" / "documented.yaml").read_text())
    out: list[tuple[str, str, Any]] = []
    for type_name in uacdocs.documented_types():
        entry = registry.get(type_name)
        own = doc["types"].get(type_name, {})
        for name, spec in doc["common"].items():
            if entry.declares(name) and name not in own:
                out.append((type_name, name, spec["value"]))
        for name, spec in own.items():
            out.append((type_name, name, spec["value"]))
    return out


# Where rules/documented.yaml (uac_model's extraction) and uacdocs.yaml differ,
# and uacdocs.yaml is the one that follows the docs. Each is a parsing or
# scoping slip in documented.yaml.
DOCUMENTED_YAML_WRONG = {
    # agents/create-peoplesoft-task.md:104 marks OUTERR as the default;
    # documented.yaml applied the shared skill's NONE (its own note suspected this).
    ("taskPeoplesoft", "outputReturnType"): "OUTERR",
}

# documented.yaml scopes `common` by declaration over every type; uacdocs.yaml
# scopes a shared skill only to the types whose agent doc includes it. A type
# the docs place outside a skill gets none of that skill's defaults.
OUT_OF_DOC_SCOPE = {
    ("taskApplicationControl", "exitCodeProcessing"),
    ("taskApplicationControl", "outputReturnType"),
    ("taskApplicationControl", "outputReturnSline"),
    ("taskUniversal", "exitCodeProcessing"),
    ("taskUniversal", "outputReturnType"),
    ("taskUniversal", "outputReturnSline"),
    ("taskFtpFileMonitor", "outputReturnType"),
    ("taskFtpFileMonitor", "outputReturnSline"),
    ("taskFtp", "outputReturnType"),
    ("taskFtp", "outputReturnSline"),
    ("taskPeoplesoft", "outputReturnSline"),
}


def _excluded(type_name: str, name: str) -> bool:
    """A category the type's agent doc says does not apply (Timer: retries)."""
    body = uacdocs.raw()["types"].get(type_name, {})
    return name in body.get("exclude", [])


@pytest.mark.parametrize(
    ("type_name", "name", "value"),
    _documented_defaults(),
    ids=lambda v: str(v),
)
def test_default_agrees_with_documented_yaml(type_name: str, name: str, value: Any) -> None:
    doc = uacdocs.field_doc(type_name, name)
    if (type_name, name) in OUT_OF_DOC_SCOPE or _excluded(type_name, name):
        assert doc is None or not doc.has_default, "now in scope: drop it from OUT_OF_DOC_SCOPE"
        return
    assert doc is not None, f"{type_name}.{name} is not in uacdocs.yaml"
    assert doc.has_default, f"{type_name}.{name} has no default in uacdocs.yaml"
    assert doc.default == DOCUMENTED_YAML_WRONG.get((type_name, name), value)


# The prose spells two values differently from the way UAC stores them: it
# writes the `-- None --` menu entry as "None", and quotes a count. Measured
# over every shipped template: 560 values a doc confirms, 2 it spells oddly.
DOC_PROSE_ARTIFACTS = {"taskZos.autoRestartOption", "taskZos.overrideJclDelInstCount"}


def test_shipped_defaults_agree_with_the_vendor_documentation() -> None:
    """A default decides what a file may leave out, so it must be the vendor's.

    uac-compose ships one set of defaults and uses it on every controller: a field a
    file leaves out has to mean the same at both ends of a promotion. That is
    only honest if the value is the one the vendor documents, not a habit of
    whichever controller the templates were measured on. Where the docs say
    nothing the value is still shipped, still the same everywhere, and the
    controller that disagrees states the field instead -- `uac-compose learn` lists
    those fields.
    """
    from uac_compose.core.templates import TemplateSet, shipped_release

    templates = TemplateSet(shipped_release(None))
    conflicts = []
    for path in sorted(templates.root.glob("*.json")):
        type_name = path.stem
        documented = uacdocs.fields(type_name)
        for field_name, value in templates.get(type_name).items():
            doc = documented.get(field_name)
            if doc is None or not doc.has_default or doc.default == value:
                continue
            if f"{type_name}.{field_name}" in DOC_PROSE_ARTIFACTS:
                continue
            conflicts.append(f"{type_name}.{field_name}: doc {doc.default!r}, shipped {value!r}")
    assert not conflicts, "\n".join(conflicts)
