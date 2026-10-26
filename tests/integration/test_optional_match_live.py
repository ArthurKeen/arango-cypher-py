"""OPTIONAL MATCH, aggregate null semantics and clause order, run on ArangoDB.

Each case is a query whose old translation ran and returned wrong rows: an
OPTIONAL MATCH kept only the first match (``LET f = FIRST(...)``) or, after a
WITH, dropped the rows with no match; ``count(x)`` and ``collect(x)`` counted
and kept nulls; a MATCH after ``WITH … LIMIT`` was hoisted above the LIMIT.
LDBC BI12 exposed the first. The unit counterpart is
``tests/test_translate_optional_match_rows.py``.
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

_DB = "arango_cypher_optional_match_it"

#: alice follows bob and carol; bob, carol and dave follow no one. Only alice
#: has a nickname.
_USERS = [
    {"_key": "alice", "name": "alice", "nickname": "al", "age": 30},
    {"_key": "bob", "name": "bob", "age": 40},
    {"_key": "carol", "name": "carol"},
    {"_key": "dave", "name": "dave"},
]
_FOLLOWS = [("alice", "bob"), ("alice", "carol")]


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
        for name in ("docs", "persons", "places"):
            database.create_collection(name)
        database.create_collection("users").import_bulk(_USERS)
        database.create_collection("follows", edge=True).import_bulk(
            [{"_from": f"users/{a}", "_to": f"users/{b}"} for a, b in _FOLLOWS]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_optional_match_yields_every_match_and_null_extends_the_rest(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN u.name AS u, f.name AS f ORDER BY u, f",
    )
    assert rows == [
        {"u": "alice", "f": "bob"},
        {"u": "alice", "f": "carol"},
        {"u": "bob", "f": None},
        {"u": "carol", "f": None},
        {"u": "dave", "f": None},
    ]


def test_count_over_an_optional_match_skips_the_null_rows(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN u.name AS u, count(f) AS n ORDER BY u",
    )
    assert rows == [
        {"u": "alice", "n": 2},
        {"u": "bob", "n": 0},
        {"u": "carol", "n": 0},
        {"u": "dave", "n": 0},
    ]


def test_optional_match_before_with_keeps_the_rows_without_a_match(db: Any) -> None:
    """The BI12 shape: OPTIONAL MATCH, then aggregate in a WITH."""
    rows = _rows(
        db,
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) "
        "WITH u, count(f) AS n RETURN n, count(u) AS users ORDER BY n",
    )
    assert rows == [{"n": 0, "users": 3}, {"n": 2, "users": 1}]


def test_collect_skips_nulls(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN u.name AS u, collect(f.name) AS fs ORDER BY u",
    )
    assert [(r["u"], sorted(r["fs"])) for r in rows] == [
        ("alice", ["bob", "carol"]),
        ("bob", []),
        ("carol", []),
        ("dave", []),
    ]


def test_count_distinct_skips_nulls(db: Any) -> None:
    assert _rows(db, "MATCH (u:User) RETURN count(DISTINCT u.nickname) AS n") == [1]


def test_count_of_a_property_skips_missing_values(db: Any) -> None:
    assert _rows(db, "MATCH (u:User) RETURN count(u.age) AS n") == [2]


def test_sum_over_no_rows_is_zero(db: Any) -> None:
    assert _rows(db, "MATCH (u:User {name: 'nobody'}) RETURN sum(u.age) AS s") == [0]


def test_a_match_between_two_withs_reads_the_limited_rows(db: Any) -> None:
    """A MATCH between two WITHs was compiled above both, so the LIMIT applied
    to the traversal's rows instead of to the users."""
    rows = _rows(
        db,
        "MATCH (u:User) WITH u ORDER BY u.name LIMIT 1 MATCH (u)-[:FOLLOWS]->(f:User) "
        "WITH f RETURN f.name AS name ORDER BY name",
    )
    assert rows == [{"name": "bob"}, {"name": "carol"}]


def test_a_leading_optional_match_keeps_its_null_row(db: Any) -> None:
    assert _rows(db, "OPTIONAL MATCH (u:User {name: 'nobody'}) WITH u RETURN u") == [None]


def test_an_unwind_between_match_and_with_is_applied(db: Any) -> None:
    rows = _rows(
        db, "MATCH (u:User {name: 'alice'}) UNWIND [1, 2] AS k WITH u, k RETURN u.name AS u, k ORDER BY k"
    )
    assert rows == [{"u": "alice", "k": 1}, {"u": "alice", "k": 2}]
