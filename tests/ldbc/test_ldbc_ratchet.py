"""LDBC corpus integrity and translation ratchet (WP-32; PRD §12.1 LDBC-1, LDBC-3).

Default suite: no database, about a second. When the ratchet fails, fix the
regression — or, for an intentional change, record it::

    .venv/bin/python -m tests.ldbc.ledger --write
"""

from __future__ import annotations

import pytest

from tests.ldbc.corpus import QUERIES_DIR, hash_mismatches, load, unlisted_files
from tests.ldbc.ledger import (
    KNOWN_OUTCOMES,
    LAYOUTS,
    PASSING_OUTCOMES,
    build_ledger,
    load_ledger,
    outcomes,
)
from tests.tck.ledger import diff_ledger

_REGEN = ".venv/bin/python -m tests.ldbc.ledger --write"


# ---------------------------------------------------------------------------
# Corpus integrity (LDBC-1: verbatim, pinned, complete)
# ---------------------------------------------------------------------------


def test_vendored_files_match_their_pins():
    assert hash_mismatches(load()) == [], (
        "a vendored LDBC query changed. Upstream files are never edited — add a variant "
        "(queries/UPSTREAM.md). For a deliberate refresh: .venv/bin/python -m tests.ldbc.corpus --rehash"
    )


def test_every_vendored_file_is_in_the_manifest():
    assert unlisted_files(load()) == []


def test_corpus_is_the_full_interactive_v2_and_bi_workloads():
    ids = {op.id for op in load() if op.variant_of is None}
    expected = (
        {f"IC{i}" for i in range(1, 15)}
        | {f"IS{i}" for i in range(1, 8)}
        | {f"INS{i}" for i in range(1, 9)}
        | {f"DEL{i}" for i in range(1, 9)}
        | {f"BI{i}" for i in range(1, 21)}
    )
    assert ids == expected


def test_out_of_subset_is_exactly_the_gds_and_apoc_operations():
    assert {op.id for op in load() if op.out_of_subset and not op.variant_of} == {
        "IC14",
        "BI10",
        "BI15",
        "BI19",
        "BI20",
    }


def test_variants_declare_their_change():
    ops = load()
    files = {op.id: op.file for op in ops}
    variants = [op for op in ops if op.variant_of]
    assert variants, "expected at least the INS1 schema-fix variant"
    for op in variants:
        assert op.reason
        assert op.text().startswith(f"// VARIANT of {files[op.variant_of]}"), op.file


def test_license_and_notice_travel_with_the_queries():
    assert (QUERIES_DIR / "LICENSE").read_text().lstrip().startswith("Apache License")
    notice = (QUERIES_DIR / "NOTICE").read_text()
    assert "not LDBC Benchmark Results" in notice


# ---------------------------------------------------------------------------
# Translation ratchet (LDBC-3)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def measured():
    return build_ledger()


@pytest.fixture(scope="module")
def committed():
    return load_ledger()


def test_ledger_exists_and_covers_every_cell(committed):
    assert committed, f"translation_ledger.tsv is missing. Generate it with:\n  {_REGEN}"
    assert len(committed) == len(load()) * len(LAYOUTS)


def test_no_cell_regressed(measured, committed):
    diff = diff_ledger(outcomes(committed), outcomes(measured), passing=PASSING_OUTCOMES)
    assert not diff.regressions, (
        f"{len(diff.regressions)} LDBC cell(s) stopped translating:\n"
        + "\n".join(f"    {c}: {was} -> {now}" for c, (was, now) in sorted(diff.regressions.items()))
        + "\n\nFix the cause; do not regenerate the ledger to silence it."
    )
    assert not diff.removed, f"cells no longer measured: {sorted(diff.removed)}. If intentional:\n  {_REGEN}"


def test_ledger_is_current(measured, committed):
    """Improvements and changed blockers must be recorded; the diff is the review artifact."""
    stale = {c: (committed.get(c), now) for c, now in measured.items() if committed.get(c) != now}
    assert not stale, (
        f"{len(stale)} LDBC cell(s) changed:\n"
        + "\n".join(f"    {c}: {was} -> {now}" for c, (was, now) in sorted(stale.items())[:25])
        + f"\n\nRecord them with:\n  {_REGEN}"
    )


def test_every_outcome_is_known(measured):
    assert {outcome for outcome, _ in measured.values()} <= KNOWN_OUTCOMES


def test_no_operation_crashes_the_translator(measured):
    """A crash is a transpiler bug: never recorded as a tolerated state."""
    crashes = {c: d for c, (o, d) in measured.items() if o == "crash"}
    assert not crashes, crashes
