"""Record the Neo4j oracle's results for the LDBC reads (WP-33 part 2).

For every read binding in ``params/<scale>.yml`` the verbatim reference query
runs on Neo4j, and the rows are written to ``expected/<scale>/<op>.json``. The
execution tier (``execution.py``) compares translated AQL against these
snapshots, so it needs no Neo4j; ``tests/integration/test_ldbc_crossvalidate.py``
re-records in memory under ``RUN_CROSS=1`` and fails if the oracle drifted.

Each snapshot carries the query's SHA-256 from the manifest: an edited query
or a refreshed corpus invalidates it instead of being compared against stale
rows. Temporal values are written in LDBC's text format
(``2010-01-03T15:10:31.499+00:00``, dates ``1984-03-11``), the form ArangoDB
stores them in.

Record (needs the Neo4j of ``docker-compose.neo4j.yml``)::

    RUN_CROSS=1 .venv/bin/python -m tests.ldbc.oracle --record
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from tests.helpers.result_equivalence import has_order_by, sort_list_columns
from tests.ldbc import params as P
from tests.ldbc.corpus import LDBC_DIR, Operation, load

logger = logging.getLogger(__name__)

EXPECTED_DIR = LDBC_DIR / "expected"


def snapshot_path(op_id: str, scale: str = "sf0.003") -> Path:
    return EXPECTED_DIR / scale / f"{op_id}.json"


def jsonable(value: Any) -> Any:
    """A Neo4j result value → JSON, temporal values in LDBC's text format."""
    native = value.to_native() if hasattr(value, "to_native") else value
    if isinstance(native, dt.datetime):
        if native.tzinfo is None:
            raise ValueError(f"local datetime {native!r} in an oracle result; LDBC values are zoned")
        return P._ldbc_datetime(native)
    if isinstance(native, dt.date):
        return native.isoformat()
    if isinstance(native, list):
        return [jsonable(v) for v in native]
    if isinstance(native, dict):
        return {k: jsonable(v) for k, v in native.items()}
    if hasattr(value, "months") and hasattr(value, "nanoseconds"):  # neo4j.time.Duration
        raise ValueError(f"duration {value!r} in an oracle result has no AQL counterpart to compare with")
    return native


def run_binding(session: Any, op: Operation, binding: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Columns and JSON-safe rows of *op* on Neo4j for *binding*."""
    from tests.integration.neo4j_reference import _coerce

    result = session.run(op.text(), {k: P.to_neo4j(v) for k, v in binding.items()})
    columns = list(result.keys())
    rows = [[jsonable(_coerce(record[c])) for c in columns] for record in result]
    return columns, rows


def record(driver: Any, scale: str = "sf0.003") -> dict[str, dict[str, Any]]:
    """Snapshots for every operation in the committed params, keyed by operation id."""
    from tests.ldbc.datasets import DATASETS, fetch
    from tests.ldbc.neo4j_loader import load_neo4j

    bindings = P.read(scale)
    ops = {op.id: op for op in load()}
    version = driver.get_server_info().agent
    out: dict[str, dict[str, Any]] = {}
    for workload, dataset in P._WORKLOAD_DATASET.items():
        load_neo4j(driver, fetch(dataset), encoding=DATASETS[dataset].encoding)
        with driver.session() as session:
            for op_id in sorted(o for o in bindings if ops[o].workload == workload):
                op = ops[op_id]
                recorded = []
                for b in bindings[op_id]:
                    logger.debug("%s %s", op_id, b["binding"])
                    columns, rows = run_binding(session, op, b["binding"])
                    unordered = frozenset(i for i, c in enumerate(columns) if c in op.unordered_columns)
                    rows = [sort_list_columns(r, unordered) for r in rows]
                    if len(rows) != b["rows"]:
                        raise RuntimeError(
                            f"{op_id}: the oracle now returns {len(rows)} rows for a binding params recorded "
                            f"with {b['rows']}; regenerate params first"
                        )
                    recorded.append(
                        {
                            "binding": {k: P._dump_value(v) for k, v in b["binding"].items()},
                            "columns": columns,
                            "rows": rows,
                        }
                    )
                out[op_id] = {
                    "kind": "read",
                    "operation": op_id,
                    "file": op.file,
                    "sha256": op.sha256,
                    "dataset": dataset,
                    "oracle": version,
                    "ordered": has_order_by(op.text()),
                    "bindings": recorded,
                }
    out.update(record_writes(driver, scale))
    return out


def record_writes(driver: Any, scale: str = "sf0.003") -> dict[str, dict[str, Any]]:
    """The Neo4j post-state delta of every write binding, each run in a rolled-back transaction."""
    from tests.ldbc.datasets import DATASETS, fetch
    from tests.ldbc.neo4j_loader import load_neo4j
    from tests.ldbc.writes import dump_neo4j, neo4j_write_delta

    dataset = P._WORKLOAD_DATASET["interactive_v2"]
    load_neo4j(driver, fetch(dataset), encoding=DATASETS[dataset].encoding)
    with driver.session() as session:
        before = session.execute_read(dump_neo4j)
    version = driver.get_server_info().agent
    ops = {op.id: op for op in load()}
    out: dict[str, dict[str, Any]] = {}
    for op_id, binding in sorted(P.read_writes(scale).items()):
        op = ops[op_id]
        d = neo4j_write_delta(driver, op.text(), {k: P.to_neo4j(v) for k, v in binding.items()}, before)
        if not any(d.values()):
            raise RuntimeError(f"{op_id}: the write binding changes nothing on the oracle; choose another")
        out[op_id] = {
            "kind": "write",
            "operation": op_id,
            "file": op.file,
            "sha256": op.sha256,
            "dataset": dataset,
            "oracle": version,
            "bindings": [{"binding": {k: P._dump_value(v) for k, v in binding.items()}, "delta": d}],
        }
    return out


def write(snapshots: dict[str, dict[str, Any]], scale: str = "sf0.003") -> None:
    (EXPECTED_DIR / scale).mkdir(parents=True, exist_ok=True)
    for op_id, snap in snapshots.items():
        snapshot_path(op_id, scale).write_text(
            json.dumps(snap, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8"
        )


def read(op_id: str, scale: str = "sf0.003") -> dict[str, Any] | None:
    path = snapshot_path(op_id, scale)
    if not path.exists():
        return None
    snap = json.loads(path.read_text(encoding="utf-8"))
    for b in snap["bindings"]:
        b["binding"] = {k: P._load_value(v) for k, v in b["binding"].items()}
    return snap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record the Neo4j oracle's LDBC read results.")
    ap.add_argument("--record", action="store_true", help="write expected/<scale>/<op>.json")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    from tests.integration.neo4j_reference import get_driver

    driver = get_driver()
    try:
        snapshots = record(driver)
    finally:
        driver.close()
    total = sum(len(s["bindings"]) for s in snapshots.values())
    print(f"{len(snapshots)} operations, {total} bindings recorded")
    if args.record:
        write(snapshots)
        print(f"wrote {EXPECTED_DIR / 'sf0.003'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
