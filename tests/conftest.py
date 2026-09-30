"""Shared fixtures.

The corpus is real production data at UAC_COMPOSE_CORPUS; tests skip without it.
"""

from __future__ import annotations

import pytest

from uac_compose.core.templates import TemplateSet
from uac_compose.envelope import ObjectEnvelope
from uac_compose.ingest.archive import read_corpus
from uac_compose.paths import corpus_dir

RELEASE = "8.0.2.0"


@pytest.fixture(scope="session")
def corpus() -> list[ObjectEnvelope]:
    archives = sorted(corpus_dir().glob("*.zip"))
    if not archives:
        pytest.skip(f"no corpus at {corpus_dir()} (set UAC_COMPOSE_CORPUS)")
    return read_corpus(archives)


@pytest.fixture(scope="session")
def templates() -> TemplateSet:
    return TemplateSet(RELEASE)
