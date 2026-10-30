"""Unit tests for the read-then-write DELETE translator (``_translate_v0.deletes``)."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.deletes import (
    _edge_collections,
    _node_collections,
    _path_variables,
    _pattern_variables,
)
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


_GUARD = 'OR FAIL("DeletedEntityAccess'


@pytest.mark.parametrize(
    ("cypher", "variable"),
    [
        ("MATCH (n:User) DELETE n RETURN n.name", "n"),
        ("MATCH (n:User) DELETE n RETURN labels(n)", "n"),
        ("MATCH (n:User) DELETE n RETURN properties(n) AS p", "n"),
        ("MATCH (n:User) DETACH DELETE n RETURN keys(n) AS k", "n"),
        ("MATCH (n:User) DELETE n RETURN n.age + 1 AS older", "n"),
        ("MATCH (:User)-[r:FOLLOWS]->(:User) DELETE r RETURN r.since", "r"),
    ],
)
def test_reading_a_deleted_entity_fails_each_returned_row(cypher: str, variable: str) -> None:
    """Neo4j raises DeletedEntityAccess for each row that reads a property or
    the labels of what the query deleted — at run time, so a match with no
    rows still succeeds: the guard is a per-row FILTER, not a refusal."""
    aql = translate(cypher, mapping=mapping_bundle_for("pg")).aql
    guard = f"FILTER {variable} == null {_GUARD}"
    assert guard in aql
    assert aql.index(f"LET {variable} = _row.{variable}") < aql.index(guard) < aql.rindex("RETURN")


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:User) DELETE n RETURN n",
        "MATCH (n:User) DELETE n RETURN count(*) AS c",
        "MATCH (:User)-[r:FOLLOWS]->(:User) DELETE r RETURN type(r)",
        "MATCH (:User)-[r:FOLLOWS]->(m:User) DELETE r RETURN m.name",
    ],
)
def test_returning_without_reading_a_deleted_entity_is_not_guarded(cypher: str) -> None:
    assert _GUARD not in translate(cypher, mapping=mapping_bundle_for("pg")).aql


def test_path_variables_are_found_in_order() -> None:
    clauses = _reading_clauses("MATCH p = (a)-->(b), q = (b)-->(c) OPTIONAL MATCH r = (c)-->() RETURN p")
    assert _path_variables(clauses) == ["p", "q", "r"]


def test_detach_deleting_a_path_removes_its_nodes_and_relationships() -> None:
    aql = translate(
        "MATCH p = (:User)-[:FOLLOWS]->(:User) DETACH DELETE p", mapping=mapping_bundle_for("pg")
    ).aql
    assert "LET _deleted_nodes = UNIQUE(REMOVE_VALUE(FLATTEN(_matched[*].p.nodes[*]._id), null))" in aql
    assert "LET _deleted_rels = UNIQUE(REMOVE_VALUE(FLATTEN(_matched[*].p.edges[*]._id), null))" in aql
    assert "LET _detach0" in aql  # edges touching the path's nodes go too


def test_deleting_a_path_without_detach_is_refused() -> None:
    with pytest.raises(CoreError, match="only supported as DETACH DELETE") as err:
        translate("MATCH p = (:User)-[:FOLLOWS]->(:User) DELETE p", mapping=mapping_bundle_for("pg"))
    assert err.value.code == "UNSUPPORTED"


def test_a_two_hop_named_path_reads_its_unnamed_edges_from_the_traversal() -> None:
    aql = translate("MATCH p = (:User)-->()-->() RETURN p", mapping=mapping_bundle_for("lpg")).aql
    path = next(line.strip() for line in aql.splitlines() if line.strip().startswith("LET p ="))
    assert ".edges[0]" in path  # not an undeclared variable (ERR 1203)
