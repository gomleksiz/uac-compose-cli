"""Compact idioms (SPEC §6.4). Each is used only when lossless, otherwise the raw
UAC field is written; `parse` accepts both forms, so any UAC field stays legal.

- name/value lists: `- region: eu` for `{name: region, value: eu}`
- controller-labelled fields (custom fields, event template refs): the value only
- var pairs: `agent: ${x}` for a stored `agentVar: ${x}`
- durations: `1h 49m` for `00:01:49:00` (fields the docs mark DD:HH:MM:SS)
- `email_on:` for one plain email notification
- set-variable runs: adjacent setVariableActions rows alike but for the
  variable are one row with `variables: [- name: value]`
- calendars: `businessDays: [Monday, ...]`, `customDays: [name]`,
  `quarters: [Jan 1, Apr 1, Jul 1, Oct 1]`; custom days: `observed: {Sat: Fri}`
- flag lists in sub-object rows: `ops: [read, update]` for `opRead: true,
  opUpdate: true` (data/rules/aliases.yaml `flags:`)
- event template attributes: `- file_name: File Name` for
  `{name: file_name, label: File Name, type: Text}`; `type:` only when not Text
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from uac_compose import registry, uacdocs
from uac_compose.core.extensions import ExtensionMap
from uac_compose.paths import data_root
from uac_compose.yamlio import Flow

PAIR_FIELDS = ("variables", "httpHeaders", "urlParameters", "providerParameters")
PAIR_KEYS = frozenset({"name", "value"})

# The controller owns the label: a POST with a different one comes back with
# its own (verified live in uac_model). Write the value only.
LABELLED = (
    "customField1",
    "customField2",
    "customChoiceField1",
    "eventTemplate",
    "templateEventTemplate",
)

SCALAR_PAIRS = ("resPriority",)


# --- name/value lists -----------------------------------------------------------


def emit_pairs(rows: Any) -> list[dict[str, Any]] | None:
    if not isinstance(rows, list) or not rows:
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - PAIR_KEYS:
            return None
        name = row.get("name")
        if not isinstance(name, str) or not name or name in PAIR_KEYS:
            return None
        out.append({name: row.get("value")})
    return out


def parse_pairs(written: Any) -> Any:
    """`[{n: v}]` -> `[{name: n, value: v}]`; anything else is returned as written."""
    if not isinstance(written, list) or not written:
        return written
    rows: list[dict[str, Any]] = []
    for item in written:
        if not isinstance(item, dict) or len(item) != 1:
            return written
        ((name, value),) = item.items()
        if name in PAIR_KEYS:
            return written  # UAC's own `- name: x` row
        rows.append({"name": str(name)} if value is None else {"name": str(name), "value": value})
    return rows


# --- controller-labelled fields ---------------------------------------------------


def emit_labelled(stored: Any) -> Any:
    if isinstance(stored, dict) and "value" in stored and set(stored) <= {"label", "value"}:
        value = stored["value"]
        return None if isinstance(value, dict) else value
    return None


def parse_labelled(written: Any) -> Any:
    return written if isinstance(written, dict) else {"value": written}


def without_labels(obj: dict[str, Any]) -> dict[str, Any]:
    """The object as far as it is the file's to state: controller labels removed."""
    out = dict(obj)
    for name in LABELLED:
        value = out.get(name)
        if isinstance(value, dict) and "label" in value:
            out[name] = {k: v for k, v in value.items() if k != "label"}
    for name, spec in member_lists(str(out.get("type", ""))).items():
        if name in out:
            out[name] = without_owned(spec, out[name])
    return out


# --- lists of references (data/rules/member_lists.yaml) ---------------------------


@dataclass(frozen=True)
class MemberList:
    name: tuple[str, ...]
    owned: tuple[tuple[str, ...], ...]


