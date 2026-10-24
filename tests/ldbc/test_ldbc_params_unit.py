"""The committed LDBC read bindings (WP-34). No database: generation needs Neo4j, checking does not."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.ldbc import params as P
from tests.ldbc.corpus import load


@pytest.fixture(scope="module")
def committed():
    return P.read("sf0.003")


def test_every_oracle_runnable_read_has_bindings(committed):
    assert set(committed) == {op.id for op in P.read_operations()}


def test_out_of_subset_and_writes_have_no_read_bindings(committed):
    excluded = {op.id for op in load() if op.out_of_subset or op.kind != "read"}
    assert not excluded & set(committed)


@pytest.mark.parametrize("op", P.read_operations(), ids=lambda op: op.id)
def test_each_read_has_enough_non_empty_bindings(committed, op):
    non_empty = [b for b in committed[op.id] if b["rows"] > 0]
    if op.id in P.EMPTY_AT_SCALE["sf0.003"]:
        assert not non_empty and committed[op.id], "an EMPTY_AT_SCALE entry must record its (empty) binding"
    else:
        assert len(non_empty) >= P.WANTED, f"{op.id}: {len(non_empty)} non-empty binding(s)"


@pytest.mark.parametrize("op", P.read_operations(), ids=lambda op: op.id)
def test_bindings_supply_exactly_the_query_parameters(committed, op):
    for b in committed[op.id]:
        assert sorted(b["binding"]) == P.query_parameters(op)


def test_empty_at_scale_entries_say_how_they_were_proven():
    for scale, entries in P.EMPTY_AT_SCALE.items():
        for op_id, how in entries.items():
            assert len(how) > 20, f"{scale}/{op_id}: record how emptiness was established"


def test_bi_datetimes_use_ldbcs_text_format_and_convert_per_engine(committed):
    v = committed["BI1"][0]["binding"]["datetime"]
    assert isinstance(v, P.DateTimeParam)
    assert P.to_arango(v) == v.text
    assert v.text.endswith(".000+00:00") and len(v.text) == len("2012-03-09T00:00:00.000+00:00")
    assert isinstance(P.to_neo4j(v), dt.datetime) and P.to_neo4j(v).tzinfo is not None


def test_interactive_dates_are_epoch_ms_integers(committed):
    v = committed["IC2"][0]["binding"]["maxDate"]
    assert isinstance(v, int) and v > 1_000_000_000_000


def test_ldbc_datetime_formatting():
    assert P._ldbc_datetime(dt.datetime(2010, 1, 3, 15, 10, 31, 499000, tzinfo=dt.UTC)) == (
        "2010-01-03T15:10:31.499+00:00"
    )


def test_conversions_recurse_into_lists():
    v = [P.DateTimeParam("2012-01-01T00:00:00.000+00:00"), 3]
    assert P.to_arango(v) == ["2012-01-01T00:00:00.000+00:00", 3]
    assert isinstance(P.to_neo4j(v)[0], dt.datetime)
