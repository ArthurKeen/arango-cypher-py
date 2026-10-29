"""A type the mapping declares for a property does not decide what an operator
computes (PRD §8.2).

The schema analyzer declares numeric properties ``"string"``. Taken at its
word, ``n.age + 1`` concatenated (``"211"``), ``ORDER BY n.id`` sorted as text
(1, 10, 11, 2, 9) and ``WHERE n.id < 10`` matched nothing. ``_MAPPING`` makes
the same declarations the analyzer does; the last test acquires the mapping
from the database itself.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle

from arango_cypher import translate

pytestmark = pytest.mark.integration

_DB = "arango_cypher_declared_types_it"

_PROPS = {
    "id": {"field": "id", "type": "string"},  # numbers, declared as the analyzer does
    "age": {"field": "age", "type": "string"},
    "name": {"field": "name", "type": "string"},
    "score": {"field": "score", "type": "integer"},  # whole and fractional values
}
_MAPPING = MappingBundle(
    conceptual_schema={"entities": [], "relationships": []},
    physical_mapping={
        "entities": {"People": {"collectionName": "people", "style": "COLLECTION", "properties": _PROPS}},
        "relationships": {},
    },
    metadata={},
)
_IDS = [1, 2, 9, 10, 11]


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
        database.create_collection("people").import_bulk(
            [
                {"_key": str(i), "id": i, "age": 20 + i, "name": n, "score": s}
                for i, n, s in zip(_IDS, ["b", "B", "a", "é", "e"], [7, 8, 7.5, 9, 3], strict=True)
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str, mapping: MappingBundle = _MAPPING) -> list[Any]:
    q = translate(cypher, mapping=mapping)
    return [
        next(iter(r.values())) if isinstance(r, dict) and len(r) == 1 else r
        for r in db.aql.execute(q.aql, bind_vars=q.bind_vars)
    ]


def test_numbers_declared_string_sort_as_numbers(db: Any) -> None:
    assert _rows(db, "MATCH (n:People) RETURN n.id AS id ORDER BY n.id") == _IDS


def test_numbers_declared_string_compare_as_numbers(db: Any) -> None:
    assert _rows(db, "MATCH (n:People) WHERE n.id < 10 RETURN n.id AS id ORDER BY id") == [1, 2, 9]


def test_numbers_declared_string_add(db: Any) -> None:
    assert _rows(db, "MATCH (n:People) RETURN n.age + 1 AS a ORDER BY a") == [22, 23, 30, 31, 32]


def test_strings_declared_string_still_sort_by_code_point(db: Any) -> None:
    assert _rows(db, "MATCH (n:People) RETURN n.name AS name ORDER BY n.name") == sorted(
        ["b", "B", "a", "é", "e"]
    )


def test_declared_integers_truncate_only_when_whole(db: Any) -> None:
    rows = _rows(db, "MATCH (n:People) RETURN n.id AS id, n.score / 2 AS half ORDER BY id")
    assert [r["half"] for r in rows] == [3, 4, 3.75, 4, 1]


def test_a_mapping_acquired_from_the_database(db: Any) -> None:
    pytest.importorskip("schema_analyzer", reason="the schema analyzer is an optional extra")
    from arango_cypher.schema_acquire import acquire_mapping_bundle

    mapping = acquire_mapping_bundle(db)
    label = next(iter(mapping.physical_mapping["entities"]))
    assert _rows(db, f"MATCH (n:{label}) RETURN n.id AS id ORDER BY n.id", mapping) == _IDS
    assert _rows(db, f"MATCH (n:{label}) WHERE n.id < 10 RETURN n.id AS id ORDER BY id", mapping) == [1, 2, 9]
    assert _rows(db, f"MATCH (n:{label}) RETURN n.age + 1 AS a ORDER BY a", mapping) == [22, 23, 30, 31, 32]
