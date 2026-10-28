"""Pattern comprehensions and ``exists(pattern)``, run on ArangoDB.

A pattern comprehension compiled only its first hop and ignored a
collection-style label on its end node, so
``[(t)<-[:HAS_TAG]-(m:Message)-[:HAS_CREATOR]->(p) | m]`` listed every tagged
forum, post and comment whoever created them (LDBC BI8); an end node already
bound was declared again (ERR 1511). ``exists((a)-->())`` was always true.
Tag 100 is on post 1 and comment 3 (both by ann) and on forum 4.
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

_DB = "arango_cypher_pattern_comprehension_it"


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
            "Forum": _entity("forum"),
            "Person": _entity("person"),
            "Tag": _entity("tag"),
            "Message": {"style": "UNION", "members": ["Post", "Comment"]},
        },
        "relationships": {
            "HAS_CREATOR": _rel("has_creator", ["Post", "Comment"], "Person"),
            "HAS_TAG": _rel("has_tag", ["Forum", "Post", "Comment"], "Tag"),
            "KNOWS": _rel("knows", "Person", "Person"),
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
            "post": [{"_key": "p1", "id": 1}, {"_key": "p2", "id": 2}],
            "comment": [{"_key": "c3", "id": 3}],
            "forum": [{"_key": "f4", "id": 4}],
            "person": [
                {"_key": "ann", "id": 10, "name": "ann"},
                {"_key": "bo", "id": 11, "name": "bo"},
                {"_key": "cy", "id": 12, "name": "cy"},
            ],
            "tag": [{"_key": "t1", "id": 100}],
        }
        for coll, rows in docs.items():
            database.create_collection(coll).import_bulk(rows)
        edges = {
            "has_creator": [
                ("post/p1", "person/ann"),
                ("post/p2", "person/bo"),
                ("comment/c3", "person/ann"),
            ],
            "has_tag": [("post/p1", "tag/t1"), ("comment/c3", "tag/t1"), ("forum/f4", "tag/t1")],
            "knows": [("person/ann", "person/bo"), ("person/bo", "person/cy")],
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


def test_every_hop_and_the_end_label_are_matched(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (t:Tag {id: 100}), (p:Person) "
        "RETURN p.name AS p, [(t)<-[:HAS_TAG]-(m:Message)-[:HAS_CREATOR]->(p) | m.id] AS ms ORDER BY p",
    )
    assert [(r["p"], sorted(r["ms"])) for r in rows] == [("ann", [1, 3]), ("bo", []), ("cy", [])]


def test_a_bound_end_node_is_matched_not_rebound(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Person {name: 'ann'}), (m:Post) RETURN m.id AS m, [(p)<-[:HAS_CREATOR]-(m) | 1] AS made ORDER BY m",
    )
    assert rows == [{"m": 1, "made": [1]}, {"m": 2, "made": []}]


def test_a_new_relationship_variable_is_introduced(db: Any) -> None:
    rows = _rows(db, "MATCH (p:Person {name: 'bo'}) RETURN [(p)-[k:KNOWS]-(f) | f.name] AS fs")
    assert sorted(rows[0]["fs"]) == ["ann", "cy"]


def test_a_named_path(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Person {name: 'ann'}) "
        "RETURN [x = (p)-[:KNOWS]->(f)-[:KNOWS]->(g) | [n IN nodes(x) | n.name]] AS ps",
    )
    assert rows == [{"ps": [["ann", "bo", "cy"]]}]


def test_a_variable_length_relationship_is_its_list_of_edges(db: Any) -> None:
    rows = _rows(
        db, "MATCH (p:Person {name: 'ann'}) RETURN [(p)-[r:KNOWS*1..2]->(f) | [f.name, size(r)]] AS fs"
    )
    assert sorted(rows[0]["fs"]) == [["bo", 1], ["cy", 2]]


def test_a_comprehension_variable_can_start_the_pattern(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Person) WITH collect(p) AS ps RETURN [x IN ps | size([(x)<-[:HAS_CREATOR]-(m:Message) | m])] AS ns",
    )
    assert sorted(rows[0]["ns"]) == [0, 1, 2]


def test_exists_of_a_pattern_is_the_pattern_predicate(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (p:Person) RETURN p.name AS p, exists((p)<-[:HAS_CREATOR]-()) AS wrote ORDER BY p",
    )
    assert rows == [{"p": "ann", "wrote": True}, {"p": "bo", "wrote": True}, {"p": "cy", "wrote": False}]
