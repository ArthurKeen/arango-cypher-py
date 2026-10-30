"""Render an LDBC benchmark run (``tests.ldbc.bench``) as Markdown, check a
rendered document against its run, and compare two runs.

- ``render(result)`` writes the disclaimer and attribution LDBC's fair-use
  policy requires (PRD LDBC-7), the environment, and one table row per
  operation × layout. Numbers are generated, never copied by hand.
- ``check(markdown, result)`` is the conformance check: the disclaimer is
  present, and every table row is one ``render`` would write from *result*.
- ``compare(old, new)`` flags what changed between two runs: a plan whose
  shape changed, an operation whose server p50 moved by more than a factor,
  and a cell whose outcome changed. Shared CI runners make p99 too noisy to
  flag, so only p50 is compared.

    .venv/bin/python -m tests.ldbc.report results/bench.json --out results/ldbc_results.md
    .venv/bin/python -m tests.ldbc.report results/new.json --compare results/old.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DISCLAIMER = (
    "These are not LDBC Benchmark Results. They use the LDBC Social Network Benchmark "
    "schema, data generator output and reference queries as a validation and performance "
    "corpus; they are not an implementation of an LDBC Benchmark and are not audited."
)
ATTRIBUTION = (
    "LDBC SNB data and reference queries: Linked Data Benchmark Council (ldbcouncil.org), Apache License 2.0."
)
_HEADER = (
    "| Operation | Layout | Outcome | Rows | Server p50 ms | Server p95 ms | Server p99 ms "
    "| Client p50 ms | Translate cold p50 ms | Translate warm p50 ms | Plan |"
)
#: A server p50 that moves by more than this factor is flagged.
P50_FACTOR = 1.5
#: Below this p50 (ms) a move is timer noise, not a regression.
P50_FLOOR_MS = 5.0


def _num(value: Any) -> str:
    return "" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def _get(cell: dict[str, Any], *path: str) -> Any:
    value: Any = cell
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def row(cell: dict[str, Any]) -> str:
    """The table row ``render`` writes for one cell."""
    rows = cell.get("rows")
    columns = [
        cell["operation"],
        cell["layout"],
        cell["outcome"],
        "/".join(str(r) for r in rows) if rows else "",
        _num(_get(cell, "server_ms", "p50")),
        _num(_get(cell, "server_ms", "p95")),
        _num(_get(cell, "server_ms", "p99")),
        _num(_get(cell, "client_ms", "p50")),
        _num(_get(cell, "translation_ms", "cold", "p50")),
        _num(_get(cell, "translation_ms", "warm", "p50")),
        _get(cell, "plan", "digest") or "",
    ]
    return "| " + " | ".join(columns) + " |"


def render(result: dict[str, Any]) -> str:
    env = result["environment"]
    lines = [
        "# LDBC SNB performance (internal)",
        "",
        f"> {DISCLAIMER}",
        ">",
        f"> {ATTRIBUTION}",
        "",
        "## Environment",
        "",
        f"- Scale: {env['data']['scale']}; data sets: "
        + ", ".join(f"{v['name']} ({v['content_digest'][:12]})" for v in env["data"]["datasets"].values()),
        f"- Server: ArangoDB {env['server']['version']} ({env['server']['engine']}, {env['server']['mode']}),"
        f" image {env['server']['image']}",
        f"- Host: {env['host']['platform']}, {env['host']['cpus']} CPUs, "
        f"{(env['host']['memory_bytes'] or 0) // 2**20} MiB",
        f"- Git: {env['git']['sha'][:12]}{' (dirty)' if env['git']['dirty'] else ''}; mappings "
        + ", ".join(f"{k} {v}" for k, v in env["mappings"].items()),
        f"- Settings: {json.dumps(env['settings'], sort_keys=True)}",
        f"- Recorded: {env['recorded_at']}",
        "",
        "## Results",
        "",
        _HEADER,
        "|" + "|".join(["---"] * (_HEADER.count("|") - 1)) + "|",
    ]
    lines += [row(cell) for cell in result["cells"]]
    return "\n".join(lines) + "\n"


def check(markdown: str, result: dict[str, Any]) -> list[str]:
    """Problems with *markdown* as a rendering of *result*; empty when it conforms."""
    problems: list[str] = []
    if DISCLAIMER not in markdown:
        problems.append("the LDBC fair-use disclaimer is missing")
    if ATTRIBUTION not in markdown:
        problems.append("the LDBC attribution is missing")
    expected = {row(cell) for cell in result["cells"]}
    found = {line for line in markdown.splitlines() if line.startswith("| ") and line != _HEADER}
    for line in sorted(found - expected):
        problems.append(f"row not generated from the run: {line}")
    for line in sorted(expected - found):
        problems.append(f"row of the run missing: {line}")
    return problems


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Flags between two runs (module docstring)."""
    flags: list[str] = []
    before = {(c["operation"], c["layout"]): c for c in old["cells"]}
    for cell in new["cells"]:
        key = (cell["operation"], cell["layout"])
        name = f"{key[0]}@{key[1]}"
        prev = before.get(key)
        if prev is None:
            continue
        if prev["outcome"] != cell["outcome"]:
            flags.append(f"{name}: outcome {prev['outcome']} -> {cell['outcome']}")
            continue
        was, now = _get(prev, "plan", "digest"), _get(cell, "plan", "digest")
        if was and now and was != now:
            flags.append(f"{name}: plan changed ({was} -> {now})")
        p_was, p_now = _get(prev, "server_ms", "p50"), _get(cell, "server_ms", "p50")
        if p_was is not None and p_now is not None and max(p_was, p_now) >= P50_FLOOR_MS:
            ratio = p_now / p_was if p_was else float("inf")
            if ratio > P50_FACTOR or ratio < 1 / P50_FACTOR:
                flags.append(f"{name}: server p50 {p_was:.3f} -> {p_now:.3f} ms (x{ratio:.2f})")
    return flags


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render, check or compare LDBC benchmark runs.")
    ap.add_argument("result", type=Path)
    ap.add_argument("--out", type=Path, help="write the Markdown report here")
    ap.add_argument("--check", type=Path, help="check this Markdown document against the run")
    ap.add_argument("--compare", type=Path, help="flag changes from this earlier run")
    args = ap.parse_args(argv)
    result = json.loads(args.result.read_text(encoding="utf-8"))
    status = 0
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(result), encoding="utf-8")
        print(f"wrote {args.out}")
    if args.check:
        problems = check(args.check.read_text(encoding="utf-8"), result)
        for p in problems:
            print(f"conformance: {p}", file=sys.stderr)
        status = 1 if problems else status
    if args.compare:
        flags = compare(json.loads(args.compare.read_text(encoding="utf-8")), result)
        print("\n".join(f"- {f}" for f in flags) if flags else "- no plan changes or p50 moves")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
