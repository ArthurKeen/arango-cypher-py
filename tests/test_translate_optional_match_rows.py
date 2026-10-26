"""OPTIONAL MATCH row semantics and clause order in WITH pipelines.

An OPTIONAL MATCH compiles to a subquery of its matches that the outer query
iterates — or one empty row when there is none — so a row with several matches
yields several rows and a row with none survives with nulls. The live
counterpart (which runs each shape) is
``tests/integration/test_optional_match_live.py``.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_ROWS = "LENGTH(_opt_rows) > 0 ? _opt_rows : [{}]"


def _aql(cypher: str, fixture: str = "pg") -> str:
    return translate(cypher, mapping=mapping_bundle_for(fixture)).aql


@pytest.mark.parametrize("fixture", ["pg", "lpg"])
def test_optional_match_iterates_its_matches(fixture):
    aql = _aql("MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN u.name, f.name", fixture)
    assert "FIRST(" not in aql
    assert "LET _opt_rows = (" in aql
    assert f"FOR _opt_row IN ({_ROWS})" in aql
    assert "LET f = _opt_row.f" in aql


def test_node_only_optional_match_is_not_limited_to_one_node():
    aql = _aql('MATCH (u:User {name: "a"}) OPTIONAL MATCH (p:Person {name: "b"}) RETURN u.name, p.name')
    assert "LIMIT 1" not in aql
    assert _ROWS in aql


def test_optional_match_where_filters_inside_the_subquery():
    aql = _aql(
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) WHERE f.age > 25 RETURN u.name, f.name"
    )
    sub = aql[aql.index("LET _opt_rows = (") : aql.index("FOR _opt_row")]
    assert "f.age > 25" in sub


def test_type_of_an_optional_relationship_keeps_its_column_name():
    aql = _aql("MATCH (u:User) OPTIONAL MATCH (u)-[r:FOLLOWS]->(f:User) RETURN u.name, f.name, type(r)")
    assert "LET r = _opt_row.r" in aql
    assert re.search(r"RETURN \{[^}]*\btype: ", aql), aql


def test_optional_match_in_a_with_pipeline_is_not_an_inner_join():
    aql = _aql(
        "MATCH (u:User) OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) WITH u, count(f) AS n RETURN n, count(u)"
    )
    assert _ROWS in aql


def test_optional_match_after_with_uses_the_with_bindings():
    aql = _aql(
        "MATCH (u:User) WITH u, 1 AS one ORDER BY u.name LIMIT 1 "
        "OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN u.name, f.name"
    )
    assert _ROWS in aql


def test_a_leading_optional_match_before_with_compiles_with_nothing_bound():
    aql = _aql("OPTIONAL MATCH (u:User {name: 'x'}) WITH u RETURN u")
    assert aql.lstrip().startswith("LET _opt_rows = (") or "\nLET _opt_rows = (" in aql
    assert _ROWS in aql


def test_a_match_between_two_withs_stays_below_the_first():
    aql = _aql("MATCH (u:User) WITH u LIMIT 1 MATCH (u)-[:FOLLOWS]->(f:User) WITH f RETURN f.name")
    assert aql.index("LIMIT 1") < aql.index("OUTBOUND")


def test_an_unwind_between_match_and_with_is_compiled():
    aql = _aql("MATCH (u:User) UNWIND [1, 2] AS k WITH u, k RETURN u.name, k")
    assert "FOR k IN [1,2]" in aql


def test_an_unwind_after_with_reads_the_with_bindings():
    aql = _aql("MATCH (u:User) WITH collect(u.name) AS names UNWIND names AS n RETURN n")
    assert re.search(r"FOR n IN names\b", aql), aql


def test_an_unwind_before_the_first_match_fails_closed():
    with pytest.raises(CoreError) as exc:
        _aql("UNWIND [1] AS k MATCH (u:User) WITH u, k RETURN k")
    assert exc.value.code == "NOT_IMPLEMENTED"


def test_a_second_with_projects_cypher_names_not_aql_names():
    """``WITH DISTINCT`` renames ``u`` to ``u_1``; the next WITH still sees ``u``."""
    aql = _aql(
        "MATCH (u:User)-[:FOLLOWS]->(f:User) WITH DISTINCT u, f WITH DISTINCT u, f "
        "MATCH (f)-[:FOLLOWS]->(g:User) RETURN u.name, g.name"
    )
    assert "COLLECT u_2 = u_1, f_2 = f_1" in aql
    assert "OUTBOUND f_2 " in aql
    assert "RETURN {name: u_2.name, g_name: g.name}" in aql


def test_a_renamed_variable_returns_under_its_cypher_name():
    """TCK Match7[26]: the second ``r`` is bound as ``r_1`` but its column is ``r``."""
    aql = _aql(
        "MATCH (a:User), (b:User) OPTIONAL MATCH (a)-[:FOLLOWS]->(x:User) OPTIONAL MATCH (x)-[r:FOLLOWS]->(b) RETURN x, r"
    )
    assert re.search(r"RETURN \{x: x, r: r_\d+\}", aql), aql


def test_a_single_unaliased_type_is_a_scalar_column():
    """TCK Graph4[1], [4]: ``RETURN type(r)`` is one column, not ``{type: …}``."""
    assert _aql("MATCH (u:User)-[r:FOLLOWS]->(v:User) RETURN type(r)").rstrip().endswith('RETURN "FOLLOWS"')
    aql = _aql("MATCH (u:User) OPTIONAL MATCH (u)-[r:FOLLOWS]->(v:User) RETURN type(r)", "lpg")
    assert re.search(r"RETURN r\[@\w+\]$", aql.rstrip()), aql


def test_an_undirected_self_loop_is_matched_once():
    """TCK Match7[24]: ANY yields a loop once per direction; OUTBOUND yields it once."""
    aql = _aql("MATCH (a:User) OPTIONAL MATCH (a)-[r:FOLLOWS]-(a) RETURN r")
    assert "OUTBOUND a @@" in aql and " ANY " not in aql, aql
