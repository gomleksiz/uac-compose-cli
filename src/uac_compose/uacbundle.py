"""UAC's own **Bundle**, read into one `.uac.yaml` file.

A Bundle is UAC's promotion unit: a named set of objects moved from one
controller to another. That is very nearly what a `.uac.yaml` file is, so this is a
reader rather than a second format.

**The word is overloaded, and the two meanings are kept apart.** Elsewhere in
this codebase a "bundle" is the `.uac.yaml` file; here it is UAC's object.

Three things the controller does for us, and the third is why we ask it rather
than walk the objects ourselves:

1. `bundle get` lists what the Bundle **declares** -- one workflow, say.
2. `bundle get-report` lists what it **resolves to**, with `followReferences`
   applied: that workflow, its members, and the credentials they name.
3. **Following references is the controller's rule, not ours.** It reaches a
   workflow's members and the credentials and scripts they name, and it stops:
   a global variable a task reads is not pulled in, because a `${name}` is
   resolved at run time and is not part of the definition. Guessing that
   boundary would mean reimplementing it and getting it subtly different;
   asking the report means the file holds exactly what a promotion carries.

`excludeOnExistence` says the same thing as `create_only:` -- create it there
if it is missing, never overwrite what is already there -- so it is read
straight across. UAC states it as a comma-joined list of type labels
("Calendars, Custom Days, Credentials"), which is why `EXCLUDE_SECTIONS` is a
table of labels rather than anything derived.
"""

from __future__ import annotations

from typing import Any

CLI_GROUP = "bundle"
CLI_NAME_ARG = "bundlename"

#: Report key -> `.uac.yaml` section, in the order a file creates them, so the file
#: reads the way it applies.
MEMBER_SECTIONS: dict[str, str] = {
    "bundleBusinessServices": "business_services",
    "bundleAgentClusters": "agent_clusters",
    "bundleCredentials": "credentials",
    "bundleVariables": "variables",
    "bundleVirtualResources": "virtual_resources",
    "bundleDatabaseConnections": "database_connections",
    "bundlePeoplesoftConnections": "peoplesoft_connections",
    "bundleSapConnections": "sap_connections",
    "bundleEmailConnections": "email_connections",
    "bundleCustomDays": "custom_days",
    "bundleCalendars": "calendars",
    "bundleScripts": "scripts",
    "bundleUniversalEventTemplates": "event_templates",
    "bundleTasks": "tasks",
    "bundleTriggers": "triggers",
}

#: What a Bundle may carry that a `.uac.yaml` file has no section for. Named out
#: loud rather than dropped: a promotion unit missing a piece is not that unit.
UNSUPPORTED_MEMBERS: dict[str, str] = {
    "bundleApplications": "application",
    "bundleEmailTemplates": "email template",
    "bundleOAuthClients": "OAuth client",
    "bundleReports": "report",
    "bundleSnmpManagers": "SNMP manager",
    "bundleUniversalTemplates": "universal template",
    "bundleWidgets": "widget",
}

#: `excludeOnExistence` label -> the section it protects, in UAC's own
#: spelling. Labels naming a kind this format has no section for are absent.
EXCLUDE_SECTIONS: dict[str, str] = {
    "Tasks": "tasks",
    "Triggers": "triggers",
    # measured: on 28 of 53 Bundles of a live controller
    "Agent Clusters": "agent_clusters",
    "Scripts": "scripts",
    "Calendars": "calendars",
    "Custom Days": "custom_days",
    "Credentials": "credentials",
    "Variables": "variables",
    "Virtual Resources": "virtual_resources",
    "Universal Event Templates": "event_templates",
    "Database Connections": "database_connections",
    "PeopleSoft Connections": "peoplesoft_connections",
    "SAP Connections": "sap_connections",
    "Email Connections": "email_connections",
}

#: Protected whether or not the Bundle says so. UAC has no "Business Services"
#: `excludeOnExistence` label to read, so the choice is ours: a service is
#: shared by every object filed under it, and a promotion that rewrote one
#: would rewrite a description belonging to somebody else's work. Creating a
#: missing one is all a promotion needs.
ALWAYS_CREATE_ONLY = ("business_services",)


def members(report: dict[str, Any]) -> list[tuple[str, str]]:
    """Every object the report resolves to, as `(section, name)`, in creation order."""
    found: list[tuple[str, str]] = []
    for key, section in MEMBER_SECTIONS.items():
        for row in report.get(key) or []:
            name = row.get("name") if isinstance(row, dict) else row
            if isinstance(name, str) and name and (section, name) not in found:
                found.append((section, name))
    return found


def unsupported(report: dict[str, Any]) -> list[str]:
    """What this Bundle carries that a `.uac.yaml` file cannot hold, named and counted."""
    return [
        f"{len(rows)} {label}(s)"
        for key, label in UNSUPPORTED_MEMBERS.items()
        if (rows := report.get(key))
    ]


def create_only(report: dict[str, Any]) -> list[str]:
    """`excludeOnExistence` as the sections a `create_only:` would name."""
    written = str(report.get("excludeOnExistence") or "")
    named = [
        section
        for label in (part.strip() for part in written.split(","))
        if label and label != "None" and (section := EXCLUDE_SECTIONS.get(label))
    ]
    return named + [s for s in ALWAYS_CREATE_ONLY if s not in named]
