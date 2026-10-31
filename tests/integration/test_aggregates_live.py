"""percentileDisc / percentileCont and Cypher-ordered min / max on ArangoDB.

The values are Neo4j's: ``percentileDisc`` takes the value at ``floor(p·n)``
(one less on a whole index other than 0), ``percentileCont`` interpolates at
``p·(n−1)``; a percentile outside [0, 1] fails each group at run time.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango.exceptions import AQLQueryExecuteError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_aggregates_it"


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
            [{"_key": f"u{i}", "city": "X", "age": age} for i, age in enumerate([40, 10, 30, 20, None])]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


@pytest.mark.parametrize(
    ("p", "disc", "cont"),
    [(0.0, 10, 10), (0.25, 10, 17.5), (0.5, 20, 25), (0.75, 30, 32.5), (1.0, 40, 40)],
)
def test_percentiles_match_neo4j(db: Any, p: float, disc: float, cont: float) -> None:
    rows = _run(
        db,
        "MATCH (u:User) RETURN percentileDisc(u.age, $p) AS d, percentileCont(u.age, $p) AS c",
        {"p": p},
    )
    assert rows == [{"d": disc, "c": cont}]


def test_a_percentile_outside_zero_to_one_fails(db: Any) -> None:
    with pytest.raises(AQLQueryExecuteError, match="NumberOutOfRange"):
        _run(db, "MATCH (u:User) RETURN percentileDisc(u.age, $p) AS d", {"p": 1.5})


def test_a_percentile_of_only_nulls_is_null(db: Any) -> None:
    rows = _run(db, "MATCH (u:User) WHERE u.age IS NULL RETURN percentileCont(u.age, 0.5) AS c")
    assert rows == [None]


def test_min_and_max_order_strings_by_code_point_and_types_by_rank(db: Any) -> None:
    assert _run(db, "UNWIND ['a', 'b', 'B', null] AS s RETURN min(s) AS m") == ["B"]
    assert _run(db, "UNWIND [1, 'a', null, [1, 2], 0.2, 'b'] AS x RETURN max(x) AS m") == [1]
    assert _run(db, "UNWIND [1, 'a', null, [1, 2], 0.2, 'b'] AS x RETURN min(x) AS m") == [[1, 2]]


def test_order_by_an_aggregate_reads_the_projected_value(db: Any) -> None:
    rows = _run(db, "MATCH (u:User) RETURN u.city AS city, sum(u.age) AS s ORDER BY sum(u.age) DESC")
    assert rows == [{"city": "X", "s": 100}]
