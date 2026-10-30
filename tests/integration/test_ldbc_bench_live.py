"""Smoke test of the LDBC benchmark runner on SF0.003 (plan WP-43)."""

from __future__ import annotations

import pytest

from tests.ldbc.bench import Settings, load_databases, run
from tests.ldbc.report import check, render

pytestmark = pytest.mark.integration


def test_a_short_run_times_reads_and_renders_a_conforming_report(ldbc_arango_url):
    settings = Settings(operations=("IS1", "IC2", "BI4"), warmup=0, repeats=1, translate_repeats=1)
    result = run(load_databases(ldbc_arango_url, settings), settings)
    cells = {(c["operation"], c["layout"]): c for c in result["cells"]}
    assert set(cells) == {(op, layout) for op in ("IS1", "IC2", "BI4") for layout in ("pg", "lpg")}
    for cell in cells.values():
        assert cell["outcome"] == "ok", cell
        assert cell["server_ms"]["n"] == cell["bindings"]
        assert len(cell["plan"]["digest"]) == 16
        assert cell["translation_ms"]["cold"]["p50"] > 0
    assert result["environment"]["server"]["version"]
    assert check(render(result), result) == []


def test_a_row_count_other_than_the_oracles_is_not_timed(ldbc_arango_url):
    from tests.helpers.mapping_fixtures import mapping_bundle_for
    from tests.ldbc import params as P
    from tests.ldbc.bench import _cell, scale_datasets
    from tests.ldbc.corpus import load

    settings = Settings(layouts=("pg",), warmup=0, repeats=1, translate_repeats=1)
    op = next(o for o in load() if o.id == "IS1")
    bindings = [dict(b, rows=b["rows"] + 1) for b in P.read("sf0.003")["IS1"]]
    db = load_databases(ldbc_arango_url, settings)[(scale_datasets("sf0.003")["interactive_v2"], "pg")]
    cell = _cell(db, op, "pg", bindings, mapping_bundle_for("ldbc_pg"), settings)
    assert cell["outcome"] == "rows_differ"
    assert cell["rows_expected"] == [b["rows"] for b in bindings]
