"""Cypher arithmetic and ``+``, run on ArangoDB.

AQL's arithmetic is numeric only: ``null + 1`` is 1, ``-null`` is 0,
``'a' + 'b'`` is 0. Translated one to one, ``RETURN n.name + '!'`` returned 0
and ``n.age + 1`` returned 1 for a null age, silently. Each query runs on an
untyped mapping (the operand types are decided at run time) and on one that
declares the property types (the operation is chosen at translation time).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_arithmetic_it"


def _typed_mapping() -> MappingBundle:
    untyped = mapping_bundle_for("pg")
    physical = copy.deepcopy(untyped.physical_mapping)
    physical["entities"]["User"]["properties"] = {
        "name": {"field": "name", "type": "string"},
        "age": {"field": "age", "type": "integer"},
        "score": {"field": "score", "type": "number"},
        "tags": {"field": "tags", "type": "array"},
    }
    return MappingBundle(
        conceptual_schema=untyped.conceptual_schema, physical_mapping=physical, metadata=untyped.metadata
    )


_MAPPINGS = {"untyped": mapping_bundle_for("pg"), "typed": _typed_mapping()}


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
                {"_key": "a", "name": "a", "age": 30, "score": 1.5, "tags": ["x"]},
                {"_key": "b", "name": "b", "age": 7, "score": 2},
                {"_key": "c"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(params=sorted(_MAPPINGS))
def mapping(request: pytest.FixtureRequest) -> MappingBundle:
    return _MAPPINGS[request.param]


def _column(db: Any, mapping: MappingBundle, expr: str) -> list[Any]:
    q = translate(f"MATCH (n:User) RETURN n._key AS k, {expr} AS x ORDER BY k", mapping=mapping)
    return [row["x"] for row in db.aql.execute(q.aql, bind_vars=q.bind_vars)]


def _value(db: Any, expr: str) -> Any:
    q = translate(f"RETURN {expr} AS x", mapping=_MAPPINGS["untyped"])
    return next(iter(db.aql.execute(q.aql, bind_vars=q.bind_vars)))["x"]


def test_string_concatenation(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "n.name + '!'") == ["a!", "b!", None]
    assert _column(db, mapping, "'<' + n.name + '>'") == ["<a>", "<b>", None]
    assert _column(db, mapping, "n.name + n.age") == ["a30", "b7", None]


def test_arithmetic_with_a_null_operand_is_null(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "n.age + 1") == [31, 8, None]
    assert _column(db, mapping, "(n.age * 2) + 1") == [61, 15, None]
    assert _column(db, mapping, "-n.age") == [-30, -7, None]
    assert _column(db, mapping, "n.age - n.score") == [28.5, 5, None]
    assert _column(db, mapping, "n.age % 4") == [2, 3, None]
    assert _column(db, mapping, "n.age ^ 2") == [900, 49, None]


def test_a_function_boundary_keeps_its_own_null_guard(db: Any, mapping: MappingBundle) -> None:
    """coalesce() turns the null difference into 0 before the outer + 2."""
    assert _column(db, mapping, "coalesce(n.age - 1, 0) + 2") == [31, 8, 2]


def test_list_addition(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "n.tags + 'y'") == [["x", "y"], None, None]


def test_a_filter_on_arithmetic(db: Any, mapping: MappingBundle) -> None:
    q = translate("MATCH (n:User) WHERE n.age + 1 > 10 RETURN n._key AS k", mapping=mapping)
    assert list(db.aql.execute(q.aql, bind_vars=q.bind_vars)) == [{"k": "a"}]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("'a' + 'b'", "ab"),
        ("1 + 2 + 'a'", "3a"),
        ("'a' + 1 + 2", "a12"),
        ("1 + 'a'", "1a"),
        ("[1] + [2]", [1, 2]),
        ("[1] + 2", [1, 2]),
        ("0 + [1]", [0, 1]),
        # Neo4j 5.26: a null operand makes + null, lists included.
        ("[1] + null", None),
        ("null + [1]", None),
        ("[1] + [null]", [1, None]),
        ("null + 1", None),
        ("'a' + null", None),
        ("-null", None),
        ("7 / 2", 3),
        ("-7 / 2", -3),
        ("7.0 / 2", 3.5),
        ("7 % 3", 1),
        ("2 ^ 3", 8),
    ],
)
def test_literal_expressions(db: Any, expr: str, expected: Any) -> None:
    assert _value(db, expr) == expected
