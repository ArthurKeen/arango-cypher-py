"""The committed Neo4j oracle snapshots agree with the corpus and the bindings (WP-33). No database."""

from __future__ import annotations

import pytest

from tests.helpers.result_equivalence import result_difference, sort_list_columns
from tests.ldbc import oracle
from tests.ldbc import params as P
from tests.ldbc.corpus import load
from tests.ldbc.execution import KNOWN_OUTCOMES, load_execution_ledger
from tests.ldbc.ledger import LAYOUTS

_READS = P.read_operations()


@pytest.mark.parametrize("op", _READS, ids=lambda op: op.id)
def test_every_bound_read_has_a_current_snapshot(op):
    snap = oracle.read(op.id)
    assert snap is not None, (
        f"no snapshot for {op.id}: RUN_CROSS=1 .venv/bin/python -m tests.ldbc.oracle --record"
    )
    assert snap["sha256"] == op.sha256, f"{op.id}: snapshot recorded for a different query text; re-record"


@pytest.mark.parametrize("op", _READS, ids=lambda op: op.id)
def test_snapshot_rows_match_the_recorded_binding_counts(op):
    snap = oracle.read(op.id)
    committed = P.read()[op.id]
    assert [len(b["rows"]) for b in snap["bindings"]] == [b["rows"] for b in committed]
    assert [b["binding"] for b in snap["bindings"]] == [b["binding"] for b in committed]


def test_unordered_columns_are_stored_canonically():
    for op in (o for o in _READS if o.unordered_columns):
        for b in oracle.read(op.id)["bindings"]:
            positions = frozenset(i for i, c in enumerate(b["columns"]) if c in op.unordered_columns)
            assert positions, f"{op.id}: unordered_columns name no returned column"
            for row in b["rows"]:
                assert sort_list_columns(row, positions) == row


def test_collect_order_is_ignored_only_where_declared():
    expected = [[1, ["a", "b"]]]
    shuffled = [{"x": 1, "names": ["b", "a"]}]
    assert result_difference(expected, shuffled, ordered=True, unordered_positions=frozenset({1})) is None
    assert result_difference(expected, shuffled, ordered=True) is not None


def test_execution_ledger_covers_every_cell_with_known_outcomes():
    ledger = load_execution_ledger()
    assert len(ledger) == len(load()) * len(LAYOUTS)
    assert {outcome for outcome, _ in ledger.values()} <= KNOWN_OUTCOMES
    assert not [c for c, (o, _) in ledger.items() if o == "crash"]
