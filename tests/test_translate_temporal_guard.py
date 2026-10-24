"""Temporal accessors on mapped temporal properties fail closed (PRD §8.4).

Before the guard, ``p.creationDate.year`` translated to the AQL attribute
access ``p.creationDate.year`` — ``null`` on any stored timestamp — so BI1-style
queries "translated" and returned wrong rows (docs/ldbc_benchmark_plan.md §3.2).
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.temporal_guard import (
    TEMPORAL_ACCESSORS,
    reject_temporal_accessor,
    temporal_properties,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(params=["ldbc_pg", "ldbc_lpg"])
def ldbc(request):
    return mapping_bundle_for(request.param)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) RETURN p.creationDate.year AS y",
        "MATCH (p:Person) WHERE p.birthday.month = 5 RETURN p.id",
        "MATCH (p:Person {id: 1}) RETURN p.creationDate.epochMillis AS e",
        "MATCH (a:Person)-[k:KNOWS]->(b:Person) RETURN k.creationDate.day AS d",
        "MATCH (p:Post) WITH p ORDER BY p.creationDate.year RETURN p.id",
    ],
)
def test_accessor_on_a_temporal_property_is_refused(ldbc, cypher):
    with pytest.raises(CoreError) as exc:
        translate(cypher, mapping=ldbc)
    assert exc.value.code == "UNSUPPORTED"
    assert "Temporal accessor" in str(exc.value)
    assert "WP-41" in str(exc.value)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) RETURN p.creationDate AS c",
        "MATCH (p:Person) WHERE p.creationDate < $d RETURN p.id",
        # A sub-key that is not a temporal accessor is left alone.
        "MATCH (p:Person) RETURN p.creationDate.someField AS x",
        # A temporal accessor name on a non-temporal property is left alone.
        "MATCH (p:Person) RETURN p.firstName.year AS x",
    ],
)
def test_other_lookups_still_translate(ldbc, cypher):
    assert translate(cypher, mapping=ldbc, params={"d": 0}).aql


def test_mappings_without_temporal_declarations_are_unaffected():
    q = translate("MATCH (m:Movie) RETURN m.released.year AS y", mapping=mapping_bundle_for("movies_pg"))
    assert "m.released.year" in q.aql


def test_temporal_properties_collects_entities_and_relationships():
    temporal = temporal_properties(MappingResolver(mapping_bundle_for("ldbc_pg")))
    assert temporal["creationDate"] == "datetime"
    assert temporal["birthday"] == "date"
    assert "firstName" not in temporal


def test_guard_without_resolver_or_chain_is_a_no_op():
    reject_temporal_accessor(["creationDate", "year"], None)
    reject_temporal_accessor(["creationDate"], MappingResolver(mapping_bundle_for("ldbc_pg")))


def test_accessor_list_covers_the_ldbc_uses():
    assert {"year", "month", "day", "epochMillis"} <= TEMPORAL_ACCESSORS
