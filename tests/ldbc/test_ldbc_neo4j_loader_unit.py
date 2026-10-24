"""Unit tests for the Neo4j oracle loader's statements and value conversion (WP-33). No database."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.ldbc.neo4j_loader import (
    ddl_statements,
    edge_create_statement,
    neo4j_value,
    node_create_statement,
)
from tests.ldbc.schema import CONCRETE_LABELS, Column


def test_nodes_get_their_supertype_label_like_the_reference_import():
    assert node_create_statement("Comment") == "UNWIND $rows AS r CREATE (n:Message:Comment) SET n = r"
    assert node_create_statement("Person") == "UNWIND $rows AS r CREATE (n:Person) SET n = r"


def test_edges_match_both_endpoints_by_label_and_id():
    stmt = edge_create_statement("REPLY_OF", "Comment", "Post")
    assert "MATCH (a:Comment {id: r.src})" in stmt
    assert "MATCH (b:Post {id: r.dst})" in stmt
    assert "CREATE (a)-[e:REPLY_OF]->(b) SET e = r.props" in stmt


def test_identifiers_are_checked_before_reaching_cypher_text():
    with pytest.raises(ValueError, match="not a safe Cypher identifier"):
        edge_create_statement("KNOWS]->() DETACH DELETE (x", "Person", "Person")


def test_ddl_has_a_unique_id_per_label_and_supertype():
    stmts = ddl_statements()
    for label in (*CONCRETE_LABELS, "Message", "Place", "Organisation"):
        assert any(f"FOR (n:{label}) REQUIRE n.id IS UNIQUE" in s for s in stmts), label
    assert all("IF NOT EXISTS" in s for s in stmts)


def test_iso_timestamps_become_native_temporal_values():
    v = neo4j_value("2010-01-03T15:10:31.499+00:00", Column("creationDate", "datetime"), "iso")
    assert v == dt.datetime(2010, 1, 3, 15, 10, 31, 499000, tzinfo=dt.UTC)
    assert neo4j_value("1984-03-11", Column("birthday", "date"), "iso") == dt.date(1984, 3, 11)


def test_epoch_ms_timestamps_stay_integers():
    assert neo4j_value(1262531431499, Column("creationDate", "datetime"), "epoch_ms") == 1262531431499


def test_other_values_pass_through():
    assert neo4j_value(["fa", "en"], Column("speaks", "str_list"), "iso") == ["fa", "en"]
    assert neo4j_value("Firefox", Column("browserUsed", "str"), "iso") == "Firefox"
