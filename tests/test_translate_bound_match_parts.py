"""MATCH after WITH with several pattern parts, and bound variables in them (WP-36).

LDBC IS2, IC3, IC4, IC6, BI11 and BI14 failed with "Only a single pattern part
is supported" — either a comma-separated MATCH after WITH, or a multi-part first
MATCH followed by WITH. A hop ending at a variable already bound used to
re-bind it (``FOR forum, … IN …`` after ``WITH forum``), which ArangoDB rejects
(ERR 1511; LDBC IC5 on LPG); it is now correlated by ``_id``.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, layout: str = "pg") -> str:
    return translate(cypher, mapping=mapping_bundle_for(f"ldbc_{layout}")).aql


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_comma_parts_after_with_chain_through_bound_variables(layout):
    aql = _aql(
        "MATCH (p:Person {id: 1}) WITH p "
        "MATCH (p)-[:KNOWS]->(f:Person), (f)-[:IS_LOCATED_IN]->(c:City) RETURN c.name",
        layout,
    )
    assert re.search(r"FOR f, \w+ IN 1\.\.1 OUTBOUND p ", aql), aql
    assert re.search(r"FOR c, \w+ IN 1\.\.1 OUTBOUND f ", aql), aql


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_a_bound_end_node_is_correlated_not_rebound(layout):
    aql = _aql(
        "MATCH (a:Person {id: 1})-[:KNOWS]->(b:Person) WITH a, b MATCH (a)-[:KNOWS]->(b) RETURN a.id",
        layout,
    )
    assert "FOR b, " not in aql.split("WITH", 1)[-1].split("LET", 1)[0].replace(
        "FOR b, r IN 1..1 OUTBOUND a", "", 1
    )
    assert re.search(r"FILTER b_\d+\._id == b\._id", aql), aql


def test_a_multi_part_first_match_may_precede_with():
    aql = _aql("MATCH (x:Country {name: 'Chile'}), (p:Person {id: 1}) WITH p, x RETURN p.id, x.name")
    assert "FOR x IN" in aql and "FOR p IN" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:Person), (b:Person), (c:Person) WITH a, b WITH a ORDER BY c RETURN a",
        "MATCH (a:Person) WITH a ORDER BY d RETURN a",
    ],
)
def test_order_by_an_out_of_scope_variable_is_refused(cypher):
    with pytest.raises(CoreError) as exc:
        _aql(cypher)
    assert exc.value.code == "UNSUPPORTED"
    assert "not in scope" in str(exc.value)


@pytest.mark.parametrize(
    "cypher",
    [
        # a projected alias
        "MATCH (a:Person) WITH a.id AS x ORDER BY x RETURN x",
        # a variable in scope before a non-aggregating WITH
        "MATCH (a:Person) WITH a.id AS x ORDER BY a.firstName RETURN x",
        # a variable local to a list comprehension
        "MATCH (a:Person) WITH a ORDER BY size([z IN a.speaks | z]) RETURN a",
        # a collect() alias
        "MATCH (a:Person)-[:KNOWS]->(b:Person) WITH a, collect(b.id) AS ids ORDER BY size(ids) RETURN a.id",
    ],
)
def test_order_by_in_scope_still_translates(cypher):
    assert _aql(cypher)
