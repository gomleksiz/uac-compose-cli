"""Workflows in compact form (SPEC §6.6).

    Nightly:
      type: Workflow
      tasks:
        Extract:
        Load orders: {task: Load, alias: Load orders, after: Extract}
        Notify:
          after: [{Load orders: success}, {Extract: failure}]
          condition: any
          run_if: business day
          position: [120, 40]

- The member key is a handle: the task name, or `Name (2)` with `task:` when a
  task sits in the workflow twice.
- Edges and run criteria are read by vertexId, never by task name.
- Members are written in execution order; `vertexId:` appears (on every member)
  only when the ids are not 2, 3, 4... Ids start at 2.
- A workflow with a variable edge condition is written raw
  (`workflowVertices` / `workflowEdges` / `runCriteria`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from uac_compose import runcriteria

VERTICES, EDGES, CRITERIA = "workflowVertices", "workflowEdges", "runCriteria"
MEMBERS = "tasks"
FIRST_VERTEX_ID = 2

CONDITIONS = {
    "success": "Success",
    "failure": "Failure",
    "success/failure": "Success/Failure",
    "approved": "Approved",
    "approval required": "Approval Required",
}
_WORD_OF = {v: k for k, v in CONDITIONS.items()}
_EXIT = re.compile(r"^exit code\s+(.+)$", re.IGNORECASE)
_MEMBER_KEYS = ("task", "alias", "after", "condition", "run_if", "skip_if", "position", "vertexId")


class WorkflowError(ValueError):
    pass


def canonical(obj: dict[str, Any]) -> dict[str, Any]:
    """Vertices, edges and criteria are sets to UAC: compare them in a fixed order."""
    out = dict(obj)
    for key in (VERTICES, EDGES, CRITERIA):
        if isinstance(out.get(key), list):
            out[key] = sorted(out[key], key=lambda r: json.dumps(r, sort_keys=True))
    return out


# --- conditions on an edge ------------------------------------------------------------


def render_condition(stored: Any) -> Any:
    """`{value: Failure}` -> `failure`; `{type: Exit Code, value: 0-9}` -> `exit code 0-9`."""
    if isinstance(stored, dict):
        if set(stored) == {"value"} and isinstance(stored["value"], str):
            value: str = stored["value"]
            return _WORD_OF.get(value, value)
        if set(stored) == {"type", "value"} and stored["type"] == "Exit Code":
            return f"exit code {stored['value']}"
    return stored


def parse_condition(written: Any) -> Any:
    if not isinstance(written, str):
        return written
    exit_code = _EXIT.match(written.strip())
    if exit_code:
        return {"type": "Exit Code", "value": exit_code.group(1).strip()}
    return {"value": CONDITIONS.get(written.strip().lower(), written.strip())}


# --- join expressions ---------------------------------------------------------------

_TOKEN = re.compile(r'"[^"]*"|\(|\)|\bAND\b|\bOR\b|[^\s()]+', re.IGNORECASE)
_BARE = re.compile(r"^[^\s()\"]+$")


def quoted(name: str) -> str:
    if _BARE.match(name) and name.upper() not in ("AND", "OR"):
        return name
    return '"' + name + '"'


def render_join(expr: Any, handle_of: dict[str, str]) -> str | None:
    if expr == {"logicalOperator": "Or"}:
        return "any"
    if not isinstance(expr, dict):
        return None
    return _render_node(expr, handle_of, top=True)


def _render_node(node: dict[str, Any], handle_of: dict[str, str], top: bool) -> str | None:
    if node.get("type") == "condition" and set(node) == {"type", "vertexId"}:
        handle = handle_of.get(str(node["vertexId"]))
        return quoted(handle) if handle and '"' not in handle else None
    want = {"conditions", "logicalOperator"} | (set() if top else {"type"})
    if set(node) != want or (not top and node.get("type") != "compoundCondition"):
        return None
    op = node.get("logicalOperator")
    if op not in ("And", "Or") or not node["conditions"]:
        return None
    parts = [_render_node(c, handle_of, False) for c in node["conditions"]]
    if any(p is None for p in parts):
        return None
    text = f" {op.upper()} ".join(p for p in parts if p)
    return text if top else f"({text})"


def parse_join(text: str, vertex_of: dict[str, str], where: str) -> dict[str, Any] | None:
    key = text.strip().lower()
    if key in ("all", "and"):
        return None
    if key in ("any", "or"):
        return {"logicalOperator": "Or"}
    tokens: list[str] = [str(t) for t in _TOKEN.findall(text) if t.strip()]
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def expr(nested: bool) -> dict[str, Any]:
        return binary("OR", lambda: binary("AND", factor, True), nested)

    def binary(word: str, lower: Any, nested: bool) -> dict[str, Any]:
        parts = [lower()]
        while (t := peek()) and t.upper() == word:
            take()
            parts.append(lower())
        if len(parts) == 1:
            return parts[0]  # type: ignore[no-any-return]
        node: dict[str, Any] = {"conditions": parts, "logicalOperator": word.capitalize()}
        return {"type": "compoundCondition", **node} if nested else node

    def factor() -> dict[str, Any]:
        t = peek()
        if t is None:
            raise WorkflowError(f"{where}: condition ended early")
        if t == "(":
            take()
            node = expr(True)
            if peek() != ")":
                raise WorkflowError(f"{where}: unbalanced parentheses in condition")
            take()
            if node.get("type") == "condition":  # `(A)`: UAC keeps a group of one
                return {"type": "compoundCondition", "conditions": [node], "logicalOperator": "And"}
            return node
        if t == ")" or t.upper() in ("AND", "OR"):
            raise WorkflowError(f"{where}: expected a member, found {t!r}")
        name = take()
        name = name[1:-1] if len(name) >= 2 and name[0] == name[-1] == '"' else name
        if name not in vertex_of:
            raise WorkflowError(f"{where}: condition names {name!r}, which is not a member")
        return {"type": "condition", "vertexId": vertex_of[name]}

    if not tokens:
        raise WorkflowError(f"{where}: empty condition")
    top = binary("OR", lambda: binary("AND", factor, True), False)
    if pos != len(tokens):
        raise WorkflowError(f"{where}: unexpected {tokens[pos]!r} in condition")
    if top.get("type") == "condition":
        return {"conditions": [top], "logicalOperator": "And"}
    return {k: v for k, v in top.items() if k != "type"}


# --- emit ----------------------------------------------------------------------------


def _number(text: Any) -> Any:
    """A stored coordinate as a number, if it prints back the same."""
    if not isinstance(text, str):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    number: int | float = int(value) if value.is_integer() else value
    return number if _coordinate(number) == text else None


def _coordinate(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _execution_order(vertices: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    ids = [str(v["vertexId"]) for v in vertices]
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        target, source = str(e["targetId"]["value"]), str(e["sourceId"]["value"])
        if target in preds:
            preds[target].append(source)
    done: list[str] = []
    waiting = list(ids)
    while waiting:
        ready = next(
            (i for i in waiting if all(p in done or p not in preds for p in preds[i])), waiting[0]
        )
        waiting.remove(ready)
        done.append(ready)
    return done


def emit_members(
    body: dict[str, Any], criteria_default: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A uac-style workflow body with `tasks:` members, or the body unchanged."""
    vertices, edges = body.get(VERTICES), body.get(EDGES) or []
    criteria = body.get(CRITERIA) or []
    if MEMBERS in body or not isinstance(vertices, list) or not vertices:
        return body
    try:
        members = _members(vertices, edges, criteria, criteria_default or {})
    except (KeyError, TypeError, ValueError):
        return body
    if members is None:
        return body
    ids = {str(v.get("vertexId")) for v in vertices}
    loose = [r for r in criteria if str(r.get("vertexId")) not in ids]
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == VERTICES:
            out[MEMBERS] = members
        elif key == CRITERIA:
            if loose:
                out[CRITERIA] = loose
        elif key != EDGES:
            out[key] = value
    return out


