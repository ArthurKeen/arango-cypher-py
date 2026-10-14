"""The ``WITH`` prelude of a traversal declares its vertex collections as bind
parameters, never as text.

Collection names come from the mapping. Platform databases carry names such as
``IAM-AWS-DOCS-DEMO_Chunks`` that do not parse as bare AQL identifiers: spliced
into ``WITH``, they made every traversal on such a database fail with ERR 1501.
The mapping also arrives with ``/translate`` and ``/execute`` requests, so a
spliced name was an AQL injection point too. The live counterpart is
``tests/integration/test_with_collections_live.py``.
"""

from __future__ import annotations

import copy

from arango_query_core import MappingBundle

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

#: movies_pg with the collection names a platform database actually has.
RENAMED = {"persons": "IAM-persons", "movies": "IAM-movies", "acted_in": "IAM-acted_in"}
INJECTION = "x RETURN 1 //"


def renamed_movies(extra_entity_collection: str | None = None) -> MappingBundle:
    """movies_pg with hyphenated collection names (and, optionally, one more
    entity whose collection name is *extra_entity_collection*)."""
    base = mapping_bundle_for("movies_pg")
    physical = copy.deepcopy(base.physical_mapping)
    for emap in physical["entities"].values():
        emap["collectionName"] = RENAMED.get(emap["collectionName"], emap["collectionName"])
    for rmap in physical["relationships"].values():
        rmap["edgeCollectionName"] = RENAMED.get(rmap["edgeCollectionName"], rmap["edgeCollectionName"])
    conceptual = copy.deepcopy(base.conceptual_schema)
    if extra_entity_collection is not None:
        physical["entities"]["Crafted"] = {"collectionName": extra_entity_collection, "style": "COLLECTION"}
        conceptual.setdefault("entities", []).append(
            {"name": "Crafted", "labels": ["Crafted"], "properties": []}
        )
    return MappingBundle(
        conceptual_schema=conceptual,
        physical_mapping=physical,
        metadata=copy.deepcopy(base.metadata),
        source=base.source,
    )


def _declared(aql: str, bind_vars: dict[str, object]) -> list[object]:
    """The collections the ``WITH`` line declares, through its bind vars."""
    first = aql.splitlines()[0]
    assert first.startswith("WITH ")
    refs = first[len("WITH ") :].split(", ")
    assert all(ref.startswith("@@") for ref in refs), first
    return [bind_vars[ref[1:]] for ref in refs]


def test_hyphenated_vertex_collections_are_declared_as_bind_parameters() -> None:
    out = translate(
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, m.title",
        mapping=renamed_movies(),
    )
    assert sorted(_declared(out.aql, out.bind_vars)) == ["IAM-movies", "IAM-persons"]
    assert "IAM-" not in out.aql


def test_a_collection_the_query_binds_reuses_its_bind_parameter() -> None:
    out = translate("MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN m.title", mapping=renamed_movies())
    first = out.aql.splitlines()[0]
    persons_keys = [k for k, v in out.bind_vars.items() if v == "IAM-persons"]
    assert len(persons_keys) == 1, out.bind_vars
    assert f"@{persons_keys[0]}" in first


def test_a_crafted_collection_name_never_reaches_the_aql_text() -> None:
    out = translate(
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN m.title",
        mapping=renamed_movies(extra_entity_collection=INJECTION),
    )
    assert INJECTION not in out.aql and "RETURN 1" not in out.aql
    assert INJECTION in _declared(out.aql, out.bind_vars)


def test_new_bind_names_never_overwrite_a_user_parameter() -> None:
    out = translate(
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE p.name = $withCollection0 RETURN m.title",
        mapping=renamed_movies(),
        params={"withCollection0": "Keanu Reeves"},
    )
    assert out.bind_vars["withCollection0"] == "Keanu Reeves"
    assert sorted(_declared(out.aql, out.bind_vars)) == ["IAM-movies", "IAM-persons"]


def test_queries_without_a_traversal_have_no_prelude() -> None:
    out = translate("MATCH (p:Person) RETURN p.name", mapping=renamed_movies())
    assert not out.aql.startswith("WITH")
    assert not any(k.startswith("@withCollection") for k in out.bind_vars)
