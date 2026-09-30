"""Universal Extension slots as parameter names (SPEC §5.3).

    choiceField3: {label: Action, name: action, value: upload-file}  ->  action: upload-file

Rules, each forced by the corpus:
1. Key on `name`, never `label` (labels repeat within a template).
2. Never collapse onto a real UAC field of the type (`type`, `credentials`).
3. Only the plain `{label, name, value}` shape collapses; table slots stay raw.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SLOT_RE = re.compile(r"^\w*?(?:Var)?Field\d+$")
COLLAPSIBLE_KEYS = frozenset({"label", "name", "value"})


@dataclass(frozen=True)
class Slot:
    slot: str
    label: str = ""
    values: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ExtensionMap:
    """Extension template name -> parameter name -> slot."""

    templates: dict[str, dict[str, Slot]]
    declared: frozenset[str] = frozenset()
    """Every extension the declarations named, with parameters or without.
    `templates` leaves out one that declares none, so only this says it exists.
    Empty for a map read from a file or from objects: nothing is then known absent."""

    def lacks(self, template: Any) -> bool:
        """The declarations were read, and none of them is `template`."""
        return (
            bool(self.declared)
            and isinstance(template, str)
            and template not in self.declared
            and template not in self.templates
        )

    @classmethod
    def load(cls, path: Path) -> ExtensionMap:
        raw = json.loads(path.read_text())["templates"]
        return cls(
            {
                template: {
                    name: Slot(s["slot"], s.get("label", ""), tuple(s.get("values") or ()))
                    for name, s in params.items()
                }
                for template, params in raw.items()
            }
        )

    def for_template(self, template: Any) -> dict[str, Slot]:
        return self.templates.get(template, {}) if isinstance(template, str) else {}

    def save(self, path: Path) -> None:
        doc = {
            "templates": {
                template: {
                    name: {"slot": s.slot, "label": s.label, "values": list(s.values)}
                    for name, s in sorted(params.items())
                }
                for template, params in sorted(self.templates.items())
            }
        }
        path.write_text(json.dumps(doc, indent=2) + "\n")


# Slot kinds whose values come from a fixed list; above MAX_VOCABULARY distinct
# observed values a "choice" is free text.
ENUMERABLE_SLOTS = ("choiceField", "booleanField")
MAX_VOCABULARY = 25


#: Where the words an extension DECLARES are not the property UAC STORES.
#:
#: Measured over 14,732 slots on 5,286 real objects: every declared prefix
#: appears on a stored task except one. An extension that declares
#: `"Integer Field 1"` is stored as `intField1`, never `integerField1`, and a
#: create carrying the latter is refused with `Unrecognized property`. Found
#: promoting a Bundle to a controller whose copy of the extension no task had
#: used yet -- the only case where the declaration is the sole source.
DECLARED_WORDS = {"integer": "int"}


def _slot_name(mapping: Any) -> str:
    """`"Choice Field 3"` (as the extension declares it) -> `choiceField3`."""
    words = str(mapping or "").split()
    if not words:
        return ""
    first, *rest = words
    head = DECLARED_WORDS.get(first.lower(), first.lower())
    return head + "".join(w if w.isdigit() else w.capitalize() for w in rest)


def declared_map(definitions: list[dict[str, Any]]) -> ExtensionMap:
    """What each installed extension declares (`uac universal-template list`)."""
    found: dict[str, dict[str, Slot]] = {}
    names: set[str] = set()
    for definition in definitions:
        template = definition.get("name")
        if not isinstance(template, str) or not template:
            continue
        names.add(template)
        slots: dict[str, Slot] = {}
        for f in definition.get("fields") or []:
            if not isinstance(f, dict):
                continue
            name, slot = f.get("name"), _slot_name(f.get("fieldMapping"))
            if not isinstance(name, str) or not name or not SLOT_RE.match(slot):
                continue
            values = tuple(
                str(c["fieldValue"])
                for c in f.get("choices") or []
                if isinstance(c, dict) and c.get("fieldValue") is not None
            )
            slots[name] = Slot(
                slot, str(f.get("label") or ""), values if slot.startswith(ENUMERABLE_SLOTS) else ()
            )
        if slots:
            found[template] = slots
    return ExtensionMap(found, frozenset(names))


def observed_map(objects: list[dict[str, Any]]) -> ExtensionMap:
    """Parameter -> slot as real (normalized) objects use them.

    A parameter seen in two slots of one template is left out: the reverse
    direction would be a guess.
    """
    seen: dict[str, dict[str, dict[str, str]]] = {}
    vocab: dict[tuple[str, str], set[str]] = {}
    for obj in objects:
        template = obj.get("template")
        if not isinstance(template, str) or not template:
            continue
        per = seen.setdefault(template, {})
        for key, value in obj.items():
            if not SLOT_RE.match(key) or not is_collapsible(value):
                continue
            per.setdefault(value["name"], {})[key] = value["label"]
            if key.startswith(ENUMERABLE_SLOTS) and isinstance(value["value"], str | bool):
                vocab.setdefault((template, value["name"]), set()).add(str(value["value"]))
    out: dict[str, dict[str, Slot]] = {}
    for template, names in seen.items():
        out[template] = {}
        for name, by_slot in names.items():
            if len(by_slot) != 1:
                continue
            ((slot, label),) = by_slot.items()
            values = vocab.get((template, name), set())
            out[template][name] = Slot(
                slot, label, tuple(sorted(values)) if len(values) <= MAX_VOCABULARY else ()
            )
    return ExtensionMap(out)


def merge_maps(declared: ExtensionMap, observed: ExtensionMap) -> ExtensionMap:
    """The declaration, with what real objects do winning where they differ."""
    merged = {t: dict(params) for t, params in declared.templates.items()}
    for template, params in observed.templates.items():
        merged.setdefault(template, {}).update(params)
    return ExtensionMap(merged, declared.declared | observed.declared)


def is_collapsible(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == COLLAPSIBLE_KEYS


def collapse(obj: dict[str, Any], ext: ExtensionMap, reserved: frozenset[str]) -> dict[str, Any]:
    slots = ext.for_template(obj.get("template"))
    if not slots:
        return obj
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if not SLOT_RE.match(key) or not is_collapsible(value):
            out[key] = value
            continue
        name = value["name"]
        known = slots.get(name)
        if name in reserved or known is None or known.slot != key or name in out:
            out[key] = value
            continue
        out[name] = value["value"]
    return out


def expand(obj: dict[str, Any], ext: ExtensionMap, reserved: frozenset[str]) -> dict[str, Any]:
    slots = ext.for_template(obj.get("template"))
    if not slots:
        return obj
    out: dict[str, Any] = {}
    for key, value in obj.items():
        known = slots.get(key)
        if known is None or key in reserved or SLOT_RE.match(key):
            out[key] = value
            continue
        out[known.slot] = {"label": known.label, "name": key, "value": value}
    return out
