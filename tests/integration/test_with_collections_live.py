"""Traversals over collections whose names are not bare AQL identifiers.

Platform databases name collections like ``IAM-AWS-DOCS-DEMO_Chunks``. The
``WITH`` prelude spliced them in as text, so on such a database every
traversal failed with ERR 1501 (syntax error) before it ran. The unit
counterpart is ``tests/test_translate_with_collections.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.test_translate_with_collections import RENAMED, renamed_movies

pytestmark = pytest.mark.integration

_DB = "arango_cypher_with_collections_it"


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
        persons = database.create_collection(RENAMED["persons"])
        movies = database.create_collection(RENAMED["movies"])
        acted_in = database.create_collection(RENAMED["acted_in"], edge=True)
        persons.insert({"_key": "keanu", "name": "Keanu Reeves"})
        movies.insert({"_key": "matrix", "title": "The Matrix"})
        acted_in.insert({"_from": f"{RENAMED['persons']}/keanu", "_to": f"{RENAMED['movies']}/matrix"})
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def test_a_one_hop_traversal_runs(db: Any) -> None:
    q = translate(
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name AS who, m.title AS what",
        mapping=renamed_movies(),
    )
    assert q.aql.startswith("WITH @@")
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert rows == [{"who": "Keanu Reeves", "what": "The Matrix"}]


def test_a_traversal_returning_whole_nodes_runs(db: Any) -> None:
    """The query from the failing report: ``RETURN a, b`` over one hop."""
    q = translate("MATCH (a:Person)-[:ACTED_IN]->(b:Movie) RETURN a, b", mapping=renamed_movies())
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert [(r["a"]["name"], r["b"]["title"]) for r in rows] == [("Keanu Reeves", "The Matrix")]


def test_a_pattern_comprehension_runs(db: Any) -> None:
    q = translate(
        "MATCH (p:Person) RETURN p.name AS who, [(p)-[:ACTED_IN]->(m:Movie) | m.title] AS films",
        mapping=renamed_movies(),
    )
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert rows == [{"who": "Keanu Reeves", "films": ["The Matrix"]}]
