"""Load the LDBC SF0.003 samples into the reference Neo4j (WP-33).

Neo4j is the oracle for the LDBC suites, so it must hold the same graph as the
ArangoDB layouts: the totals pinned in ``test_ldbc_load.py``, the reference's
supertype labels, and each workload's timestamp encoding.

Activation: ``RUN_INTEGRATION=1 RUN_CROSS=1`` and a running Neo4j
(``docker-compose.neo4j.yml``).
"""

from __future__ import annotations

import os

import pytest

from tests.integration.test_ldbc_load import SF0003_EDGES, SF0003_NODES
from tests.ldbc.datasets import DATASETS, fetch
from tests.ldbc.neo4j_loader import load_neo4j

pytestmark = [pytest.mark.integration, pytest.mark.cross]


@pytest.fixture(scope="module")
def driver():
    if os.environ.get("RUN_CROSS") != "1":
        pytest.skip("Set RUN_CROSS=1 (and start docker-compose.neo4j.yml)")
    from tests.integration import neo4j_reference

    drv = neo4j_reference.get_driver()
    yield drv
    # Another suite sharing this Neo4j must reseed its own dataset.
    neo4j_reference._active_dataset = None
    drv.close()


@pytest.mark.parametrize(
    "dataset,value_type", [("sf0.003-epoch-ms", "INTEGER"), ("sf0.003-iso", "ZONED DATETIME")]
)
def test_load_matches_the_sample_and_the_reference_encoding(driver, dataset, value_type):
    report = load_neo4j(driver, fetch(dataset), encoding=DATASETS[dataset].encoding)
    assert dict(report.nodes_by_label) == SF0003_NODES
    assert dict(report.edges_by_type) == SF0003_EDGES
    with driver.session() as s:
        assert s.run("MATCH (m:Message) RETURN count(m) AS c").single()["c"] == 3189 + 471
        assert s.run("MATCH (c:Comment) WHERE NOT c:Message RETURN count(c) AS c").single()["c"] == 0
        row = s.run("MATCH (p:Person) RETURN valueType(p.creationDate) AS t LIMIT 1").single()
        assert row["t"].startswith(value_type)
