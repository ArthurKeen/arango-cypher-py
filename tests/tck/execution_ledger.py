#!/usr/bin/env python3
"""Per-scenario TCK *execution* ledger — the conformance number that counts.

``ledger.py`` records whether each scenario translates (or is correctly
rejected).  That is an upper bound: a query that translates can still return
the wrong rows.  This ledger records what happens when each scenario runs on a
live ArangoDB and its rows (or its expected error) are checked against the
TCK's own assertion — the measurement other Cypher-on-Arango engines report,
and the one ``docs/cypher_tck_conformance_plan.md`` tracks.

Format: sorted ``outcome<TAB>scenario_id`` lines, so a pull request diff shows
exactly which scenarios changed state.  Failure reasons are not committed (they
churn with every message change); ``analyze_execution.py --tsv`` writes them.

Regenerate after an intentional change (needs ArangoDB, see the plan)::

    RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/execution_ledger.py --write

``tests/integration/test_tck_execution_ratchet.py`` fails the build when a
scenario leaves a passing outcome, and when an improvement is not recorded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.tck.ledger import diff_ledger, load_ledger

EXECUTION_LEDGER_PATH = Path(__file__).resolve().parent / "execution_ledger.tsv"

#: Outcomes that count as conformant: the rows (or the expected error) matched
#: the TCK assertion, or the translator refused a query the TCK expects to fail.
PASSING_OUTCOMES = frozenset({"passed", "correct_rejection"})

#: Every outcome ``analyze_execution()`` assigns.
KNOWN_OUTCOMES = frozenset(
    {"passed", "correct_rejection", "skipped", "translation_failure", "execution_failure"}
)

_HEADER = (
    "# TCK execution ledger — regenerate with:\n"
    "#   RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/execution_ledger.py --write\n"
    "# Format: <outcome>\\t<scenario_id>, sorted by scenario_id.\n"
    f"# Passing outcomes: {', '.join(sorted(PASSING_OUTCOMES))}\n"
)

DEFAULT_DB_NAME = "tck_execution_ledger_db"


def build_execution_ledger(*, db_name: str = DEFAULT_DB_NAME) -> dict[str, str]:
    """Run the corpus on the configured ArangoDB; return ``{scenario_id: outcome}``."""
    from tests.tck.analyze_execution import analyze_execution

    scenarios = analyze_execution(db_name=db_name, mapping_fixture="lpg")["scenarios"]
    ledger = {sid: outcome for sid, (outcome, _reason) in scenarios.items()}
    unknown = set(ledger.values()) - KNOWN_OUTCOMES
    if unknown:
        raise ValueError(f"analyze_execution() produced unknown outcome(s): {sorted(unknown)}")
    return ledger


def write_execution_ledger(ledger: dict[str, str], path: Path = EXECUTION_LEDGER_PATH) -> None:
    lines = [_HEADER]
    for sid, outcome in sorted(ledger.items()):
        if "\t" in sid or "\n" in sid:
            raise ValueError(f"scenario id contains a delimiter: {sid!r}")
        lines.append(f"{outcome}\t{sid}\n")
    path.write_text("".join(lines), encoding="utf-8")


def load_execution_ledger(path: Path = EXECUTION_LEDGER_PATH) -> dict[str, str]:
    return load_ledger(path)


def summarize(ledger: dict[str, str]) -> dict[str, int]:
    """Outcome histogram plus the conformance total."""
    counts = {outcome: 0 for outcome in sorted(KNOWN_OUTCOMES)}
    for outcome in ledger.values():
        counts[outcome] += 1
    counts["_total"] = len(ledger)
    counts["_passing"] = sum(counts[o] for o in PASSING_OUTCOMES)
    return counts


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Regenerate the committed ledger.")
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    args = parser.parse_args()

    current = build_execution_ledger(db_name=args.db_name)
    counts = summarize(current)
    baseline = load_execution_ledger()

    if args.write:
        write_execution_ledger(current)
        print(f"wrote {EXECUTION_LEDGER_PATH.name} ({counts['_total']} scenarios)")
    elif baseline:
        diff = diff_ledger(baseline, current, passing=PASSING_OUTCOMES)
        print(f"regressions : {len(diff.regressions)}")
        print(f"improvements: {len(diff.improvements)}")
        print(f"added       : {len(diff.added)}")
        print(f"removed     : {len(diff.removed)}")

    passing = counts["_passing"]
    total = counts["_total"]
    print(f"conformant  : {passing} / {total} ({passing / total * 100:.1f}%)")
    for outcome in sorted(KNOWN_OUTCOMES):
        print(f"  {outcome:20s} {counts[outcome]:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
