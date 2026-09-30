"""Milestone 1 unit tests, each named for the rule it protects."""

from __future__ import annotations

import inspect
from typing import Any

from uac_compose import registry, routing
from uac_compose.core import inflate as inflate_module
from uac_compose.core import simplify as simplify_module
from uac_compose.core.extensions import ExtensionMap, Slot, collapse, expand
from uac_compose.core.inflate import inflate
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify

EXT = ExtensionMap(
    {
        "S3": {
            "action": Slot("choiceField1", "Action"),
            "type": Slot("choiceField2", "Type"),
            "bucket": Slot("textField1", "Bucket"),
        }
    }
)


def test_should_descend_is_shared() -> None:
    assert inflate_module.should_descend is simplify_module.should_descend
    assert "def should_descend" not in inspect.getsource(inflate_module)


def test_exclusions_are_exact_names() -> None:
    out = normalize({"type": "taskWebService", "name": "x", "version": 3, "httpVersion": "1.1"})
    assert out == {"type": "taskWebService", "name": "x", "httpVersion": "1.1"}


def test_sys_id_dropped_at_any_depth_top_level_only_at_root() -> None:
    obj = {"name": "x", "sysId": "a", "variables": [{"name": "v", "sysId": "b", "version": 1}]}
    assert normalize(obj) == {"name": "x", "variables": [{"name": "v", "version": 1}]}


def test_zero_and_false_kept() -> None:
    obj = {"name": "x", "retryMaximum": 0, "startHeld": False, "summary": "", "a": [], "b": None}
    assert normalize(obj) == {"name": "x", "retryMaximum": 0, "startHeld": False}


def test_type_and_name_kept_even_empty() -> None:
    assert normalize({"type": "taskUnix", "name": ""}) == {"type": "taskUnix", "name": ""}


def test_unset_slot_dropped() -> None:
    obj = {
        "name": "x",
        "textField1": {"label": "Bucket", "name": "bucket"},
        "textField2": {"label": "Key", "name": "key", "value": "k"},
    }
    assert normalize(obj) == {
        "name": "x",
        "textField2": {"label": "Key", "name": "key", "value": "k"},
    }


def test_default_dropped_type_and_name_kept() -> None:
    template = {"type": "taskUnix", "name": "x", "retryMaximum": 0}
    obj = {"type": "taskUnix", "name": "x", "retryMaximum": 0, "command": "ls"}
    assert simplify(obj, template) == {"type": "taskUnix", "name": "x", "command": "ls"}


def test_defaulted_field_stored_empty_travels_as_null() -> None:
    """Measured on ps1: taskSap outputReturnNline and triggerTime startingAt null."""
    template = {"type": "taskSap", "name": "x", "outputReturnNline": "100"}
    obj = {"type": "taskSap", "name": "x", "outputReturnNline": None}
    small = simplify(obj, template)
    assert small == {"type": "taskSap", "name": "x", "outputReturnNline": None}
    assert inflate(small, template) == normalize(obj)
    # A field the object does not carry at all is still left out.
    assert simplify({"type": "taskSap", "name": "x"}, template) == {"type": "taskSap", "name": "x"}


def test_sub_template_list_diffed_per_item() -> None:
    subs = {"virtualResources": {"amount": 1}}
    obj = {"name": "x", "virtualResources": [{"resource": "r", "amount": 1}, {"amount": 2}]}
    small = simplify(obj, {}, subs)
    assert small == {"name": "x", "virtualResources": [{"resource": "r"}, {"amount": 2}]}
    assert inflate(small, {}, subs) == obj


def test_descends_into_dict_with_sub_template_below() -> None:
    subs = {"actions.abortActions": {"status": "Failed"}}
    obj = {"name": "x", "actions": {"abortActions": [{"status": "Failed", "haltOnFinish": True}]}}
    small = simplify(obj, {}, subs)
    assert small == {"name": "x", "actions": {"abortActions": [{"haltOnFinish": True}]}}
    assert inflate(small, {}, subs) == obj


