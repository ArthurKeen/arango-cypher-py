"""Conformance ratchet — WP-0 of ``docs/cypher_tck_conformance_plan.md``.

Aggregate coverage numbers cannot drive a corpus to 100%: a change that fixes
ten scenarios and breaks nine reads as progress.  These tests compare a fresh
measurement against the committed per-scenario ledger so every state change is
visible and deliberate.

Deliberately **not** marked ``tck``: the dry run needs no database and takes
about nine seconds, so it belongs in the default suite where it gates every pull
request, alongside the rest of ``tests/tck/test_coverage_reporting.py``.

When one of these fails, the fix is never to weaken the test — it is either to
fix the regression or, for intentional change, to record it::

    ./.venv/bin/python tests/tck/ledger.py --write
"""

from __future__ import annotations

import pytest

from tests.tck.ledger import (
    KNOWN_OUTCOMES,
    LEDGER_PATH,
    PASSING_OUTCOMES,
    build_ledger,
    diff_ledger,
    load_ledger,
    summarize,
)

_REGEN = "./.venv/bin/python tests/tck/ledger.py --write"
_MAX_LISTED = 25


@pytest.fixture(scope="module")
def measured() -> dict[str, str]:
    """Fresh dry-run measurement of every TCK scenario (~9s, no database)."""
    return build_ledger()


@pytest.fixture(scope="module")
def committed() -> dict[str, str]:
    return load_ledger()


def _format(pairs: dict[str, tuple[str, str]]) -> str:
    listed = sorted(pairs.items())[:_MAX_LISTED]
    lines = [f"    {sid}\n      {was} -> {now}" for sid, (was, now) in listed]
    if len(pairs) > _MAX_LISTED:
        lines.append(f"    … and {len(pairs) - _MAX_LISTED} more")
    return "\n".join(lines)


def test_ledger_exists_and_is_populated(committed: dict[str, str]) -> None:
    assert committed, f"{LEDGER_PATH.name} is missing or empty. Generate it with:\n  {_REGEN}"


def test_no_scenario_regressed(measured: dict[str, str], committed: dict[str, str]) -> None:
    """A scenario must never leave a passing outcome.

    This is the ratchet.  It is the one assertion that makes incremental
    conformance work safe: any change that trades one scenario for another is
    surfaced instead of hiding inside an unchanged total.
    """
    if not committed:
        pytest.skip("no committed ledger yet")

    diff = diff_ledger(committed, measured)

    assert not diff.regressions, (
        f"{len(diff.regressions)} scenario(s) left a passing outcome "
        f"({', '.join(sorted(PASSING_OUTCOMES))}):\n"
        f"{_format(diff.regressions)}\n\n"
        "This is a conformance regression. Fix the cause; do not regenerate the "
        "ledger to silence it."
    )

    assert not diff.removed, (
        f"{len(diff.removed)} scenario(s) in the ledger are no longer measured — "
        "the corpus or the scenario-id scheme changed:\n"
        f"    {', '.join(sorted(diff.removed)[:_MAX_LISTED])}\n\n"
        f"If intentional, re-record with:\n  {_REGEN}"
    )


def test_ledger_is_current(measured: dict[str, str], committed: dict[str, str]) -> None:
    """Improvements must be recorded, or the baseline stops being a baseline.

    Failing on *progress* is intentional: an un-refreshed ledger silently lowers
    the bar for the next change, and the diff of newly-passing scenarios is the
    artifact that makes conformance work reviewable.
    """
    if not committed:
        pytest.skip("no committed ledger yet")

    diff = diff_ledger(committed, measured)
    pending = {**diff.improvements, **diff.changed}

    assert not pending and not diff.added, (
        f"The ledger is out of date: {len(diff.improvements)} improvement(s), "
        f"{len(diff.changed)} other change(s), {len(diff.added)} new scenario(s).\n"
        f"{_format(pending)}\n\n"
        f"Record them with:\n  {_REGEN}"
    )


def test_every_outcome_is_known(measured: dict[str, str]) -> None:
    """A new analyzer outcome must be classified as passing or not, explicitly."""
    unknown = set(measured.values()) - KNOWN_OUTCOMES
    assert not unknown, (
        f"analyze() produced unclassified outcome(s): {sorted(unknown)}. "
        "Add them to KNOWN_OUTCOMES and decide whether they belong in "
        "PASSING_OUTCOMES."
    )


def test_conformance_total_matches_aggregate_report(measured: dict[str, str]) -> None:
    """The ledger and the aggregate analyzer must not disagree.

    Two independent code paths count the same corpus; if they diverge, one of
    them is wrong and every number in the scorecard is suspect.
    """
    from tests.tck.analyze_coverage import analyze

    aggregate = analyze()
    counts = summarize(measured)

    assert counts["_total"] == aggregate["full"]["total"]
    assert counts["_passing"] == aggregate["full"]["passable"]
    assert counts["translatable"] == aggregate["full"]["translatable"]
    assert counts["correct_rejection"] == aggregate["full"]["correct_rejections"]
