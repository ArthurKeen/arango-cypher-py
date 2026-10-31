"""A string the mapping declares sorts and compares by code point on ArangoDB,
as in Cypher.

AQL compares strings with ICU collation (case ignored at the first level,
``é`` beside ``e``): ``Charles_de_Gaulle`` sorted before ``Charles_Haughey``,
and LDBC BI2's tie-break by tag name differed from Neo4j's.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_string_order_it"
_NAMES = ["Charles_de_Gaulle", "Charles_Haughey", "André", "Andrew", "b", "B", "a", "Ångström"]


def _mapping() -> Any:
    """The ``pg`` fixture with ``User.name`` declared a string."""
    bundle = mapping_bundle_for("pg")
    physical = copy.deepcopy(bundle.physical_mapping)
    physical["entities"]["User"].setdefault("properties", {})["name"] = {"field": "name", "type": "string"}
    return dataclasses.replace(bundle, physical_mapping=physical)


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
        rows = [{"_key": str(i), "id": i, "name": name} for i, name in enumerate(_NAMES)]
        rows.append({"_key": "nil", "id": 99})
        database.create_collection("users").import_bulk(rows)
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _names(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=_mapping())
    return [r["name"] for r in db.aql.execute(q.aql, bind_vars=q.bind_vars)]


def test_strings_sort_by_code_point_with_null_last(db: Any) -> None:
    rows = _names(db, "MATCH (n:User) RETURN n.name AS name ORDER BY name")
    assert rows == [*sorted(_NAMES), None]


def test_descending_puts_null_first(db: Any) -> None:
    rows = _names(db, "MATCH (n:User) RETURN n.name AS name ORDER BY n.name DESC")
    assert rows == [None, *sorted(_NAMES, reverse=True)]


def test_values_of_mixed_type_sort_in_cypher_order(db: Any) -> None:
    # Cypher's global order: strings, then booleans, then numbers (Neo4j 5.26
    # returns "B", "b", true, 1, 2). Strings of unknown type keep AQL's
    # collation among themselves, as their ``<`` does, so sorting and
    # comparing agree; AQL's own type order (booleans, numbers, strings) is not used.
    q = translate("UNWIND [2, 'b', 1, 'B', true] AS x RETURN x ORDER BY x", mapping=_mapping())
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert sorted(rows[:2]) == ["B", "b"]
    assert rows[2:] == [True, 1, 2]


def test_a_range_filter_compares_code_points(db: Any) -> None:
    # ``a`` and ``b`` sort after ``Z`` by code point; ICU put them before it.
    rows = _names(db, "MATCH (n:User) WHERE n.name < 'Z' RETURN n.name AS name ORDER BY n.name")
    assert rows == [n for n in sorted(_NAMES) if n < "Z"]


def test_a_top_k_by_name_keeps_the_cypher_order(db: Any) -> None:
    rows = _names(db, "MATCH (n:User) WHERE n.name IS NOT NULL RETURN n.name AS name ORDER BY n.name LIMIT 3")
    assert rows == sorted(_NAMES)[:3]
