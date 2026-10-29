"""Inline pattern properties on ArangoDB: exact by default, case-insensitive on request.

``(n {name: 'alice'})`` used to compile to ``LOWER(n.name) == LOWER('alice')``,
which matched 'Alice' (Cypher does not) and kept the persistent index on
``name`` from serving the lookup.
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

_DB = "arango_cypher_inline_properties_it"


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
        users = database.create_collection("users")
        users.import_bulk(
            [
                {"_key": "a", "id": "u1", "name": "Alice"},
                {"_key": "b", "id": "u2", "name": "alice"},
                {"_key": "c", "id": "u3"},
            ]
        )
        users.add_index({"type": "persistent", "fields": ["name"], "name": "idx_users_name"})
        database.create_collection("follows", edge=True)
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None, **opts: Any) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), params=params, **opts)
    return sorted(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_a_string_literal_matches_exactly(db: Any) -> None:
    assert _run(db, "MATCH (n:User {name: 'alice'}) RETURN n.id") == ["u2"]


def test_the_exact_match_uses_the_index(db: Any) -> None:
    q = translate("MATCH (n:User {name: 'Alice'}) RETURN n.id", mapping=mapping_bundle_for("pg"))
    plan = db.aql.explain(q.aql, bind_vars=q.bind_vars)
    assert any(node["type"] == "IndexNode" for node in plan["nodes"]), plan["nodes"]


def test_a_null_parameter_matches_nothing(db: Any) -> None:
    # Cypher's ``=`` is never true for null; AQL's ``==`` would match u3.
    assert _run(db, "MATCH (n:User {name: $name}) RETURN n.id", {"name": None}) == []
    assert _run(db, "MATCH (n:User {name: $name}) RETURN n.id", {"name": "Alice"}) == ["u1"]


def test_case_insensitive_matching_is_an_opt_in(db: Any) -> None:
    rows = _run(db, "MATCH (n:User {name: 'ALICE'}) RETURN n.id", case_insensitive_inline_strings=True)
    assert rows == ["u1", "u2"]
