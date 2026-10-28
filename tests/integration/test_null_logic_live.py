"""Cypher's three-valued logic (true / false / null), run on ArangoDB.

AQL compares two-valued: ``null == null`` and ``null != 'x'`` are true, and
``NOT null`` is true. Translated one to one, ``WHERE n.x = null`` kept rows
whose ``x`` was null and ``WHERE NOT n.name = 'b'`` kept nameless rows, both
silently. Users ``c`` and ``d`` lack properties, which is null in Cypher.
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

_DB = "arango_cypher_null_logic_it"


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
        users = database.create_collection("users")
        users.import_bulk(
            [
                {"_key": "a", "name": "a", "age": 30, "city": "SF", "active": True},
                {"_key": "b", "name": "b", "age": 15, "city": "NYC", "active": False},
                {"_key": "c", "age": 5},
                {"_key": "d", "name": "d"},
            ]
        )
        users.add_persistent_index(fields=["name"])
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _translate(cypher: str, **params: Any) -> Any:
    return translate(cypher, mapping=mapping_bundle_for("pg"), params=params or None)


def _rows(db: Any, cypher: str, **params: Any) -> list[Any]:
    q = _translate(cypher, **params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _names(db: Any, where: str, **params: Any) -> list[str]:
    return _rows(db, f"MATCH (n:User) WHERE {where} RETURN n._key AS k ORDER BY k", **params)


def _keys(rows: list[Any]) -> list[str]:
    return [r["k"] for r in rows]


def test_equality_with_null_matches_nothing(db: Any) -> None:
    assert _names(db, "n.name = null") == []
    assert _names(db, "null = n.name") == []
    assert _names(db, "n.name <> null") == []


def test_equality_with_a_null_parameter_matches_nothing(db: Any) -> None:
    assert _names(db, "n.name = $p", p=None) == []
    assert _keys(_names(db, "n.name = $p", p="a")) == ["a"]


def test_inequality_drops_rows_whose_property_is_null(db: Any) -> None:
    assert _keys(_names(db, "n.name <> 'b'")) == ["a", "d"]


def test_not_of_a_comparison_drops_rows_whose_property_is_null(db: Any) -> None:
    assert _keys(_names(db, "NOT n.name = 'b'")) == ["a", "d"]
    assert _keys(_names(db, "NOT n.age > 10")) == ["c"]


def test_de_morgan_under_not(db: Any) -> None:
    # a: false OR false; b and c: true OR false; d: null OR null.
    assert _keys(_names(db, "NOT (n.age < 20 OR n.city = 'LA')")) == ["a"]
    # NOT (true AND null) is null; NOT (false AND null) is true.
    assert _keys(_names(db, "NOT (n.age > 10 AND n.city = 'SF')")) == ["b", "c"]


def test_property_to_property_equality_ignores_two_nulls(db: Any) -> None:
    rows = _rows(
        db, "MATCH (n:User), (m:User) WHERE n.city = m.city RETURN n._key AS a, m._key AS b ORDER BY a"
    )
    assert rows == [{"a": "a", "b": "a"}, {"a": "b", "b": "b"}]


def test_not_of_a_null_boolean_property_is_null(db: Any) -> None:
    assert _keys(_names(db, "NOT n.active")) == ["b"]
    rows = _rows(db, "MATCH (n:User) RETURN n._key AS k, NOT n.active AS x ORDER BY k")
    assert [r["x"] for r in rows] == [False, True, None, None]


def test_returned_comparisons_keep_null(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) RETURN n._key AS k, n.age > 10 AS older, n.name = 'a' AS isA, "
        "n.name = null AS eqNull, n.city <> 'SF' AS notSf ORDER BY k",
    )
    assert rows == [
        {"k": "a", "older": True, "isA": True, "eqNull": None, "notSf": False},
        {"k": "b", "older": True, "isA": False, "eqNull": None, "notSf": True},
        {"k": "c", "older": False, "isA": None, "eqNull": None, "notSf": None},
        {"k": "d", "older": None, "isA": False, "eqNull": None, "notSf": None},
    ]


def test_in_with_null_operands(db: Any) -> None:
    assert _keys(_names(db, "NOT n.name IN ['a']")) == ["b", "d"]
    rows = _rows(db, "RETURN null IN [null] AS a, 1 IN [1, 2] AS b, $n IN null AS c", n=1)
    assert rows == [{"a": None, "b": True, "c": None}]


def test_string_operators_with_null_operands(db: Any) -> None:
    assert _keys(_names(db, "NOT n.name STARTS WITH 'a'")) == ["b", "d"]
    assert _names(db, "n.name CONTAINS $p", p=None) == []
    rows = _rows(db, "MATCH (n:User) RETURN n._key AS k, n.name ENDS WITH 'a' AS x ORDER BY k")
    assert [r["x"] for r in rows] == [True, False, None, False]


def test_xor_with_a_null_operand_is_null(db: Any) -> None:
    rows = _rows(db, "MATCH (n:User) RETURN n._key AS k, (n.age > 10) XOR (n.city = 'SF') AS x ORDER BY k")
    assert [r["x"] for r in rows] == [False, True, None, None]


def test_case_and_comprehensions_treat_a_null_condition_as_not_true(db: Any) -> None:
    rows = _rows(
        db, "MATCH (n:User) RETURN n._key AS k, CASE WHEN n.age > 10 THEN 1 ELSE 0 END AS x ORDER BY k"
    )
    assert [r["x"] for r in rows] == [1, 1, 0, 0]
    rows = _rows(db, "RETURN [x IN [1, null, 3] WHERE NOT x = 1] AS xs")
    assert rows == [{"xs": [3]}]


def test_an_equality_filter_still_uses_an_index(db: Any) -> None:
    """The null guard sits on the bind parameter, which ArangoDB folds away."""
    for where, params in (
        ("n.name = $p", {"p": "a"}),
        ("n.name = $p AND n.age > 3", {"p": "a"}),
        ("n.name = $p OR n.name = $q", {"p": "a", "q": "b"}),
    ):
        q = _translate(f"MATCH (n:User) WHERE {where} RETURN n", **params)
        plan = db.aql.explain(q.aql, bind_vars=q.bind_vars)
        nodes = {node["type"] for node in plan["nodes"]}
        assert "IndexNode" in nodes, (where, q.aql, nodes)
