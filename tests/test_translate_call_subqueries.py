"""``CALL { … }`` subqueries: what translates, and what is refused.

The behaviour tests run on ArangoDB in
``tests/integration/test_call_subqueries_live.py``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, layout: str = "pg") -> str:
    return translate(cypher, mapping=mapping_bundle_for(layout)).aql


def _refused(cypher: str, code: str, match: str) -> None:
    with pytest.raises(CoreError, match=match) as err:
        _aql(cypher)
    assert err.value.code == code


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_a_single_column_is_read_by_name(layout: str) -> None:
    # An aggregate column alone comes back bare elsewhere; in a body it is an object.
    aql = _aql(
        "MATCH (n:User) CALL { WITH n MATCH (n)-[:FOLLOWS]->(f:User) RETURN count(f) AS c } RETURN n.id, c",
        layout,
    )
    assert "RETURN {c: c}" in aql
    assert 'LET c = _call["c"]' in aql


def test_imported_variables_keep_their_outer_names() -> None:
    aql = _aql("MATCH (n:User) CALL { WITH n MATCH (n)-[:FOLLOWS]->(f:User) RETURN f } RETURN f.id")
    assert "OUTBOUND n @@" in aql


def test_union_and_union_all() -> None:
    body = "MATCH (u:User) RETURN u.id AS v {} MATCH (u:User) RETURN u.name AS v"
    assert "UNION_DISTINCT((" in _aql("CALL { " + body.format("UNION") + " } RETURN v")
    assert "UNION((" in _aql("CALL { " + body.format("UNION ALL") + " } RETURN v")


def test_the_columns_must_match_across_a_union() -> None:
    _refused(
        "CALL { MATCH (u:User) RETURN u.id AS a UNION MATCH (u:User) RETURN u.id AS b } RETURN a",
        "UNSUPPORTED",
        "same columns",
    )


def test_an_unaliased_expression_is_refused() -> None:
    _refused("MATCH (n:User) CALL { WITH n RETURN n.id } RETURN n", "UNSUPPORTED", "must be aliased")


def test_a_column_that_is_already_in_scope_is_refused() -> None:
    _refused("MATCH (n:User) CALL { MATCH (n:User) RETURN n } RETURN n", "UNSUPPORTED", "already declared")


def test_importing_an_undefined_variable_is_refused() -> None:
    _refused("MATCH (n:User) CALL { WITH m MATCH (m)-->(x) RETURN x } RETURN x", "UNSUPPORTED", "not defined")


@pytest.mark.parametrize(
    "importing", ["WITH n WHERE n.id = 1", "WITH n AS m", "WITH DISTINCT n", "WITH n LIMIT 1", "WITH *"]
)
def test_an_importing_with_that_does_more_than_list_variables_is_refused(importing: str) -> None:
    with pytest.raises(CoreError, match="importing WITH"):
        _aql(f"MATCH (n:User) CALL {{ {importing} MATCH (n)-[:FOLLOWS]->(f:User) RETURN f }} RETURN f")


@pytest.mark.parametrize(
    ("cypher", "match"),
    [
        ("MATCH (n:User) CALL { WITH n CREATE (m:User {id: n.id}) RETURN m } RETURN m", "Writes inside"),
        ("MATCH (n:User) CALL { MATCH (m:User) RETURN m } SET n.seen = true RETURN n", "query that writes"),
        ("MATCH (n:User) WHERE EXISTS { CALL { MATCH (m:User) RETURN m } RETURN m } RETURN n", "EXISTS"),
        ("MATCH (n:User) CALL { WITH n MATCH (n)-->(m) RETURN * } RETURN n", "RETURN \\*"),
    ],
)
def test_unsupported_forms_are_refused(cypher: str, match: str) -> None:
    with pytest.raises(CoreError, match=match) as err:
        _aql(cypher)
    assert err.value.code == "NOT_IMPLEMENTED"
