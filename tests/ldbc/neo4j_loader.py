"""Load an LDBC SNB data set into Neo4j, as the reference implementations do (WP-33).

Neo4j runs the verbatim reference queries as the correctness oracle
(docs/ldbc_benchmark_plan.md D4), so it must hold the graph the references
assume:

* every node carries its supertype label too (``:Message:Comment``,
  ``:Place:City``, ``:Organisation:Company``);
* the reference DDL: a uniqueness constraint on ``id`` per label, and the
  name/firstName/creationDate indexes;
* timestamps in the workload's encoding — epoch-ms integers for Interactive
  (``creationDate:LONG``), native ``DATETIME``/``DATE`` for BI.

Rows come from :func:`tests.ldbc.loader.iter_graph`, the same reader the
ArangoDB loader uses, so both engines receive identical graphs. An edge whose
``MATCH`` finds no endpoint would be dropped silently by Cypher; the write
counters of every batch are checked, and the per-label / per-type counts are
compared with the CSV files at the end.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from tests.ldbc.datasets import Encoding
from tests.ldbc.loader import (
    DEFAULT_BATCH_SIZE,
    EdgeRow,
    LdbcLoadError,
    LoadReport,
    NodeRow,
    count_differences,
    iter_graph,
)
from tests.ldbc.schema import CONCRETE_LABELS, REL_TYPES, SUPERTYPES, Column, labels_of

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: The reference DDL (ldbc_snb_interactive_v2_impls cypher/ddl/schema-constraints.cypher).
_UNIQUE_ID_LABELS = (*CONCRETE_LABELS, "Message", "Organisation", "Place")
_INDEXES = (
    ("Country", "name"),
    ("Person", "firstName"),
    ("Tag", "name"),
    ("TagClass", "name"),
    ("Message", "creationDate"),
    ("Comment", "creationDate"),
    ("Post", "creationDate"),
    ("Forum", "creationDate"),
    ("Person", "creationDate"),
)


def _ident(name: str) -> str:
    """A label/type/property name from the schema, checked before it reaches Cypher text."""
    if not _IDENT.match(name):
        raise ValueError(f"not a safe Cypher identifier: {name!r}")
    return name


def node_create_statement(label: str) -> str:
    labels = ":".join(_ident(lbl) for lbl in labels_of(label))
    return f"UNWIND $rows AS r CREATE (n:{labels}) SET n = r"


def edge_create_statement(rel_type: str, src_label: str, dst_label: str) -> str:
    return (
        f"UNWIND $rows AS r "
        f"MATCH (a:{_ident(src_label)} {{id: r.src}}) "
        f"MATCH (b:{_ident(dst_label)} {{id: r.dst}}) "
        f"CREATE (a)-[e:{_ident(rel_type)}]->(b) SET e = r.props"
    )


def ddl_statements() -> list[str]:
    stmts = [
        f"CREATE CONSTRAINT ldbc_{_ident(lbl).lower()}_id IF NOT EXISTS FOR (n:{lbl}) REQUIRE n.id IS UNIQUE"
        for lbl in _UNIQUE_ID_LABELS
    ]
    stmts += [
        f"CREATE INDEX ldbc_{_ident(lbl).lower()}_{_ident(prop).lower()} IF NOT EXISTS FOR (n:{lbl}) ON (n.{prop})"
        for lbl, prop in _INDEXES
    ]
    return stmts


def neo4j_value(value: Any, column: Column, encoding: Encoding) -> Any:
    """A loader value → what the reference implementation stores in Neo4j."""
    if encoding == "iso" and isinstance(value, str):
        if column.kind == "datetime":
            return dt.datetime.fromisoformat(value)
        if column.kind == "date":
            return dt.date.fromisoformat(value)
    return value


def _props(rec: dict[str, Any], columns: tuple[Column, ...], encoding: Encoding) -> dict[str, Any]:
    kinds = {c.name: c for c in columns}
    return {k: neo4j_value(v, kinds[k], encoding) for k, v in rec.items() if k not in ("src", "dst")}


class _Batcher:
    """Groups rows per statement and checks Neo4j's write counters on every flush."""

    def __init__(self, session: Any, batch_size: int) -> None:
        self._session = session
        self._batch_size = batch_size
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._kind: dict[str, str] = {}

    def add(self, statement: str, kind: str, row: dict[str, Any]) -> None:
        rows = self._pending.setdefault(statement, [])
        self._kind[statement] = kind
        rows.append(row)
        if len(rows) >= self._batch_size:
            self._flush(statement)

    def flush_all(self) -> None:
        for statement in list(self._pending):
            self._flush(statement)

    def _flush(self, statement: str) -> None:
        rows = self._pending.pop(statement, [])
        if not rows:
            return
        counters = self._session.run(statement, rows=rows).consume().counters
        created = (
            counters.nodes_created if self._kind[statement] == "node" else counters.relationships_created
        )
        if created != len(rows):
            raise LdbcLoadError(
                f"Neo4j created {created} of {len(rows)} {self._kind[statement]}s for: {statement} "
                "(an edge endpoint MATCH found nothing, or a constraint rejected a row)"
            )


