"""Unit tests for the read-then-write DELETE translator (``_translate_v0.deletes``)."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.deletes import _edge_collections, _node_collections, _pattern_variables
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _reading_clauses(cypher: str) -> list:
    tree = parse_cypher(cypher).tree
    return (
        tree.oC_Statement()
        .oC_Query()
        .oC_RegularQuery()
        .oC_SingleQuery()
        .oC_SinglePartQuery()
        .oC_ReadingClause()
    )


def test_pattern_variables_split_nodes_and_relationships() -> None:
    nodes, rels, types = _pattern_variables(
        _reading_clauses(
            "MATCH (a:User)-[r:FOLLOWS]->(b) OPTIONAL MATCH (b)-[s:FOLLOWS|LIKES*1..2]-(c:User) RETURN a"
        )
    )
    assert nodes == {"a": ["User"], "b": [], "c": ["User"]}
    assert rels == {"r": False, "s": True}
    assert types == {"r": ["FOLLOWS"], "s": ["FOLLOWS", "LIKES"]}


def test_node_collections_follow_the_label_or_cover_every_collection() -> None:
    resolver = MappingResolver(mapping_bundle_for("ldbc_pg"))
    assert _node_collections(["Message"], resolver) == ["post", "comment"]
    assert _node_collections(["Person"], resolver) == ["person"]
    assert {"person", "post", "comment", "forum"} <= set(_node_collections([], resolver))


def test_edge_collections_follow_the_relationship_types() -> None:
    resolver = MappingResolver(mapping_bundle_for("ldbc_pg"))
    assert _edge_collections(["LIKES", "HAS_CREATOR"], resolver) == ["likes", "has_creator"]
    assert _edge_collections([], resolver) == resolver.all_edge_collections()


def test_reads_come_before_every_write() -> None:
    aql = translate(
        "MATCH (n:User)-[:FOLLOWS]->(m:User) DETACH DELETE n, m", mapping=mapping_bundle_for("pg")
    ).aql
    first_write = aql.index("REMOVE PARSE_IDENTIFIER")
    assert aql.index("LET _matched") < aql.index("LET _detach0") < first_write
    assert aql.count("IN @@deleteNodes") == 1  # both variables live in users: one REMOVE


def test_without_return_the_last_removal_is_the_statement() -> None:
    aql = translate("MATCH (n:User) DELETE n", mapping=mapping_bundle_for("pg")).aql
    assert aql.splitlines()[-1].lstrip().startswith("FOR _id IN _deleted_nodes")


def test_with_return_the_rows_are_the_matched_ones() -> None:
    aql = translate("MATCH (n:User) DELETE n RETURN count(*) AS c", mapping=mapping_bundle_for("pg")).aql
    assert "FOR _row IN _matched" in aql and "LET n = _row.n" in aql


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("MATCH (n:User) DELETE n.name", "only supports a variable"),
        ("MATCH (n:User) WITH n AS m UNWIND [1] AS x DELETE x", ""),
        ("MATCH (n:User) DELETE m", "which no MATCH binds"),
    ],
)
def test_refusals(cypher: str, message: str) -> None:
    with pytest.raises(CoreError, match=message):
        translate(cypher, mapping=mapping_bundle_for("pg"))
