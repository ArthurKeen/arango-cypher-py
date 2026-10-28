"""Unlabeled nodes on a multi-collection (PG) mapping, run on ArangoDB.

``(:Person)<-[:HAS_CREATOR]-(m)`` was refused ("A single label is required"):
the translator checked an unlabeled traversal target against the mapping's
one node collection and had none to pick here. An unlabeled node matches any
node the edges lead to, so there is nothing to check (LDBC IS2, IC3 on PG).
An unlabeled start node is scanned in the collection its first edge
guarantees (``(x)<-[:HAS_CREATOR]-(m)``: a Person; LDBC IC5 on PG).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import CoreError, MappingBundle

from arango_cypher import translate

pytestmark = pytest.mark.integration

_DB = "arango_cypher_unlabeled_pg_it"


def _entity(coll: str) -> dict[str, Any]:
    return {"collectionName": coll, "style": "COLLECTION", "properties": {}}


def _rel(edge: str, domain: str | list[str], range_: str | list[str]) -> dict[str, Any]:
    return {"edgeCollectionName": edge, "style": "DEDICATED_COLLECTION", "domain": domain, "range": range_}


_MAPPING = MappingBundle(
    conceptual_schema={"entities": [], "relationships": []},
    physical_mapping={
        "entities": {"Post": _entity("post"), "Comment": _entity("comment"), "Person": _entity("person")},
        "relationships": {
            "HAS_CREATOR": _rel("has_creator", ["Post", "Comment"], "Person"),
            "KNOWS": _rel("knows", "Person", "Person"),
            "LINKS": _rel("links", ["Post", "Person"], ["Comment", "Person"]),
        },
    },
    metadata={},
)


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
        docs = {
            "post": [{"_key": "p1", "id": 1}],
            "comment": [{"_key": "c2", "id": 2}],
            "person": [{"_key": "ann", "id": 10}, {"_key": "bo", "id": 11}],
        }
        for coll, rows in docs.items():
            database.create_collection(coll).import_bulk(rows)
        edges = {
            "has_creator": [("post/p1", "person/ann"), ("comment/c2", "person/ann")],
            "knows": [("person/ann", "person/bo")],
            "links": [("post/p1", "comment/c2")],
        }
        for coll, pairs in edges.items():
            database.create_collection(coll, edge=True).import_bulk(
                [{"_from": a, "_to": b} for a, b in pairs]
            )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=_MAPPING)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_an_unlabeled_target_is_any_node_the_edge_reaches(db: Any) -> None:
    rows = _rows(db, "MATCH (:Person {id: 10})<-[:HAS_CREATOR]-(m) RETURN m.id AS id ORDER BY id")
    assert rows == [{"id": 1}, {"id": 2}]


def test_an_unlabeled_target_after_with(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Person {id: 10}) WITH p MATCH (p)<-[:HAS_CREATOR]-(m) RETURN m.id AS id ORDER BY id",
    )
    assert rows == [{"id": 1}, {"id": 2}]


def test_an_unlabeled_start_is_scanned_where_its_edge_leads(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Post {id: 1}) WITH p OPTIONAL MATCH (x)<-[:HAS_CREATOR]-(p) RETURN x.id AS creator",
    )
    assert rows == [{"creator": 10}]


def test_an_unlabeled_start_the_edge_does_not_settle_is_refused() -> None:
    """LINKS starts at a Post or a Person: two collections, no guess."""
    with pytest.raises(CoreError, match="single label is required"):
        translate("MATCH (c:Comment) WITH c MATCH (x)-[:LINKS]->(c) RETURN x", mapping=_MAPPING)