def _members(
    vertices: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
    default_crit: dict[str, Any],
) -> dict[str, Any] | None:
    for e in edges:
        cond = e.get("condition")
        if isinstance(cond, dict) and cond.get("type") == "Variable":
            return None  # SPEC §6.6: a variable condition -> the workflow is raw
    by_id = {str(v["vertexId"]): v for v in vertices}
    if len(by_id) != len(vertices):
        return None
    handle_of: dict[str, str] = {}
    used: set[str] = set()
    for v in vertices:
        task = v["task"]["value"]
        if not isinstance(task, str) or not task:
            return None
        handle, n = task, 1
        while handle in used:
            n += 1
            handle = f"{task} ({n})"
        used.add(handle)
        handle_of[str(v["vertexId"])] = handle
    order = _execution_order(vertices, edges)
    sequential = order == [str(i) for i in range(FIRST_VERTEX_ID, FIRST_VERTEX_ID + len(order))]

    members: dict[str, Any] = {}
    for vid in order:
        v = by_id[vid]
        handle = handle_of[vid]
        m: dict[str, Any] = {}
        if handle != v["task"]["value"]:
            m["task"] = v["task"]["value"]
        if "alias" in v:
            m["alias"] = v["alias"]
        after: list[Any] = []
        for e in edges:
            if str(e["targetId"]["value"]) != vid:
                continue
            source = handle_of[str(e["sourceId"]["value"])]
            extra = {k: x for k, x in e.items() if k not in ("sourceId", "targetId", "condition")}
            cond = render_condition(e["condition"]) if "condition" in e else None
            if not extra and cond == "success":
                after.append(source)
            elif not extra and isinstance(cond, str):
                after.append({source: cond})
            else:
                full = {**({"condition": cond} if "condition" in e else {}), **extra}
                after.append({source: full})
        if after:
            m["after"] = after[0] if len(after) == 1 and isinstance(after[0], str) else after
        if "conditionExpression" in v:
            join = render_join(v["conditionExpression"], handle_of)
            if join is None:
                m["conditionExpression"] = v["conditionExpression"]
            else:
                m["condition"] = join
        rows = [
            {k: x for k, x in r.items() if k not in ("task", "vertexId")}
            for r in criteria
            if str(r.get("vertexId")) == vid
        ]
        lines = [runcriteria.line_for(r, default_crit) for r in rows]
        keys = [ln[0] for ln in lines if ln]
        if rows and all(lines) and len(set(keys)) == len(keys):
            for key, line in (ln for ln in lines if ln):
                m[key] = line
        elif rows:
            m[CRITERIA] = rows
        x, y = _number(v.get("vertexX")), _number(v.get("vertexY"))
        if x is not None and y is not None:
            m["position"] = [x, y]
        else:
            m.update({k: v[k] for k in ("vertexX", "vertexY") if k in v})
        if not sequential:
            m["vertexId"] = int(vid) if vid.isdigit() and str(int(vid)) == vid else vid
        for k, x in v.items():
            if k not in ("task", "alias", "vertexId", "vertexX", "vertexY", "conditionExpression"):
                m[k] = x
        members[handle] = m or None
    return members


