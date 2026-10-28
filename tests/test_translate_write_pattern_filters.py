"""Writes honour the whole MATCH pattern they act on (docs/ldbc_benchmark_plan.md §3.5).

Found by the LDBC write tier: ``MATCH (:Person {id: $p})-[likes:LIKES]->(:Post {id: $q})
DELETE likes`` dropped the end node's ``{id: $q}`` and its ``:Post`` label, so
the AQL removed every LIKES edge of the person — to posts and comments. The
write path compiles its own MATCH; it now filters end-node labels and
properties and relationship properties as the read path does, and refuses
reading clauses or pattern parts it would otherwise ignore.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_DEL2 = "MATCH (:Person {id: $personId})-[likes:LIKES]->(:Post {id: $postId}) DELETE likes"


def _t(cypher: str, layout: str):
    return translate(cypher, mapping=mapping_bundle_for(f"ldbc_{layout}"))


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_delete_keeps_the_end_node_property_filter(layout):
    assert "FILTER (v.id == @postId)" in _t(_DEL2, layout).aql


def test_delete_keeps_a_collection_style_end_label_on_a_polymorphic_edge():
    q = _t(_DEL2, "pg")
    m = re.search(r"FILTER \(?IS_SAME_COLLECTION\(@(\w+), v\)", q.aql)
    assert m, q.aql
    assert q.bind_vars[m.group(1)] == "post"


def test_no_label_check_where_the_relationship_already_guarantees_it():
    aql = _t("MATCH (a:Person {id: 1})-[k:KNOWS]->(b:Person) DELETE k", "pg").aql
    assert "IS_SAME_COLLECTION" not in aql


def test_relationship_properties_are_filters():
    aql = _t("MATCH (a:Person {id: 1})-[k:KNOWS {creationDate: 5}]->(b:Person) DELETE k", "pg").aql
    assert "FILTER (k.creationDate == 5)" in aql


@pytest.mark.parametrize(
    "cypher,fragment",
    [
        ("MATCH (a:Person {id: 1}) UNWIND [1, 2] AS x SET a.n = x", "more than one reading clause"),
    ],
)
def test_ignored_narrowing_is_refused(cypher, fragment):
    with pytest.raises(CoreError) as exc:
        _t(cypher, "pg")
    assert exc.value.code == "UNSUPPORTED"
    assert fragment in str(exc.value)
