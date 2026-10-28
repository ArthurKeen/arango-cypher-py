"""Unnamed nodes and relationships, and grouped RETURN columns, on ArangoDB.

An unnamed node used to be emitted as ``v`` (an unnamed start node as ``u``,
an unnamed relationship as ``r``) whatever else the query named. Two unnamed
nodes declared ``v`` twice (ERR 1511, LDBC BI3), and a node the user named
``v`` later in the pattern became a back-reference to the unnamed one, which
joined the two and returned no rows. A grouped RETURN of ``a.name, b.name``
emitted two ``name`` keys (AQL keeps the last) and its ORDER BY resolved
``a.name`` to ``b.name``'s group variable.

Users a -> b -> c -> d follow each other in a chain.
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

_DB = "arango_cypher_anonymous_vars_it"


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
            [{"_key": k, "name": k, "rank": i} for i, k in enumerate("abcd")]
        )
        for coll in ("docs", "persons", "places"):  # named by a traversal's WITH clause
            database.create_collection(coll)
        database.create_collection("follows", edge=True).import_bulk(
            [{"_from": f"users/{a}", "_to": f"users/{b}"} for a, b in ("ab", "bc", "cd")]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        pytest.param(
            "MATCH (a:User)-[:FOLLOWS]->(:User)-[:FOLLOWS]->(:User) RETURN a.name AS a ORDER BY a",
            [{"a": "a"}, {"a": "b"}],
            id="two-unnamed-nodes",
        ),
        pytest.param(
            "MATCH (a:User)-[:FOLLOWS]->(:User)-[:FOLLOWS]->(v:User) RETURN a.name AS a, v.name AS v ORDER BY a",
            [{"a": "a", "v": "c"}, {"a": "b", "v": "d"}],
            id="unnamed-node-then-named-v",
        ),
        pytest.param(
            "MATCH (a:User)-[]->(b:User)-[r:FOLLOWS]->(c:User) RETURN a.name AS a ORDER BY a",
            [{"a": "a"}, {"a": "b"}],
            id="unnamed-relationship-then-named-r",
        ),
        pytest.param(
            "MATCH (:User)-[:FOLLOWS]->(u:User) RETURN u.name AS u ORDER BY u",
            [{"u": "b"}, {"u": "c"}, {"u": "d"}],
            id="unnamed-start-then-named-u",
        ),
        pytest.param(
            "MATCH (a:User) WITH a MATCH (a)-[:FOLLOWS]->(:User)-[:FOLLOWS]->(:User) RETURN a.name AS a ORDER BY a",
            [{"a": "a"}, {"a": "b"}],
            id="after-with-two-unnamed-nodes",
        ),
        pytest.param(
            "MATCH (v:User {name: 'a'}) WITH v MATCH (x:User)-[:FOLLOWS]->(:User) "
            "RETURN x.name AS x, v.name AS v ORDER BY x",
            [{"x": "a", "v": "a"}, {"x": "b", "v": "a"}, {"x": "c", "v": "a"}],
            id="after-with-v-then-unnamed-node",
        ),
    ],
)
def test_unnamed_pattern_elements_never_share_a_variable(db: Any, cypher: str, expected: list[Any]) -> None:
    assert _rows(db, cypher) == expected


def test_grouped_columns_from_the_same_property_keep_both_and_sort_by_the_right_one(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (a:User)-[:FOLLOWS]->(b:User) RETURN a.name, b.name, count(*) AS n ORDER BY n DESC, a.name DESC",
    )
    assert rows == [
        {"name": "c", "b_name": "d", "n": 1},
        {"name": "b", "b_name": "c", "n": 1},
        {"name": "a", "b_name": "b", "n": 1},
    ]
    rows = _rows(
        db, "MATCH (a:User)-[:FOLLOWS]->(b:User) RETURN a.rank, b.rank, count(*) AS n ORDER BY b.rank DESC"
    )
    assert [r["b_rank"] for r in rows] == [3, 2, 1]
