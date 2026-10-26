"""LDBC execution ratchet: translated AQL vs the Neo4j oracle's snapshots (WP-33; PRD §12.1 LDBC-3/4/5).

Loads SF0.003 into both layouts, runs every read binding through the
translator and ArangoDB, and compares with ``tests/ldbc/expected/``. No Neo4j
needed. When it fails, fix the regression — or record an intentional change::

    .venv/bin/python -m tests.ldbc.execution --write
"""

from __future__ import annotations

import pytest

from tests.ldbc.execution import (
    PASSING_OUTCOMES,
    build_execution_ledger,
    load_execution_ledger,
    loaded_databases,
)
from tests.tck.ledger import diff_ledger

pytestmark = pytest.mark.integration

_REGEN = ".venv/bin/python -m tests.ldbc.execution --write"


@pytest.fixture(scope="module")
def measured(ldbc_arango_url):
    return build_execution_ledger(loaded_databases(ldbc_arango_url))


@pytest.fixture(scope="module")
def committed():
    return load_execution_ledger()


def _outcomes(ledger):
    return {cell: outcome for cell, (outcome, _) in ledger.items()}


def test_no_cell_regressed(measured, committed):
    diff = diff_ledger(_outcomes(committed), _outcomes(measured), passing=PASSING_OUTCOMES)
    assert not diff.regressions, (
        f"{len(diff.regressions)} LDBC cell(s) no longer match the oracle:\n"
        + "\n".join(
            f"    {c}: {was} -> {now}  ({measured[c][1]})"
            for c, (was, now) in sorted(diff.regressions.items())
        )
        + "\n\nFix the cause; do not regenerate the ledger to silence it."
    )
    assert not diff.removed, f"cells no longer measured: {sorted(diff.removed)}"


def test_execution_ledger_is_current(measured, committed):
    stale = {c: (committed.get(c), now) for c, now in measured.items() if committed.get(c) != now}
    assert not stale, (
        f"{len(stale)} LDBC execution cell(s) changed:\n"
        + "\n".join(f"    {c}: {was} -> {now}" for c, (was, now) in sorted(stale.items())[:25])
        + f"\n\nRecord them with:\n  {_REGEN}"
    )


def test_no_cell_crashed(measured):
    crashes = {c: d for c, (o, d) in measured.items() if o == "crash"}
    assert not crashes, crashes
