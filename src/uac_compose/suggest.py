"""`export --suggest-templates`: find fields that repeat, write them once.

After an export is emitted, the objects of each section are searched for a set
of fields that several of them state with the same value (the same agent,
credentials, retry settings, business services...). Each set becomes a
`templates:` entry, and every object that holds it says `like: <entry>` in
place of those fields.

Display only, like the groups: `like:` copies the entry and the object's own
fields win, so an object that states none of the moved fields compiles to the
object it compiled to before. Each rewritten object is compiled both ways
before it is kept; one that differs puts its template back where it was.

Greedy and deterministic: the set that saves the most written fields is taken
first, then the next one over the objects still free. An object uses one
template at most, so a reader looks in one place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from uac_compose.core.templates import TemplateSet
from uac_compose.yamlio import TEMPLATES

#: An entry is worth writing when this many objects use it...
MIN_USES = 3
#: ...and it holds at least this many fields.
MIN_FIELDS = 2

#: Never moved: what the object is, what it already copies, a note about it,
#: and a workflow's own members.
KEEP = frozenset({"type", "like", "comment", "tasks"})

#: Sections whose objects may copy from a template (not the service list).
SKIP_SECTIONS = frozenset({"business_services", TEMPLATES})

Item = tuple[str, str]  # (field, value as canonical JSON)


@dataclass
class Suggested:
    name: str
    section: str
    fields: dict[str, Any]
    users: list[str]

    @property
    def saved(self) -> int:
        """Written fields saved: each user drops them and writes one `like:`."""
        return len(self.users) * (len(self.fields) - 1) - len(self.fields)


def _items(body: dict[str, Any]) -> frozenset[Item]:
    return frozenset(
        (k, json.dumps(v, sort_keys=True, default=str)) for k, v in body.items() if k not in KEEP
    )


def _best(
    free: dict[str, frozenset[Item]], min_uses: int, min_fields: int
) -> tuple[frozenset[Item], list[str]] | None:
    """The field set that saves the most, and the objects that hold it."""
    count: dict[Item, int] = {}
    for items in free.values():
        for item in items:
            count[item] = count.get(item, 0) + 1
    # A field set used by `min_uses` objects is made only of fields that many
    # objects state; everything else only hides the shared part.
    frequent = {n: frozenset(i for i in items if count[i] >= min_uses) for n, items in free.items()}
    shapes = sorted({s for s in frequent.values() if len(s) >= min_fields}, key=sorted)
    candidates: set[frozenset[Item]] = set(shapes)
    for i, a in enumerate(shapes):
        for b in shapes[i + 1 :]:
            common = a & b
            if len(common) >= min_fields:
                candidates.add(common)
    # Objects with the same shape are counted once, by how many they are.
    weight: dict[frozenset[Item], int] = {}
    for shape in frequent.values():
        weight[shape] = weight.get(shape, 0) + 1
    holding: dict[Item, set[frozenset[Item]]] = {}
    for shape in weight:
        for item in shape:
            holding.setdefault(item, set()).add(shape)
    best: tuple[int, int, frozenset[Item]] | None = None
    for candidate in candidates:
        rarest, *rest = sorted(candidate, key=lambda i: len(holding[i]))
        uses = sum(weight[s] for s in holding[rarest] if all(s in holding[i] for i in rest))
        saved = uses * (len(candidate) - 1) - len(candidate)
        if uses < min_uses or saved <= 0:
            continue
        # most saved; then the larger set; then a stable order
        key = (saved, len(candidate), candidate)
        if (
            best is None
            or key[:2] > best[:2]
            or (key[:2] == best[:2] and sorted(key[2]) < sorted(best[2]))
        ):
            best = key
    if best is None:
        return None
    chosen = best[2]
    return chosen, [n for n, items in frequent.items() if chosen <= items]


def _slug(value: Any) -> str:
    if isinstance(value, list) and value and isinstance(value[0], str):
        value = value[0]
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = re.sub(r"\$\{([^}]*)\}", r"\1", str(value))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-")


#: Fields whose value names the template best, in order.
NAMING = (
    "template",
    "agent",
    "agentCluster",
    "credentials",
    "business_services",
    "calendar",
    "connection",
)


def _name(fields: dict[str, Any], section: str, taken: set[str]) -> str:
    words = [_slug(fields[k]) for k in NAMING if k in fields]
    words = [w for w in words if w][:2] or [section.rstrip("s").replace("_", "-") + "-common"]
    base = "-".join(words)
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def _uses(body: dict[str, Any], template: str, moved: set[str]) -> dict[str, Any]:
    """`body` with `like: template` after its type, and the moved fields gone."""
    out: dict[str, Any] = {}
    if "type" in body:
        out["type"] = body["type"]
    out["like"] = template
    out.update({k: v for k, v in body.items() if k != "type" and k not in moved})
    return out


def _same(
    section: str,
    name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    template: tuple[str, dict[str, Any]],
    templates: TemplateSet | None,
) -> bool:
    """Does `after` (which copies `template`) compile to what `before` does?"""
    from uac_compose.compile import CompileError, compile_object

    try:
        old = compile_object(section, name, before, templates)
        new = compile_object(
            section, name, after, templates, lambda n: template[1] if n == template[0] else None
        )
    except (CompileError, ValueError):
        return False
    return old == new


def suggest(
    document: dict[str, Any],
    templates: TemplateSet | None = None,
    *,
    min_uses: int = MIN_USES,
    min_fields: int = MIN_FIELDS,
) -> tuple[dict[str, Any], list[Suggested]]:
    """(document with `templates:` and `like:`, the templates written)."""
    taken = {name for section in document.values() if isinstance(section, dict) for name in section}
    taken |= set(document.get(TEMPLATES) or {})
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in document.items()}
    found: list[Suggested] = []
    for section, entries in document.items():
        if section in SKIP_SECTIONS or not isinstance(entries, dict):
            continue
        free = {n: _items(b) for n, b in entries.items() if isinstance(b, dict) and "like" not in b}
        while True:
            best = _best(free, min_uses, min_fields)
            if best is None:
                break
            chosen, users = best
            for user in users:
                del free[user]
            keys = {k for k, _ in chosen}
            first = entries[users[0]]
            fields = {k: first[k] for k in first if k in keys}
            name = _name(fields, section, taken)
            rewritten = {u: _uses(entries[u], name, keys) for u in users}
            if not all(
                _same(section, u, entries[u], rewritten[u], (name, fields), templates)
                for u in users
            ):
                continue  # leave these objects as they were
            out[section].update(rewritten)
            found.append(Suggested(name, section, fields, users))
    if not found:
        return document, []
    written = dict(out.get(TEMPLATES) or {})
    written.update({s.name: s.fields for s in found})
    # Templates first, after the service list: a reader meets an entry
    # before the first `like:` that names it.
    head = {k: out[k] for k in ("business_services",) if k in out}
    rest = {k: v for k, v in out.items() if k not in head and k != TEMPLATES}
    return {**head, TEMPLATES: written, **rest}, found


def report(found: list[Suggested]) -> list[str]:
    """One line per template, then the total."""
    lines = [
        f"template {s.name}: {len(s.fields)} fields, used by {len(s.users)} {s.section}"
        for s in found
    ]
    total = sum(s.saved for s in found)
    lines.append(f"{len(found)} template(s) suggested; {total} written fields saved")
    return lines