# --- parse ---------------------------------------------------------------------------


def _endpoint(member: dict[str, Any], handle: str, vid: str) -> dict[str, Any]:
    """An edge end as UAC stores it: task name, the member's alias, the vertex id."""
    out: dict[str, Any] = {"taskName": str(member.get("task", handle))}
    if "alias" in member:
        out["taskAlias"] = member["alias"]
    out["value"] = vid
    return out


def parse_members(
    body: dict[str, Any], where: str, criteria_default: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`tasks:` members -> workflowVertices / workflowEdges / runCriteria."""
    members = body.get(MEMBERS)
    if members is None:
        return body
    if not isinstance(members, dict):
        raise WorkflowError(f"{where}: a workflow's tasks: is a mapping of members")
    if VERTICES in body:
        raise WorkflowError(f"{where}: write members under tasks: or workflowVertices, not both")
    entries = {str(h): (e or {}) for h, e in members.items()}
    for h, e in entries.items():
        if not isinstance(e, dict):
            raise WorkflowError(f"{where}: member {h!r} must be a mapping")
    stated = [e for e in entries.values() if "vertexId" in e]
    if stated and len(stated) != len(entries):
        raise WorkflowError(f"{where}: vertexId on some members but not all")
    vertex_of: dict[str, str] = {}
    for n, (h, e) in enumerate(entries.items()):
        vid = str(e["vertexId"]) if stated else str(FIRST_VERTEX_ID + n)
        if vid in ("0", "1"):
            raise WorkflowError(f"{where}: {h}: vertexId must be 2 or more")
        vertex_of[h] = vid
    if len(set(vertex_of.values())) != len(vertex_of):
        raise WorkflowError(f"{where}: two members share a vertexId")

    vertices, edges, rows = [], [], list(body.get(CRITERIA) or [])
    for h, e in entries.items():
        vid, task = vertex_of[h], str(e.get("task", h))
        v: dict[str, Any] = {"task": {"value": task}, "vertexId": vid}
        if "alias" in e:
            v["alias"] = e["alias"]
        if "position" in e:
            pos = e["position"]
            if not (isinstance(pos, list) and len(pos) == 2):
                raise WorkflowError(f"{where}: {h}: position is [x, y]")
            v["vertexX"], v["vertexY"] = _coordinate(pos[0]), _coordinate(pos[1])
        if "condition" in e:
            join = parse_join(str(e["condition"]), vertex_of, f"{where}: {h}")
            if join is not None:
                v["conditionExpression"] = join
        for k, x in e.items():
            if k not in (*_MEMBER_KEYS, CRITERIA):
                v[k] = x
        vertices.append(v)
        after = e.get("after")
        items = [] if after is None else after if isinstance(after, list) else [after]
        for item in items:
            if isinstance(item, str):
                source, spec = item, None
            elif isinstance(item, dict) and len(item) == 1:
                ((source, spec),) = item.items()
            else:
                raise WorkflowError(f"{where}: {h}: after: takes names or {{name: condition}}")
            if source not in vertex_of:
                raise WorkflowError(f"{where}: {h}: after {source!r}, which is not a member")
            edge: dict[str, Any] = {}
            if spec is None:
                edge["condition"] = {"value": "Success"}
            elif isinstance(spec, dict) and not ({"value", "type"} >= set(spec)):
                edge.update({k: x for k, x in spec.items() if k != "condition"})
                if "condition" in spec:
                    edge["condition"] = parse_condition(spec["condition"])
            else:
                edge["condition"] = parse_condition(spec)
            edge["sourceId"] = _endpoint(entries[source], source, vertex_of[source])
            edge["targetId"] = _endpoint(e, h, vid)
            edges.append(edge)
        default = criteria_default or {}
        for key, kind in (("run_if", runcriteria.RUN), ("skip_if", runcriteria.SKIP)):
            if key in e:
                try:
                    row = runcriteria.drop_defaults(runcriteria.parse(str(e[key]), kind), default)
                except runcriteria.CriteriaError as error:
                    raise WorkflowError(f"{where}: {h}: {error}") from error
                rows.append({**row, "task": task, "vertexId": vid})
        for raw in e.get(CRITERIA) or []:
            rows.append({**raw, "task": task, "vertexId": vid})

    out = {k: x for k, x in body.items() if k not in (MEMBERS, CRITERIA)}
    out[VERTICES] = vertices
    if edges:
        out[EDGES] = edges
    if rows:
        out[CRITERIA] = rows
    return out
