"""The committed LDBC read bindings (WP-34). No database: generation needs Neo4j, checking does not."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from tests.ldbc import params as P
from tests.ldbc.corpus import load
from tests.ldbc.datasets import DATASETS

#: Scales with a committed params/<scale>.yml.
COMMITTED_SCALES = sorted(p.stem for p in P.PARAMS_DIR.glob("*.yml"))


@pytest.fixture(scope="module", params=COMMITTED_SCALES)
def scale(request):
    return request.param


@pytest.fixture(scope="module")
def committed(scale):
    return P.read(scale)


def test_every_scale_with_data_sets_has_committed_bindings():
    assert COMMITTED_SCALES == sorted(P.SCALE_DATASETS)


def test_scale_data_sets_are_in_the_catalog_with_their_workloads_encoding():
    for scale, by_workload in P.SCALE_DATASETS.items():
        assert set(by_workload) == {"interactive_v2", "bi"}, scale
        assert DATASETS[by_workload["interactive_v2"]].encoding == "epoch_ms"
        assert DATASETS[by_workload["bi"]].encoding == "iso"
        assert {DATASETS[n].scale_factor for n in by_workload.values()} == {scale.removeprefix("sf")}


def test_an_unknown_scale_names_the_known_ones():
    with pytest.raises(ValueError, match=r"unknown LDBC scale 'sf7'.*sf0\.003"):
        P.workload_datasets("sf7")


def test_the_committed_file_records_its_data_sets(scale):
    doc = yaml.safe_load(P.params_path(scale).read_text(encoding="utf-8"))
    assert doc["scale"] == scale
    assert doc["datasets"] == P.workload_datasets(scale)


def test_every_oracle_runnable_read_has_bindings(committed):
    assert set(committed) == {op.id for op in P.read_operations()}


def test_out_of_subset_and_writes_have_no_read_bindings(committed):
    excluded = {op.id for op in load() if op.out_of_subset or op.kind != "read"}
    assert not excluded & set(committed)


@pytest.mark.parametrize("op", P.read_operations(), ids=lambda op: op.id)
def test_each_read_has_enough_non_empty_bindings(scale, committed, op):
    non_empty = [b for b in committed[op.id] if b["rows"] > 0]
    if op.id in P.EMPTY_AT_SCALE.get(scale, {}):
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
