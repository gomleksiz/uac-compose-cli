"""Short type names: `Unix` for `taskUnix`, resolved per section (SPEC §6.2).

Mechanical (strip `task`/`trigger`, split the camel case), with overrides for
the abbreviated trigger types. `File Monitor` is a task under `tasks:` and a
trigger under `triggers:`; raw UAC names always work.
"""

from __future__ import annotations

import re
from functools import cache

from uac_compose import registry

TRIGGER_OVERRIDES = {
    "triggerFm": "File Monitor",
    "triggerUm": "Universal Monitor",
    "triggerTm": "Task Monitor",
    "triggerEm": "Email Monitor",
    "triggerZm": "Zos Monitor",
    "triggerVm": "Variable Monitor",
    "triggerApplMonitor": "Application Monitor",
    "triggerTemp": "Temporary",
}

# An agent cluster is written with the words the controller itself shows for
# its type (`agent-cluster list` answers `Linux/Unix`, `Windows`, `IBM i`).
CLUSTER_OVERRIDES = {
    "unixAgentCluster": "Linux/Unix",
    "windowsAgentCluster": "Windows",
    "ibmiAgentCluster": "IBM i",
}

# Extra spellings accepted on input, as UAC's own UI writes them.
EXTRA_INPUT = {
    "timer": "taskSleep",
    "linux/unix": "taskUnix",
    "linux": "taskUnix",
    "z/os": "taskZos",
    "z/os monitor": "taskZosMonitor",
    "ibm i": "taskIbmi",
    "agent file monitor": "taskFileMonitor",
    "remote file monitor": "taskFtpFileMonitor",
    "file transfer": "taskFtp",
    "universal command": "taskUcmd",
}

SECTION_KINDS = {
    "tasks": ("task", "workflow"),
    "triggers": ("trigger",),
    "agent_clusters": ("agent_cluster",),
}


def alias_for(type_name: str) -> str:
    if type_name in TRIGGER_OVERRIDES:
        return TRIGGER_OVERRIDES[type_name]
    if type_name in CLUSTER_OVERRIDES:
        return CLUSTER_OVERRIDES[type_name]
    stem = re.sub(r"^(task|trigger)", "", type_name)
    return re.sub(r"(?<!^)(?=[A-Z])", " ", stem)


@cache
def _table(section: str) -> dict[str, str]:
    kinds = SECTION_KINDS[section]
    out: dict[str, str] = {}
    for name, entry in registry.load().items():
        if entry.kind in kinds:
            out[alias_for(name).lower()] = name
            out[name.lower()] = name
    for spelling, name in EXTRA_INPUT.items():
        if registry.load()[name].kind in kinds:
            out[spelling] = name
    return out


def to_uac(section: str, written: str) -> str | None:
    """The UAC type a `type:` names in this section, or None."""
    return _table(section).get(written.strip().lower())


def known(section: str) -> list[str]:
    return sorted({alias_for(t) for t in _table(section).values()})
