"""Unit tests for the Neo4j oracle loader's statements and value conversion (WP-33). No database."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.ldbc.neo4j_loader import (
    _wipe,
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


class _Result:
    def __init__(self, c: int) -> None:
        self._c = c

    def single(self) -> dict[str, int]:
        return {"c": self._c}


class FakeSession:
    """Mirrors ``neo4j.Session.run(query, **params)``: holds *rels* and *nodes*, deletes up to ``n`` per call."""

    def __init__(self, rels: int, nodes: int) -> None:
        self.left = {"r": rels, "n": nodes}
        self.calls: list[tuple[str, int]] = []

    def run(self, query: str, parameters: dict | None = None, **kwargs: int) -> _Result:
        n = kwargs["n"]
        kind = "r" if "-[r]->" in query else "n"
        if kind == "n":
            assert self.left["r"] == 0, "nodes are deleted only once no relationship is left"
        deleted = min(n, self.left[kind])
        self.left[kind] -= deleted
        self.calls.append((kind, deleted))
        return _Result(deleted)


def test_wipe_deletes_relationships_then_nodes_in_bounded_transactions():
    session = FakeSession(rels=25, nodes=12)
    _wipe(session, batch=10)
    assert session.left == {"r": 0, "n": 0}
    assert session.calls == [("r", 10), ("r", 10), ("r", 5), ("r", 0), ("n", 10), ("n", 2), ("n", 0)]


def test_wipe_of_an_empty_database_runs_one_probe_per_kind():
    session = FakeSession(rels=0, nodes=0)
    _wipe(session)
    assert session.calls == [("r", 0), ("n", 0)]
