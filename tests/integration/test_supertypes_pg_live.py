"""Supertypes on PG (UNION over member collections), run on ArangoDB.

``Message`` is every ``post`` and every ``comment`` document. A tag is attached
to a forum as well as to messages, so a traversal into ``(m:Message)`` must
drop the forum; a scan must see both member collections; a node naming a
supertype its primary label is not a member of matches nothing. ``Place``
mixes members: ``City`` and ``Country`` share the ``place`` collection under a
type field (LABEL), beside them a ``Region`` that is not a Place, and
``Continent`` has a collection of its own. The unit counterpart is
``tests/test_translate_supertypes_pg.py``.
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

_DB = "arango_cypher_supertypes_pg_it"


def _entity(coll: str) -> dict[str, Any]:
    return {"collectionName": coll, "style": "COLLECTION", "properties": {}}


def _rel(edge: str, domain: str | list[str], range_: str) -> dict[str, Any]:
    return {"edgeCollectionName": edge, "style": "DEDICATED_COLLECTION", "domain": domain, "range": range_}


_MAPPING = MappingBundle(
    conceptual_schema={
        "entities": [
            {"name": "Message", "labels": ["Message"], "subtypes": ["Post", "Comment"], "properties": []},
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
            "LIKES": _rel("likes", "Person", ["Post", "Comment"]),
        },
    },
    metadata={},
)


def _label(type_value: str) -> dict[str, Any]:
    return {"collectionName": "place", "style": "LABEL", "typeField": "type", "typeValue": type_value}


_MIXED = MappingBundle(
    conceptual_schema={
        "entities": [
            {
                "name": "Place",
                "labels": ["Place"],
                "subtypes": ["City", "Country", "Continent"],
                "properties": [],
            },
        ],
        "relationships": [],
    },
    physical_mapping={
        "entities": {
            "City": _label("City"),
            "Country": _label("Country"),
            "Region": _label("Region"),
            "Continent": _entity("continent"),
            "Person": _entity("person"),
            "Place": {"style": "UNION", "members": ["City", "Country", "Continent"]},
        },
        "relationships": {
            "IS_LOCATED_IN": _rel("located_in", "Person", ["City", "Country", "Region", "Continent"]),
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
            "post": [{"_key": "p1", "id": 1, "length": 40}, {"_key": "p2", "id": 2, "length": 5}],
            "comment": [{"_key": "c3", "id": 3, "length": 12}],
            "forum": [{"_key": "f4", "id": 4}],
            "person": [{"_key": "ann", "id": 10, "name": "ann"}, {"_key": "bo", "id": 11, "name": "bo"}],
            "tag": [{"_key": "t1", "id": 100, "name": "graphs"}, {"_key": "t2", "id": 101, "name": "unused"}],
            "place": [
                {"_key": "c1", "id": 1, "type": "City"},
                {"_key": "k2", "id": 2, "type": "Country"},
                {"_key": "r3", "id": 3, "type": "Region"},
            ],
            "continent": [{"_key": "e4", "id": 4}],
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
            "likes": [("person/bo", "post/p1"), ("person/bo", "comment/c3")],
            "located_in": [
                ("person/ann", "place/c1"),
                ("person/ann", "place/r3"),
                ("person/ann", "continent/e4"),
            ],
        }
        for coll, pairs in edges.items():
            database.create_collection(coll, edge=True).import_bulk(
                [{"_from": a, "_to": b} for a, b in pairs]
            )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str, mapping: MappingBundle = _MAPPING, **params: Any) -> list[Any]:
    q = translate(cypher, mapping=mapping, params=params or None)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_a_scan_sees_every_member_collection(db: Any) -> None:
    assert _rows(db, "MATCH (m:Message) RETURN m.id AS id ORDER BY id") == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_inline_properties_select_across_members(db: Any) -> None:
    assert _rows(db, "MATCH (m:Message {id: $id}) RETURN m.length AS n", id=3) == [{"n": 12}]


def test_where_and_aggregates_over_the_union(db: Any) -> None:
    assert _rows(db, "MATCH (m:Message) WHERE m.length > 10 RETURN count(m) AS n") == [2]


def test_a_traversal_target_drops_documents_of_other_collections(db: Any) -> None:
    """t1 is on p1, c3 and forum f4; only the first two are Messages."""
    rows = _rows(db, "MATCH (t:Tag {id: 100})<-[:HAS_TAG]-(m:Message) RETURN m.id AS id ORDER BY id")
    assert rows == [{"id": 1}, {"id": 3}]


def test_a_target_the_edge_guarantees(db: Any) -> None:
    rows = _rows(db, "MATCH (p:Person {id: 10})<-[:HAS_CREATOR]-(m:Message) RETURN m.id AS id ORDER BY id")
    assert rows == [{"id": 1}, {"id": 3}]


def test_a_supertype_start_node(db: Any) -> None:
    rows = _rows(db, "MATCH (m:Message)-[:HAS_CREATOR]->(p:Person) RETURN m.id AS m, p.name AS p ORDER BY m")
    assert rows == [{"m": 1, "p": "ann"}, {"m": 2, "p": "bo"}, {"m": 3, "p": "ann"}]


def test_optional_supertype_target_null_extends(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (t:Tag) OPTIONAL MATCH (t)<-[:HAS_TAG]-(m:Message) "
        "RETURN t.name AS t, count(m) AS n ORDER BY t",
    )
    assert rows == [{"t": "graphs", "n": 2}, {"t": "unused", "n": 0}]


def test_label_predicate_on_a_supertype(db: Any) -> None:
    rows = _rows(
        db, "MATCH (t:Tag {id: 100})<-[:HAS_TAG]-(x:Forum) RETURN x.id AS id, x:Message AS isMessage"
    )
    assert rows == [{"id": 4, "isMessage": False}]


def test_a_supertype_the_primary_is_not_a_member_of_matches_nothing(db: Any) -> None:
    assert _rows(db, "MATCH (x:Person:Message) RETURN x.id") == []


def test_a_member_label_implies_its_supertype(db: Any) -> None:
    assert _rows(db, "MATCH (m:Post:Message) RETURN m.id AS id ORDER BY id") == [{"id": 1}, {"id": 2}]


def test_deleting_an_edge_matched_through_the_union(db: Any) -> None:
    q = translate(
        "MATCH (m:Message {id: 3})<-[r:LIKES]-(p:Person {id: 11}) DELETE r", mapping=_MAPPING, params=None
    )
    db.aql.execute(q.aql, bind_vars=q.bind_vars)
    rows = _rows(db, "MATCH (p:Person {id: 11})-[:LIKES]->(m:Message) RETURN m.id AS id")
    assert rows == [{"id": 1}]


def test_a_quoted_variable_scans_and_filters_the_union(db: Any) -> None:
    """Union bind names embed the variable; a quoted one was ERR 1501."""
    assert _rows(db, "MATCH (`a m`:Message) RETURN `a m`.id AS id ORDER BY id") == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]
    rows = _rows(db, "MATCH (t:Tag {id: 100})<-[:HAS_TAG]-(`a m`:Message) RETURN `a m`.id AS id ORDER BY id")
    assert rows == [{"id": 1}, {"id": 3}]


def test_a_scan_over_label_and_collection_members(db: Any) -> None:
    """City and Country are filtered by type out of ``place``; Region is left out."""
    rows = _rows(db, "MATCH (x:Place) RETURN x.id AS id ORDER BY id", _MIXED)
    assert rows == [{"id": 1}, {"id": 2}, {"id": 4}]


def test_inline_properties_select_a_label_member(db: Any) -> None:
    assert _rows(db, "MATCH (x:Place {id: 2}) RETURN x.type AS t", _MIXED) == [{"t": "Country"}]


def test_a_traversal_target_tests_label_membership(db: Any) -> None:
    """ann is located in a City, a Region and a Continent; the Region is not a Place."""
    rows = _rows(db, "MATCH (p:Person)-[:IS_LOCATED_IN]->(x:Place) RETURN x.id AS id ORDER BY id", _MIXED)
    assert rows == [{"id": 1}, {"id": 4}]


def test_label_predicate_with_label_members(db: Any) -> None:
    rows = _rows(db, "MATCH (x:City) RETURN x:Place AS a", _MIXED) + _rows(
        db, "MATCH (x:Region) RETURN x:Place AS a", _MIXED
    )
    assert rows == [{"a": True}, {"a": False}]
