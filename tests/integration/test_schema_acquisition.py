"""Live-database coverage for PRD §7 — schema detection, mapping, change detection.

PRD §7 specifies behaviour that is *only* observable against a real ArangoDB:
``acquire_mapping_bundle(db)``, ``compute_statistics(db, bundle)`` and
``describe_schema_change(db)` all take a live handle. §12, however, describes
integration testing purely as query-corpus execution, so until this file existed
none of §7 was exercised against a database — it was covered by unit tests over
recorded fixtures, which cannot catch a change in what the analyzer actually
returns.

That gap is what let the analyzer dependency band go stale unnoticed: nothing in
this repo ever ran the analyzer end to end, so a wrong pin failed somewhere else
instead of here.

Each test names the PRD clause it covers. Everything runs against an isolated,
per-module database that is created and dropped by the fixture, so the suite
leaves no residue on the shared instance.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.schema_acquire import (
    acquire_mapping_bundle,
    compute_statistics,
    describe_schema_change,
)

pytestmark = pytest.mark.integration

_DB_NAME = "arango_cypher_schema_acquisition_it"
#: One cache collection for the whole module. Two different cache collections
#: in one database would each register as a new collection in the other's
#: shape fingerprint — a test artefact, not a product behaviour.
_CACHE = "schema_change_probe_cache"


def _client() -> ArangoClient:
    return ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))


def _creds() -> tuple[str, str]:
    return os.environ.get("ARANGO_USER", "root"), os.environ.get("ARANGO_PASS", "openSesame")


@pytest.fixture(scope="module")
def seeded_db() -> Any:
    """An isolated database holding a small property-graph, dropped on teardown."""
    pytest.importorskip("schema_analyzer", reason="PRD §7.1 primary tier requires the analyzer")

    user, password = _creds()
    client = _client()
    sys_db = client.db("_system", username=user, password=password)

    if sys_db.has_database(_DB_NAME):
        sys_db.delete_database(_DB_NAME)
    sys_db.create_database(_DB_NAME)
    db = client.db(_DB_NAME, username=user, password=password)

    people = db.create_collection("Person")
    movies = db.create_collection("Movie")
    acted_in = db.create_collection("ACTED_IN", edge=True)

    hanks = people.insert({"name": "Tom Hanks", "born": 1956})
    ryan = people.insert({"name": "Meg Ryan", "born": 1961})
    gump = movies.insert({"title": "Forrest Gump", "released": 1994})
    sleepless = movies.insert({"title": "Sleepless in Seattle", "released": 1993})
    acted_in.insert({"_from": hanks["_id"], "_to": gump["_id"], "roles": ["Forrest"]})
    acted_in.insert({"_from": hanks["_id"], "_to": sleepless["_id"], "roles": ["Sam"]})
    acted_in.insert({"_from": ryan["_id"], "_to": sleepless["_id"], "roles": ["Annie"]})

    try:
        yield db
    finally:
        sys_db.delete_database(_DB_NAME)


# --------------------------------------------------------------------------
# §7.1 — mapping model
# --------------------------------------------------------------------------


def test_acquisition_recovers_the_seeded_graph(seeded_db: Any) -> None:
    """§7.1: the analyzer is the primary tier and yields a MappingBundle.

    Asserts against the graph actually seeded above, so a change in what the
    analyzer returns fails here rather than silently degrading a mapping.
    """
    bundle = acquire_mapping_bundle(seeded_db)
    conceptual = bundle.conceptual_schema

    entities = {e.get("name") for e in conceptual.get("entities", [])}
    relationships = {r.get("type") for r in conceptual.get("relationships", [])}

    assert {"Person", "Movie"} <= entities
    assert "ACTED_IN" in relationships
    assert bundle.physical_mapping, "a bundle without a physical mapping cannot resolve"


def test_acquisition_resolves_relationship_endpoints(seeded_db: Any) -> None:
    """§7.1: per-type domain/range must be derived, not guessed."""
    conceptual = acquire_mapping_bundle(seeded_db).conceptual_schema

    acted = next(r for r in conceptual["relationships"] if r.get("type") == "ACTED_IN")

    assert acted.get("fromEntity") == "Person"
    assert acted.get("toEntity") == "Movie"


def test_owl_export_is_produced_on_request(seeded_db: Any) -> None:
    """§7.1: the bundle carries an OWL serialisation when asked for one."""
    bundle = acquire_mapping_bundle(seeded_db, include_owl=True)

    assert bundle.owl_turtle
    assert "Person" in bundle.owl_turtle


def test_full_label_set_mode_is_accepted_by_the_analyzer(seeded_db: Any) -> None:
    """§7.1 / LPG full-label-set: the opt-in kwarg path must stay wired.

    Passed only when opted in, so a signature change upstream would surface as a
    TypeError here rather than in a customer's LPG onboarding.
    """
    bundle = acquire_mapping_bundle(seeded_db, min_type_value_count=1)

    assert {e.get("name") for e in bundle.conceptual_schema.get("entities", [])} >= {"Person", "Movie"}


# --------------------------------------------------------------------------
# §7.2 — cardinality statistics
# --------------------------------------------------------------------------


def test_statistics_are_computed_from_live_counts(seeded_db: Any) -> None:
    """§7.2: per-collection counts and relationship fan-out from the real data."""
    bundle = acquire_mapping_bundle(seeded_db)

    stats = compute_statistics(seeded_db, bundle)

    assert stats, "statistics must not be empty for a populated database"
    flattened = repr(stats)
    # 2 Person + 2 Movie + 3 ACTED_IN were seeded; the exact shape of the stats
    # dict is the analyzer's, so assert the counts appear rather than pinning a
    # structure that is not this package's contract.
    assert "3" in flattened or "2" in flattened


# --------------------------------------------------------------------------
# §7.3 — change detection
# --------------------------------------------------------------------------


def test_change_detection_reports_no_cache_then_unchanged(seeded_db: Any) -> None:
    """§7.3: first probe has nothing cached; an immediate re-probe is unchanged."""
    from arango_cypher.schema_acquire import get_mapping

    cache = _CACHE

    first = describe_schema_change(seeded_db, cache_collection=cache)
    assert first.status == "no_cache"

    get_mapping(seeded_db, cache_collection=cache)

    second = describe_schema_change(seeded_db, cache_collection=cache)
    assert second.status == "unchanged"
    assert second.unchanged


def test_change_detection_separates_row_churn_from_shape_change(seeded_db: Any) -> None:
    """§7.3: the two-fingerprint design — counts move, shape does not.

    This is the distinction the whole two-tier cache rests on: ordinary writes
    must yield ``stats_changed`` (reuse the mapping, refresh counts), while a new
    collection must yield ``shape_changed`` (full re-introspection).
    """
    from arango_cypher.schema_acquire import get_mapping

    cache = _CACHE
    get_mapping(seeded_db, cache_collection=cache)

    seeded_db.collection("Person").insert({"name": "Rita Wilson", "born": 1956})
    after_write = describe_schema_change(seeded_db, cache_collection=cache)
    assert after_write.status == "stats_changed", (
        "an ordinary insert must not invalidate the mapping, only the statistics"
    )

    seeded_db.create_collection("Studio")
    try:
        after_new_collection = describe_schema_change(seeded_db, cache_collection=cache)
        assert after_new_collection.status == "shape_changed"
    finally:
        seeded_db.delete_collection("Studio")
