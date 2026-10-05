"""Cypher names and strings against a real AQL parser.

The unit tests (``tests/test_translate_name_safety.py``) assert on AQL text;
whether that text means what the Cypher meant is the server's call. Before the
name guard, the splice query below returned every user from this database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_name_safety_it"
_SPLICE = "MATCH (n:User) WHERE n.`a\\` = n.`) OR true //` RETURN n.name AS x"


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
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
        users.insert_many(
            [
                {"_key": "alice", "name": "alice", "first name": "Alice", 'a"b': 1},
                {"_key": "bob", "name": "bob", "first name": "Bob", 'a"b': 2},
                {"_key": "slash", "name": "a\\"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_the_splice_never_reaches_the_server(db: Any) -> None:
    with pytest.raises(CoreError, match="backslash or a backtick"):
        _run(db, _SPLICE)


def test_a_quoted_name_with_a_space_reads_that_attribute(db: Any) -> None:
    rows = _run(db, "MATCH (n:User) WHERE n.`first name` = 'Bob' RETURN n.name AS x")
    assert rows == [{"x": "bob"}]


def test_a_backslash_in_a_string_literal_stays_inside_it(db: Any) -> None:
    assert _run(db, "MATCH (n:User) WHERE n.name = 'a\\\\' RETURN n.name AS x") == [{"x": "a\\"}]
    assert _run(db, 'MATCH (n:User) WHERE n.name = "a\\\\" RETURN n.name AS x') == [{"x": "a\\"}]


def test_remove_drops_exactly_the_quoted_attribute(db: Any) -> None:
    _run(db, "MATCH (n:User) WHERE n.name = 'alice' REMOVE n.`a\"b`")
    alice = db.collection("users").get("alice")
    assert 'a"b' not in alice
    assert alice["first name"] == "Alice"
    assert db.collection("users").get("bob")['a"b'] == 2


def test_remove_drops_a_plain_attribute_with_or_without_with(db: Any) -> None:
    users = db.collection("users")
    users.insert({"_key": "carol", "name": "carol", "age": 1, "keep": 1})
    _run(db, "MATCH (n:User) WHERE n.name = 'carol' REMOVE n.age")
    assert "age" not in users.get("carol") and users.get("carol")["keep"] == 1
    users.update({"_key": "carol", "age": 2})
    _run(db, "MATCH (n:User) WHERE n.name = 'carol' WITH n REMOVE n.age")
    assert "age" not in users.get("carol")


def test_a_backtick_quoted_parameter_binds(db: Any) -> None:
    assert _run(db, "MATCH (n:User) WHERE n.name = $`who` RETURN n.name AS x", {"who": "bob"}) == [
        {"x": "bob"}
    ]
