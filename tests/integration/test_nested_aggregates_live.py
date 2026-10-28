"""Aggregates nested in projection expressions, run on ArangoDB (WP-37).

``head(collect(x))``, ``100 * count(x)`` and ``list + collect(x)`` were
refused ("Unsupported function in v0: collect"); a RETURN with ``collect()``
and another aggregate put PUSH in COLLECT AGGREGATE, which ArangoDB 3.11
rejects (ERR 1574). Users a and b live in SF, c in NYC, d nowhere.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_nested_aggregates_it"


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
        database.create_collection("users").import_bulk(
            [
                {"_key": "a", "name": "a", "city": "SF", "age": 30},
                {"_key": "b", "name": "b", "city": "SF", "age": 40},
                {"_key": "c", "name": "c", "city": "NYC", "age": 20},
                {"_key": "d", "name": "d"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_arithmetic_over_aggregates(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) RETURN n.city AS city, 100 * count(*) + count(n.age) AS score, "
        "count(n.age) + count(n.city) AS both",
    )
    assert sorted(rows, key=lambda r: r["score"]) == [
        {"city": None, "score": 100, "both": 0},
        {"city": "NYC", "score": 101, "both": 2},
        {"city": "SF", "score": 202, "both": 4},
    ]


def test_a_function_of_collect_keeps_the_incoming_order(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) WITH n ORDER BY n.age DESC "
        "WITH n.city AS city, head(collect(n.name)) AS oldest RETURN city, oldest ORDER BY city",
    )
    assert rows == [
        {"city": None, "oldest": "d"},
        {"city": "NYC", "oldest": "c"},
        {"city": "SF", "oldest": "b"},
    ]


def test_collect_beside_another_aggregate(db: Any) -> None:
    """PUSH in COLLECT AGGREGATE was ERR 1574."""
    rows = _rows(db, "MATCH (n:User) RETURN collect(n.name) AS names, count(DISTINCT n.city) AS cities")
    assert sorted(rows[0]["names"]) == ["a", "b", "c", "d"]
    assert rows[0]["cities"] == 2


def test_a_list_plus_collect_after_with(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) WITH n.city AS city, collect(n.name) AS first "
        "MATCH (m:User) WHERE m.city = city "
        "WITH city, first, first + collect(m.age) AS both RETURN city, size(both) AS n ORDER BY city",
    )
    assert rows == [{"city": "NYC", "n": 2}, {"city": "SF", "n": 4}]


def test_a_grouping_key_inside_an_aggregate_expression(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) WHERE n.city IS NOT NULL "
        "RETURN n.city AS city, n.city + ':' + toString(count(*)) AS label ORDER BY city",
    )
    assert rows == [{"city": "NYC", "label": "NYC:1"}, {"city": "SF", "label": "SF:2"}]


def test_a_comprehension_over_collect(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) RETURN [x IN collect(n.name) WHERE x <> 'a' | toUpper(x)] AS names")
    assert sorted(rows[0]) == ["B", "C", "D"]  # one column: the value itself


def test_an_aggregate_where_a_comprehension_binds_its_variable_is_refused() -> None:
    with pytest.raises(CoreError, match="Aggregation is not allowed"):
        translate("MATCH (n:User) RETURN [x IN [1, 2] | count(*)] AS xs", mapping=mapping_bundle_for("pg"))


def test_order_by_a_grouped_expression_of_a_renamed_variable(db: Any) -> None:
    """WITH DISTINCT renames p, and ORDER BY p.city emitted SORT p.city after
    the COLLECT, where p is out of scope (LDBC BI8's ORDER BY person.id)."""
    rows = _rows(
        db,
        "MATCH (p:User) WHERE p.city IS NOT NULL WITH DISTINCT p "
        "RETURN p.city AS city, count(*) AS c ORDER BY p.city DESC",
    )
    assert rows == [{"city": "SF", "c": 2}, {"city": "NYC", "c": 1}]
