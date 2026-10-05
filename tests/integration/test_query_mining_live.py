"""Query mining against a real ArangoDB: every AQL statement it issues.

The unit tests use a python-arango double that does not parse AQL, which is how
``e.graph`` (GRAPH is an AQL keyword, even after a dot) reached production and
broke ``GET /examples``. Here each statement — the write check, the source run,
the verify loop through the real transpiler, the store and its listing — runs
on a real server, seeded with the movies PG dataset.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.query_mining.binding import SourceRejected, run_source
from arango_cypher.query_mining.harvest import SAVED_QUERIES, harvest_saved_queries
from arango_cypher.query_mining.miner import mine_query
from arango_cypher.query_mining.store import list_examples, save_outcomes
from arango_cypher.schema_acquire import acquire_mapping_bundle
from tests.integration.datasets import seed_movies_pg_dataset

pytestmark = pytest.mark.integration

_DB = "arango_cypher_mining_it"
_LLM_ENV = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")

CAST_AQL = "FOR m IN movies FILTER m.title == @t FOR p, e IN 1..1 INBOUND m acted_in RETURN e"


@pytest.fixture(scope="module")
def mined_db() -> Iterator[tuple[Any, Any]]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    with pytest.MonkeyPatch.context() as mp:
        for name in _LLM_ENV:
            mp.delenv(name, raising=False)
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)
        sys_db.create_database(_DB)
        try:
            db = client.db(_DB, username=user, password=password)
            seed_movies_pg_dataset(db)
            saved = db.create_collection(SAVED_QUERIES, system=True)
            saved.insert(
                {
                    "_key": "cast",
                    "name": "Cast of a movie",
                    "description": "Everyone who acted in a given movie",
                    "queryText": CAST_AQL,
                    "bindVariables": {"t": "The Matrix"},
                    "graphId": "movies",
                }
            )
            saved.insert(
                {
                    "_key": "writer",
                    "name": "Tag movies",
                    "description": "writes",
                    "queryText": "FOR m IN movies UPDATE m WITH {tagged: true} IN movies",
                    "bindVariables": {},
                    "graphId": "movies",
                }
            )
            yield db, acquire_mapping_bundle(db)
        finally:
            if sys_db.has_database(_DB):
                sys_db.delete_database(_DB)


class _Provider:
    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        cypher = "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN r"
        reply = f'{{"question": "Who acted in The Matrix?", "cypher": "{cypher}", "params": {{"t": "The Matrix"}}}}'
        return reply, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_harvests_from_the_real_system_collection(mined_db: tuple[Any, Any]) -> None:
    db, _ = mined_db
    names = sorted(q.name for q in harvest_saved_queries(db, graph="movies").queries)
    assert names == ["Cast of a movie", "Tag movies"]


def test_a_writing_saved_query_is_refused_from_its_real_plan(mined_db: tuple[Any, Any]) -> None:
    db, _ = mined_db
    writer = next(q for q in harvest_saved_queries(db).queries if q.key == "writer")
    with pytest.raises(SourceRejected, match="writes"):
        run_source(db, writer)
    assert db.collection("movies").find({"tagged": True}).count() == 0


def test_mines_stores_and_lists_a_verified_example(mined_db: tuple[Any, Any]) -> None:
    db, bundle = mined_db
    cast = next(q for q in harvest_saved_queries(db).queries if q.key == "cast")
    outcome = mine_query(db, bundle, cast, _Provider(), schema_summary="")
    assert outcome.verified, outcome.reason
    assert save_outcomes(db, [outcome], mapping_hash="h", model="scripted")["saved"] == 1

    for graph, expected in (("movies", 1), (None, 1), ("other_graph", 0)):
        rows = list_examples(db, graph=graph)
        assert len(rows) == expected, graph
    row = list_examples(db, graph="movies")[0]
    assert row["question"] == "Who acted in The Matrix?"
    assert "_id" not in row and "_rev" not in row
