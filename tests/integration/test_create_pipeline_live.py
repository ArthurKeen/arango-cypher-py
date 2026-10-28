"""CREATE before a WITH (``… CREATE … WITH … MATCH … CREATE``), run on ArangoDB.

These were refused ("Updating clauses are not supported"; LDBC INS1/4/6/7).
Inserting where each CREATE stands writes the LPG edge collection twice and
reads the node collection after writing it (ERR 1579). Reads now come first
and each collection is written once, on PG and LPG. Each test gets a fresh
database.
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

_DB = "arango_cypher_create_pipeline_it"


def _pg() -> MappingBundle:
    def ent(c: str) -> dict[str, Any]:
        return {"collectionName": c, "style": "COLLECTION", "properties": {}}

    def rel(e: str, d: str, r: str) -> dict[str, Any]:
        return {"edgeCollectionName": e, "style": "DEDICATED_COLLECTION", "domain": d, "range": r}

    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {"Person": ent("person"), "Forum": ent("forum"), "Tag": ent("tag")},
            "relationships": {
                "HAS_MODERATOR": rel("has_moderator", "Forum", "Person"),
                "HAS_TAG": rel("has_tag", "Forum", "Tag"),
            },
        },
        metadata={},
    )


def _lpg() -> MappingBundle:
    def ent(t: str) -> dict[str, Any]:
        return {"collectionName": "nodes", "style": "LABEL", "typeField": "type", "typeValue": t}

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
            "entities": {"Person": ent("Person"), "Forum": ent("Forum"), "Tag": ent("Tag")},
            "relationships": {"HAS_MODERATOR": rel("HAS_MODERATOR"), "HAS_TAG": rel("HAS_TAG")},
        },
        metadata={},
    )


_MAPPINGS = {"pg": _pg(), "lpg": _lpg()}
_INS4 = (
    "MATCH (p:Person {id: 1}) CREATE (f:Forum {id: 10, title: 'Wall'})-[:HAS_MODERATOR]->(p) "
    "WITH f UNWIND $tagIds AS tagId MATCH (t:Tag {id: tagId}) CREATE (f)-[:HAS_TAG]->(t)"
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
        database.create_collection("person").import_bulk([{"_key": "ann", "id": 1}])
        database.create_collection("tag").import_bulk([{"_key": "t1", "id": 100}, {"_key": "t2", "id": 101}])
        database.create_collection("forum")
        for e in ("has_moderator", "has_tag", "edges"):
            database.create_collection(e, edge=True)
        database.create_collection("nodes").import_bulk(
            [
                {"_key": "ann", "id": 1, "type": "Person"},
                {"_key": "t1", "id": 100, "type": "Tag"},
                {"_key": "t2", "id": 101, "type": "Tag"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(params=sorted(_MAPPINGS))
def layout(request: pytest.FixtureRequest) -> str:
    return request.param


def _run(db: Any, layout: str, cypher: str, **params: Any) -> list[Any]:
    q = translate(cypher, mapping=_MAPPINGS[layout], params=params or None)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _graph(db: Any, layout: str) -> tuple[list[Any], list[tuple[str, str, str]]]:
    """The forums, and every edge as (type, from id, to id), whatever the layout."""
    ids = {}
    for coll in ("person", "tag", "forum", "nodes"):
        for d in db.collection(coll).all():
            ids[d["_id"]] = d["id"]
    if layout == "pg":
        forums = [d["title"] for d in db.collection("forum").all()]
        edges = [
            (t.upper(), ids[e["_from"]], ids[e["_to"]])
            for t in ("has_moderator", "has_tag")
            for e in db.collection(t).all()
        ]
    else:
        forums = [d["title"] for d in db.collection("nodes").all() if d["type"] == "Forum"]
        edges = [(e["type"], ids[e["_from"]], ids[e["_to"]]) for e in db.collection("edges").all()]
    return forums, sorted(edges)


def test_a_create_pipeline_writes_every_part(db: Any, layout: str) -> None:
    """LDBC INS4's shape."""
    _run(db, layout, _INS4, tagIds=[100, 101])
    assert _graph(db, layout) == (
        ["Wall"],
        [("HAS_MODERATOR", 10, 1), ("HAS_TAG", 10, 100), ("HAS_TAG", 10, 101)],
    )


def test_a_later_part_that_matches_nothing_keeps_the_earlier_creation(db: Any, layout: str) -> None:
    _run(db, layout, _INS4, tagIds=[])
    assert _graph(db, layout) == (["Wall"], [("HAS_MODERATOR", 10, 1)])


def test_a_return_reads_the_last_part(db: Any, layout: str) -> None:
    rows = _run(
        db,
        layout,
        "MATCH (p:Person {id: 1}) CREATE (f:Forum {id: 10, title: 'Wall'})-[:HAS_MODERATOR]->(p) "
        "WITH f, count(*) AS n UNWIND $tagIds AS tagId MATCH (t:Tag {id: tagId}) CREATE (f)-[:HAS_TAG]->(t) "
        "RETURN f.title AS title, t.id AS tag ORDER BY tag",
        tagIds=[100, 101],
    )
    assert rows == [{"title": "Wall", "tag": 100}, {"title": "Wall", "tag": 101}]


def test_reading_what_the_query_created_is_refused(layout: str) -> None:
    with pytest.raises(CoreError, match="reads what the query created"):
        translate(
            "MATCH (p:Person {id: 1}) CREATE (f:Forum {id: 10})-[:HAS_MODERATOR]->(p) "
            "WITH p MATCH (x:Forum) RETURN x",
            mapping=_MAPPINGS[layout],
        )


def test_a_return_over_several_rows_inserts_once(db: Any, layout: str) -> None:
    """Inlined into the final loop, the last stage drew the INSERTs into it:
    they ran once per returned row (ERR 1210)."""
    rows = _run(
        db,
        layout,
        "UNWIND [1, 2, 3, 4] AS x CREATE (f:Forum {id: x, title: 'f'}) WITH f WHERE f.id % 2 = 0 RETURN f.id AS id",
    )
    assert sorted(r["id"] for r in rows) == [2, 4]
    assert _graph(db, layout)[0] == ["f", "f", "f", "f"]


def test_a_named_created_relationship_is_a_variable(db: Any, layout: str) -> None:
    rows = _run(
        db,
        layout,
        "MATCH (p:Person {id: 1}) CREATE (f:Forum {id: 10, title: 'Wall'})-[r:HAS_MODERATOR]->(p) "
        "WITH r, count(*) AS n UNWIND [1] AS one RETURN n",
    )
    assert rows == [1]
