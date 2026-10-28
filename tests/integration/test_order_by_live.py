"""ORDER BY, run on ArangoDB: null placement, DISTINCT, aliases.

Cypher sorts null after every value ascending and before every value
descending; AQL sorts null below every value, so both directions were
reversed for nulls. ``RETURN DISTINCT x ORDER BY x DESC`` sorted ascending,
and an alias named like a variable (``RETURN a.name AS a ORDER BY a``)
sorted by the variable. User d has no name and no age.
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

_DB = "arango_cypher_order_by_it"


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
                {"_key": "a", "name": "b", "age": 30, "city": "SF"},
                {"_key": "b", "name": "a", "age": 20, "city": "SF"},
                {"_key": "c", "name": "c", "age": 40, "city": "NYC"},
                {"_key": "d"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_null_sorts_last_ascending_and_first_descending(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) RETURN n.age AS a ORDER BY n.age")
    assert [r["a"] for r in rows] == [20, 30, 40, None]
    rows = _rows(db, "MATCH (n:User) RETURN n.age AS a ORDER BY n.age DESC")
    assert [r["a"] for r in rows] == [None, 40, 30, 20]


def test_null_placement_after_with_and_after_aggregation(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) WITH n.name AS name ORDER BY name RETURN name")
    assert rows == ["a", "b", "c", None]
    rows = _rows(db, "MATCH (n:User) RETURN n.city AS city, count(*) AS c ORDER BY city DESC")
    assert rows == [{"city": None, "c": 1}, {"city": "SF", "c": 2}, {"city": "NYC", "c": 1}]


def test_distinct_honours_the_direction(db: Any) -> None:
    assert _rows(db, "MATCH (n:User) RETURN DISTINCT n.city AS city ORDER BY city DESC") == [
        None,
        "SF",
        "NYC",
    ]


def test_an_alias_named_like_a_variable_sorts_by_the_alias(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) WHERE n.name IS NOT NULL WITH n RETURN n.name AS n ORDER BY n DESC")
    assert rows == [
        {"n": "c"},
        {"n": "b"},
        {"n": "a"},
    ]
