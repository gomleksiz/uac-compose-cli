"""Agent clusters and user groups.

No corpus holds either kind, so these objects are invented in the shape a live
controller answers with (measured: 35 clusters and 104 groups on one
controller, 5 and 3 on another). The live measurements themselves are in
docs/LESSONS.md; the invariants were checked over every one of those objects.
"""

from __future__ import annotations

from typing import Any

import pytest

from uac_compose import idioms, references, registry, typealias, validate, yamlio
from uac_compose.compile import Planned, compile_object
from uac_compose.core.inflate import inflate
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.style import compact
from uac_compose.transport import KIND_BY_KEY, kind_for_type, name_arg
from uac_compose.workflow import canonical

CLUSTER: dict[str, Any] = {
    "type": "unixAgentCluster",
    "name": "Batch Cluster",
    "sysId": "c0",
    "exportTable": "ops_unix_agent_cluster",
    "distribution": "Round Robin",
    "agentLimitAmount": 10,
    "agentLimitType": "Unlimited",
    "limitAmount": 5,
    "limitType": "Unlimited",
    "ignoreInactiveAgents": False,
    "ignoreSuspendedAgents": False,
    "strictBsrvcMembership": False,
    "notifications": {"emailNotifications": [], "snmpNotifications": []},
    "opswiseGroups": [],
    # runtime state: never definition
    "currentCount": 0,
    "lastAgentUsed": "agent-a",
    "resolutionStatus": "None",
    "suspended": False,
    "agents": [
        {
            "sysId": "m1",
            "agentName": "agent-a",
            "type": "Linux/Unix",
            "membershipSuspended": False,
            "membershipDecommissioned": False,
            "membershipSuspendedOn": None,
            "membershipResumedOn": None,
        },
        {"sysId": "m2", "agentName": "agent-b", "type": "Linux/Unix"},
    ],
}

GROUP: dict[str, Any] = {
    "type": "userGroup",
    "name": "Operators",
    "sysId": "g0",
    "ctrlNavigationVisibility": False,
    "description": "people who run things",
    "parent": "Everyone",
    "groupMembers": [
        {"sysId": "u1", "user": {"name": "Ada Lovelace", "value": "ada"}},
        {"sysId": "u2", "user": {"name": "Alan Turing", "value": "alan"}},
    ],
    "groupRoles": [{"sysId": "r1", "role": {"description": "Some role", "value": "ops_a"}}],
    "permissions": [
        {
            "sysId": "p1",
            "permissionType": "Task",
            "nameWildcard": "*",
            "allGroups": False,
            "defaultGroup": True,
            "notGroups": False,
            "opCreate": False,
            "opDelete": False,
            "opExecute": True,
            "opRead": True,
            "opUpdate": False,
            "commands": "ALL",
            "opswiseGroups": ["Finance"],
        }
    ],
}


@pytest.fixture(scope="module")
def ts() -> TemplateSet:
    return TemplateSet("8.0.2.0")


# --- plumbing ---------------------------------------------------------------------


def test_the_three_cluster_types_route_to_one_section() -> None:
    for type_name in ("unixAgentCluster", "windowsAgentCluster", "ibmiAgentCluster"):
        assert type_name in registry.load()
        assert kind_for_type(type_name) is KIND_BY_KEY["agent_clusters"]
    assert kind_for_type("userGroup") is KIND_BY_KEY["user_groups"]


def test_user_group_get_takes_groupname() -> None:
    """Measured: `uac user-group get groupname=...`, not the default `usergroupname`."""
    assert name_arg("user-group") == "groupname"
    assert name_arg("agent-cluster") == "agentclustername"


def test_cluster_types_are_written_as_the_controller_shows_them() -> None:
    assert typealias.to_uac("agent_clusters", "Linux/Unix") == "unixAgentCluster"
    assert typealias.to_uac("agent_clusters", "IBM i") == "ibmiAgentCluster"
    assert typealias.to_uac("agent_clusters", "windowsAgentCluster") == "windowsAgentCluster"
    # the same words stay a task type under `tasks:`
    assert typealias.to_uac("tasks", "Linux/Unix") == "taskUnix"


def test_runtime_state_is_not_definition() -> None:
    flat = normalize(CLUSTER)
    for name in ("currentCount", "lastAgentUsed", "resolutionStatus", "suspended"):
        assert name not in flat
    assert flat["agents"][0] == {"agentName": "agent-a", "type": "Linux/Unix"}


def test_the_shipped_defaults_are_the_controllers_own(ts: TemplateSet) -> None:
    """Measured by creating a cluster with a name only and reading it back."""
    unix = ts.get("unixAgentCluster")
    assert unix["distribution"] == "Any" and unix["limitAmount"] == 5
    assert ts.get("windowsAgentCluster") == unix == ts.get("ibmiAgentCluster")
    assert ts.get("userGroup") == {"ctrlNavigationVisibility": False}
    assert ts.sub_templates["permissions"]["defaultGroup"] is True


