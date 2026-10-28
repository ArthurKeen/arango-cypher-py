"""Inline properties of a MATCH after WITH, run on ArangoDB.

A WITH that groups again renames a carried variable (``city`` becomes
``city_1``); the inline property ``(q:User {city: city})`` of a later MATCH
kept the old name, which AQL read as a collection (ERR 1203, LDBC IC6).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_bound_match_properties_it"


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
                {"_key": "a", "id": "u1", "city": "SF"},
                {"_key": "b", "id": "u2", "city": "SF"},
                {"_key": "c", "id": "u3", "city": "NYC"},
            ]
        )
        for coll in ("docs", "persons", "places"):  # named by a traversal's WITH clause
            database.create_collection(coll)
        database.create_collection("follows", edge=True).import_bulk(
            [{"_from": "users/a", "_to": "users/b", "w": 3}, {"_from": "users/a", "_to": "users/c", "w": 3}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def test_inline_properties_refer_to_a_variable_a_with_renamed(db: Any) -> None:
    q = translate(
        "MATCH (t:User {id: 'u1'}) WITH t.city AS city MATCH (p:User) WITH city, count(p) AS n "
        "MATCH (q:User {city: city})-[:FOLLOWS {w: n}]->(r:User {city: city}) RETURN q.id AS id, r.id AS to",
        mapping=mapping_bundle_for("pg"),
    )
    assert "city_1" in q.aql
    assert list(db.aql.execute(q.aql, bind_vars=q.bind_vars)) == [{"id": "u1", "to": "u2"}]
