"""Relationships of several types (``-[:A|B]->``), run on ArangoDB, on PG and LPG.

Several types were refused ("Exactly one relationship type is required",
LDBC IC12). On PG a traversal follows each type's edge collection; on LPG it
tests the type field against the list. A MATCH after WITH also takes more
than two hops now, and keeps its relationships distinct, as Cypher does.

ann KNOWS bo, ann FOLLOWS cy, bo FOLLOWS cy.
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

_DB = "arango_cypher_multi_type_it"


def _pg() -> MappingBundle:
    def rel(edge: str) -> dict[str, Any]:
        return {
            "edgeCollectionName": edge,
            "style": "DEDICATED_COLLECTION",
            "domain": "Person",
            "range": "Person",
        }

    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {"Person": {"collectionName": "person", "style": "COLLECTION", "properties": {}}},
            "relationships": {"KNOWS": rel("knows"), "FOLLOWS": rel("follows")},
        },
        metadata={},
    )


def _lpg() -> MappingBundle:
    def rel(t: str) -> dict[str, Any]:
        return {
            "edgeCollectionName": "edges",
            "style": "GENERIC_WITH_TYPE",
            "typeField": "type",
            "typeValue": t,
        }

    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {
                "Person": {
                    "collectionName": "nodes",
                    "style": "LABEL",
                    "typeField": "type",
                    "typeValue": "Person",
                }
            },
            "relationships": {"KNOWS": rel("KNOWS"), "FOLLOWS": rel("FOLLOWS")},
        },
        metadata={},
    )


_MAPPINGS = {"pg": _pg(), "lpg": _lpg()}
_PEOPLE = [("ann", 1), ("bo", 2), ("cy", 3)]
_EDGES = [("ann", "bo", "KNOWS"), ("ann", "cy", "FOLLOWS"), ("bo", "cy", "FOLLOWS")]


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
        database.create_collection("person").import_bulk([{"_key": k, "id": i} for k, i in _PEOPLE])
        database.create_collection("nodes").import_bulk(
            [{"_key": k, "id": i, "type": "Person"} for k, i in _PEOPLE]
        )
        for t in ("knows", "follows"):
            database.create_collection(t, edge=True).import_bulk(
                [{"_from": f"person/{a}", "_to": f"person/{b}"} for a, b, tt in _EDGES if tt.lower() == t]
            )
        database.create_collection("edges", edge=True).import_bulk(
            [{"_from": f"nodes/{a}", "_to": f"nodes/{b}", "type": t} for a, b, t in _EDGES]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(params=sorted(_MAPPINGS))
def mapping(request: pytest.FixtureRequest) -> MappingBundle:
    return _MAPPINGS[request.param]


def _rows(db: Any, mapping: MappingBundle, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_a_hop_follows_every_listed_type(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(
        db,
        mapping,
        "MATCH (a:Person {id: 1})-[r:KNOWS|FOLLOWS]->(b:Person) RETURN b.id AS b, type(r) AS t ORDER BY b",
    )
    assert rows == [{"b": 2, "t": "KNOWS"}, {"b": 3, "t": "FOLLOWS"}]


def test_a_variable_length_hop_over_several_types(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(db, mapping, "MATCH (a:Person {id: 1})-[:KNOWS|FOLLOWS*2]->(c:Person) RETURN c.id AS c")
    assert rows == [{"c": 3}]  # ann KNOWS bo FOLLOWS cy


def test_a_type_not_listed_is_not_followed(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(db, mapping, "MATCH (a:Person {id: 2})-[:KNOWS]->(b:Person) RETURN b.id AS b")
    assert rows == []


def test_a_long_match_after_with(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(
        db,
        mapping,
        "MATCH (a:Person {id: 1}) WITH a MATCH (a)-[:KNOWS]->(b:Person)-[:FOLLOWS]->(c:Person)<-[:FOLLOWS]-(d:Person) "
        "RETURN d.id AS d ORDER BY d",
    )
    assert rows == [{"d": 1}]  # ann FOLLOWS cy as well; bo's own edge is not matched twice


def test_one_relationship_is_not_matched_twice_in_a_pattern(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(
        db,
        mapping,
        "MATCH (a:Person {id: 1}) WITH a MATCH (a)-[:KNOWS]-(b:Person)-[:KNOWS]-(c:Person) RETURN c.id AS c",
    )
    assert rows == []  # only ann-bo KNOWS: reaching back to ann reuses that edge
