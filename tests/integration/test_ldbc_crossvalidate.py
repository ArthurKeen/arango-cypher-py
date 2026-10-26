"""Re-verify the committed LDBC oracle snapshots against live Neo4j (WP-33).

The execution tier trusts ``tests/ldbc/expected/``; this suite re-records them
in memory and fails if the oracle's answers changed (a Neo4j upgrade, a data
set change, a binding drift). Activation: ``RUN_INTEGRATION=1 RUN_CROSS=1``.
"""

from __future__ import annotations

import os

import pytest

from tests.ldbc import oracle

pytestmark = [pytest.mark.integration, pytest.mark.cross]


@pytest.fixture(scope="module")
def recorded(ldbc_neo4j_uri):
    from tests.integration import neo4j_reference

    driver = neo4j_reference.get_driver(ldbc_neo4j_uri)
    try:
        yield oracle.record(driver)
    finally:
        neo4j_reference._active_dataset = None  # the LDBC graph replaced whatever was loaded
        driver.close()


def test_live_oracle_matches_the_committed_snapshots(recorded):
    """Reads and writes alike: the live recording renders to the committed file, byte for byte."""
    import json

    drifted = []
    for op_id, live in recorded.items():
        path = oracle.snapshot_path(op_id)
        rendered = json.dumps(live, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            drifted.append(op_id)
    assert not drifted, (
        f"the oracle's answers changed for {drifted}. If intended (e.g. a Neo4j upgrade), re-record with "
        ".venv/bin/python -m tests.ldbc.oracle --record and review the diff."
    )
