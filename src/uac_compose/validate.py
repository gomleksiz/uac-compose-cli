"""Import validation (SPEC §8.2): independent rungs, each returning findings.

Errors stop the import; warnings (`??`) never do. Every rung is measured over
the corpus first: an exported object must validate clean (a validator that
rejects valid objects gets turned off -- LESSONS #25).

- `static(...)`: what the file says, before the controller is asked anything.
- `for_create(...)`: requirements that apply only to an object being created
  (a file that updates two fields of an existing task need not restate its
  agent).
- `references_(...)`: names the controller and the file do not know.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from uac_compose import inactive, references, registry, uacdocs
from uac_compose.compile import Planned
from uac_compose.core.extensions import ExtensionMap, expand
from uac_compose.core.inflate import inflate
from uac_compose.core.templates import TemplateSet
from uac_compose.paths import data_root

SECRETS = ("runtimePassword", "runtimePassPhrase", "runtimeToken", "defaultPwd")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{'!!' if self.level == 'error' else '??'} {self.where}: {self.message}"


def _error(p: Planned, message: str) -> Finding:
    return Finding("error", p.label, message)


def _warn(p: Planned, message: str) -> Finding:
    return Finding("warning", p.label, message)


def _is_variable(value: Any) -> bool:
    return isinstance(value, str) and "${" in value


# --- static rungs ----------------------------------------------------------------------


def rung_type(p: Planned) -> list[Finding]:
    return [] if p.type in registry.load() else [_error(p, f"unknown type {p.type!r}")]


def rung_fields(p: Planned, ext: ExtensionMap | None = None) -> list[Finding]:
    """Top-level names vs the registry; `*VarCheck`/`use*Var` named as what they are.

    A Universal task also holds its extension's parameters by name, which the
    registry knows nothing about: those are checked against the extension map
    `learn` built from the controller.

    An extension the map does not hold at all is one error, said once. Checked
    parameter by parameter, a live import said "neither a field ... nor a
    parameter" 13 times for one task, and never that the extension itself was
    neither on the controller nor in the --extensions folder.
    """
    entry = registry.get(p.type)
    template = p.obj.get("template")
    parameters = ext.for_template(template) if ext is not None else {}
    universal = p.type in ("taskUniversal", "taskUniversalMonitor")
    absent = universal and ext is not None and not _is_variable(template) and ext.lacks(template)
    out = []
    if absent:
        out.append(
            _error(
                p,
                f"extension {template!r} is not on this controller and not frozen; "
                "run `uac-compose freeze` on the controller it came from",
            )
        )
    for key in p.obj:
        if key in ("type", "_uacType") or entry.declares(key) or key in parameters:
            continue
        if key.endswith("VarCheck") or re.match(r"^use\w+Var$", key):
            twin = key[: -len("Check")] if key.endswith("VarCheck") else key[3:4].lower() + key[4:]
            out.append(_error(p, f"{key} is a UI toggle the controller rejects; write {twin} only"))
        elif absent:
            continue  # its parameters cannot be checked: the error above says why
        elif universal:
            out.append(
                _error(
                    p,
                    f"{key!r} is neither a field of {p.type} nor a parameter of "
                    f"extension {p.obj.get('template')!r}",
                )
            )
        else:
            close = difflib.get_close_matches(key, entry.fields, n=1)
            hint = f" (did you mean {close[0]}?)" if close else ""
            out.append(_error(p, f"{key!r} is not a field of {p.type}{hint}"))
    return out


# Nested shapes the registry does not describe fully.
_EXEMPT_SUB = re.compile(r"(^|\.)(condition|conditionExpression|criteria|jobSteps)(\.|$)")


def rung_sub_fields(p: Planned) -> list[Finding]:
    entry = registry.get(p.type)
    out: list[Finding] = []

    def walk(value: Any, path: str) -> None:
        if _EXEMPT_SUB.search(path) or path not in entry.sub_fields:
            return
        allowed = set(entry.sub_fields[path])
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key, sub in row.items():
                if key not in allowed:
                    out.append(_error(p, f"{path}: {key!r} is not a field of {path}"))
                else:
                    walk(sub, f"{path}.{key}")

    for key, value in p.obj.items():
        if isinstance(value, dict | list):
            walk(value, key)
    return out


def rung_secrets(p: Planned) -> list[Finding]:
    return [
        _error(
            p,
            f"{key} is a secret and never belongs in a bundle; set it on the controller "
            "(or use a credential)",
        )
        for key in p.obj
        if key in SECRETS
    ]


def rung_var_pairs(p: Planned) -> list[Finding]:
    out = []
    for plain, twin in registry.var_pairs(p.type):
        if "." in plain:
            continue
        if plain in p.obj and twin in p.obj:
            out.append(_error(p, f"{plain} and {twin} are both stated; UAC reads one, write one"))
        elif _is_variable(p.obj.get(plain)) and plain != "resPriority":
            out.append(_error(p, f"{plain} holds a variable; write it as {twin}"))
    return out


def rung_system_operations(p: Planned) -> list[Finding]:
    actions = p.obj.get("actions")
    rows = actions.get("systemOperations") if isinstance(actions, dict) else None
    return [
        _error(p, f"actions.systemOperations[{i}] has no operation")
        for i, row in enumerate(rows or [])
        if isinstance(row, dict) and not row.get("operation")
    ]


def rung_enums(p: Planned) -> list[Finding]:
    """A WARNING, not an error: the docs' value maps are not closed. Measured over
    the corpus: 39 stored values outside them (30 taskFtp `format: Text`, some
    `UNKNOWN`), all accepted by the controller."""
    out = []
    for name, doc in uacdocs.fields(p.type).items():
        value = p.obj.get(name)
        if not doc.values or value is None or _is_variable(value) or isinstance(value, bool):
            continue
        if str(value) not in doc.values:
            out.append(
                _warn(p, f"{name} {value!r} is not one the docs list ({', '.join(doc.values)})")
            )
    return out


FORMATS = {
    "DD:HH:MM:SS": re.compile(r"^\d{2,}:\d{2}:\d{2}:\d{2}$"),
    "HH:MM": re.compile(r"^\d{2}:\d{2}$"),
    "yyyy-MM-dd": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "1,5,22-30": re.compile(r"^\s*-?\d+(\s*-\s*-?\d+)?(\s*,\s*-?\d+(\s*-\s*-?\d+)?)*\s*$"),
}


def rung_formats(p: Planned) -> list[Finding]:
    out = []
    for name, doc in uacdocs.fields(p.type).items():
        value = p.obj.get(name)
        pattern = FORMATS.get(doc.format or "")
        if pattern is None or not isinstance(value, str) or _is_variable(value):
            continue
        if not pattern.match(value):
            hint = " (all four parts: 00:01:49:00)" if doc.format == "DD:HH:MM:SS" else ""
            out.append(_error(p, f"{name} {value!r} is not in the format {doc.format}{hint}"))
    return out


def rung_exclusive(p: Planned) -> list[Finding]:
    obj, out = p.obj, []
    broadcast = obj.get("broadcastCluster") or obj.get("broadcastClusterVar")
    if broadcast and any(
        obj.get(k) for k in ("agent", "agentVar", "agentCluster", "agentClusterVar")
    ):
        out.append(_error(p, "a broadcast cluster cannot be combined with an agent or cluster"))
    if obj.get("waitForOutput") is True and obj.get("outputFailureOnly") is True:
        out.append(_error(p, "waitForOutput and outputFailureOnly cannot both be true"))
    return out


@cache
def _vocab() -> dict[str, list[str]]:
    raw = yaml.safe_load((data_root() / "rules" / "vocabularies.yaml").read_text())
    return dict(raw["fields"])


def rung_vocabularies(p: Planned) -> list[Finding]:
    out = []
    for path, allowed in _vocab().items():
        type_name, _, rest = path.partition(".")
        if type_name != p.type:
            continue
        head, _, leaf = rest.partition(".")
        value = p.obj.get(head)
        values: list[Any]
        if leaf:
            values = [r.get(leaf) for r in value or [] if isinstance(r, dict)]
        elif head == "businessDays" and isinstance(value, dict):
            values = [d for d in str(value.get("value") or "").split(",") if d]
        else:
            values = [] if value is None else [value]
        for v in values:
            if v is not None and not _is_variable(v) and v not in allowed:
                out.append(_error(p, f"{rest} {v!r} is not one of {', '.join(allowed)}"))
    return out


# Edge conditions valid only from some source types (prompts/workflow-build.md).
_FAILURE_LIKE = ("Failure", "Success/Failure")
_NO_FAILURE_FROM = ("taskWorkflow", "taskSleep", "taskManual")
_APPROVAL_ONLY = ("Approved", "Approval Required")


def rung_edges(p: Planned, type_of: dict[str, str]) -> list[Finding]:
    """`type_of`: task name -> type, for the members this run can see."""
    out = []
    for edge in p.obj.get("workflowEdges") or []:
        cond = (edge.get("condition") or {}).get("value") if isinstance(edge, dict) else None
        source = str(
            ((edge.get("sourceId") or {}) if isinstance(edge, dict) else {}).get("taskName", "")
        )
        source_type = type_of.get(source)
        if source_type is None:
            continue
        if cond in _FAILURE_LIKE and source_type in _NO_FAILURE_FROM:
            out.append(_error(p, f"edge from {source!r}: a {source_type} cannot end in failure"))
        if cond in _APPROVAL_ONLY and source_type != "taskApproval":
            out.append(_error(p, f"edge from {source!r}: {cond} is only for an Approval task"))
    return out


def static(
    planned: list[Planned],
    type_of: dict[str, str] | None = None,
    ext: ExtensionMap | None = None,
) -> list[Finding]:
    """Every static rung over every object. Unknown types stop the other rungs."""
    type_of = {**(type_of or {}), **{p.name: p.type for p in planned if p.section == "tasks"}}
    out: list[Finding] = []
    for p in planned:
        found = rung_type(p)
        if found:
            out += found
            continue
        out += rung_fields(p, ext)
        for rung in (
            rung_sub_fields,
            rung_secrets,
            rung_var_pairs,
            rung_system_operations,
            rung_enums,
            rung_formats,
            rung_exclusive,
            rung_vocabularies,
        ):
            out += rung(p)
        out += rung_edges(p, type_of)
    return out


# --- create rungs ------------------------------------------------------------------------

TARGETS = (
    "agent",
    "agentVar",
    "agentCluster",
    "agentClusterVar",
    "broadcastCluster",
    "broadcastClusterVar",
)


def _matches(value: Any, condition: Any) -> bool:
    if isinstance(condition, dict) and "not" in condition:
        return not _matches(value, condition["not"])
    allowed = condition if isinstance(condition, list) else [condition]
    value = None if value in ("", None) else value
    return any(value == a and type(value) is type(a) for a in allowed)


def for_create(p: Planned, templates: TemplateSet) -> list[Finding]:
    """Requirements of a create, read against the object as it will be posted."""
    if p.type not in registry.load():
        return []
    full = inflate(p.obj, templates.get(p.type), templates.sub_templates)
    out = []
    docs = uacdocs.fields(p.type)
    for name, doc in docs.items():
        missing = doc.required_when and full.get(name) in (None, "")
        if missing and all(_matches(full.get(k), c) for k, c in doc.required_when.items()):
            said = ", ".join(f"{k}={full.get(k)!r}" for k in doc.required_when)
            out.append(_error(p, f"{name} is required when {said}"))
    if any(d.scope == "agent_task" for d in docs.values()) and not any(
        full.get(t) for t in TARGETS
    ):
        out.append(
            _error(
                p,
                "no execution target: state an agent, agent cluster or "
                "broadcast cluster (or its *Var)",
            )
        )
    return out


# --- warnings ------------------------------------------------------------------------------


def inactive_stated(p: Planned, templates: TemplateSet) -> list[Finding]:
    """A field the file states that its own settings make inactive: it does nothing."""
    if p.type not in registry.load():
        return []
    full = inflate(p.obj, templates.get(p.type), templates.sub_templates)
    dead = sorted(inactive.inactive_fields(full) & set(p.obj))
    return [_warn(p, f"{f} is stated but inactive under this object's own settings") for f in dead]


def references_(
    planned: list[Planned], known: dict[str, set[str]], ext: ExtensionMap | None = None
) -> list[Finding]:
    """Names neither the controller (references.json) nor the file itself defines.

    With `ext`, a Universal task's parameters are read in their slots, where a
    credential or script parameter names one.
    """
    defined: dict[str, set[str]] = {}
    for p in planned:
        defined.setdefault(p.section, set()).add(p.name)
    out = []
    refused = references.refused_when_missing()
    for p in planned:
        if p.type not in registry.load():
            continue
        obj = p.obj
        if ext is not None:
            obj = expand(obj, ext, frozenset(registry.get(p.type).fields))
        for field_name, kind, name in references.named(p.type, obj):
            pool = known.get(kind, set()) | defined.get(kind, set())
            if kind not in known and kind not in defined:
                continue  # nothing to check against
            if name not in pool:
                close = difflib.get_close_matches(name, sorted(pool), n=1)
                hint = f" (did you mean {close[0]!r}?)" if close else ""
                finding = _error if field_name in refused else _warn
                out.append(
                    finding(
                        p,
                        f"{field_name} names {kind[:-1]} {name!r}, which is "
                        f"not on the controller or in this file{hint}",
                    )
                )
    return out


def known_from(references_json: dict[str, list[dict[str, str]]]) -> dict[str, set[str]]:
    return {kind: {r["name"] for r in rows} for kind, rows in references_json.items()}


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]
