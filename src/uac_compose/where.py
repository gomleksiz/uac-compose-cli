"""Field filters: which of the named objects a step or an export takes.

Two ways to write one filter, both ending as a list of `Test`s that must all
hold:

    where: {type: linux, agent: 'stone*', command: {regex: 'sleep \\d+'}}
    Task.find(name='*sleep*').where(type=linux).where(agent='stone*')

| YAML                     | chain            | holds when                 |
|--------------------------|------------------|----------------------------|
| `agent: 'stone*'`        | `agent='stone*'` | a wildcard or equal value  |
| `agent: [A, B]`          | `agent=[A, B]`   | any of them                |
| `agent: {not: X}`        | `agent!=X`       | X does not hold            |
| `command: {regex: R}`    | `command~R`      | the regex is found         |
| `command: {contains: T}` | `command*=T`     | the text is in it          |

Case is ignored everywhere. A field is a UAC field name or a dotted path
through rows (`actions.setVariableActions.variableName`): it holds when any
value on the path does. `type` takes the words `-t` takes (`linux`, `timer`).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from uac_compose import typealias


class WhereError(ValueError):
    """A filter that cannot be read."""


@dataclass(frozen=True)
class Test:
    field: str
    op: str
    """match, any, not, regex, contains."""
    value: Any

    def __str__(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


OPS = ("not", "regex", "contains")


def parse(mapping: Any) -> list[Test]:
    """The YAML form: field -> value, list, or `{not|regex|contains: ...}`."""
    if not isinstance(mapping, dict) or not mapping:
        raise WhereError("`where:` is a mapping of field -> value")
    return [_test(str(k), v) for k, v in mapping.items()]


def _test(field_name: str, value: Any) -> Test:
    if isinstance(value, list):
        if not value or any(isinstance(v, (dict, list)) for v in value):
            raise WhereError(f"{field_name}: a list holds plain values")
        return Test(field_name, "any", [str(v) for v in value])
    if isinstance(value, dict):
        if len(value) != 1 or next(iter(value)) not in OPS:
            raise WhereError(
                f"{field_name}: use one of {{not: ...}}, {{regex: ...}}, {{contains: ...}}"
            )
        op, inner = next(iter(value.items()))
        if op == "not":
            return Test(field_name, "not", _test(field_name, inner))
        if op == "regex":
            try:
                re.compile(str(inner))
            except re.error as error:
                raise WhereError(
                    f"{field_name}: {inner!r} is not a valid regex: {error}"
                ) from error
        return Test(field_name, op, str(inner))
    if value is None:
        return Test(field_name, "match", "")
    return Test(field_name, "match", str(value))


def to_yaml(tests: list[Test]) -> dict[str, Any]:
    """The YAML form of the tests, as a step's `where:` writes it: `parse` reversed."""
    out: dict[str, Any] = {}
    for test in tests:
        if test.field in out:
            raise WhereError(
                f"two tests on {test.field!r}: a `where:` holds one per field; combine "
                "them, e.g. a list [a, b] or one regex"
            )
        out[test.field] = _yaml_value(test)
    return out


def _yaml_value(test: Test) -> Any:
    if test.op == "not":
        return {"not": _yaml_value(test.value)}
    if test.op in ("regex", "contains"):
        return {test.op: test.value}
    return list(test.value) if test.op == "any" else test.value


# --- the chained form -----------------------------------------------------------------

_CALL = re.compile(r"\s*\.?\s*(\w+)\s*\(")
_TERM = re.compile(r"^\s*([\w.]+)\s*(!=|~=|~|\*=|=)\s*(.*?)\s*$", re.DOTALL)


@dataclass(frozen=True)
class Query:
    kind: str
    """The word before `.find`: Task, Variable, Credential..."""
    names: list[str]
    tests: list[Test]


