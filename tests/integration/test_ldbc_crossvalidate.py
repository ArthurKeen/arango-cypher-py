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
def recorded():
    if os.environ.get("RUN_CROSS") != "1":
        pytest.skip("Set RUN_CROSS=1 (and start docker-compose.neo4j.yml)")
    from tests.integration import neo4j_reference

    driver = neo4j_reference.get_driver()
    try:
        yield oracle.record(driver)
    finally:
        neo4j_reference._active_dataset = None  # the LDBC graph replaced whatever was loaded
        driver.close()


def test_live_oracle_matches_the_committed_snapshots(recorded):
    drifted = []
    for op_id, live in recorded.items():
        committed = oracle.read(op_id)
        live_bindings = [{**b, "binding": b["binding"]} for b in live["bindings"]]
        committed_rows = [(b["columns"], b["rows"]) for b in committed["bindings"]] if committed else None
        if committed_rows != [(b["columns"], b["rows"]) for b in live_bindings]:
            drifted.append(op_id)
    assert not drifted, (
        f"the oracle's answers changed for {drifted}. If intended (e.g. a Neo4j upgrade), re-record with "
        "RUN_CROSS=1 .venv/bin/python -m tests.ldbc.oracle --record and review the diff."
    )
