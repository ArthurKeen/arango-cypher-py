"""LDBC write comparison: canonical rows, deltas, and the committed write bindings (WP-33 part 3). No database."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.ldbc import oracle
from tests.ldbc import params as P
from tests.ldbc.writes import NEW_ID, _canon, _props, delta, delta_difference

_WRITES = P.write_operations()


def test_canonical_rows_ignore_key_order():
    assert _canon(["Person", {"b": 1, "a": 2}]) == _canon(["Person", {"a": 2, "b": 1}])


def test_null_and_bookkeeping_properties_are_dropped():
    doc = {
        "_key": "Person_1",
        "_id": "nodes/Person_1",
        "type": "Person",
        "labels": ["Person"],
        "id": 1,
        "x": None,
    }
    assert _props(doc, {"_key", "_id", "_rev", "type", "labels"}) == {"id": 1}


def test_delta_lists_rows_added_and_removed_with_multiplicity():
    before = (Counter({"n1": 1, "n2": 1}), Counter({"e1": 2}))
    after = (Counter({"n1": 1, "n3": 1}), Counter({"e1": 1}))
    assert delta(before, after) == {
        "nodes_added": ["n3"],
        "nodes_removed": ["n2"],
        "edges_added": [],
        "edges_removed": ["e1"],
    }


def test_delta_difference_names_the_first_mismatch():
    expected = {"nodes_added": [], "nodes_removed": [], "edges_added": [], "edges_removed": ["e1"]}
    assert delta_difference(expected, dict(expected)) is None
    too_much = {**expected, "edges_removed": ["e1", "e2"]}
    assert delta_difference(expected, too_much) == "edges_removed: 0 missing, 1 unexpected; unexpected ['e2']"


@pytest.mark.parametrize("op", _WRITES, ids=lambda op: op.id)
def test_each_write_binding_supplies_exactly_its_parameters(op):
    binding = P.read_writes()[op.id]
    assert sorted(binding) == P.query_parameters(op)


@pytest.mark.parametrize("op", _WRITES, ids=lambda op: op.id)
def test_each_write_has_a_current_snapshot_that_changes_the_graph(op):
    snap = oracle.read(op.id)
    assert snap is not None and snap["kind"] == "write"
    assert snap["sha256"] == op.sha256
    for b in snap["bindings"]:
        assert any(b["delta"].values()), f"{op.id}: a write binding that changes nothing tests nothing"


def test_created_entities_use_ids_outside_the_data_set():
    created = [
        v
        for b in P.read_writes().values()
        for k, v in b.items()
        if k in ("personId", "forumId", "postId", "commentId") and isinstance(v, int) and v >= NEW_ID
    ]
    assert created, "inserts must create entities with fresh ids"
