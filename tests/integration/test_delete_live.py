"""DELETE and DETACH DELETE, run on ArangoDB: read every matched row, then write.

Deleting inside the matching loop failed as soon as a pattern matched more
than one row: a DETACH subquery walked edges the loop had already removed
(ERR 1579 on PG, ERR 1202 on LPG; LDBC DEL6, DEL7). A delete after OPTIONAL
MATCH or over a supertype was refused (DEL1, DEL4, DEL6 on PG). Each test
gets a fresh database: the writes are the point.

Comment c2 replies to post p1, c3 to c2; ann created p1 and c3.
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

_DB = "arango_cypher_delete_it"


def _entity(coll: str) -> dict[str, Any]:
    return {"collectionName": coll, "style": "COLLECTION", "properties": {}}


def _rel(edge: str, domain: str | list[str], range_: str | list[str]) -> dict[str, Any]:
    return {"edgeCollectionName": edge, "style": "DEDICATED_COLLECTION", "domain": domain, "range": range_}


_MAPPING = MappingBundle(
    conceptual_schema={
        "entities": [
            {"name": "Message", "labels": ["Message"], "subtypes": ["Post", "Comment"], "properties": []}
        ],
        "relationships": [],
    },
    physical_mapping={
        "entities": {
            "Post": _entity("post"),
            "Comment": _entity("comment"),
            "Person": _entity("person"),
            "Message": {"style": "UNION", "members": ["Post", "Comment"]},
        },
        "relationships": {
            "HAS_CREATOR": _rel("has_creator", ["Post", "Comment"], "Person"),
            "REPLY_OF": _rel("reply_of", "Comment", ["Post", "Comment"]),
            "KNOWS": _rel("knows", "Person", "Person"),
        },
    },
    metadata={},
)


@pytest.fixture
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
            "comment": [{"_key": "c2", "id": 2}, {"_key": "c3", "id": 3}],
            "person": [{"_key": "ann", "id": 10}, {"_key": "bo", "id": 11}],
        }
        for coll, rows in docs.items():
            database.create_collection(coll).import_bulk(rows)
        edges = {
            "has_creator": [("post/p1", "person/ann"), ("comment/c3", "person/ann")],
            "reply_of": [("comment/c2", "post/p1"), ("comment/c3", "comment/c2")],
            "knows": [("person/ann", "person/bo")],
        }
        for coll, pairs in edges.items():
            database.create_collection(coll, edge=True).import_bulk(
                [{"_from": a, "_to": b} for a, b in pairs]
            )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=_MAPPING)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _keys(db: Any, coll: str) -> list[str]:
    return sorted(doc["_key"] for doc in db.collection(coll).all())


def _edges(db: Any, coll: str) -> list[tuple[str, str]]:
    return sorted((e["_from"], e["_to"]) for e in db.collection(coll).all())


def test_detach_delete_over_a_variable_length_pattern(db: Any) -> None:
    """LDBC DEL7: a comment and every reply to it, transitively."""
    rows = _run(db, "MATCH (:Comment {id: 2})<-[:REPLY_OF*0..]-(c:Comment) DETACH DELETE c RETURN count(*)")
    assert rows == [2]
    assert _keys(db, "comment") == []
    assert _edges(db, "reply_of") == []
    assert _edges(db, "has_creator") == [("post/p1", "person/ann")]


def test_detach_delete_of_a_supertype_variable(db: Any) -> None:
    """LDBC DEL6: a post and its thread, across both member collections."""
    _run(db, "MATCH (:Post {id: 1})<-[:REPLY_OF*0..]-(m:Message) DETACH DELETE m")
    assert _keys(db, "post") == [] and _keys(db, "comment") == []
    assert _edges(db, "has_creator") == [] and _edges(db, "reply_of") == []
    assert _keys(db, "person") == ["ann", "bo"]


def test_delete_after_optional_match_skips_the_unmatched(db: Any) -> None:
    """LDBC DEL1 shape: bo created nothing, so only bo goes."""
    rows = _run(
        db,
        "MATCH (p:Person {id: 11}) OPTIONAL MATCH (p)<-[:HAS_CREATOR]-(m:Message) DETACH DELETE p, m RETURN count(*)",
    )
    assert rows == [1]
    assert _keys(db, "person") == ["ann"]
    assert _edges(db, "knows") == []
    assert _keys(db, "post") == ["p1"] and _keys(db, "comment") == ["c2", "c3"]


def test_two_deleted_variables_in_one_collection(db: Any) -> None:
    _run(db, "MATCH (a:Person {id: 10})-[:KNOWS]->(b:Person) DETACH DELETE a, b")
    assert _keys(db, "person") == []
    assert _edges(db, "knows") == [] and _edges(db, "has_creator") == []


def test_delete_relationships_only(db: Any) -> None:
    rows = _run(db, "MATCH (:Comment)-[r:REPLY_OF]->(:Message) DELETE r RETURN count(*)")
    assert rows == [2]
    assert _edges(db, "reply_of") == []
    assert _keys(db, "comment") == ["c2", "c3"]


def test_a_node_matched_on_several_rows_is_deleted_once(db: Any) -> None:
    """ann created two messages: two rows, one deletion."""
    rows = _run(db, "MATCH (p:Person {id: 10})<-[:HAS_CREATOR]-(m:Message) DETACH DELETE p RETURN count(*)")
    assert rows == [2]
    assert _keys(db, "person") == ["bo"]
