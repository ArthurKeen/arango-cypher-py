"""Cypher's toInteger / toFloat / toString / toBoolean, run on ArangoDB.

AQL's conversions are total where Cypher's return null, and TO_NUMBER does not
truncate: toInteger(3.7) was 3.7, toInteger('abc') was 0, toBoolean('false')
was true and toString(null) was "". Each column runs on an untyped mapping
(the argument's type is tested at run time) and a typed one.
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

_DB = "arango_cypher_conversions_it"


def _typed_mapping() -> MappingBundle:
    untyped = mapping_bundle_for("pg")
    physical = copy.deepcopy(untyped.physical_mapping)
    physical["entities"]["User"]["properties"] = {
        "f": {"field": "f", "type": "number"},
        "s": {"field": "s", "type": "string"},
        "b": {"field": "b", "type": "boolean"},
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
                {"_key": "a", "f": 3.7, "s": "42", "b": True},
                {"_key": "b", "f": -3.7, "s": " -2.5e1 ", "b": False},
                {"_key": "c", "f": 0, "s": "abc"},
                {"_key": "d"},
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


def test_to_integer_truncates_and_rejects_non_numeric_strings(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "toInteger(n.f)") == [3, -3, 0, None]
    assert _column(db, mapping, "toInteger(n.s)") == [42, -25, None, None]
    assert _column(db, mapping, "toInteger(n.b)") == [1, 0, None, None]


def test_to_float_rejects_non_numeric_strings(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "toFloat(n.f)") == [3.7, -3.7, 0, None]
    assert _column(db, mapping, "toFloat(n.s)") == [42, -25, None, None]


def test_to_string_keeps_null(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "toString(n.s)") == ["42", " -2.5e1 ", "abc", None]
    assert _column(db, mapping, "toString(n.b)") == ["true", "false", None, None]


def test_to_boolean_reads_true_and_false_only(db: Any, mapping: MappingBundle) -> None:
    assert _column(db, mapping, "toBoolean(n.b)") == [True, False, None, None]
    assert _column(db, mapping, "toBoolean(n.s)") == [None, None, None, None]
    assert _column(db, mapping, "toBoolean(n.f)") == [True, True, False, None]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("toInteger('42')", 42),
        ("toInteger(' 7 ')", 7),
        ("toInteger('3.9')", 3),
        ("toInteger(-3.9)", -3),
        ("toInteger('x')", None),
        ("toFloat('1e2')", 100),
        ("toFloat('.5')", 0.5),
        ("toBoolean('TRUE')", True),
        ("toBoolean('false')", False),
        ("toBoolean('yes')", None),
        ("toString(null)", None),
        ("toString(12)", "12"),
    ],
)
def test_literal_conversions(db: Any, expr: str, expected: Any) -> None:
    q = translate(f"RETURN {expr} AS x", mapping=_MAPPINGS["untyped"])
    assert next(iter(db.aql.execute(q.aql, bind_vars=q.bind_vars)))["x"] == expected


def test_conversions_inside_aggregates(db: Any) -> None:
    q = translate(
        "MATCH (n:User) RETURN sum(toInteger(n.f)) AS s, count(toInteger(n.s)) AS c",
        mapping=_MAPPINGS["typed"],
    )
    assert list(db.aql.execute(q.aql, bind_vars=q.bind_vars)) == [{"s": 0, "c": 2}]
