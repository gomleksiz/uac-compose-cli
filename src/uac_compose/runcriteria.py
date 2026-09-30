"""Workflow run criteria as one line (SPEC §6.4): `run_if` / `skip_if`.

    business day | holiday | custom day X | mon, fri | weekdays
    1st business day of month | every monday of month | nth 3 day of month
    last business day of month minus 1 day | ${V} != EU
    ... and ${V} = 1 | ... at run time

A line is used only when it parses back to exactly the stored record
(`line_for` checks); anything else stays a raw `runCriteria` row.
"""

from __future__ import annotations

import re
from typing import Any

RUN, SKIP = "Run Criteria", "Skip Criteria"
DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_DAY_WORDS = {
    **{d.lower(): d for d in DAYS},
    "sunday": "Sun",
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
}
_GROUPS = {"weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri"), "weekends": ("Sun", "Sat")}
MODES = ("businessDay", "holiday", "customDay", "specificDay", "complex", "variable")
OPERATORS = ("!=", ">=", "<=", "regex", "=", ">", "<")
_VOCAB = {
    "day": "Day",
    "business day": "Business Day",
    "year": "Year",
    "month": "Month",
    "week": "Week",
    "quarter": "Quarter",
    **{w: w.title() for w in _DAY_WORDS if len(w) > 3},
}
_ORDINAL = re.compile(r"^(\d{1,2})(st|nd|rd|th)$")
_ADJUST = re.compile(r"\s+(plus|minus)\s+(\d+)\s+(.+)$", re.IGNORECASE)
_AT = re.compile(r"\s+at\s+run\s+time$", re.IGNORECASE)


class CriteriaError(ValueError):
    pass


def _word(text: str) -> str:
    return _VOCAB.get(text.strip().lower(), text.strip())


def _plain(value: str) -> str:
    return value.lower() if _VOCAB.get(value.lower()) == value else value


# --- parse ----------------------------------------------------------------------


def parse(line: str, kind: str = RUN) -> dict[str, Any]:
    """One line -> the stored record, every mode flag stated."""
    rest = line.strip()
    if not rest:
        raise CriteriaError("an empty run criterion says nothing")
    out: dict[str, Any] = {"type": kind, **{m: False for m in MODES}}
    out.update({f"specificDay{d}": False for d in DAYS})
    at = _AT.search(rest)
    out["evaluateAt"] = "Run Time" if at else "Trigger Time"
    if at:
        rest = rest[: at.start()].strip()
    parts = re.split(r"\s+and\s+", rest, flags=re.IGNORECASE)
    try:
        for part in parts:
            out.update(_condition(part))
    except CriteriaError:
        if len(parts) == 1:
            raise
        out.update(_condition(rest))  # `and` inside a variable value
    if not out["complex"]:
        out["complexNoun"], out["complexQualifier"] = {"value": "Day"}, {"value": "Year"}
    return out


def _condition(text: str) -> dict[str, Any]:
    low = text.strip().lower()
    if low == "business day":
        return {"businessDay": True}
    if low == "holiday":
        return {"holiday": True}
    if low.startswith("custom day "):
        return {"customDay": True, "customDayChoice": text.strip()[len("custom day ") :].strip()}
    if re.search(r"\sof\s", low):
        return _complex(text.strip())
    days = _weekdays(low)
    if days:
        return {"specificDay": True, **{f"specificDay{d}": True for d in days}}
    return _variable(text.strip())


def _weekdays(low: str) -> list[str]:
    found: list[str] = []
    for token in (t for t in re.split(r"[,\s]+", low) if t):
        if token in _GROUPS:
            found += list(_GROUPS[token])
        elif token in _DAY_WORDS:
            found.append(_DAY_WORDS[token])
        else:
            return []
    return [d for d in DAYS if d in found]


