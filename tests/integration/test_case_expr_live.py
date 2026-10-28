"""Cypher CASE run on ArangoDB.

AQL has no CASE expression; the translator emits nested ternaries. Before
that, every CASE was an ERR 1501 syntax error on the server while the goldens
(``tests/fixtures/cases/case_expr.yml``) stayed green. A null condition must
fall through to the next branch, as in Cypher.
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

_DB = "arango_cypher_case_expr_it"


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
                {"_key": "a", "name": "a", "age": 30, "status": "active"},
                {"_key": "b", "name": "b", "age": 15, "status": "inactive"},
                {"_key": "c", "name": "c", "age": 5},
                {"_key": "d", "name": "d"},
            ]
        )
        for coll in ("docs", "persons", "places"):  # named by a traversal's WITH clause
            database.create_collection(coll)
        database.create_collection("follows", edge=True).import_bulk([{"_from": "users/a", "_to": "users/b"}])
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_generic_case_takes_the_first_true_branch_and_skips_null_conditions(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) RETURN n.name AS n, CASE WHEN n.age > 18 THEN 'adult' "
        "WHEN n.age > 12 THEN 'teen' ELSE 'child' END AS c ORDER BY n",
    )
    assert rows == [
        {"n": "a", "c": "adult"},
        {"n": "b", "c": "teen"},
        {"n": "c", "c": "child"},
        {"n": "d", "c": "child"},
    ]


def test_simple_case_compares_the_test_value(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) RETURN n.name AS n, CASE n.status WHEN 'active' THEN 1 "
        "WHEN 'inactive' THEN 0 ELSE -1 END AS c ORDER BY n",
    )
    assert [r["c"] for r in rows] == [1, 0, -1, -1]


def test_simple_case_null_test_value_matches_no_when(db: Any) -> None:
    """``null = null`` is null in Cypher, so ``CASE null WHEN null`` takes the ELSE."""
    rows = _rows(
        db,
        "MATCH (n:User) RETURN n.name AS n, CASE n.status WHEN null THEN 'missing' ELSE 'set' END AS c ORDER BY n",
    )
    assert [r["c"] for r in rows] == ["set", "set", "set", "set"]


def test_case_without_else_is_null(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) RETURN n.name AS n, CASE WHEN n.age > 18 THEN 'yes' END AS c ORDER BY n")
    assert [r["c"] for r in rows] == ["yes", None, None, None]


def test_case_in_with_where_and_order_by(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) WITH n, CASE WHEN n.age IS NULL THEN 0 ELSE n.age END AS age "
        "WHERE CASE WHEN age > 10 THEN true ELSE false END "
        "RETURN n.name AS n ORDER BY CASE n.name WHEN 'b' THEN 0 ELSE 1 END, n",
    )
    assert rows == [{"n": "b"}, {"n": "a"}]


def test_case_on_an_optional_relationship(db: Any) -> None:
    """LDBC IS7: CASE WHEN r IS NULL over an OPTIONAL MATCH."""
    rows = _rows(
        db,
        "MATCH (n:User) OPTIONAL MATCH (n)-[r:FOLLOWS]->(:User) "
        "RETURN n.name AS n, CASE WHEN r IS NULL THEN false ELSE true END AS follows ORDER BY n",
    )
    assert [r["follows"] for r in rows] == [True, False, False, False]
