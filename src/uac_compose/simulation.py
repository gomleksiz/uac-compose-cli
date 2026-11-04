"""Simulations: the one kind with no name of its own.

The controller keys a simulation by its task, the workflow it is limited to
(none: every run of the task outside a simulated workflow's own row), and the
vertex in that workflow (`-1`: every vertex that runs the task). Measured on
a test controller (8.1, 2026-11-04):

- `GET /resources/simulation?taskname=T` answers the simulation with no
  workflow, also when T has others limited to workflows;
- with `workflowname` it needs `vertexid` too ("Required vertexid.");
- a task that does not exist answers 404, like an absent simulation.

A file keys each simulation by a handle (`handle`): the task name, or
"TASK in WORKFLOW", or "TASK in WORKFLOW (vertex N)". The handle is a label;
the identity is the `task`, `workflow` and `vertexId` fields, and `task`
defaults to the handle.
"""

from __future__ import annotations

from typing import Any

TYPE = "simulation"
EXPORT_TABLE = "ops_simulation"
#: Every vertex of the workflow that runs the task.
ANY_VERTEX = "-1"


def is_simulation(obj: dict[str, Any]) -> bool:
    """A stamped simulation, or one as the controller answers (no type of its own)."""
    return obj.get("type") == TYPE or obj.get("exportTable") == EXPORT_TABLE


def identity(obj: dict[str, Any]) -> tuple[str, str | None, str]:
    """(task, workflow or None, vertex id): what the controller keys it by."""
    task = str(obj.get("task") or "")
    workflow = obj.get("workflow") or None
    vertex = obj.get("vertexId")
    vertex = ANY_VERTEX if vertex in (None, "") else str(vertex)
    return task, (str(workflow) if workflow else None), vertex


def handle(obj: dict[str, Any]) -> str:
    """The key a simulation is written under: unique on a controller."""
    task, workflow, vertex = identity(obj)
    if workflow is None:
        return task
    shown = f"{task} in {workflow}"
    return shown if vertex == ANY_VERTEX else f"{shown} (vertex {vertex})"


def query(obj: dict[str, Any]) -> dict[str, str]:
    """`get` arguments for this simulation (`uac simulation get` names)."""
    task, workflow, vertex = identity(obj)
    if workflow is None:
        return {"taskname": task}
    return {"taskname": task, "workflowname": workflow, "vertexid": vertex}


def same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return identity(a) == identity(b)