def _universal(**slots: Any) -> dict[str, Any]:
    return {"type": "taskUniversal", "name": "x", "template": "S3", **slots}


def test_collapse_keys_on_name_not_label() -> None:
    obj = _universal(choiceField1={"label": "Something else", "name": "action", "value": "copy"})
    assert collapse(obj, EXT, frozenset())["action"] == "copy"


def test_collapse_never_shadows_real_field() -> None:
    reserved = frozenset(registry.get("taskUniversal").fields)
    assert "type" in reserved
    obj = _universal(choiceField2={"label": "Type", "name": "type", "value": "file"})
    out = collapse(obj, EXT, reserved)
    assert out["type"] == "taskUniversal"
    assert out["choiceField2"] == {"label": "Type", "name": "type", "value": "file"}


def test_table_slot_not_collapsed() -> None:
    table = {"label": "Bucket", "name": "bucket", "value": "b", "fields": [{"n": 1}]}
    obj = _universal(textField1=table)
    assert collapse(obj, EXT, frozenset()) == obj


def test_slot_other_than_mapped_not_collapsed() -> None:
    obj = _universal(textField9={"label": "Action", "name": "action", "value": "copy"})
    assert collapse(obj, EXT, frozenset()) == obj


def test_collapse_expand_reversible() -> None:
    obj = _universal(
        choiceField1={"label": "Action", "name": "action", "value": "copy"},
        textField1={"label": "Bucket", "name": "bucket", "value": "b"},
    )
    # The real field names must be reserved: EXT declares a parameter called `type`.
    reserved = frozenset(registry.get("taskUniversal").fields)
    small = collapse(obj, EXT, reserved)
    assert small == _universal(action="copy", bucket="b")
    assert expand(small, EXT, reserved) == obj


def test_boolean_slot_keeps_real_boolean() -> None:
    ext = ExtensionMap({"S3": {"recursive": Slot("booleanField1", "Recursive")}})
    obj = _universal(booleanField1={"label": "Recursive", "name": "recursive", "value": True})
    small = collapse(obj, ext, frozenset())
    assert small["recursive"] is True
    assert expand(small, ext, frozenset()) == obj


def test_declared_slot_names_are_the_ones_uac_stores() -> None:
    """An extension declares `"Integer Field 1"`; UAC stores `intField1`.

    A create carrying `integerField1` is refused with `Unrecognized property`,
    and the declaration is the only source of a slot for an extension no local
    task has used yet -- exactly the case a promotion hits. The corpus test
    checks every prefix against 14,732 real slots; this pins the one that was
    wrong.
    """
    from uac_compose.core.extensions import _slot_name

    assert _slot_name("Integer Field 1") == "intField1"
    assert _slot_name("Choice Field 3") == "choiceField3"
    assert _slot_name("Large Text Field 2") == "largeTextField2"
    assert _slot_name("") == ""


def test_var_pairs_include_nested_paths() -> None:
    pairs = registry.var_pairs("taskUnix")
    assert ("agent", "agentVar") in pairs
    assert ("credentials", "credentialsVar") in pairs
    assert all(not a.endswith("Var") for a, _ in pairs)


def test_reference_fields() -> None:
    refs = registry.reference_fields("taskUnix")
    assert {"agent", "credentials", "opswiseGroups"} <= refs
    assert "command" not in refs


def test_credential_type_moved_not_overwritten() -> None:
    stamped = routing.stamp({"name": "c", "type": "Standard"}, "credential")
    assert stamped == {"name": "c", "type": "credential", "_uacType": "Standard"}
    assert routing.restore(stamped) == {"name": "c", "type": "Standard"}


def test_routing_key_stripped_on_restore() -> None:
    assert routing.restore(routing.stamp({"name": "s"}, "script")) == {"name": "s"}


def test_resolve_by_export_table_not_type() -> None:
    entry = registry.resolve("ops_virtual_resource", "Renewable")
    assert entry is not None and entry.type == "virtualResource"
