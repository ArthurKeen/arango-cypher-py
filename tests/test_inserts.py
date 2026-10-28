"""Unit tests for the CREATE pipeline translator (``_translate_v0.inserts``)."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.inserts import _match_names, is_create_pipeline
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _multi_part(cypher: str):  # noqa: ANN202 - ANTLR context
    return (
        parse_cypher(cypher)
        .tree.oC_Statement()
        .oC_Query()
        .oC_RegularQuery()
        .oC_SingleQuery()
        .oC_MultiPartQuery()
    )


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (a:User) CREATE (b:User) WITH b MATCH (c:User) CREATE (b)-[:FOLLOWS]->(c)", True),
        ("MATCH (a:User) WITH a CREATE (b:User)", False),  # a CREATE only at the end: the existing path
        ("MATCH (a:User) CREATE (b:User) WITH b SET b.x = 1", False),
    ],
)
def test_is_create_pipeline(cypher: str, expected: bool) -> None:
    assert is_create_pipeline(_multi_part(cypher)) is expected


def test_match_names() -> None:
    m = parse_cypher("MATCH (a:User)-[r:FOLLOWS]->(:Person) WHERE (a)-[:LIKES]->() RETURN a").tree
    match = (
        m.oC_Statement()
        .oC_Query()
        .oC_RegularQuery()
        .oC_SingleQuery()
        .oC_SinglePartQuery()
        .oC_ReadingClause(0)
        .oC_Match()
    )
    assert _match_names(match) == ({"a", "r"}, {"User", "Person"}, {"FOLLOWS", "LIKES"})


def test_writes_come_last_and_each_collection_once() -> None:
    aql = translate(
        "MATCH (a:User {id: 'u1'}) CREATE (b:Person {id: 'u9'})-[:FOLLOWS]->(a) "
        "WITH b MATCH (c:User {id: 'u2'}) CREATE (b)-[:FOLLOWS]->(c)",
        mapping=mapping_bundle_for("pg"),
    ).aql
    assert aql.count(" INSERT ") == 2  # persons once, follows once
    assert aql.index("LET _s2") < aql.index(" INSERT ")


@pytest.mark.parametrize(
    "tail",
    [
        "WITH b MATCH (b)-[:FOLLOWS]->(c:User) RETURN c",  # a created variable
        "WITH a MATCH (c:User)-[:FOLLOWS]->(a) RETURN c",  # a created type and label
    ],
)
def test_reading_what_was_created_is_refused(tail: str) -> None:
    with pytest.raises(CoreError, match="reads what the query created") as exc:
        translate(f"MATCH (a:User) CREATE (b:User)-[:FOLLOWS]->(a) {tail}", mapping=mapping_bundle_for("pg"))
    assert exc.value.code == "NOT_IMPLEMENTED"
