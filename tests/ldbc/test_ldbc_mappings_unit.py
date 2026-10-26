"""The committed LDBC mapping fixtures are exactly what ``tests/ldbc/mappings.py`` builds."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingResolver

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.ldbc import mappings
from tests.ldbc.loader import lpg_indexes, pg_indexes
from tests.ldbc.schema import CONCRETE_LABELS, REL_TYPES, SUPERTYPES


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_committed_fixture_is_current(layout):
    committed = mappings.fixture_path(layout).read_text(encoding="utf-8")
    assert committed == mappings.render(layout), (
        "stale fixture: run `.venv/bin/python -m tests.ldbc.mappings --write`"
    )


def test_check_mode_reports_current_fixtures():
    assert mappings.main([]) == 0


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_every_concrete_label_and_type_resolves(layout):
    r = MappingResolver(mapping_bundle_for(f"ldbc_{layout}"))
    supertypes = set(SUPERTYPES) if layout == "lpg" else set()
    assert set(r.all_entity_labels()) == set(CONCRETE_LABELS) | supertypes
    assert sorted(r.all_relationship_types()) == sorted(REL_TYPES)


@pytest.mark.parametrize("supertype", sorted(SUPERTYPES))
def test_lpg_supertypes_are_multi_label_over_their_members(supertype):
    r = MappingResolver(mapping_bundle_for("ldbc_lpg"))
    assert r.resolve_entity(supertype)["style"] == "MULTI_LABEL"
    assert r.supertype_members(supertype) == list(SUPERTYPES[supertype])


@pytest.mark.parametrize("supertype", sorted(SUPERTYPES))
def test_pg_supertypes_still_fail_closed_until_stage_2(supertype):
    with pytest.raises(CoreError) as exc:
        translate(f"MATCH (n:{supertype}) RETURN n.id", mapping=mapping_bundle_for("ldbc_pg"))
    assert exc.value.code == "MAPPING_NOT_FOUND"


def test_polymorphic_relationships_declare_union_endpoints():
    rel = mappings.build("pg")["physicalMapping"]["relationships"]["HAS_CREATOR"]
    assert rel["domain"] == ["Post", "Comment"]
    assert rel["range"] == "Person"


def test_indexes_match_what_the_loader_creates():
    pg = mappings.build("pg")["physicalMapping"]["entities"]
    declared = {(e["collectionName"], tuple(i["fields"])) for e in pg.values() for i in e["indexes"]}
    assert declared == {(s.collection, s.fields) for s in pg_indexes()}

    lpg = mappings.build("lpg")["physicalMapping"]
    vcis = {tuple(i["fields"]) for i in lpg["relationships"]["KNOWS"]["indexes"] if i.get("vci")}
    assert vcis == {s.fields for s in lpg_indexes() if s.collection == "edges"}


def test_lpg_traversal_uses_the_anchor_led_vci():
    q = translate(
        "MATCH (p:Person {id: 1})-[:KNOWS]->(f:Person) RETURN f.id",
        mapping=mapping_bundle_for("ldbc_lpg"),
    )
    assert "idx_edges_from_type" in q.aql


def test_build_rejects_an_unknown_layout():
    with pytest.raises(ValueError, match="unknown layout"):
        mappings.build("hybrid")  # type: ignore[arg-type]
