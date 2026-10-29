"""DISTINCT on ArangoDB: it keeps the order of the rows it reads, and SKIP/LIMIT
apply to the distinct rows.

``ORDER BY c DESC WITH DISTINCT city LIMIT 2`` kept the two cities that sort
first by name, because AQL's COLLECT re-sorts its groups (LDBC BI4 picked the
wrong 100 forums); ``RETURN DISTINCT a, b LIMIT 2`` limited the rows before
removing duplicates, and could return one row.
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

_DB = "arango_cypher_distinct_order_it"


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
        # Zurich has the most users and Aachen the fewest; by name the order is
        # the reverse, which is what a re-sorting COLLECT returns.
        people = [("Zurich", "CH", 50)] * 3 + [("Paris", "FR", 40)] * 2 + [("Aachen", "DE", 30)]
        database.create_collection("users").import_bulk(
            [{"_key": f"u{i}", "city": c, "country": k, "age": a + i} for i, (c, k, a) in enumerate(people)]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_with_distinct_keeps_the_order_its_limit_reads(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (n:User) WITH n.city AS city, count(n) AS c ORDER BY c DESC, city "
        "WITH DISTINCT city LIMIT 2 RETURN city",
    )
    assert rows == ["Zurich", "Paris"]


def test_with_distinct_keeps_the_first_row_of_each_value(db: Any) -> None:
    # Each city's oldest user decides its place: Zurich (52), Paris (44), Aachen (35).
    rows = _run(db, "MATCH (n:User) WITH n ORDER BY n.age DESC WITH DISTINCT n.city AS city RETURN city")
    assert rows == ["Zurich", "Paris", "Aachen"]


def test_return_distinct_limit_counts_distinct_rows(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (n:User) WITH n ORDER BY n.city DESC RETURN DISTINCT n.city AS city, n.country AS c LIMIT 2",
    )
    assert rows == [{"city": "Zurich", "c": "CH"}, {"city": "Paris", "c": "FR"}]


def test_single_column_return_distinct_keeps_the_order_and_slices_after(db: Any) -> None:
    rows = _run(db, "MATCH (n:User) WITH n ORDER BY n.age DESC RETURN DISTINCT n.city SKIP 1 LIMIT 2")
    assert rows == ["Paris", "Aachen"]
