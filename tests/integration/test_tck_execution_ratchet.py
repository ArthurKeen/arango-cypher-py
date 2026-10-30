"""TCK execution ratchet — every scenario run on ArangoDB, rows checked.

The dry-run ratchet (``tests/tck/test_conformance_ratchet.py``) guards whether
scenarios *translate*.  This one guards whether they return the TCK's expected
rows (or its expected error) on a live database, which is the number
``docs/cypher_tck_conformance_plan.md`` reports.  The whole corpus runs in
about a minute, so it runs in the integration job on every pull request.

When it fails, fix the regression; record an intentional change with::

    RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/execution_ledger.py --write
"""

from __future__ import annotations

import pytest

from tests.tck.execution_ledger import (
    EXECUTION_LEDGER_PATH,
    KNOWN_OUTCOMES,
    PASSING_OUTCOMES,
    build_execution_ledger,
    load_execution_ledger,
)
from tests.tck.ledger import diff_ledger

pytestmark = pytest.mark.integration

_REGEN = "RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/execution_ledger.py --write"
_MAX_LISTED = 25


@pytest.fixture(scope="module")
def measured() -> dict[str, str]:
    return build_execution_ledger()


@pytest.fixture(scope="module")
def committed() -> dict[str, str]:
    ledger = load_execution_ledger()
    assert ledger, f"{EXECUTION_LEDGER_PATH.name} is missing or empty. Generate it with:\n  {_REGEN}"
    return ledger


def _format(pairs: dict[str, tuple[str, str]]) -> str:
    listed = sorted(pairs.items())[:_MAX_LISTED]
    lines = [f"    {sid}\n      {was} -> {now}" for sid, (was, now) in listed]
    if len(pairs) > _MAX_LISTED:
        lines.append(f"    … and {len(pairs) - _MAX_LISTED} more")
    return "\n".join(lines)


def test_no_scenario_regressed(measured: dict[str, str], committed: dict[str, str]) -> None:
    diff = diff_ledger(committed, measured, passing=PASSING_OUTCOMES)
    assert not diff.regressions, (
        f"{len(diff.regressions)} scenario(s) stopped passing on ArangoDB:\n"
        f"{_format(diff.regressions)}\n\n"
        "This is a conformance regression. Fix the cause; do not regenerate the ledger to silence it."
    )
    assert not diff.removed, (
        f"{len(diff.removed)} scenario(s) in the ledger are no longer measured:\n"
        f"    {', '.join(sorted(diff.removed)[:_MAX_LISTED])}\n\nIf intentional, re-record with:\n  {_REGEN}"
    )


def test_ledger_is_current(measured: dict[str, str], committed: dict[str, str]) -> None:
    """Improvements must be recorded, or the baseline silently stops being one."""
    diff = diff_ledger(committed, measured, passing=PASSING_OUTCOMES)
    assert not diff.improvements and not diff.added, (
        f"The execution ledger is out of date: {len(diff.improvements)} improvement(s), "
        f"{len(diff.added)} new scenario(s).\n{_format(diff.improvements)}\n\nRecord them with:\n  {_REGEN}"
    )


def test_every_outcome_is_known(measured: dict[str, str]) -> None:
    unknown = set(measured.values()) - KNOWN_OUTCOMES
    assert not unknown, f"unclassified execution outcome(s): {sorted(unknown)}"
