"""Which objects an object names (data/rules/references.yaml)."""

from __future__ import annotations

import re
from functools import cache
from typing import Any

import yaml

from uac_compose.paths import data_root


@cache
def _table() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((data_root() / "rules" / "references.yaml").read_text())
    return raw


def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("value")
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, str) and v and "${" not in v]


def _walk(node: Any, path: list[str]) -> list[Any]:
    """Every value at a dotted path, stepping through dicts and lists alike."""
    if not path:
        return [node]
    head, rest = path[0], path[1:]
    if isinstance(node, list):
        return [v for item in node for v in _walk(item, path)]
    if isinstance(node, dict) and head in node:
        return _walk(node[head], rest)
    return []


def named(type_name: str, obj: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(field, kind, name) for every object this one names, in field order.

    Workflow members are not here: `workflow.member_tasks` reads them.
    """
    table = _table()
    out: list[tuple[str, str, str]] = []
    by_type = table.get("by_type") or {}
    slots = table.get("slots") or {}
    for field_name, value in obj.items():
        kind = table["fields"].get(field_name) or by_type.get(field_name, {}).get(type_name)
        if not kind and (slot := re.fullmatch(r"(\D+)\d+", field_name)):
            kind = slots.get(slot.group(1))
        if kind:
            out += [(field_name, kind, n) for n in _names(value)]
    for path, kind in (table.get("sub") or {}).items():
        if path.split(".")[0] in obj:
            for value in _walk(obj, path.split(".")):
                out += [(path, kind, n) for n in _names(value)]
    return out


def slot_kinds() -> dict[str, str]:
    """Extension slot prefix (`credentialField`) -> the kind its value names."""
    return dict(_table().get("slots") or {})


def refused_when_missing() -> frozenset[str]:
    """Reference paths whose unknown name makes the controller refuse the object."""
    return frozenset(_table().get("refused_when_missing") or ())


def member_tasks(obj: dict[str, Any]) -> list[str]:
    """The task names a workflow's vertices name."""
    out: list[str] = []
    for vertex in obj.get("workflowVertices") or []:
        task = vertex.get("task") if isinstance(vertex, dict) else None
        name = task.get("value") if isinstance(task, dict) else None
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return out


Renames = dict[tuple[str, str], str]
"""(kind, old name) -> new name."""


def _parents(node: Any, path: list[str]) -> list[dict[str, Any]]:
    """The dicts at a dotted path, stepping through dicts and lists alike."""
    if isinstance(node, list):
        return [d for item in node for d in _parents(item, path)]
    if not isinstance(node, dict):
        return []
    if not path:
        return [node]
    return _parents(node.get(path[0]), path[1:]) if path[0] in node else []


def rename(
    type_name: str,
    obj: dict[str, Any],
    renames: Renames,
    slot_kinds: dict[str, str] | None = None,
) -> list[str]:
    """Point every name `obj` holds for a renamed object at its new name.

    Changes `obj` in place and says each change. The same table as `named`
    decides which fields hold names, plus the workflow vertices; `slot_kinds`
    gives the kind of a Universal task's parameters written by name.
    """
    if not renames:
        return []
    table = _table()
    by_type = table.get("by_type") or {}
    slots = table.get("slots") or {}
    notes: list[str] = []

    def fix(value: Any, kind: str, where: str) -> Any:
        if isinstance(value, dict) and "value" in value:
            return {**value, "value": fix(value["value"], kind, where)}
        if isinstance(value, list):
            return [fix(v, kind, where) for v in value]
        new = renames.get((kind, value)) if isinstance(value, str) else None
        if new is None:
            return value
        notes.append(f"{where}: {value!r} -> {new!r} (renamed in this file)")
        return new

    for field_name in list(obj):
        kind = table["fields"].get(field_name) or by_type.get(field_name, {}).get(type_name)
        if not kind and (slot := re.fullmatch(r"(\D+)\d+", field_name)):
            kind = slots.get(slot.group(1))
        kind = kind or (slot_kinds or {}).get(field_name)
        if kind:
            obj[field_name] = fix(obj[field_name], kind, field_name)
    for path, kind in (table.get("sub") or {}).items():
        *head, last = path.split(".")
        for parent in _parents(obj, head):
            if last in parent:
                parent[last] = fix(parent[last], kind, path)
    for vertex in obj.get("workflowVertices") or []:
        if isinstance(vertex, dict) and isinstance(vertex.get("task"), dict):
            was = vertex["task"].get("value")
            if isinstance(was, str) and ("tasks", was) in renames:
                # the edges and run criteria that name this vertex follow too
                fields, _, _ = swap_task(
                    {k: obj[k] for k in ("workflowEdges", "runCriteria") if k in obj}
                    | {"workflowVertices": [vertex]},
                    was,
                    renames[("tasks", was)],
                )
                obj.update({k: v for k, v in fields.items() if k != "workflowVertices"})
                now = renames[("tasks", was)]
                notes.append(f"workflow vertex: {was!r} -> {now!r} (renamed in this file)")
                vertex["task"] = fields["workflowVertices"][0]["task"]
    return notes


def swap_task(
    workflow: dict[str, Any], old: str, new: str
) -> tuple[dict[str, Any], int, dict[str, int]]:
    """A workflow's vertices that run `old` run `new`, and everything that names them.

    A workflow names a vertex's task three times: the vertex's `task`, the
    `taskName` of each edge end (`sourceId` / `targetId`, beside the vertex
    id), and the `task` of each run/skip criteria row (beside `vertexId`).
    Changing only the vertex was refused, measured live: "Task with name ...,
    vertex id "3", and id ... not found in workflow; Task Run Criteria is
    invalid." Returns (the three fields, vertices swapped, rows that followed).
    """
    vertices = [dict(v) for v in workflow.get("workflowVertices") or []]
    ids: set[str] = set()
    for vertex in vertices:
        task = vertex.get("task")
        if isinstance(task, dict) and task.get("value") == old:
            vertex["task"] = {**task, "value": new}
            ids.add(str(vertex.get("vertexId")))
    fields: dict[str, Any] = {"workflowVertices": vertices}
    follow = {"workflowEdges": 0, "runCriteria": 0}
    if not ids:
        return fields, 0, follow
    edges = [
        {k: (dict(v) if isinstance(v, dict) else v) for k, v in e.items()}
        for e in workflow.get("workflowEdges") or []
        if isinstance(e, dict)
    ]
    for edge in edges:
        for end in ("sourceId", "targetId"):
            ref = edge.get(end)
            if isinstance(ref, dict) and str(ref.get("value")) in ids:
                ref["taskName"] = new
                follow["workflowEdges"] += 1
    rows = [dict(r) for r in workflow.get("runCriteria") or [] if isinstance(r, dict)]
    for row in rows:
        if str(row.get("vertexId")) in ids and row.get("task") == old:
            row["task"] = new
            follow["runCriteria"] += 1
    if "workflowEdges" in workflow:
        fields["workflowEdges"] = edges
    if "runCriteria" in workflow:
        fields["runCriteria"] = rows
    return fields, len(ids), follow
