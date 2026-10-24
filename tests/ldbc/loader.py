"""Load an LDBC SNB data set into ArangoDB as PG or LPG (docs/ldbc_benchmark_plan.md §5.4).

Both layouts hold the same logical graph, and :func:`load` proves it: it
counts what the CSV files contain, counts what the database holds afterwards,
and raises if any label or relationship type differs. Nothing is dropped
silently — an unparsable value, a dangling endpoint or a failed bulk import
stops the load with the file, row and column that caused it.

* **PG** — one vertex collection per concrete label (``person``, ``post``,
  ``comment``, ``city`` …) keyed by ``str(id)``; one edge collection per
  relationship type (``has_creator`` …). Polymorphic edges point into
  whichever member collection the endpoint belongs to.
* **LPG** — every node in ``nodes`` with ``type`` (concrete label) and
  ``labels`` (all labels, e.g. ``["Message", "Comment"]``), keyed
  ``<Label>_<id>`` because ids repeat across labels; every edge in ``edges``
  with ``type``.

Empty CSV fields are omitted, matching the reference Neo4j import
(``--ignore-empty-strings=true``): a post with no image has no ``imageFile``,
so ``coalesce(m.imageFile, m.content)`` behaves the same on both engines.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from arango.exceptions import DocumentInsertError

from tests.integration.datasets import _ensure_persistent_index
from tests.integration.seed import _ensure_doc_collection, _ensure_edge_collection, _reset_collection
from tests.ldbc.datasets import Encoding, part_files
from tests.ldbc.schema import (
    CONCRETE_LABELS,
    EDGE_FILES,
    NODE_FILES,
    REL_TYPES,
    Column,
    EdgeFile,
    NodeFile,
    labels_of,
    snake,
)

logger = logging.getLogger(__name__)

Layout = Literal["pg", "lpg"]

LPG_NODES = "nodes"
LPG_EDGES = "edges"
TYPE_FIELD = "type"
LABELS_FIELD = "labels"

#: Documents per bulk-import request.
DEFAULT_BATCH_SIZE = 10_000

_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


class LdbcLoadError(RuntimeError):
    """The data set could not be loaded faithfully."""


@dataclass(frozen=True)
class IndexSpec:
    collection: str
    fields: tuple[str, ...]
    name: str
    unique: bool = False


def pg_collection(label: str) -> str:
    return snake(label)


def pg_edge_collection(rel_type: str) -> str:
    return snake(rel_type)


def pg_indexes() -> list[IndexSpec]:
    """The reference DDL (unique ``id``, name/firstName, creationDate) per collection."""
    specs = [
        IndexSpec(pg_collection(lbl), ("id",), f"idx_{pg_collection(lbl)}_id", unique=True)
        for lbl in CONCRETE_LABELS
    ]
    for lbl, prop in (("Person", "firstName"), ("Country", "name"), ("Tag", "name"), ("TagClass", "name")):
        specs.append(IndexSpec(pg_collection(lbl), (prop,), f"idx_{pg_collection(lbl)}_{snake(prop)}"))
    for lbl in ("Person", "Forum", "Post", "Comment"):
        specs.append(
            IndexSpec(pg_collection(lbl), ("creationDate",), f"idx_{pg_collection(lbl)}_creation_date")
        )
    return specs


def lpg_indexes() -> list[IndexSpec]:
    """Type-led node indexes, a labels array index, and edge VCIs led by ``_from``/``_to``.

    The translator's traversal index hint prefers a VCI whose first field is the
    traversal's anchor (``arango_cypher/_translate_v0/hints.py``), so both
    directions get one.
    """
    return [
        IndexSpec(LPG_NODES, (TYPE_FIELD, "id"), "idx_nodes_type_id", unique=True),
        IndexSpec(LPG_NODES, (TYPE_FIELD,), "idx_nodes_type"),
        IndexSpec(LPG_NODES, (f"{LABELS_FIELD}[*]",), "idx_nodes_labels"),
        IndexSpec(LPG_NODES, (TYPE_FIELD, "name"), "idx_nodes_type_name"),
        IndexSpec(LPG_NODES, (TYPE_FIELD, "firstName"), "idx_nodes_type_first_name"),
        IndexSpec(LPG_NODES, (TYPE_FIELD, "creationDate"), "idx_nodes_type_creation_date"),
        IndexSpec(LPG_EDGES, ("_from", TYPE_FIELD), "idx_edges_from_type"),
        IndexSpec(LPG_EDGES, ("_to", TYPE_FIELD), "idx_edges_to_type"),
    ]


@dataclass
class LoadReport:
    layout: Layout
    encoding: Encoding
    nodes_by_label: Counter[str] = field(default_factory=Counter)
    edges_by_type: Counter[str] = field(default_factory=Counter)
    seconds: float = 0.0

    @property
    def total_nodes(self) -> int:
        return sum(self.nodes_by_label.values())

    @property
    def total_edges(self) -> int:
        return sum(self.edges_by_type.values())


# ---------------------------------------------------------------------------
# CSV → values
# ---------------------------------------------------------------------------

_MISSING = object()


def read_rows(entity_dir: Path) -> Iterator[tuple[str, int, list[str]]]:
    """Yield ``(file name, 1-based line, fields)`` for every row of an entity."""
    files = part_files(entity_dir)
    if not files:
        raise LdbcLoadError(f"{entity_dir}: no part-*.csv[.gz] files")
    for f in files:
        raw = gzip.open(f, "rb") if f.suffix == ".gz" else f.open("rb")
        with raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as fh:
            for n, row in enumerate(csv.reader(fh, delimiter="|", quotechar='"'), start=1):
                yield f.name, n, row


def convert(raw: str, col: Column, encoding: Encoding) -> Any:
    """One CSV field → a document value, or ``_MISSING`` for an empty field."""
    if raw == "":
        return _MISSING
    kind = col.kind
    if kind in ("id", "int", "start", "end"):
        return int(raw)
    if kind in ("datetime", "date"):
        if encoding == "epoch_ms":
            return int(raw)
        if not _ISO_PREFIX.match(raw):
            raise ValueError(f"{raw!r} is not ISO-8601 (is the data set epoch-ms?)")
        return raw
    if kind == "str_list":
        return raw.split(";")
    return raw


def _parse(row: list[str], cols: tuple[Column, ...], encoding: Encoding, where: str) -> dict[str, Any]:
    if len(row) != len(cols):
        raise LdbcLoadError(
            f"{where}: expected {len(cols)} fields ({', '.join(c.name for c in cols)}), got {len(row)}"
        )
    out: dict[str, Any] = {}
    for raw, col in zip(row, cols, strict=True):
        try:
            v = convert(raw, col, encoding)
        except ValueError as e:
            raise LdbcLoadError(f"{where}: column {col.name!r} ({col.kind}): {e}") from e
        if v is not _MISSING:
            out[col.name] = v
    return out


# ---------------------------------------------------------------------------
# Rows → documents
# ---------------------------------------------------------------------------


def _node_label(nf: NodeFile, rec: dict[str, Any], where: str) -> str:
    label = nf.label if nf.label is not None else rec.pop("type", None)
    if label not in CONCRETE_LABELS:
        raise LdbcLoadError(f"{where}: label {label!r} is not a concrete LDBC label")
    return label


def node_doc(nf: NodeFile, rec: dict[str, Any], label: str, layout: Layout) -> tuple[str, dict[str, Any]]:
    """``(collection, document)`` for one parsed node row."""
    if layout == "pg":
        return pg_collection(label), {"_key": str(rec["id"]), **rec}
    return LPG_NODES, {
        "_key": f"{label}_{rec['id']}",
        TYPE_FIELD: label,
        LABELS_FIELD: labels_of(label),
        **rec,
    }


def node_ref(label: str, node_id: int, layout: Layout) -> str:
    if layout == "pg":
        return f"{pg_collection(label)}/{node_id}"
    return f"{LPG_NODES}/{label}_{node_id}"


def edge_doc(
    ef: EdgeFile, rec: dict[str, Any], src_label: str, dst_label: str, layout: Layout
) -> tuple[str, dict[str, Any]]:
    src, dst = rec.pop("src"), rec.pop("dst")
    doc = {"_from": node_ref(src_label, src, layout), "_to": node_ref(dst_label, dst, layout), **rec}
    if layout == "pg":
        return pg_edge_collection(ef.rel_type), doc
    return LPG_EDGES, {**doc, TYPE_FIELD: ef.rel_type}


class _LabelIndex:
    """Concrete label of every node id, per id space, for endpoint resolution."""

    def __init__(self) -> None:
        self._by_space: dict[str, dict[int, str]] = {}

    def add(self, space: str, node_id: int, label: str) -> None:
        self._by_space.setdefault(space, {})[node_id] = label

    def resolve(self, space: str, node_id: int, where: str) -> str:
        label = self._by_space.get(space, {}).get(node_id)
        if label is None:
            raise LdbcLoadError(f"{where}: endpoint {space} {node_id} does not exist")
        return label


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------


def _collections(layout: Layout) -> tuple[list[str], list[str]]:
    if layout == "pg":
        return [pg_collection(lbl) for lbl in CONCRETE_LABELS], [pg_edge_collection(t) for t in REL_TYPES]
    return [LPG_NODES], [LPG_EDGES]


class _BulkWriter:
    def __init__(self, db: Any, batch_size: int) -> None:
        self._db = db
        self._batch_size = batch_size
        self._pending: dict[str, list[dict[str, Any]]] = {}

    def add(self, collection: str, doc: dict[str, Any]) -> None:
        batch = self._pending.setdefault(collection, [])
        batch.append(doc)
        if len(batch) >= self._batch_size:
            self._flush(collection)

    def flush_all(self) -> None:
        for name in list(self._pending):
            self._flush(name)

    def _flush(self, collection: str) -> None:
        batch = self._pending.pop(collection, [])
        if not batch:
            return
        try:
            res = self._db.collection(collection).import_bulk(batch, halt_on_error=True, details=True)
        except DocumentInsertError as e:
            raise LdbcLoadError(
                f"bulk import of {len(batch)} documents into {collection!r} failed: {e}"
            ) from e
        if res.get("errors") or res.get("created") != len(batch):
            details = "; ".join((res.get("details") or [])[:5])
            raise LdbcLoadError(
                f"bulk import into {collection!r}: created {res.get('created')} of {len(batch)}, "
                f"errors {res.get('errors')}: {details}"
            )


def _db_counts(db: Any, layout: Layout) -> tuple[Counter[str], Counter[str]]:
    nodes: Counter[str] = Counter()
    edges: Counter[str] = Counter()
    if layout == "pg":
        for lbl in CONCRETE_LABELS:
            nodes[lbl] = db.collection(pg_collection(lbl)).count()
        for t in REL_TYPES:
            edges[t] = db.collection(pg_edge_collection(t)).count()
    else:
        q = "FOR d IN @@c COLLECT t = d[@f] WITH COUNT INTO n RETURN [t, n]"
        for t, n in db.aql.execute(q, bind_vars={"@c": LPG_NODES, "f": TYPE_FIELD}):
            nodes[t] = n
        for t, n in db.aql.execute(q, bind_vars={"@c": LPG_EDGES, "f": TYPE_FIELD}):
            edges[t] = n
    return +nodes, +edges


def count_differences(expected: Counter[str], actual: Counter[str]) -> list[str]:
    """``"Comment 471 → 470"`` for every name whose counts differ, sorted."""
    return [
        f"{k} {expected[k]} → {actual[k]}"
        for k in sorted(set(expected) | set(actual))
        if expected[k] != actual[k]
    ]


def _dangling_endpoints(db: Any, edge_collections: list[str]) -> dict[str, int]:
    q = "FOR e IN @@c FILTER DOCUMENT(e._from) == null OR DOCUMENT(e._to) == null COLLECT WITH COUNT INTO n RETURN n"
    out = {}
    for c in edge_collections:
        n = next(iter(db.aql.execute(q, bind_vars={"@c": c})), 0)
        if n:
            out[c] = n
    return out


def load(
    db: Any,
    data_dir: Path,
    *,
    layout: Layout,
    encoding: Encoding,
    batch_size: int = DEFAULT_BATCH_SIZE,
    check_dangling: bool = True,
) -> LoadReport:
    """Load the ``initial_snapshot`` under *data_dir* into *db* as *layout*.

    Idempotent: the layout's collections are truncated first. Raises
    :class:`LdbcLoadError` if the database does not end up holding exactly
    what the CSV files contain.
    """
    if layout not in ("pg", "lpg"):
        raise ValueError(f"unknown layout {layout!r}; expected 'pg' or 'lpg'")
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    snapshot = data_dir / "initial_snapshot"
    if not snapshot.is_dir():
        raise LdbcLoadError(f"{data_dir}: no initial_snapshot directory")

    started = time.monotonic()
    vertex_colls, edge_colls = _collections(layout)
    for name in vertex_colls:
        _reset_collection(_ensure_doc_collection(db, name))
    for name in edge_colls:
        _reset_collection(_ensure_edge_collection(db, name))

    report = LoadReport(layout=layout, encoding=encoding)
    labels = _LabelIndex()
    writer = _BulkWriter(db, batch_size)

    for nf in NODE_FILES:
        for fname, line, row in read_rows(snapshot / nf.part / nf.name):
            where = f"{nf.part}/{nf.name}/{fname}:{line}"
            rec = _parse(row, nf.columns, encoding, where)
            label = _node_label(nf, rec, where)
            labels.add(nf.id_space, rec["id"], label)
            writer.add(*node_doc(nf, rec, label, layout))
            report.nodes_by_label[label] += 1
    writer.flush_all()

    for ef in EDGE_FILES:
        for fname, line, row in read_rows(snapshot / ef.part / ef.name):
            where = f"{ef.part}/{ef.name}/{fname}:{line}"
            rec = _parse(row, ef.columns, encoding, where)
            src_label = labels.resolve(ef.src_space, rec["src"], where)
            dst_label = labels.resolve(ef.dst_space, rec["dst"], where)
            writer.add(*edge_doc(ef, rec, src_label, dst_label, layout))
            report.edges_by_type[ef.rel_type] += 1
    writer.flush_all()

    for spec in pg_indexes() if layout == "pg" else lpg_indexes():
        _ensure_persistent_index(
            db.collection(spec.collection), list(spec.fields), name=spec.name, unique=spec.unique
        )

    db_nodes, db_edges = _db_counts(db, layout)
    diffs = count_differences(report.nodes_by_label, db_nodes) + count_differences(
        report.edges_by_type, db_edges
    )
    if diffs:
        raise LdbcLoadError(f"{layout} load does not match the CSV files (csv → db): {'; '.join(diffs)}")
    if check_dangling:
        dangling = _dangling_endpoints(db, edge_colls)
        if dangling:
            raise LdbcLoadError(f"{layout} load has edges with missing endpoints: {dangling}")

    report.seconds = time.monotonic() - started
    logger.info(
        "loaded LDBC %s (%s): %d nodes, %d edges in %.1fs",
        layout,
        encoding,
        report.total_nodes,
        report.total_edges,
        report.seconds,
    )
    return report
