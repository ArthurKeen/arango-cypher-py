"""``CALL { … }`` subqueries on ArangoDB (LDBC plan WP-42).

Each case runs the translated AQL: a correlated body sees the imported
variables, a row whose subquery returns nothing is dropped, and names the body
shares with the outer query do not collide (ERR 1511).
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

_DB = "arango_cypher_call_subqueries_it"


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
                {"_key": "a", "id": "u1", "city": "SF"},
                {"_key": "b", "id": "u2", "city": "SF"},
                {"_key": "c", "id": "u3", "city": "NYC"},
                {"_key": "d", "id": "u4", "city": "LA"},
            ]
        )
        for coll in ("docs", "persons", "places"):  # named by a traversal's WITH clause
            database.create_collection(coll)
        database.create_collection("follows", edge=True).import_bulk(
            [
                {"_from": "users/a", "_to": "users/b"},
                {"_from": "users/a", "_to": "users/c"},
                {"_from": "users/b", "_to": "users/c"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_a_correlated_aggregate_runs_once_per_row(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (n:User) CALL { WITH n MATCH (n)-[:FOLLOWS]->(f:User) RETURN count(f) AS c } "
        "RETURN n.id AS id, c ORDER BY id",
    )
    assert rows == [{"id": "u1", "c": 2}, {"id": "u2", "c": 1}, {"id": "u3", "c": 0}, {"id": "u4", "c": 0}]


def test_a_row_whose_subquery_returns_nothing_is_dropped(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (n:User) CALL { WITH n MATCH (n)-[:FOLLOWS]->(f:User) RETURN f } "
        "RETURN n.id AS id, f.id AS fid ORDER BY id, fid",
    )
    assert rows == [{"id": "u1", "fid": "u2"}, {"id": "u1", "fid": "u3"}, {"id": "u2", "fid": "u3"}]


def test_an_uncorrelated_subquery_joins_every_row(db: Any) -> None:
    rows = _run(db, "UNWIND [1, 2] AS x CALL { MATCH (u:User) RETURN count(u) AS n } RETURN x, n ORDER BY x")
    assert rows == [{"x": 1, "n": 4}, {"x": 2, "n": 4}]


def test_union_all_keeps_duplicates_and_union_removes_them(db: Any) -> None:
    body = "MATCH (u:User) RETURN u.city AS city {} MATCH (u:User) WHERE u.city = 'SF' RETURN u.city AS city"
    union_all = _run(db, "CALL { " + body.format("UNION ALL") + " } RETURN count(city) AS n")
    union = _run(db, "CALL { " + body.format("UNION") + " } RETURN count(city) AS n")
    assert union_all == [6]
    assert union == [3]


def test_names_the_body_shares_with_the_outer_query_do_not_collide(db: Any) -> None:
    # The body's own ``u`` and ``f`` are new variables, not the outer ones.
    rows = _run(
        db,
        "MATCH (u:User)-[:FOLLOWS]->(f:User) "
        "CALL { MATCH (u:User)-[:FOLLOWS]->(f:User) RETURN count(f) AS edges } "
        "RETURN u.id AS id, f.id AS fid, edges ORDER BY id, fid",
    )
    assert rows == [
        {"id": "u1", "fid": "u2", "edges": 3},
        {"id": "u1", "fid": "u3", "edges": 3},
        {"id": "u2", "fid": "u3", "edges": 3},
    ]


def test_a_collected_list_is_imported_into_a_union(db: Any) -> None:
    # The shape of LDBC BI4: a list built by WITH, unwound in each branch.
    rows = _run(
        db,
        "MATCH (n:User) WHERE n.city = 'SF' WITH collect(n) AS ns "
        "CALL { WITH ns UNWIND ns AS m MATCH (m)-[:FOLLOWS]->(f:User) RETURN f.id AS fid "
        "UNION ALL WITH ns UNWIND ns AS m RETURN m.id AS fid } "
        "RETURN fid, count(*) AS n ORDER BY fid",
    )
    assert rows == [{"fid": "u1", "n": 1}, {"fid": "u2", "n": 2}, {"fid": "u3", "n": 2}]


def test_the_subquery_reads_a_parameter(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (n:User) CALL { WITH n MATCH (n)-[:FOLLOWS]->(f:User) WHERE f.city = $city RETURN count(f) AS c } "
        "RETURN n.id AS id, c ORDER BY id",
        {"city": "NYC"},
    )
    assert [r["c"] for r in rows] == [1, 1, 0, 0]


def test_an_aggregate_column_keeps_its_cypher_name_after_a_rename(db: Any) -> None:
    # ``a`` is grouped by the first WITH and bound again by the second MATCH;
    # the column was named after the AQL variable (``a_1``).
    rows = _run(
        db,
        "MATCH (a:User)-[:FOLLOWS]->(b:User) WITH a, count(b) AS c "
        "MATCH (a)-[:FOLLOWS]->(b2:User) RETURN a.id AS id, count(b2) AS d, a ORDER BY id",
    )
    assert [(r["id"], r["d"], r["a"]["id"]) for r in rows] == [("u1", 2, "u1"), ("u2", 1, "u2")]
