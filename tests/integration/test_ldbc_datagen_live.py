"""Datagen at the pinned commit reproduces LDBC's published SF0.003 samples (decision 12).

This is what licenses pinning a generated data set (SF0.1) by content digest:
the generator and the options in tests/ldbc/datagen.py yield exactly the rows
LDBC published. Opt-in, as it runs Datagen (about a minute per encoding, plus a
one-off image build of several minutes)::

    RUN_INTEGRATION=1 RUN_LDBC_DATAGEN=1 pytest tests/integration/test_ldbc_datagen_live.py
"""

from __future__ import annotations

import os

import pytest

from tests.ldbc import datasets as ds
from tests.ldbc.datagen import generate
from tests.ldbc.stack import docker_available

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _opted_in() -> None:
    if os.environ.get("RUN_INTEGRATION") != "1" or os.environ.get("RUN_LDBC_DATAGEN") != "1":
        pytest.skip("RUN_INTEGRATION=1 and RUN_LDBC_DATAGEN=1 required")
    if not docker_available():
        pytest.skip("Docker is not available")


@pytest.mark.parametrize("name", ["sf0.003-iso", "sf0.003-epoch-ms"])
def test_regenerating_a_sample_reproduces_its_published_rows(name, tmp_path):
    sample = ds.DATASETS[name]
    layout = generate(sample.scale_factor, sample.encoding, tmp_path / "out")
    assert ds.content_digest(layout / "initial_snapshot") == sample.content_digest
