"""The transpilers' own collections never reach the analyzed schema (issue #19).

Both arango-cypher-py and arango-sparql-py keep a schema cache and mined
examples in the databases they serve. The analyzer skips only ``_`` system
collections, so before this fix all four became classes, an invented
``SchemaCache`` shard family, statistics, sharding-profile entries and OWL
classes in cypher's schema. This runs the real analyzer (and the heuristic
tier, and the fingerprints) on a database holding all four next to a small
property graph.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.schema_acquire import (
    _build_heuristic_mapping,
    _full_fingerprint,
    _shape_fingerprint,
    acquire_mapping_bundle,
    classify_schema,
)

pytestmark = pytest.mark.integration

_DB = "arango_cypher_service_collections_it"
SERVICE = (
    "arango_cypher_schema_cache",
    "arango_cypher_examples",
    "arango_sparql_schema_cache",
    "arango_sparql_examples",
)
#: What the analyzer made of them: PascalCase class names and a shard family.
TELLTALES = (
    *SERVICE,
    "ArangoCypherSchemaCache",
    "ArangoCypherExamples",
    "ArangoSparqlSchemaCache",
    "ArangoSparqlExamples",
    "SchemaCache",
)


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    pytest.importorskip("schema_analyzer")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        people = database.create_collection("Person")
        movies = database.create_collection("Movie")
        acted_in = database.create_collection("ACTED_IN", edge=True)
        hanks = people.insert({"name": "Tom Hanks", "born": 1956})
        movie = movies.insert({"title": "Big", "released": 1988})
        acted_in.insert({"_from": hanks["_id"], "_to": movie["_id"], "roles": ["Josh"]})
        for name in SERVICE:
            database.create_collection(name).insert({"_key": "doc", "payload": {"bundle": "x" * 2000}})
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _visible(bundle: Any) -> str:
    return json.dumps(
        [bundle.conceptual_schema, bundle.physical_mapping, bundle.metadata, bundle.owl_turtle or ""],
        default=str,
    )


def test_the_analyzer_never_sees_them(db: Any) -> None:
    bundle = acquire_mapping_bundle(db, include_owl=True)
    text = _visible(bundle)
    leaked = [t for t in TELLTALES if t in text]
    assert leaked == [], f"service collections leaked into the analyzed schema: {leaked}"
    # The user's graph is still all there.
    assert set(bundle.physical_mapping["entities"]) >= {"Person", "Movie"}
    assert "ACTED_IN" in bundle.physical_mapping["relationships"]


def test_the_heuristic_tier_never_sees_them(db: Any) -> None:
    bundle = _build_heuristic_mapping(db, classify_schema(db))
    assert [t for t in TELLTALES if t in _visible(bundle)] == []


def test_writing_to_them_does_not_change_the_fingerprints(db: Any) -> None:
    shape, full = _shape_fingerprint(db), _full_fingerprint(db)
    for name in SERVICE:
        db.collection(name).insert({"payload": "another write"})
    db.create_collection("arango_sparql_examples_tmp_probe")  # a user collection does count
    try:
        assert _shape_fingerprint(db) != shape
    finally:
        db.delete_collection("arango_sparql_examples_tmp_probe")
    assert (_shape_fingerprint(db), _full_fingerprint(db)) == (shape, full)
