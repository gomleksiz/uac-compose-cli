"""Derive per-type default templates from one controller's objects (SPEC §5.4).

Default = the most frequent value, if it has support >= 0.6 over >= 3
instances, AND:
1. the field is present on EVERY instance (else `inflate` resurrects it on the
   objects that never had it: measured 5286 -> 5268 for `exitCodes`);
2. a nested value has one shape on every instance (edge `condition` varies);
3. it is not `name`, not a reference, not in data/rules/never_default.yaml.

A documented default (data/uacdocs.yaml) wins over the statistic, but only
through the same presence gate, and only if the corpus shows the value at
least once ("disputed" otherwise: `None` vs `-- None --`). A type with too few
instances takes the shipped template, cut down to the fields every instance
it does have carries.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from uac_compose import registry, uacdocs
from uac_compose.core.normalize import normalize
from uac_compose.core.templates import TemplateSet, allowed, allowed_sub
from uac_compose.envelope import ObjectEnvelope

MIN_SUPPORT = 0.6
MIN_INSTANCES = 3


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return frozenset((k, _shape(v)) for k, v in value.items())
    if isinstance(value, list):
        return "list"
    return "scalar"


@dataclass
class Accumulator:
    counts: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    total: int = 0

    def add(self, obj: dict[str, Any]) -> None:
        self.total += 1
        for key, value in obj.items():
            self.counts[key][_encode(value)] += 1

    def present_everywhere(self, key: str) -> bool:
        return key in self.counts and sum(self.counts[key].values()) == self.total

    def observed(self, key: str, value: Any) -> bool:
        return _encode(value) in self.counts.get(key, Counter())

    def template(self, min_support: float = MIN_SUPPORT) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, counter in self.counts.items():
            if not self.present_everywhere(key):
                continue
            encoded, hits = counter.most_common(1)[0]
            if hits / self.total < min_support:
                continue
            if len({_shape(json.loads(e)) for e in counter}) != 1:
                continue
            out[key] = json.loads(encoded)
        return out


@dataclass
class TypeReport:
    instances: int = 0
    source: str = "derived"  # derived | shipped | none
    defaults: int = 0
    documented: int = 0
    withheld: list[tuple[str, str]] = field(default_factory=list)
    """Documented defaults that did not reach the template, and why."""
    overruled: list[tuple[str, Any, Any]] = field(default_factory=list)
    """(field, statistic, documented): the documented value won."""


@dataclass
class Derived:
    templates: dict[str, dict[str, Any]]
    sub_templates: dict[str, dict[str, Any]]
    report: dict[str, TypeReport]


def sub_items(obj: dict[str, Any], path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """(dotted path, item) for every dict inside a list of dicts, at any depth."""
    for key, value in obj.items():
        child = f"{path}.{key}" if path else key
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield child, item
                    yield from sub_items(item, child)
        elif isinstance(value, dict):
            yield from sub_items(value, child)


def _documented(
    type_name: str, template: dict[str, Any], acc: Accumulator, report: TypeReport
) -> None:
    """Overlay the vendor defaults where the presence gate lets them through."""
    refs = registry.reference_fields(type_name)
    for name, doc in uacdocs.fields(type_name).items():
        if not doc.has_default or name in ("type", "name"):
            continue
        if name in refs:
            report.withheld.append((name, "reference"))
        elif acc.total == 0 or not acc.present_everywhere(name):
            report.withheld.append((name, "not on every instance"))
        elif not acc.observed(name, doc.default):
            report.withheld.append((name, f"disputed: no object holds {doc.default!r}"))
        else:
            if template.get(name, doc.default) != doc.default:
                report.overruled.append((name, template[name], doc.default))
            template[name] = doc.default
            report.documented += 1


def derive(
    envelopes: Iterable[ObjectEnvelope],
    shipped: TemplateSet | None = None,
    *,
    min_support: float = MIN_SUPPORT,
    min_instances: int = MIN_INSTANCES,
) -> Derived:
    by_type: dict[str, Accumulator] = defaultdict(Accumulator)
    by_path: dict[str, Accumulator] = defaultdict(Accumulator)
    for envelope in envelopes:
        clean = normalize(envelope.obj)
        by_type[envelope.type].add(clean)
        for path, item in sub_items(clean):
            by_path[path].add(item)

    templates: dict[str, dict[str, Any]] = {}
    report: dict[str, TypeReport] = {}
    for type_name in sorted(set(by_type) | set(registry.types())):
        acc = by_type.get(type_name, Accumulator())
        rep = TypeReport(instances=acc.total)
        if acc.total >= min_instances:
            template = acc.template(min_support)
            _documented(type_name, template, acc, rep)
        elif shipped is not None and shipped.get(type_name):
            rep.source = "shipped"
            template = {
                k: v
                for k, v in shipped.get(type_name).items()
                if acc.total == 0 or acc.present_everywhere(k)
            }
        else:
            rep.source = "none"
            template = {}
        template = allowed(type_name, template)
        rep.defaults = len(template)
        templates[type_name] = template
        report[type_name] = rep

    subs = {
        path: allowed_sub(path, acc.template(min_support))
        for path, acc in sorted(by_path.items())
        if acc.total >= min_instances
    }
    return Derived(templates, subs, report)
