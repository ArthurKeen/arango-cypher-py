"""Undirected self-loops and named paths in OPTIONAL MATCH / after WITH.

Live behaviour: the TCK (Match3 [12], CountingSubgraphMatches1 [2]/[3]/[6],
Match7 [16]-[20], Match9 [9], Delete3 [1]) through the execution ratchet.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str) -> str:
    return translate(cypher, mapping=mapping_bundle_for("lpg")).aql


@pytest.mark.parametrize("case_id", ["C1121", "C1122", "C1123", "C1124"])
def test_goldens(corpus_cases, case_id: str) -> None:
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture))
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n)-[r]-(n) RETURN n, r",
        "MATCH (n:User)--(n) RETURN count(*)",
        "MATCH (a)-->(n)-[r]-(n) RETURN r",
    ],
)
def test_an_undirected_hop_back_to_its_start_traverses_one_way(cypher: str) -> None:
    aql = _aql(cypher)
    assert " ANY " not in aql and "OUTBOUND" in aql


def test_an_undirected_hop_to_another_node_stays_undirected() -> None:
    assert " ANY " in _aql("MATCH (a)-[r]-(b) RETURN a, b")


def test_the_undirected_edge_scan_counts_both_orientations() -> None:
    aql = _aql("MATCH ()--() RETURN count(*)")
    assert "._from == " in aql and "? [0] : [0, 1])" in aql
    assert "[0, 1]" not in _aql("MATCH ()-->() RETURN count(*)")


def test_optional_match_returns_its_named_path() -> None:
    aql = _aql("MATCH (a:User) OPTIONAL MATCH p = (a)-[:FOLLOWS]->(b) RETURN p")
    assert "LET p = {nodes: FLATTEN([[a], [b]]), edges: FLATTEN([[r]])}" in aql
    assert "LET p = _opt_row.p" in aql


def test_a_variable_length_hop_contributes_its_traversal_to_the_path() -> None:
    aql = _aql("MATCH (a:User) OPTIONAL MATCH p = (a)-[*]->(c) RETURN p")
    assert "SLICE(_hop_path.vertices, 1)" in aql and "_hop_path.edges" in aql


def test_a_path_read_from_its_bound_end_is_reversed_back() -> None:
    aql = _aql("MATCH (b:User) WITH b MATCH p = (a:User)-[:FOLLOWS]->(b) RETURN p")
    # Compiled from b (the bound end), written from a: the path is a, then b.
    assert "LET p = {nodes: REVERSE(FLATTEN([[b], [a]])), edges: REVERSE(FLATTEN([[r]]))}" in aql
