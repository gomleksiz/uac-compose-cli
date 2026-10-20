"""Add agent clusters, user groups and webhooks to data/registry/types.yaml.

Throwaway, like distill_docs.py. The registry is generated in uac_model from
data/openapi.json (src/uac_ai/model/registry.py); these kinds were never in
its table of singleton schemas, so they were never copied here. This walks the
spec exactly as that generator does -- `allOf` for fields and scalar types,
`$ref` recursion for sub-object fields -- so an entry made here is the entry
uac_model would make. The same rows belong in uac_model's table, so the next
copy of data/ does not drop them.

    uv run python scripts/add_registry_types.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCALAR_TYPES = {"string", "integer", "boolean", "number"}

# schema -> (kind, type, export table); the export tables are what the
# controller stamps on `list-advanced` answers (measured on a live controller).
NEW: dict[str, tuple[str, str, str]] = {
    "UnixAgentClusterWsData": ("agent_cluster", "unixAgentCluster", "ops_unix_agent_cluster"),
    "WindowsAgentClusterWsData": (
        "agent_cluster",
        "windowsAgentCluster",
        "ops_windows_agent_cluster",
    ),
    "IbmiAgentClusterWsData": ("agent_cluster", "ibmiAgentCluster", "ops_ibmi_agent_cluster"),
    "UserGroupWsData": ("user_group", "userGroup", "ops_user_group"),
    "WebhookWsData": ("webhook", "webhook", "ops_webhook"),
}


def _ref(part: dict[str, Any]) -> str | None:
    ref = part.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def properties(name: str, schemas: dict[str, Any], seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if name in seen or name not in schemas:
        return set()
    seen.add(name)
    schema = schemas[name]
    found = set(schema.get("properties") or {})
    for part in schema.get("allOf") or []:
        if (ref := _ref(part)) is not None:
            found |= properties(ref, schemas, seen)
        found |= set(part.get("properties") or {})
    return found


def scalar_types(
    name: str, schemas: dict[str, Any], seen: set[str] | None = None
) -> dict[str, str]:
    seen = seen if seen is not None else set()
    if name in seen or name not in schemas:
        return {}
    seen.add(name)
    schema = schemas[name]
    found: dict[str, str] = {}
    for part in schema.get("allOf") or []:
        if (ref := _ref(part)) is not None:
            found |= scalar_types(ref, schemas, seen)
        found |= _declared(part.get("properties") or {})
    return found | _declared(schema.get("properties") or {})


def _declared(props: dict[str, Any]) -> dict[str, str]:
    return {
        n: str(d["type"])
        for n, d in props.items()
        if isinstance(d, dict) and d.get("type") in SCALAR_TYPES
    }


def sub_properties(
    name: str, schemas: dict[str, Any], seen: set[str] | None = None
) -> dict[str, set[str]]:
    seen = seen if seen is not None else set()
    if name in seen or name not in schemas:
        return {}
    seen.add(name)
    schema = schemas[name]
    props: dict[str, Any] = dict(schema.get("properties") or {})
    found: dict[str, set[str]] = {}
    for part in schema.get("allOf") or []:
        if (ref := _ref(part)) is not None:
            found |= sub_properties(ref, schemas, seen)
        props |= dict(part.get("properties") or {})
    for prop, definition in props.items():
        target = definition.get("items") if definition.get("type") == "array" else definition
        child = _ref(target or {})
        if child is None:
            continue
        names = properties(child, schemas)
        if not names:
            continue
        found[prop] = names
        for deeper, deeper_names in sub_properties(child, schemas, set(seen)).items():
            found[f"{prop}.{deeper}"] = deeper_names
    return found


def parents(name: str, schemas: dict[str, Any]) -> list[str]:
    out: list[str] = []
    node = name
    while True:
        refs = [r for p in schemas.get(node, {}).get("allOf") or [] if (r := _ref(p))]
        if not refs:
            return out
        node = refs[0]
        out.append(node)


def main() -> None:
    schemas = json.loads((ROOT / "data" / "openapi.json").read_text())["components"]["schemas"]
    path = ROOT / "data" / "registry" / "types.yaml"
    doc = yaml.safe_load(path.read_text())
    for schema, (kind, type_name, table) in NEW.items():
        doc["types"][type_name] = {
            "export_table": table,
            "field_types": dict(sorted(scalar_types(schema, schemas).items())),
            "fields": sorted(properties(schema, schemas)),
            "kind": kind,
            "observed_only": False,
            "parents": parents(schema, schemas),
            "schema_name": schema,
            "sub_fields": {
                k: sorted(v) for k, v in sorted(sub_properties(schema, schemas).items())
            },
            "type": type_name,
        }
        print(f"{type_name}: {len(doc['types'][type_name]['fields'])} fields")
    doc["types"] = dict(sorted(doc["types"].items()))
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=100))


if __name__ == "__main__":
    main()
