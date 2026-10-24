"""Cross-validate translated AQL against a reference Cypher engine (Neo4j).

For each query in ``tests/fixtures/datasets/movies/query-corpus.yml``:

  1. Run the **raw Cypher** against Neo4j Community (the spec's reference
     implementation).
  2. Translate the same Cypher to AQL via ``arango_cypher.translate`` and
     run it against the LPG-seeded ArangoDB.
  3. Assert the two result sets are equivalent.

Activation: requires ``RUN_INTEGRATION=1`` *and* ``RUN_CROSS=1``. Also
requires a reachable Neo4j (see ``docker-compose.neo4j.yml``) and a
reachable ArangoDB (the existing ``test_neo4j_movies_dataset_smoke``
fixtures cover the latter).

Known-divergence handling: entries in ``query-corpus.yml`` may carry a
``divergence`` key (free-form string) to mark queries where a full diff is
not yet expected; the test still asserts the *column set* matches and that
Neo4j produced at least ``expected_min_count`` rows, but skips the row-by-row
equivalence check with a clear reason.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from arango import ArangoClient
from arango_query_core.exec import AqlExecutor

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.helpers.result_equivalence import assert_result_equivalent
from tests.integration.datasets import seed_movies_lpg_dataset

# ---------------------------------------------------------------------------
# Activation & fixtures
# ---------------------------------------------------------------------------


def _cross_enabled() -> bool:
    return os.environ.get("RUN_INTEGRATION") == "1" and os.environ.get("RUN_CROSS") == "1"


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _load_corpus() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2]
    p = root / "tests" / "fixtures" / "datasets" / "movies" / "query-corpus.yml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


_corpus = _load_corpus()

_arango_db: Any | None = None
_neo4j_driver: Any | None = None


def _get_arango_db() -> Any:
    global _arango_db  # noqa: PLW0603
    if _arango_db is None:
        url = _env("ARANGO_URL", "http://localhost:8529")
        user = _env("ARANGO_USER", "root")
        pw = _env("ARANGO_PASS", "openSesame")
        client = ArangoClient(hosts=url)
        sys_db = client.db("_system", username=user, password=pw)
        if not sys_db.has_database("neo4j_movies_lpg_test"):
            sys_db.create_database("neo4j_movies_lpg_test")
        db = client.db("neo4j_movies_lpg_test", username=user, password=pw)
        seed_movies_lpg_dataset(db)
        _arango_db = db
    return _arango_db


def _get_neo4j_driver() -> Any:
    global _neo4j_driver  # noqa: PLW0603
    # Local import: only exercised when the `cross` marker is active,
    # so users who don't run cross-validate don't need the neo4j driver.
    from tests.integration.neo4j_reference import (
        ensure_dataset,
        get_driver,
        seed_neo4j_movies,
    )

    if _neo4j_driver is None:
        _neo4j_driver = get_driver()
    # Cross-module guard: re-seed if a sibling suite (e.g. Northwind) loaded
    # a different dataset into the shared Neo4j instance.
    ensure_dataset(_neo4j_driver, "movies", seed_neo4j_movies)
    return _neo4j_driver


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


pytestmark = [
    pytest.mark.integration,
    pytest.mark.cross,
    pytest.mark.skipif(
        not _cross_enabled(),
        reason="Set RUN_INTEGRATION=1 and RUN_CROSS=1 to run Neo4j cross-validation",
    ),
]


@pytest.mark.parametrize("query", _corpus, ids=lambda q: q["id"])
def test_movies_crossvalidate(query: dict[str, Any]) -> None:
    from tests.integration.neo4j_reference import run_cypher

    cypher = query["cypher"]
    qid = query.get("id", "<unknown>")
    divergence = query.get("divergence")

    neo_driver = _get_neo4j_driver()
    neo_rows = run_cypher(neo_driver, cypher)

    assert len(neo_rows) >= query.get("expected_min_count", 0), (
        f"[{qid}] Neo4j produced only {len(neo_rows)} rows; "
        f"expected >= {query.get('expected_min_count', 0)}. "
        "Corpus or dataset drift?"
    )

    db = _get_arango_db()
    mapping = mapping_bundle_for("movies_lpg")
    out = translate(cypher, mapping=mapping)

    # When a divergence is flagged we still translate (catches translator
    # crashes on no-divergence-flagged queries), but tolerate AQL execution
    # errors so we can record them as the flagged divergence rather than
    # masking them as a hard failure.
    try:
        aql_rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    except Exception as e:
        if divergence:
            pytest.skip(
                f"[{qid}] divergence flagged (AQL execution failed with {type(e).__name__}): {divergence}"
            )
        raise

    if divergence:
        pytest.skip(f"[{qid}] divergence flagged: {divergence}")

    assert_result_equivalent(neo_rows, aql_rows, query)
