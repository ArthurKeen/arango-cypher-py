#!/usr/bin/env python3
"""Per-scenario TCK conformance ledger — the progress signal for WP-0.

``analyze_coverage.py`` reports aggregates, which are enough to describe coverage
but not to *drive* it: a change that fixes ten scenarios and breaks nine looks
like progress.  This module records the outcome of every individual scenario so
regressions are visible per scenario, and commits that record to the repo.

The ledger is a sorted TSV of ``outcome<TAB>scenario_id`` — sorted and
line-oriented so a pull request diff shows exactly which scenarios changed
state, which is the review signal a JSON blob cannot give.

Regenerate after an intentional change::

    ./.venv/bin/python tests/tck/ledger.py --write

The ratchet in ``tests/tck/test_conformance_ratchet.py`` fails the build when a
scenario leaves a passing outcome, and passes (loudly) when scenarios improve.

See ``docs/cypher_tck_conformance_plan.md``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.tck.analyze_coverage import analyze

LEDGER_PATH = Path(__file__).resolve().parent / "conformance_ledger.tsv"

#: Outcomes that count as conformant.  A scenario the TCK expects to fail is
#: conformant when we reject it, which is why ``correct_rejection`` is here and
#: why "translatable" alone is never the target — see the plan's
#: "definition of done".
PASSING_OUTCOMES = frozenset({"translatable", "correct_rejection"})

#: Every outcome ``analyze()`` can assign.  Kept explicit so a new outcome in the
#: analyzer fails a test rather than silently landing in the ledger.
KNOWN_OUTCOMES = frozenset(
    {
        "translatable",
        "correct_rejection",
        "translate_fail",
        "parse_rejected",
        "harness_skip",
        "no_query",
    }
)

_HEADER = (
    "# TCK conformance ledger — regenerate with:\n"
    "#   ./.venv/bin/python tests/tck/ledger.py --write\n"
    "# Format: <outcome>\\t<scenario_id>, sorted by scenario_id.\n"
    f"# Passing outcomes: {', '.join(sorted(PASSING_OUTCOMES))}\n"
)


@dataclass(frozen=True)
class LedgerDiff:
    """What changed between a committed ledger and a fresh measurement."""

    regressions: dict[str, tuple[str, str]]
    """scenario_id -> (was, now); left a passing outcome.  Build-failing."""

    improvements: dict[str, tuple[str, str]]
    """scenario_id -> (was, now); entered a passing outcome."""

    changed: dict[str, tuple[str, str]]
    """scenario_id -> (was, now); changed between two non-passing outcomes."""

    added: dict[str, str]
    """scenario_id -> outcome; present now, absent from the ledger."""

    removed: dict[str, str]
    """scenario_id -> outcome; in the ledger, absent now."""

    @property
    def is_regression(self) -> bool:
        return bool(self.regressions) or bool(self.removed)


def build_ledger() -> dict[str, str]:
    """Measure the corpus and return ``{scenario_id: outcome}``."""
    metrics = analyze(collect_scenarios=True)
    scenarios: dict[str, str] = metrics["scenarios"]
    unknown = {o for o in scenarios.values()} - KNOWN_OUTCOMES
    if unknown:
        raise ValueError(f"analyze() produced unknown outcome(s): {sorted(unknown)}")
    return scenarios


def write_ledger(ledger: dict[str, str], path: Path = LEDGER_PATH) -> None:
    lines = [_HEADER]
    for sid, outcome in sorted(ledger.items()):
        if "\t" in sid or "\n" in sid:
            raise ValueError(f"scenario id contains a delimiter: {sid!r}")
        lines.append(f"{outcome}\t{sid}\n")
    path.write_text("".join(lines), encoding="utf-8")


def load_ledger(path: Path = LEDGER_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    ledger: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        outcome, _, sid = line.partition("\t")
        if not sid:
            raise ValueError(f"malformed ledger line: {line!r}")
        ledger[sid] = outcome
    return ledger


def diff_ledger(baseline: dict[str, str], current: dict[str, str]) -> LedgerDiff:
    """Compare a committed ledger against a fresh measurement."""
    regressions: dict[str, tuple[str, str]] = {}
    improvements: dict[str, tuple[str, str]] = {}
    changed: dict[str, tuple[str, str]] = {}

    for sid, now in current.items():
        was = baseline.get(sid)
        if was is None or was == now:
            continue
        was_ok = was in PASSING_OUTCOMES
        now_ok = now in PASSING_OUTCOMES
        if was_ok and not now_ok:
            regressions[sid] = (was, now)
        elif now_ok and not was_ok:
            improvements[sid] = (was, now)
        else:
            changed[sid] = (was, now)

    added = {sid: o for sid, o in current.items() if sid not in baseline}
    removed = {sid: o for sid, o in baseline.items() if sid not in current}
    return LedgerDiff(regressions, improvements, changed, added, removed)


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
    args = parser.parse_args()

    current = build_ledger()
    counts = summarize(current)
    baseline = load_ledger()

    if args.write:
        write_ledger(current)
        print(f"wrote {LEDGER_PATH.relative_to(Path.cwd())} ({counts['_total']} scenarios)")
    else:
        diff = diff_ledger(baseline, current) if baseline else None
        if diff is not None:
            print(f"regressions : {len(diff.regressions)}")
            print(f"improvements: {len(diff.improvements)}")
            print(f"added       : {len(diff.added)}")
            print(f"removed     : {len(diff.removed)}")

    passing = counts["_passing"]
    total = counts["_total"]
    print(f"conformant  : {passing} / {total} ({passing / total * 100:.1f}%)")
    for outcome in sorted(KNOWN_OUTCOMES):
        print(f"  {outcome:18s} {counts[outcome]:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