def parse_query(text: str) -> Query:
    """`Task.find(name='*sleep*').where(type=linux).where(agent!=x)`."""
    head = re.match(r"\s*(\w+)", text)
    if head is None:
        raise WhereError(f"{text!r}: start with the kind, as in Task.find(...)")
    kind, rest = head.group(1), text[head.end() :]
    names: list[str] = []
    tests: list[Test] = []
    while rest.strip():
        call = _CALL.match(rest)
        if call is None or call.group(1) not in ("find", "where"):
            raise WhereError(f"{rest.strip()!r}: expected .find(...) or .where(...)")
        args, rest = _balanced(rest[call.end() :])
        for term in _split(args):
            if call.group(1) == "find" and not _TERM.match(term):
                names.append(_value(term))  # find('*sleep*')
                continue
            found = _TERM.match(term)
            if found is None:
                raise WhereError(f"{term!r}: write field=value, !=, ~ (regex) or *= (contains)")
            field_name, op, raw = found.groups()
            value = _value(raw)
            if field_name == "name" and op == "=" and call.group(1) == "find":
                names += value if isinstance(value, list) else [value]
            else:
                tests.append(_term_test(field_name, op, value))
    return Query(kind, names, tests)


def _term_test(field_name: str, op: str, value: Any) -> Test:
    if op == "=":
        return _test(field_name, value)
    if op == "!=":
        return Test(field_name, "not", _test(field_name, value))
    if op in ("~", "~="):
        return _test(field_name, {"regex": value})
    return _test(field_name, {"contains": value})


def parse_terms(terms: list[str]) -> list[Test]:
    """`export --where 'agent=stone*'`: one test per term, as inside `.where(...)`.

    The whole term is the one test, so a regex may hold `(`, `)` and `,`:
    `name~(19|20)\\d{2,4}`."""
    tests: list[Test] = []
    for term in terms:
        found = _TERM.match(term)
        if found is None:
            raise WhereError(f"{term!r}: write field=value, !=, ~ (regex) or *= (contains)")
        field_name, op, raw = found.groups()
        tests.append(_term_test(field_name, op, _value(raw)))
    return tests


def _balanced(text: str) -> tuple[str, str]:
    """The arguments up to the `)` that closes the call, and what follows it."""
    depth, quote = 0, ""
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            if depth == 0 and c == ")":
                return text[:i], text[i + 1 :]
            depth -= 1
    raise WhereError("a call is not closed with `)`")


def _split(args: str) -> list[str]:
    out, depth, quote, start = [], 0, "", 0
    for i, c in enumerate(args):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(args[start:i])
            start = i + 1
    out.append(args[start:])
    return [t for t in (t.strip() for t in out) if t]


def _value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_value(v) for v in _split(raw[1:-1])]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


# --- testing an object ----------------------------------------------------------------


def values(obj: Any, path: list[str]) -> list[Any]:
    """Every value on a dotted path, through lists of rows: what a test sees."""
    if isinstance(obj, list):
        return [v for item in obj for v in values(item, path)]
    if not path:
        if isinstance(obj, dict) and "value" in obj:  # a controller-labelled field
            return [obj["value"]]
        return [obj]
    if isinstance(obj, dict) and path[0] in obj:
        return values(obj[path[0]], path[1:])
    return []


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _one(test: Test, value: Any, section: str) -> bool:
    text = _text(value)
    if test.op == "regex":
        return re.search(str(test.value), text, re.IGNORECASE) is not None
    if test.op == "contains":
        return str(test.value).lower() in text.lower()
    patterns = test.value if test.op == "any" else [test.value]
    for pattern in patterns:
        if test.field == "type" and section in typealias.SECTION_KINDS:
            # `linux`, `Timer`, `taskUnix`: the words -t takes, or a wildcard on them
            wanted = typealias.to_uac(section, pattern)
            if wanted is not None and wanted == text:
                return True
            if fnmatch.fnmatchcase(typealias.alias_for(text).lower(), pattern.lower()):
                return True
        if fnmatch.fnmatchcase(text.lower(), pattern.lower()):
            return True
    return False


def holds(tests: list[Test], obj: dict[str, Any], section: str) -> bool:
    """Every test holds on `obj`, an object as the controller answers it."""
    return all(_holds(t, obj, section) for t in tests)


def _holds(test: Test, obj: dict[str, Any], section: str) -> bool:
    if test.op == "not":
        return not _holds(test.value, obj, section)
    if test.field == "name":
        found = [str(obj.get("scriptName") or obj.get("name") or "")]
    else:
        found = values(obj, test.field.split(".")) or [None]
    return any(_one(test, v, section) for v in found)
