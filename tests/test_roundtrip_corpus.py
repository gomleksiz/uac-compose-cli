"""Milestone 1: inflate(simplify(x, t), t) == normalize(x) for every corpus object."""

from __future__ import annotations

import json
import re

import pytest

from uac_compose import registry
from uac_compose.core.extensions import SLOT_RE, _slot_name
from uac_compose.core.inflate import inflate
from uac_compose.core.normalize import normalize
from uac_compose.core.simplify import simplify
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope

pytestmark = pytest.mark.corpus

CORPUS_SIZE = 5286


def test_corpus_has_every_object(corpus: list[ObjectEnvelope]) -> None:
    assert len(corpus) == CORPUS_SIZE
    assert not [e.source for e in corpus if e.warnings]


def test_roundtrip_corpus(corpus: list[ObjectEnvelope], templates: TemplateSet) -> None:
    subs = templates.sub_templates
    failures = [
        f"{e.type}/{e.name}"
        for e in corpus
        if inflate(simplify(e.obj, templates.get(e.type), subs), templates.get(e.type), subs)
        != normalize(e.obj)
    ]
    assert not failures, f"{len(failures)} of {len(corpus)}: {failures[:5]}"


def test_roundtrip_corpus_with_extensions_collapsed(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    subs, ext = templates.sub_templates, templates.extensions
    failures = []
    for e in corpus:
        t = templates.get(e.type)
        reserved = frozenset(registry.get(e.type).fields)
        small = simplify(e.obj, t, subs, extensions=ext, reserved=reserved)
        if inflate(small, t, subs, extensions=ext, reserved=reserved) != normalize(e.obj):
            failures.append(f"{e.type}/{e.name}")
    assert not failures, f"{len(failures)} of {len(corpus)}: {failures[:5]}"


#: Every `fieldMapping` prose form a live controller's 276 extensions declare.
DECLARED_MAPPINGS = (
    "Array Field 1",
    "Boolean Field 1",
    "Choice Field 1",
    "Credential Field 1",
    "Float Field 1",
    "Integer Field 1",
    "Large Text Field 1",
    "Script Field 1",
    "Text Field 1",
    "Database Connection Field 1",
    "SAP Connection Field 1",
)


@pytest.mark.corpus
def test_every_declared_slot_name_is_one_real_tasks_store(
    corpus: list[ObjectEnvelope],
) -> None:
    """The declaration and the stored object must agree about the property name.

    `_slot_name` turns an extension's declared `fieldMapping` prose into the
    property a task carries. A name it invents that no object ever stores is a
    create the controller refuses with `Unrecognized property` -- which is how
    `integerField` (really `intField`) was found, promoting a Bundle to a
    controller whose copy of the extension no task had used yet. That is the
    one case where the declaration is the only source of a slot.
    """
    stored = {
        re.sub(r"\d+$", "", key)
        for e in corpus
        if e.type in ("taskUniversal", "taskUniversalMonitor")
        for key in normalize(e.obj)
        if SLOT_RE.match(key)
    }
    wrong = [
        (prose, _slot_name(prose))
        for prose in DECLARED_MAPPINGS
        if re.sub(r"\d+$", "", _slot_name(prose)) not in stored
    ]
    assert not wrong, f"no real task stores these: {wrong}"


def test_simplify_actually_compresses(corpus: list[ObjectEnvelope], templates: TemplateSet) -> None:
    """A differ that removes nothing would pass the round trip too."""
    subs = templates.sub_templates
    before = sum(len(json.dumps(normalize(e.obj))) for e in corpus)
    after = sum(len(json.dumps(simplify(e.obj, templates.get(e.type), subs))) for e in corpus)
    assert after / before < 0.55


def test_collapse_actually_collapses(corpus: list[ObjectEnvelope], templates: TemplateSet) -> None:
    subs, ext = templates.sub_templates, templates.extensions
    universal = [e for e in corpus if e.type == "taskUniversal"]
    reserved = frozenset(registry.get("taskUniversal").fields)
    plain = sum(len(json.dumps(simplify(e.obj, templates.get(e.type), subs))) for e in universal)
    collapsed = sum(
        len(
            json.dumps(
                simplify(e.obj, templates.get(e.type), subs, extensions=ext, reserved=reserved)
            )
        )
        for e in universal
    )
    assert collapsed < plain * 0.6
