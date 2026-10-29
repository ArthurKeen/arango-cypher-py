"""A later scan of a ``UNION`` supertype stays below the variables bound before it.

ArangoDB hoists a subquery that reads no variable to the top of the query; a
member scan hoisted above an earlier loop rides along every row of it, and
LDBC BI1 on PG (3,660 messages) passed the 1 GB query memory limit. The scan
now reads a variable bound before it, so it runs where it is written.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_supertype_scan_placement_it"


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("post").import_bulk(
            [{"_key": str(i), "id": i, "length": i} for i in range(30)]
        )
        database.create_collection("comment").import_bulk(
            [{"_key": str(i), "id": 100 + i, "length": i} for i in range(5)]
        )
        database.create_collection("person").import_bulk([{"_key": str(i), "id": i} for i in range(3)])
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _member_scans_follow(db: Any, aql: str, bind_vars: dict[str, Any], node_type: str) -> bool:
    """Whether every member scan subquery starts after the first *node_type* node."""
    nodes = db.aql.explain(aql, bind_vars=bind_vars)["nodes"]
    types = [n["type"] for n in nodes]
    first = types.index(node_type)
    starts = [i for i, t in enumerate(types) if t == "SubqueryStartNode"]
    return bool(starts) and all(i > first for i in starts[-2:])


def test_a_scan_after_an_aggregate_runs_after_it(db: Any) -> None:
    q = translate(
        "MATCH (m:Message) WITH count(m) AS total MATCH (x:Message) WHERE x.length < 3 RETURN total, count(x) AS n",
        mapping=mapping_bundle_for("ldbc_pg"),
    )
    assert list(db.aql.execute(q.aql, bind_vars=q.bind_vars)) == [{"total": 35, "n": 6}]
    assert _member_scans_follow(db, q.aql, q.bind_vars, "CollectNode"), q.aql


def test_a_second_pattern_part_scans_below_the_first(db: Any) -> None:
    q = translate(
        "MATCH (p:Person), (m:Message) WHERE m.length = 0 RETURN p.id AS p, m.id AS m ORDER BY p, m",
        mapping=mapping_bundle_for("ldbc_pg"),
    )
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert rows == [{"p": p, "m": m} for p in range(3) for m in (0, 100)]
    assert _member_scans_follow(db, q.aql, q.bind_vars, "EnumerateCollectionNode"), q.aql