def _complex(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"complex": True}
    adjust = _ADJUST.search(text)
    if adjust:
        out["complexAdjustment"] = adjust.group(1).title()
        out["complexAdjustmentAmount"] = int(adjust.group(2))
        out["complexAdjustmentType"] = _word(adjust.group(3))
        text = text[: adjust.start()].strip()
    left, _, right = re.split(r"(\sof\s)", text, maxsplit=1)
    words = left.split()
    if not words:
        raise CriteriaError(f"{text!r}: no day before `of`")
    head = words[0].lower()
    if head == "nth" and len(words) > 1 and words[1].isdigit():
        out["complexAdjective"], out["complexNthAmount"] = "Nth", int(words[1])
        words = words[2:]
    elif head in ("every", "last"):
        out["complexAdjective"] = head.title()
        words = words[1:]
    elif _ORDINAL.match(head):
        out["complexAdjective"] = head
        words = words[1:]
    nouns = [_word(p) for p in " ".join(words).split(",") if p.strip()]
    qualifiers = [_word(p) for p in right.split(",") if p.strip()]
    if not nouns or not qualifiers:
        raise CriteriaError(f"{text!r}: write `<day> of <period>`")
    out["complexNouns"] = [{"value": n} for n in nouns]
    out["complexNoun"] = {"value": nouns[0]}
    out["complexQualifiers"] = [{"value": q} for q in qualifiers]
    out["complexQualifier"] = {"value": qualifiers[0]}
    return out


def _variable(text: str) -> dict[str, Any]:
    for op in OPERATORS:
        pattern = rf"\s+{op}\s+" if op.isalpha() else rf"\s*{re.escape(op)}\s*"
        m = re.search(pattern, text)
        if not m:
            continue
        name, value = text[: m.start()].strip(), text[m.end() :].strip()
        ref = re.match(r"^\$\{([^}]+)\}$", name)
        name = ref.group(1) if ref else name
        if not name:
            raise CriteriaError(f"{text!r}: no variable before {op!r}")
        out: dict[str, Any] = {"variable": True, "variableName": name, "variableOp": op}
        if value:
            out["variableValue"] = value
        return out
    raise CriteriaError(f"cannot read run criterion {text!r}")


# --- render -----------------------------------------------------------------------


def render(row: dict[str, Any]) -> tuple[str, str] | None:
    """(`run_if`|`skip_if`, line) for an effective record, or None."""
    parts: list[str] = []
    if row.get("businessDay"):
        parts.append("business day")
    if row.get("holiday"):
        parts.append("holiday")
    if row.get("customDay"):
        if not row.get("customDayChoice"):
            return None
        parts.append(f"custom day {row['customDayChoice']}")
    if row.get("specificDay"):
        days = [d for d in DAYS if row.get(f"specificDay{d}")]
        if not days:
            return None
        parts.append(", ".join(d.lower() for d in days))
    if row.get("complex"):
        text = _render_complex(row)
        if text is None:
            return None
        parts.append(text)
    if row.get("variable"):
        if not row.get("variableName"):
            return None
        value = row.get("variableValue")
        parts.append(
            f"${{{row['variableName']}}} {row.get('variableOp') or '='} {value or ''}".rstrip()
        )
    if not parts:
        return None
    line = " and ".join(parts)
    if row.get("evaluateAt") == "Run Time":
        line += " at run time"
    key = {RUN: "run_if", SKIP: "skip_if"}.get(str(row.get("type") or RUN))
    return (key, line) if key else None


def _values(row: dict[str, Any], plural: str) -> list[str]:
    return [str(r.get("value")) for r in row.get(plural) or [] if isinstance(r, dict)]


def _render_complex(row: dict[str, Any]) -> str | None:
    nouns, quals = _values(row, "complexNouns"), _values(row, "complexQualifiers")
    if not nouns or not quals:
        return None
    adjective = str(row.get("complexAdjective") or "Every")
    head = f"nth {row.get('complexNthAmount')}" if adjective == "Nth" else adjective.lower()
    line = f"{head} {', '.join(_plain(n) for n in nouns)} of {', '.join(_plain(q) for q in quals)}"
    if str(row.get("complexAdjustment") or "None") != "None":
        line += (
            f" {str(row['complexAdjustment']).lower()} {row.get('complexAdjustmentAmount', 1)}"
            f" {str(row.get('complexAdjustmentType') or 'Day').lower()}"
        )
    return line


# --- the lossless check --------------------------------------------------------------


def drop_defaults(row: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not (k in default and default[k] == v)}


def line_for(row: dict[str, Any], default: dict[str, Any]) -> tuple[str, str] | None:
    """The line for a simplified row, only if it parses back to that row exactly."""
    rendered = render({**default, **row})
    if rendered is None:
        return None
    key, line = rendered
    try:
        back = parse(line, RUN if key == "run_if" else SKIP)
    except CriteriaError:
        return None
    return rendered if drop_defaults(back, default) == row else None