# --- the invariants ----------------------------------------------------------------


@pytest.mark.parametrize("obj", [CLUSTER, GROUP], ids=["cluster", "group"])
def test_round_trip(obj: dict[str, Any], ts: TemplateSet) -> None:
    t, subs = ts.get(obj["type"]), ts.sub_templates
    assert inflate(simplify(obj, t, subs), t, subs) == normalize(obj)


@pytest.mark.parametrize("obj", [CLUSTER, GROUP], ids=["cluster", "group"])
def test_compile_emit_is_simplify(obj: dict[str, Any], ts: TemplateSet) -> None:
    section, name, body, ok = compact.emit_object(obj, obj["type"], ts)
    assert ok
    back = yamlio.load(yamlio.dump({section: {name: body}}))[section][name]
    got = compile_object(section, name, back, ts)
    stamped = kind_for_type(obj["type"]).stamp(obj)
    want = simplify(stamped, ts.get(obj["type"]), ts.sub_templates, extensions=ts.extensions)
    assert canonical(idioms.without_labels(got)) == canonical(idioms.without_labels(want))


def test_a_cluster_reads_as_a_list_of_agent_names(ts: TemplateSet) -> None:
    section, _, body, _ = compact.emit_object(CLUSTER, "unixAgentCluster", ts)
    assert section == "agent_clusters"
    assert body == {
        "type": "Linux/Unix",
        "agents": ["agent-a", "agent-b"],
        "distribution": "Round Robin",
    }


def test_a_group_reads_as_lists_of_names(ts: TemplateSet) -> None:
    _, _, body, _ = compact.emit_object(GROUP, "userGroup", ts)
    assert body["groupMembers"] == ["ada", "alan"]
    assert body["groupRoles"] == ["ops_a"]
    # a permission row keeps only what differs from the controller's own row,
    # its type first and its true operations as one list
    assert body["permissions"] == [
        {
            "type": "Task",
            "ops": ["read", "execute"],
            "nameWildcard": "*",
            "allGroups": False,
            "commands": "ALL",
            "opswiseGroups": ["Finance"],
        }
    ]


def test_a_member_row_with_more_than_its_name_stays_raw() -> None:
    """An idiom is used only when lossless (rule 4)."""
    spec = idioms.member_lists("userGroup")["groupMembers"]
    odd = [{"user": {"name": "Ada", "value": "ada"}, "note": "x"}]
    assert idioms.emit_members(spec, odd) is None
    assert idioms.parse_members(spec, ["ada"]) == [{"user": {"value": "ada"}}]


# --- references ------------------------------------------------------------------


def test_what_each_kind_names() -> None:
    named = {(k, n) for _, k, n in references.named("userGroup", normalize(GROUP))}
    assert named == {
        ("users", "ada"),
        ("users", "alan"),
        ("user_groups", "Everyone"),
        ("business_services", "Finance"),
    }
    named = {(k, n) for _, k, n in references.named("unixAgentCluster", normalize(CLUSTER))}
    assert named == {("agents", "agent-a"), ("agents", "agent-b")}
    approval = {"approvalCriteria": {"criteria": [{"group": "Operators"}]}}
    assert ("user_groups", "Operators") in {
        (k, n) for _, k, n in references.named("taskApproval", approval)
    }


def test_an_unknown_member_stops_the_import_before_it_writes() -> None:
    """Measured live: the controller refuses the WHOLE group or cluster for one."""
    planned = [
        Planned(
            "user_groups",
            "Operators",
            compile_object(
                "user_groups", "Operators", {"groupMembers": ["ada", "ghost"], "parent": "Nowhere"}
            ),
        ),
    ]
    known = {"users": {"ada"}, "user_groups": set()}
    findings = validate.references_(planned, known)
    levels = {f.message.split(" names ")[1].split(",")[0]: f.level for f in findings}
    assert levels["user 'ghost'"] == "error"
    assert levels["user_group 'Nowhere'"] == "warning"  # not measured as refused


def test_a_parent_group_is_created_before_its_child() -> None:
    """The file lists the child first; the controller needs the parent to exist."""
    from uac_compose.compile import creation_order

    child = Planned("user_groups", "Child", {"type": "userGroup", "name": "Child", "parent": "Mum"})
    mum = Planned("user_groups", "Mum", {"type": "userGroup", "name": "Mum"})
    other = Planned("user_groups", "Other", {"type": "userGroup", "name": "Other"})
    assert [p.name for p in creation_order([child, other, mum])] == ["Other", "Mum", "Child"]
