"""MappingBundle -> PredicateIndex, step 1 of the synthbank port.

The shape-derivation rules are a port of ``arango-sparql-py``'s mechanical
four-rule derivation, so each rule gets a test that would fail if the port
drifted — particularly the ``linked_entity`` false-positive guard, which is the
rule a naive implementation gets wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arango_query_core.mapping import MappingBundle

from arango_cypher.nl2cypher.predicate_index_builder import (
    build_predicate_index,
    build_predicate_signals,
    predicate_iri_for_property,
    predicate_iri_for_relationship,
)


def _bundle(entities: list[dict], relationships: list[dict]) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": entities, "relationships": relationships},
        physical_mapping={},
        metadata={},
    )


def _by_iri(index) -> dict:
    return {p.iri: p for p in index.retrieve("", k=10_000, dump=True)}


# --------------------------------------------------------------------------
# Shape derivation — one test per ported rule
# --------------------------------------------------------------------------


def test_node_property_is_a_literal_predicate() -> None:
    index = build_predicate_index(_bundle([{"name": "Person", "properties": [{"name": "name"}]}], []))
    pred = _by_iri(index)[predicate_iri_for_property("Person", "name")]

    assert pred.kind == "datatype"
    assert pred.shape == "literal"
    assert pred.domain == "Person"
    assert pred.range == "string"


def test_range_with_properties_and_no_outgoing_edges_is_a_value_object() -> None:
    """Rule 2: children non-empty and all datatype -> value_object."""
    index = build_predicate_index(
        _bundle(
            [
                {"name": "Product", "properties": [{"name": "sku"}]},
                {"name": "Price", "properties": [{"name": "amount", "type": "double"}]},
            ],
            [{"type": "HAS_PRICE", "fromEntity": "Product", "toEntity": "Price"}],
        )
    )
    pred = _by_iri(index)[predicate_iri_for_relationship("HAS_PRICE")]

    assert pred.shape == "value_object"
    assert pred.shape_detail == (("amount", "double"),), (
        "shape_detail must carry the datatype children so the renderer can emit the extra hop"
    )


def test_range_with_no_children_is_a_category_instance() -> None:
    """Rule 3: zero children -> category_instance."""
    index = build_predicate_index(
        _bundle(
            [{"name": "Movie", "properties": [{"name": "title"}]}, {"name": "Genre", "properties": []}],
            [{"type": "IN_GENRE", "fromEntity": "Movie", "toEntity": "Genre"}],
        )
    )

    assert _by_iri(index)[predicate_iri_for_relationship("IN_GENRE")].shape == "category_instance"


def test_undeclared_range_degrades_to_category_instance_without_raising() -> None:
    """Rule 3 also covers an undeclared range — it must degrade, not crash."""
    index = build_predicate_index(
        _bundle(
            [{"name": "Movie", "properties": [{"name": "title"}]}],
            [{"type": "IN_GENRE", "fromEntity": "Movie", "toEntity": "Genre"}],
        )
    )

    assert _by_iri(index)[predicate_iri_for_relationship("IN_GENRE")].shape == "category_instance"


def test_range_with_outgoing_edges_is_linked_entity_not_value_object() -> None:
    """Rule 4 — the false-positive guard.

    ``Manager`` has a property, so a naive "range has >=1 property" test would
    call this a value_object. It also has an outgoing relationship, which makes
    it a linked entity.
    """
    index = build_predicate_index(
        _bundle(
            [
                {"name": "Company", "properties": [{"name": "name"}]},
                {"name": "Manager", "properties": [{"name": "name"}]},
                {"name": "Report", "properties": [{"name": "name"}]},
            ],
            [
                {"type": "EMPLOYS", "fromEntity": "Company", "toEntity": "Manager"},
                {"type": "HAS_DIRECT_REPORT", "fromEntity": "Manager", "toEntity": "Report"},
            ],
        )
    )
    pred = _by_iri(index)[predicate_iri_for_relationship("EMPLOYS")]

    assert pred.shape == "linked_entity"
    assert pred.shape_detail == ()


# --------------------------------------------------------------------------
# Labels, signals, robustness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ACTED_IN", "acted in"), ("actedIn", "acted in"), ("has-price", "has price")],
)
def test_labels_are_humanised_so_retrieval_can_match_questions(raw: str, expected: str) -> None:
    index = build_predicate_index(
        _bundle(
            [{"name": "A", "properties": []}, {"name": "B", "properties": []}],
            [{"type": raw, "fromEntity": "A", "toEntity": "B"}],
        )
    )

    assert _by_iri(index)[predicate_iri_for_relationship(raw)].label == expected


def test_retrieval_matches_a_natural_question_via_the_humanised_label() -> None:
    index = build_predicate_index(
        _bundle(
            [{"name": "Person", "properties": []}, {"name": "Movie", "properties": []}],
            [{"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"}],
        )
    )

    hits = index.retrieve("which person acted in a movie?", k=5)

    assert [h.iri for h in hits] == [predicate_iri_for_relationship("ACTED_IN")]


def test_orderable_signal_tracks_declared_property_type() -> None:
    signals = build_predicate_signals(
        _bundle(
            [
                {
                    "name": "Product",
                    "properties": [
                        {"name": "price", "type": "double"},
                        {"name": "released", "type": "date"},
                        {"name": "sku", "type": "string"},
                    ],
                }
            ],
            [],
        )
    )

    assert signals[predicate_iri_for_property("Product", "price")].orderable
    assert signals[predicate_iri_for_property("Product", "released")].orderable
    assert not signals[predicate_iri_for_property("Product", "sku")].orderable


def test_optional_relation_defaults_false_without_instance_data() -> None:
    """The documented no-instance-data contract: False, never a crash."""
    bundle = _bundle(
        [{"name": "A", "properties": []}, {"name": "B", "properties": []}],
        [{"type": "R", "fromEntity": "A", "toEntity": "B"}],
    )
    iri = predicate_iri_for_relationship("R")

    assert not build_predicate_signals(bundle)[iri].optional_relation
    assert build_predicate_signals(bundle, optional_relations=frozenset({iri}))[iri].optional_relation


@pytest.mark.parametrize(
    "conceptual",
    [{}, {"entities": [], "relationships": []}, {"entities": "not-a-list"}, {"relationships": [None, 42]}],
)
def test_degraded_or_malformed_schemas_yield_an_empty_index_not_an_error(conceptual: dict) -> None:
    bundle = MappingBundle(conceptual_schema=conceptual, physical_mapping={}, metadata={})

    assert build_predicate_index(bundle).retrieve("anything", k=10, dump=True) == []


def test_none_mapping_is_tolerated() -> None:
    assert build_predicate_index(None).retrieve("anything", k=10, dump=True) == []


# --------------------------------------------------------------------------
# Against a real shipped mapping
# --------------------------------------------------------------------------


def test_real_icij_mapping_produces_predicates_for_every_relationship_and_property() -> None:
    raw = json.loads(Path("tests/fixtures/mappings/icij_paradise_papers.json").read_text())
    bundle = MappingBundle(
        conceptual_schema=raw["conceptual_schema"],
        physical_mapping=raw.get("physical_mapping", {}),
        metadata=raw.get("metadata", {}),
    )

    predicates = _by_iri(build_predicate_index(bundle))
    cs = raw["conceptual_schema"]
    expected = sum(len(e.get("properties", [])) for e in cs["entities"]) + len(cs["relationships"])

    assert len(predicates) == expected
    assert predicate_iri_for_relationship("OFFICER_OF") in predicates
    assert predicate_iri_for_property("Entity", "jurisdiction") in predicates
    assert {p.kind for p in predicates.values()} == {"object", "datatype"}


# --------------------------------------------------------------------------
# Feasibility proof: the promoted catalog drives off a Cypher-derived index
# --------------------------------------------------------------------------


def test_promoted_shape_catalog_gates_run_against_a_cypher_index() -> None:
    """The whole point of step 1: ``arango_query_core.nl.synthbank``'s gates
    were promoted so a Cypher front end could reuse them unmodified.

    Asserts every gate evaluates without error and that a typed schema makes
    all nine shapes eligible — if a future engine release changes the gate
    contract, this fails here rather than deep inside bank generation.
    """
    synthbank = pytest.importorskip("arango_query_core.nl.synthbank")

    bundle = _bundle(
        [
            {"name": "Person", "properties": [{"name": "name"}, {"name": "born", "type": "date"}]},
            {
                "name": "Movie",
                "properties": [
                    {"name": "title"},
                    {"name": "released", "type": "int"},
                    {"name": "rating", "type": "double"},
                ],
            },
            {"name": "Genre", "properties": []},
            # A leaf, attribute-bearing entity: the only way value_object fires.
            {"name": "BoxOffice", "properties": [{"name": "gross", "type": "double"}]},
        ],
        [
            {"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"},
            {"type": "DIRECTED", "fromEntity": "Person", "toEntity": "Movie"},
            {"type": "IN_GENRE", "fromEntity": "Movie", "toEntity": "Genre"},
            {"type": "GROSSED", "fromEntity": "Movie", "toEntity": "BoxOffice"},
        ],
    )
    index = build_predicate_index(bundle)
    signals = build_predicate_signals(bundle, optional_relations=frozenset({"rel:DIRECTED"}))
    predicates = index.retrieve("", k=10_000, dump=True)

    eligible = {
        shape.name: sum(1 for p in predicates if shape.applies(p, index, signals))
        for shape in synthbank.SHAPE_CATALOG
    }

    assert all(count > 0 for count in eligible.values()), (
        f"every promoted shape should be reachable from a typed Cypher schema; got {eligible}"
    )
    assert sum(eligible.values()) >= len(synthbank.SHAPE_CATALOG)


def test_untyped_properties_silently_disable_the_ordering_shapes() -> None:
    """A finding worth pinning: without declared property types, ``top_n`` /
    ``offset`` never fire, because ``orderable`` is derived from the type.

    Schema-analyzer bundles carry types, but heuristic fallbacks may not — and
    the failure mode is a quietly thinner bank, not an error.
    """
    synthbank = pytest.importorskip("arango_query_core.nl.synthbank")

    untyped = _bundle(
        [
            {"name": "Person", "properties": [{"name": "name"}, {"name": "born"}]},
            {"name": "Movie", "properties": [{"name": "title"}]},
        ],
        [{"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"}],
    )
    index = build_predicate_index(untyped)
    signals = build_predicate_signals(untyped)
    predicates = index.retrieve("", k=10_000, dump=True)

    ordering = {"top_n", "offset"}
    for shape in synthbank.SHAPE_CATALOG:
        if shape.name in ordering:
            assert not any(shape.applies(p, index, signals) for p in predicates)