@cache
def member_lists(type_name: str) -> dict[str, MemberList]:
    raw = yaml.safe_load((data_root() / "rules" / "member_lists.yaml").read_text())
    table = (raw.get("lists") or {}).get(type_name) or {}
    return {
        field_name: MemberList(
            tuple(str(spec["name"]).split(".")),
            tuple(tuple(str(p).split(".")) for p in spec.get("owned") or ()),
        )
        for field_name, spec in table.items()
    }


def _pop_path(row: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """`row` without the value at `path`, and without mappings left empty by it."""
    head, rest = path[0], path[1:]
    if head not in row:
        return row
    out = dict(row)
    if not rest:
        del out[head]
    elif isinstance(out[head], dict):
        inner = _pop_path(out[head], rest)
        if inner:
            out[head] = inner
        else:
            del out[head]
    return out


def _at(row: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = row
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def _row_for(name: Any, path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = name
    for step in reversed(path):
        node = {step: node}
    result: dict[str, Any] = node
    return result


def without_owned(spec: MemberList, rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        if isinstance(row, dict):
            for path in spec.owned:
                row = _pop_path(row, path)
        out.append(row)
    return out


def emit_members(spec: MemberList, rows: Any) -> list[Any] | None:
    """Each row as its name, when the name is all the row holds beyond labels."""
    if not isinstance(rows, list) or not rows:
        return None
    names = []
    for row in without_owned(spec, rows):
        name = _at(row, spec.name) if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or row != _row_for(name, spec.name):
            return None
        names.append(name)
    return names


def parse_members(spec: MemberList, written: Any) -> Any:
    """A plain name back to its row; a raw row is left as written."""
    if not isinstance(written, list):
        return written
    return [_row_for(item, spec.name) if isinstance(item, str) else item for item in written]


# --- var pairs ------------------------------------------------------------------


def reference_pairs(type_name: str) -> list[tuple[str, str]]:
    if type_name not in registry.load():
        return []
    return [
        (a, b) for a, b in registry.var_pairs(type_name) if "." not in a and a not in SCALAR_PAIRS
    ]


def _is_variable(value: Any) -> bool:
    return isinstance(value, str) and "${" in value


def emit_var_pairs(type_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """`agentVar: ${x}` written as `agent: ${x}` (UAC never fills both halves)."""
    renames = {
        twin: plain
        for plain, twin in reference_pairs(type_name)
        if twin in body and plain not in body and _is_variable(body[twin])
    }
    return {renames.get(k, k): v for k, v in body.items()}


def parse_var_pairs(type_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """A `${...}` value under the plain half goes to the `*Var` half."""
    renames = {
        plain: twin
        for plain, twin in reference_pairs(type_name)
        if plain in body and twin not in body and _is_variable(body[plain])
    }
    return {renames.get(k, k): v for k, v in body.items()}


# --- durations ----------------------------------------------------------------------

FOUR_PART = re.compile(r"^(\d{2,}):(\d{2}):(\d{2}):(\d{2})$")
HUMAN = re.compile(r"^(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$")


def duration_fields(type_name: str) -> set[str]:
    return {n for n, d in uacdocs.fields(type_name).items() if d.format == "DD:HH:MM:SS"}


def render_duration(stored: str) -> str | None:
    m = FOUR_PART.match(stored)
    if not m:
        return None
    d, h, mi, s = (int(x) for x in m.groups())
    if h > 23 or mi > 59 or s > 59:
        return None
    parts = [f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (mi, "m"), (s, "s")) if n]
    text = " ".join(parts) or "0s"
    return text if parse_duration(text) == stored else None


def parse_duration(written: Any) -> Any:
    """`1h 49m` -> `00:01:49:00`, always four parts; anything else as written."""
    if not isinstance(written, str) or not written.strip():
        return written
    m = HUMAN.match(written.strip())
    if not m or not any(m.groups()):
        return written
    d, h, mi, s = (int(x or 0) for x in m.groups())
    mi, s = mi + s // 60, s % 60
    h, mi = h + mi // 60, mi % 60
    d, h = d + h // 24, h % 24
    return f"{d:02d}:{h:02d}:{mi:02d}:{s:02d}"


# --- email_on -------------------------------------------------------------------------

EMAIL_KEYS = frozenset({"status", "notifyOnLateFinish", "emailConnection", "emailTemplate"})
EMAIL_EVENTS = {
    "failure": ("status", "Failed"),
    "success": ("status", "Success"),
    "late finish": ("notifyOnLateFinish", True),
}


def emit_email_on(actions: dict[str, Any]) -> dict[str, Any]:
    rows = actions.get("emailNotifications")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return actions
    row = rows[0]
    if set(row) - EMAIL_KEYS or row.get("notifyOnLateFinish", True) is not True:
        return actions
    events = []
    if "status" in row:
        match = [e for e, (k, v) in EMAIL_EVENTS.items() if k == "status" and v == row["status"]]
        if not match:
            return actions
        events.append(match[0])
    if row.get("notifyOnLateFinish"):
        events.append("late finish")
    if not events:
        return actions
    idiom: dict[str, Any] = {"email_on": events}
    if "emailConnection" in row:
        idiom["email_connection"] = row["emailConnection"]
    if "emailTemplate" in row:
        idiom["email_template"] = row["emailTemplate"]
    out: dict[str, Any] = {}
    for key, value in actions.items():
        if key == "emailNotifications":
            out.update(idiom)
        else:
            out[key] = value
    return out


class IdiomError(ValueError):
    pass


# --- set-variable runs --------------------------------------------------------------
#
# Adjacent setVariableActions rows that differ only in the variable they set
# are one row with a `variables:` list, the same `- name: value` the other
# action rows use. Order is kept: only neighbours are joined.

SET_VARIABLE_KEYS = ("variableName", "variableValue")


def _set_variable_shared(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict) or "variables" in row:
        return None
    name = row.get("variableName")
    if not isinstance(name, str) or not name or "variableValue" not in row:
        return None
    return {k: v for k, v in row.items() if k not in SET_VARIABLE_KEYS}


def emit_set_variables(actions: dict[str, Any]) -> dict[str, Any]:
    rows = actions.get("setVariableActions")
    if not isinstance(rows, list) or len(rows) < 2:
        return actions
    # Runs of rows alike but for the variable: [shared fields, [row, ...]].
    runs: list[tuple[dict[str, Any] | None, list[Any]]] = []
    for row in rows:
        shared = _set_variable_shared(row)
        if shared is not None and runs and runs[-1][0] == shared:
            runs[-1][1].append(row)
        else:
            runs.append((shared, [row]))
    out: list[Any] = []
    for shared, run in runs:
        if shared is None or len(run) < 2:
            out.extend(run)
        else:
            pairs = [{r["variableName"]: r["variableValue"]} for r in run]
            out.append({**shared, "variables": pairs})
    if parse_set_variables(out) != rows:
        return actions
    return {**actions, "setVariableActions": out}


def parse_set_variables(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    out: list[Any] = []
    for row in rows:
        variables = row.get("variables") if isinstance(row, dict) else None
        if not isinstance(variables, list):
            out.append(row)
            continue
        shared = {k: v for k, v in row.items() if k != "variables"}
        for item in variables:
            if not isinstance(item, dict) or len(item) != 1:
                return rows
            ((name, value),) = item.items()
            out.append({**shared, "variableName": str(name), "variableValue": value})
    return out


def parse_email_on(actions: Any, where: str) -> Any:
    if not isinstance(actions, dict) or "email_on" not in actions:
        extra = {"email_connection", "email_template"} & set(actions or {})
        if extra:
            raise IdiomError(f"{where}: {', '.join(sorted(extra))} without email_on")
        return actions
    written = actions["email_on"]
    events = [written] if isinstance(written, str) else list(written or [])
    row: dict[str, Any] = {}
    for event in events:
        key = str(event).strip().lower()
        if key not in EMAIL_EVENTS:
            raise IdiomError(
                f"{where}: unknown email_on event {event!r}; use {', '.join(EMAIL_EVENTS)}"
            )
        field, value = EMAIL_EVENTS[key]
        if field in row and row[field] != value:
            raise IdiomError(f"{where}: email_on names two statuses; write emailNotifications")
        row[field] = value
    ordered: dict[str, Any] = {k: row[k] for k in ("status", "notifyOnLateFinish") if k in row}
    if "email_connection" in actions:
        ordered["emailConnection"] = actions["email_connection"]
    if "email_template" in actions:
        ordered["emailTemplate"] = actions["email_template"]
    out: dict[str, Any] = {}
    for key, value in actions.items():
        if key == "email_on":
            out["emailNotifications"] = [*(actions.get("emailNotifications") or []), ordered]
        elif key not in ("email_connection", "email_template", "emailNotifications"):
            out[key] = value
    return out


# --- calendars and custom days ------------------------------------------------------------
#
# Two weekday vocabularies, both UAC's: a calendar says `Monday`, a custom day
# says `Mon` (validation refuses the wrong one).


def emit_business_days(stored: Any) -> list[str] | None:
    """`{value: "Monday,Friday"}` -> `[Monday, Friday]`."""
    if not isinstance(stored, dict) or set(stored) != {"value"}:
        return None
    value = stored["value"]
    if not isinstance(value, str) or not value:
        return None
    days = value.split(",")
    return Flow(days) if all(days) and ",".join(days) == value else None


def parse_business_days(written: Any) -> Any:
    if isinstance(written, list) and all(isinstance(d, str) for d in written):
        return {"value": ",".join(written)}
    return written


def emit_names(rows: Any) -> list[str] | None:
    """A calendar's `customDays: [{name: X}]` -> `[X]`."""
    if not isinstance(rows, list) or not rows:
        return None
    if not all(
        isinstance(r, dict) and set(r) == {"name"} and isinstance(r["name"], str) for r in rows
    ):
        return None
    return [r["name"] for r in rows]


def parse_names(written: Any) -> Any:
    if isinstance(written, list) and written and all(isinstance(n, str) for n in written):
        return [{"name": n} for n in written]
    return written


def emit_observed(rules: Any) -> dict[str, str] | None:
    """`[{actualDayOfWeek: Sat, observedDayOfWeek: Fri}]` -> `{Sat: Fri}`."""
    if not isinstance(rules, list) or not rules:
        return None
    out: dict[str, str] = {}
    for r in rules:
        if not isinstance(r, dict) or set(r) != {"actualDayOfWeek", "observedDayOfWeek"}:
            return None
        actual = r["actualDayOfWeek"]
        if not isinstance(actual, str) or actual in out:
            return None
        out[actual] = r["observedDayOfWeek"]
    return out


def parse_observed(written: Any) -> Any:
    if not isinstance(written, dict):
        return written
    return [{"actualDayOfWeek": a, "observedDayOfWeek": o} for a, o in written.items()]


# A calendar's four quarter starts, `{day: '1', month: Jan}` each, are one
# line: `quarters: [Jan 1, Apr 1, Jul 1, Oct 1]`. All four or none.
QUARTERS = ("firstQuarterStart", "secondQuarterStart", "thirdQuarterStart", "fourthQuarterStart")
QUARTER = re.compile(r"^(\S+) (\d+)$")


def _quarter(stored: Any) -> str | None:
    if not isinstance(stored, dict) or set(stored) != {"day", "month"}:
        return None
    day, month = stored["day"], stored["month"]
    if not isinstance(day, str) or not isinstance(month, str):
        return None
    text = f"{month} {day}"
    return text if _parse_quarter(text) == stored else None


def _parse_quarter(written: Any) -> Any:
    m = QUARTER.match(written) if isinstance(written, str) else None
    return {"day": m.group(2), "month": m.group(1)} if m else written


def emit_quarters(body: dict[str, Any]) -> dict[str, Any]:
    starts = [_quarter(body.get(q)) for q in QUARTERS]
    if "quarters" in body or any(s is None for s in starts):
        return body
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == QUARTERS[0]:
            out["quarters"] = Flow(starts)
        elif key not in QUARTERS:
            out[key] = value
    return out


def parse_quarters(body: dict[str, Any], where: str) -> dict[str, Any]:
    if "quarters" not in body:
        return body
    written = body["quarters"]
    if not isinstance(written, list) or len(written) != len(QUARTERS):
        raise IdiomError(f"{where}: quarters takes four starts, e.g. [Jan 1, Apr 1, Jul 1, Oct 1]")
    both = [q for q in QUARTERS if q in body]
    if both:
        raise IdiomError(f"{where}: quarters and {both[0]} say the same thing; write one")
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "quarters":
            for field, start in zip(QUARTERS, written, strict=True):
                row = _parse_quarter(start)
                if not isinstance(row, dict):
                    raise IdiomError(f"{where}: quarter start {start!r} is not like `Jan 1`")
                out[field] = row
        else:
            out[key] = value
    return out


# --- event template attributes ------------------------------------------------------------
#
# Measured on two live controllers: 163 rows, every one with name, label and
# type; 144 are Text. Six attributes are called `name`, `label` or `type`: as a
# key they would read as that row field, so those rows keep UAC's shape.

ATTRIBUTE_KEYS = frozenset({"name", "label", "type"})
ATTRIBUTE_TYPE = "Text"


def emit_attributes(rows: Any) -> list[Any] | None:
    """`[{name: n, label: l, type: t}]` -> `[{n: l}]`, with `type: t` only when not Text.

    A row that is not exactly name, label and type stays as it is.
    """
    if not isinstance(rows, list) or not rows:
        return None
    out: list[Any] = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or name in ATTRIBUTE_KEYS
            or set(row) != ATTRIBUTE_KEYS
            or not isinstance(row["label"], str)
        ):
            out.append(row)
            continue
        short: dict[str, Any] = {name: row["label"]}
        if row["type"] != ATTRIBUTE_TYPE:
            short["type"] = row["type"]
        out.append(short)
    return out


def parse_attributes(written: Any) -> Any:
    """`- n: l` back to `{name: n, label: l, type: Text}`; a UAC row is left as written."""
    if not isinstance(written, list):
        return written
    rows: list[Any] = []
    for item in written:
        own = [k for k in item if k not in ATTRIBUTE_KEYS] if isinstance(item, dict) else []
        if len(own) != 1 or set(item) - {own[0], "type"}:
            rows.append(item)
            continue
        label = item[own[0]]
        row: dict[str, Any] = {"name": str(own[0])}
        if label is not None:
            row["label"] = label
        row["type"] = item.get("type", ATTRIBUTE_TYPE)
        rows.append(row)
    return rows


def _emit_by_type(type_name: str, key: str, value: Any) -> tuple[str, Any] | None:
    """(key, value) for a calendar / custom day / event template idiom, if lossless."""
    if type_name == "calendar" and key == "businessDays":
        days = emit_business_days(value)
        if days is not None and parse_business_days(days) == value:
            return key, days
    if type_name == "calendar" and key == "customDays":
        names = emit_names(value)
        if names is not None and parse_names(names) == value:
            return key, names
    if type_name == "customDay" and key == "observedRules":
        observed = emit_observed(value)
        if observed is not None and parse_observed(observed) == value:
            return "observed", observed
    if type_name == "universalEventTemplate" and key == "attributes":
        rows = emit_attributes(value)
        if rows is not None and parse_attributes(rows) == value:
            return key, rows
    return None


def _parse_by_type(type_name: str, key: str, value: Any) -> tuple[str, Any] | None:
    if type_name == "calendar" and key == "businessDays":
        return key, parse_business_days(value)
    if type_name == "calendar" and key == "customDays":
        return key, parse_names(value)
    if type_name == "customDay" and key == "observed":
        return "observedRules", parse_observed(value)
    if type_name == "universalEventTemplate" and key == "attributes":
        return key, parse_attributes(value)
    return None


# --- aliases, sub-object rows, ordering (data/rules/aliases.yaml) ---------------


@cache
def _alias_rules() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((data_root() / "rules" / "aliases.yaml").read_text())
    return raw


@cache
def aliases(type_name: str) -> dict[str, str]:
    """UAC field -> the name the compact style writes, for this type's own fields."""
    if type_name not in registry.load():
        return {}
    entry = registry.get(type_name)
    rules = _alias_rules()
    found = {**(rules.get("common") or {}), **((rules.get("types") or {}).get(type_name) or {})}
    # An alias never shadows a real field of the type.
    return {
        name: alias
        for name, alias in found.items()
        if entry.declares(name) and not entry.declares(alias)
    }


@cache
def sub_aliases(path: str) -> dict[str, str]:
    found: dict[str, str] = (_alias_rules().get("sub") or {}).get(path) or {}
    return found


@cache
def sub_order(path: str) -> tuple[str, ...]:
    return tuple((_alias_rules().get("order") or {}).get(path) or ())


@cache
def sub_pairs() -> frozenset[str]:
    return frozenset(_alias_rules().get("pairs") or ())


@cache
def sub_flags(path: str) -> dict[str, dict[str, str]]:
    """`{list name: {uac flag: word}}` for the rows at `path`."""
    found: dict[str, dict[str, str]] = (_alias_rules().get("flags") or {}).get(path) or {}
    return found


def emit_flags(row: dict[str, Any], path: str) -> dict[str, Any]:
    """The flags a row holds `true` as one list of words, where the first of them was."""
    for name, table in sub_flags(path).items():
        words = [word for field, word in table.items() if row.get(field) is True]
        if not words or name in row:
            continue
        out: dict[str, Any] = {}
        for key, value in row.items():
            if key in table and value is True:
                if name not in out:
                    out[name] = Flow(words)
            else:
                out[key] = value
        row = out
    return row


def parse_flags(row: dict[str, Any], path: str) -> dict[str, Any]:
    for name, table in sub_flags(path).items():
        if name not in row:
            continue
        written = row[name]
        words = [written] if isinstance(written, str) else written
        back = {word: field for field, word in table.items()}
        if not isinstance(words, list) or not all(w in back for w in words):
            raise IdiomError(f"{path}: {name} takes a list of {', '.join(back)}")
        out: dict[str, Any] = {}
        for key, value in row.items():
            if key != name:
                out[key] = value
                continue
            for word in words:
                if back[word] in row:
                    raise IdiomError(f"{path}: {name} and {back[word]} say the same thing")
                out[back[word]] = True
        row = out
    return row


def _rename(row: dict[str, Any], table: dict[str, str]) -> dict[str, Any]:
    return {table.get(k, k): v for k, v in row.items()}


def _reorder(row: dict[str, Any], first: tuple[str, ...]) -> dict[str, Any]:
    head = {k: row[k] for k in first if k in row}
    return {**head, **{k: v for k, v in row.items() if k not in head}}


def emit_rows(value: Any, path: str) -> Any:
    """One sub-object list or mapping, made readable: renamed, reordered, paired."""
    if isinstance(value, list):
        return [emit_rows(item, path) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, held in value.items():
        child = f"{path}.{key}"
        if child in sub_pairs():
            pairs = emit_pairs(held)
            out[key] = held if pairs is None or parse_pairs(pairs) != held else pairs
        else:
            out[key] = emit_rows(held, child) if isinstance(held, dict | list) else held
    return _reorder(_rename(emit_flags(out, path), sub_aliases(path)), sub_order(path))


def parse_rows(value: Any, path: str) -> Any:
    if isinstance(value, list):
        return [parse_rows(item, path) for item in value]
    if not isinstance(value, dict):
        return value
    back = {v: k for k, v in sub_aliases(path).items()}
    out: dict[str, Any] = {}
    for key, held in parse_flags(_rename(value, back), path).items():
        child = f"{path}.{key}"
        if child in sub_pairs():
            out[key] = parse_pairs(held)
        else:
            out[key] = parse_rows(held, child) if isinstance(held, dict | list) else held
    return out


# --- all of them -------------------------------------------------------------------------


def effective_aliases(
    type_name: str, body: dict[str, Any], ext: ExtensionMap | None
) -> dict[str, str]:
    """This object's aliases: an alias a real extension parameter claims is dropped."""
    table = aliases(type_name)
    if not table:
        return table
    claimed = set(ext.for_template(body.get("template"))) if ext is not None else set()
    return {name: alias for name, alias in table.items() if alias not in claimed}


def emit(type_name: str, body: dict[str, Any], ext: ExtensionMap | None = None) -> dict[str, Any]:
    """A flat standard-style body with every idiom that is lossless for it."""
    durations = duration_fields(type_name)
    members = member_lists(type_name)
    body = {k: emit_rows(v, k) if isinstance(v, dict | list) else v for k, v in body.items()}
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in members:
            names = emit_members(members[key], value)
            out[key] = value if names is None else names
        elif key in PAIR_FIELDS:
            pairs = emit_pairs(value)
            out[key] = value if pairs is None or parse_pairs(pairs) != value else pairs
        elif key in LABELLED:
            short = emit_labelled(value)
            out[key] = value if short is None else short
        elif key in durations and isinstance(value, str):
            out[key] = render_duration(value) or value
        elif key == "actions" and isinstance(value, dict):
            out[key] = emit_set_variables(emit_email_on(value))
        elif (typed := _emit_by_type(type_name, key, value)) is not None:
            out[typed[0]] = typed[1]
        else:
            out[key] = value
    if type_name == "calendar":
        out = emit_quarters(out)
    return _rename(emit_var_pairs(type_name, out), effective_aliases(type_name, body, ext))


def parse(
    type_name: str, body: dict[str, Any], where: str, ext: ExtensionMap | None = None
) -> dict[str, Any]:
    """Every idiom back to UAC fields. Raw UAC fields pass through."""
    table = effective_aliases(type_name, body, ext)
    body = _rename(body, {v: k for k, v in table.items()})
    body = {k: parse_rows(v, k) if isinstance(v, dict | list) else v for k, v in body.items()}
    body = parse_var_pairs(type_name, body)
    if type_name == "calendar":
        body = parse_quarters(body, where)
    durations = duration_fields(type_name)
    members = member_lists(type_name)
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in members:
            out[key] = parse_members(members[key], value)
        elif key in PAIR_FIELDS:
            out[key] = parse_pairs(value)
        elif key in LABELLED:
            out[key] = parse_labelled(value)
        elif key in durations:
            out[key] = parse_duration(value)
        elif key == "actions":
            out[key] = parse_email_on(value, where)
            if isinstance(out[key], dict) and "setVariableActions" in out[key]:
                sets = parse_set_variables(out[key]["setVariableActions"])
                out[key] = {**out[key], "setVariableActions": sets}
        elif (typed := _parse_by_type(type_name, key, value)) is not None:
            if typed[0] in out or (typed[0] != key and typed[0] in body):
                raise IdiomError(f"{where}: {key} and {typed[0]} say the same thing; write one")
            out[typed[0]] = typed[1]
        else:
            out[key] = value
    return out
