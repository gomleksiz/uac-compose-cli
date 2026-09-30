"""`export --suggest-templates`: shared fields become a `templates:` entry."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from uac_compose import suggest, yamlio
from uac_compose.compile import compile_bundle
from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.style import compact


def _task(command: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "Unix",
        "agent": "linux-01",
        "credentials": "etl-svc",
        "retryMaximum": 3,
        "command": command,
        **extra,
    }


def _compiled(document: dict[str, Any], templates: TemplateSet | None = None) -> list[Any]:
    bundle = yamlio.read_bundle(yamlio.dump(document))
    return [(p.section, p.name, p.obj) for p in compile_bundle(bundle, templates)]


def test_fields_three_objects_share_become_one_template() -> None:
    document = {"tasks": {f"Load {n}": _task(f"/opt/{n}.sh") for n in "ABC"}}
    new, found = suggest.suggest(document)
    assert [s.name for s in found] == ["linux-01-etl-svc"]
    assert new["templates"] == {
        "linux-01-etl-svc": {"agent": "linux-01", "credentials": "etl-svc", "retryMaximum": 3}
    }
    assert new["tasks"]["Load A"] == {
        "type": "Unix",
        "like": "linux-01-etl-svc",
        "command": "/opt/A.sh",
    }
    assert _compiled(new) == _compiled(document)


def test_two_objects_are_not_enough() -> None:
    document = {"tasks": {f"Load {n}": _task(f"/opt/{n}.sh") for n in "AB"}}
    assert suggest.suggest(document) == (document, [])


def test_one_shared_field_is_not_a_template() -> None:
    document = {"tasks": {n: {"type": "Unix", "agent": "a", "command": n} for n in "ABCD"}}
    assert suggest.suggest(document)[1] == []


def test_an_object_states_its_own_value_not_the_template() -> None:
    tasks = {f"Load {n}": _task(f"/opt/{n}.sh") for n in "ABC"}
    tasks["Load D"] = _task("/opt/D.sh", retryMaximum=5)
    new, _ = suggest.suggest({"tasks": tasks})
    assert new["tasks"]["Load D"]["retryMaximum"] == 5
    assert _compiled(new) == _compiled({"tasks": tasks})


def test_a_name_never_hides_an_object() -> None:
    tasks = {f"Load {n}": _task(f"/opt/{n}.sh") for n in "ABC"}
    tasks["linux-01-etl-svc"] = {"type": "Unix", "command": "x"}
    new, found = suggest.suggest({"tasks": tasks})
    assert found[0].name == "linux-01-etl-svc-2"
    assert _compiled(new) == _compiled({"tasks": tasks})


def test_type_and_comment_stay_on_the_object() -> None:
    tasks = {n: {"type": "Unix", "comment": "c", "agent": "a", "command": "x"} for n in "ABC"}
    new, found = suggest.suggest({"tasks": tasks})
    assert found[0].fields == {"agent": "a", "command": "x"}
    assert new["tasks"]["A"] == {"type": "Unix", "like": "a", "comment": "c"}


def test_the_report_counts_what_was_saved() -> None:
    document = {"tasks": {f"Load {n}": _task(f"/opt/{n}.sh") for n in "ABCD"}}
    _, found = suggest.suggest(document)
    assert suggest.report(found) == [
        "template linux-01-etl-svc: 3 fields, used by 4 tasks",
        "1 template(s) suggested; 5 written fields saved",
    ]


@pytest.mark.corpus
def test_templates_never_change_an_object(
    corpus: list[ObjectEnvelope], templates: TemplateSet
) -> None:
    """Each archive is one export: with templates it compiles to the same objects."""
    by_source: dict[str, list[ObjectEnvelope]] = defaultdict(list)
    for e in corpus:
        by_source[str(e.source).split(":")[0]].append(e)
    used = 0
    for envelopes in by_source.values():
        document = compact.emit_document([(e.obj, e.type) for e in envelopes], templates)
        new, found = suggest.suggest(document, templates)
        used += sum(len(s.users) for s in found)
        assert _compiled(new, templates) == _compiled(document, templates)
    assert used >= 3900  # measured: 3,955 of 5,286 objects use a template