def _neo4j_counts(session: Any) -> tuple[Counter[str], Counter[str]]:
    nodes: Counter[str] = Counter()
    edges: Counter[str] = Counter()
    for lbl in CONCRETE_LABELS:
        nodes[lbl] = session.run(f"MATCH (n:{_ident(lbl)}) RETURN count(n) AS c").single()["c"]
    for t in REL_TYPES:
        edges[t] = session.run(f"MATCH ()-[r:{_ident(t)}]->() RETURN count(r) AS c").single()["c"]
    return +nodes, +edges


#: Relationships or nodes deleted per transaction when wiping the database.
WIPE_BATCH = 10_000

#: One bounded delete per transaction. ``MATCH (n) CALL {…} IN TRANSACTIONS``
#: streams every node through one outer query and exhausted the 512 MB heap's
#: transaction memory pool at SF0.1 (367k nodes, 1.76M edges); relationships go
#: first so no node delete has to detach millions of them at once.
_WIPE_STATEMENTS = (
    "MATCH ()-[r]->() WITH r LIMIT $n DELETE r RETURN count(*) AS c",
    "MATCH (n) WITH n LIMIT $n DETACH DELETE n RETURN count(*) AS c",
)


def _wipe(session: Any, batch: int = WIPE_BATCH) -> None:
    for stmt in _WIPE_STATEMENTS:
        while session.run(stmt, n=batch).single()["c"]:
            pass


def load_neo4j(
    driver: Any,
    data_dir: Path,
    *,
    encoding: Encoding,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> LoadReport:
    """Replace the Neo4j database's contents with the data set under *data_dir*."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    started = time.monotonic()
    report = LoadReport(layout="neo4j", encoding=encoding)
    with driver.session() as session:
        _wipe(session)
        for stmt in ddl_statements():
            session.run(stmt).consume()
        session.run("CALL db.awaitIndexes(300)").consume()

        batcher = _Batcher(session, batch_size)
        nodes_written = False
        for item in iter_graph(data_dir, encoding):
            if isinstance(item, NodeRow):
                batcher.add(
                    node_create_statement(item.label), "node", _props(item.rec, item.file.columns, encoding)
                )
                report.nodes_by_label[item.label] += 1
            elif isinstance(item, EdgeRow):
                if not nodes_written:
                    # Edges MATCH their endpoints, so every node must be written first.
                    batcher.flush_all()
                    nodes_written = True
                stmt = edge_create_statement(item.file.rel_type, item.src_label, item.dst_label)
                row = {
                    "src": item.rec["src"],
                    "dst": item.rec["dst"],
                    "props": _props(item.rec, item.file.columns, encoding),
                }
                batcher.add(stmt, "relationship", row)
                report.edges_by_type[item.file.rel_type] += 1
        batcher.flush_all()

        db_nodes, db_edges = _neo4j_counts(session)
        diffs = count_differences(report.nodes_by_label, db_nodes) + count_differences(
            report.edges_by_type, db_edges
        )
        if diffs:
            raise LdbcLoadError(f"Neo4j load does not match the CSV files (csv → db): {'; '.join(diffs)}")
        for sup, members in SUPERTYPES.items():
            n = session.run(f"MATCH (n:{_ident(sup)}) RETURN count(n) AS c").single()["c"]
            expected = sum(report.nodes_by_label[m] for m in members)
            if n != expected:
                raise LdbcLoadError(f"Neo4j :{sup} holds {n} nodes, expected {expected} (its members' total)")

    report.seconds = time.monotonic() - started
    logger.info(
        "loaded LDBC into Neo4j (%s): %d nodes, %d edges in %.1fs",
        encoding,
        report.total_nodes,
        report.total_edges,
        report.seconds,
    )
    return report
