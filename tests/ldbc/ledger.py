"""LDBC translation ledger: the outcome of translating every operation per layout (WP-32).

The dry-run tier of the LDBC outcome model (docs/ldbc_benchmark_plan.md §5.2):
it needs no database, so ``test_ldbc_ratchet.py`` runs it in the default suite
and fails when a cell leaves ``translatable`` or an improvement goes
unrecorded — the same ratchet as the TCK ledger, whose diff it reuses. The
execution tier (``executes`` / ``matches_oracle``) is a separate ledger
(WP-33) because it needs a database.

"Translatable" is not "correct": only the execution ledger can say that.

Format, one line per cell, sorted::

    <outcome>\\t<operation id>@<layout>\\t<detail>

Regenerate with::

    .venv/bin/python -m tests.ldbc.ledger --write
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.ldbc.corpus import LDBC_DIR, Operation, load

LEDGER_PATH = LDBC_DIR / "translation_ledger.tsv"
LAYOUTS = ("pg", "lpg")

PASSING_OUTCOMES = frozenset({"translatable"})

#: ``crash`` is a non-CoreError exception: a transpiler bug, never a state to keep.
KNOWN_OUTCOMES = frozenset({"translatable", "fails_closed", "syntax_error", "out_of_subset", "crash"})

_DETAIL_MAX = 140

Cell = tuple[str, str]  # (outcome, detail)


def _detail(text: str) -> str:
    """First line, whitespace collapsed, mapping inventories dropped, truncated."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.split(r"\. Available (entities|relationships):", first)[0]
    first = re.sub(r"\s+", " ", first).replace("\t", " ")
    return first if len(first) <= _DETAIL_MAX else first[: _DETAIL_MAX - 1] + "…"


def classify(op: Operation, layout: str) -> Cell:
    """Translate *op* against the ``ldbc_<layout>`` fixture and name the outcome."""
    if op.out_of_subset:
        return "out_of_subset", _detail(op.out_of_subset)
    try:
        translate(op.text(), mapping=mapping_bundle_for(f"ldbc_{layout}"))
    except Exception as e:  # noqa: BLE001 — every exception is an outcome to record
        return classify_error(e)
    return "translatable", ""


def classify_error(e: Exception) -> Cell:
    """The ledger outcome for an exception raised by ``translate()``."""
    if isinstance(e, CoreError):
        if e.code == "CYPHER_SYNTAX_ERROR":
            return "syntax_error", _detail(str(e))
        return "fails_closed", _detail(f"{e.code}: {e}")
    return "crash", _detail(f"{type(e).__name__}: {e}")


def build_ledger() -> dict[str, Cell]:
    """``{"<op>@<layout>": (outcome, detail)}`` for every operation and layout."""
    return {f"{op.id}@{layout}": classify(op, layout) for op in load() for layout in LAYOUTS}


def summarize(ledger: dict[str, Cell]) -> dict[str, Counter[str]]:
    """Outcome counts per layout."""
    out: dict[str, Counter[str]] = {layout: Counter() for layout in LAYOUTS}
    for cell, (outcome, _) in ledger.items():
        out[cell.rsplit("@", 1)[1]][outcome] += 1
    return out


def write_cells(ledger: dict[str, Cell], path: Path, preamble: list[str]) -> None:
    """Write *ledger* as ``<outcome>\\t<cell>\\t<detail>`` lines under a comment header
    of *preamble* plus per-layout outcome counts. Shared by both LDBC ledgers."""
    lines = [f"# {line}" for line in preamble]
    for layout, counts in summarize(ledger).items():
        lines.append(f"# {layout}: " + ", ".join(f"{o} {n}" for o, n in sorted(counts.items())))
    body = "".join(f"{outcome}\t{cell}\t{detail}\n" for cell, (outcome, detail) in sorted(ledger.items()))
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")


def write_ledger(ledger: dict[str, Cell], path: Path = LEDGER_PATH) -> None:
    write_cells(
        ledger,
        path,
        [
            "LDBC SNB translation ledger — regenerate with:",
            "  .venv/bin/python -m tests.ldbc.ledger --write",
            "Format: <outcome>\\t<operation>@<layout>\\t<detail>, sorted by cell.",
            f"Passing outcomes: {', '.join(sorted(PASSING_OUTCOMES))}. Translatable is not correct;",
            "the execution ledger (WP-33) records which translations return the oracle's rows.",
        ],
    )


def load_ledger(path: Path = LEDGER_PATH) -> dict[str, Cell]:
    if not path.exists():
        return {}
    ledger: dict[str, Cell] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError(f"malformed ledger line: {line!r}")
        outcome, cell, detail = parts
        ledger[cell] = (outcome, detail)
    return ledger


def outcomes(ledger: dict[str, Cell]) -> dict[str, str]:
    """The ``{cell: outcome}`` view the shared TCK diff works on."""
    return {cell: outcome for cell, (outcome, _) in ledger.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure or record the LDBC translation ledger.")
    ap.add_argument("--write", action="store_true", help=f"write {LEDGER_PATH.name}")
    args = ap.parse_args(argv)
    ledger = build_ledger()
    if args.write:
        write_ledger(ledger)
        print(f"wrote {LEDGER_PATH}")
    for layout, counts in summarize(ledger).items():
        print(f"{layout}: " + ", ".join(f"{o} {n}" for o, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
