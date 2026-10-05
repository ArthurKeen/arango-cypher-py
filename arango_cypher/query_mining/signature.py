"""Compare what two queries returned: the "same results" pass rule.

A saved AQL query and the Cypher written for it rarely return rows of the same
*shape* — the AQL may ``RETURN p`` (a path), the transpiled Cypher a node and an
edge per row. What must agree is **which documents** came back. So a result is
reduced to a signature: the sets of vertex and edge ``_id``\\ s found anywhere in
its rows (inside paths, nested objects and arrays), plus — for results that
carry no documents at all, such as counts — the multiset of scalar rows. An
edge's ``_from`` / ``_to`` count as vertices the result touches, so a query that
returns edges and one that returns the nodes they connect can agree.

Verdicts
--------
``identical``
    Same vertex ids and same edge ids.
``same_vertices``
    Same vertex ids; one side returned edges and the other none. A query that
    returns paths and one that returns the endpoints mean the same answer.
``same_values``
    Neither side returned documents and the scalar rows match (order-free,
    floats compared to :data:`FLOAT_DIGITS` places).
``different``
    Anything else; the detail says what differs.

Only the first three pass.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

FLOAT_DIGITS = 6
PASSING = frozenset({"identical", "same_vertices", "same_values"})


@dataclass(frozen=True)
class ResultSignature:
    vertices: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()
    #: Canonical JSON of rows that hold no documents (counts, projections).
    scalars: Counter[str] = field(default_factory=Counter)
    rows: int = 0

    @property
    def has_documents(self) -> bool:
        return bool(self.vertices or self.edges)

    def describe(self) -> str:
        return f"{self.rows} rows, {len(self.vertices)} vertices, {len(self.edges)} edges"


def _is_document(value: dict[str, Any]) -> bool:
    return isinstance(value.get("_id"), str) and "/" in value["_id"]


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, FLOAT_DIGITS)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items()) if not k.startswith("_")}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def signature_of(rows: list[Any]) -> ResultSignature:
    vertices: set[str] = set()
    edges: set[str] = set()
    scalars: Counter[str] = Counter()

    def walk(value: Any) -> bool:
        """Collect documents under *value*; True if any was found."""
        if isinstance(value, dict):
            if _is_document(value):
                if isinstance(value.get("_from"), str) and isinstance(value.get("_to"), str):
                    edges.add(value["_id"])
                    vertices.update((value["_from"], value["_to"]))
                else:
                    vertices.add(value["_id"])
                return True
            found = False
            for v in value.values():
                found = walk(v) or found
            return found
        if isinstance(value, list):
            found = False
            for v in value:
                found = walk(v) or found
            return found
        return False

    for row in rows:
        if not walk(row):
            scalars[json.dumps(_canonical(row), sort_keys=True, default=str)] += 1
    return ResultSignature(frozenset(vertices), frozenset(edges), scalars, len(rows))


@dataclass(frozen=True)
class Verdict:
    kind: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.kind in PASSING


def compare(source: ResultSignature, candidate: ResultSignature) -> Verdict:
    if not source.has_documents and not candidate.has_documents:
        if source.scalars == candidate.scalars:
            return Verdict("same_values")
        return Verdict(
            "different", f"values differ: source {source.describe()}, candidate {candidate.describe()}"
        )

    if source.vertices != candidate.vertices:
        missing = len(source.vertices - candidate.vertices)
        extra = len(candidate.vertices - source.vertices)
        return Verdict("different", f"vertices differ: {missing} missing, {extra} extra")
    if source.edges == candidate.edges:
        return Verdict("identical")
    if not source.edges or not candidate.edges:
        return Verdict("same_vertices", "one side returned no edges")
    missing = len(source.edges - candidate.edges)
    extra = len(candidate.edges - source.edges)
    return Verdict("different", f"edges differ: {missing} missing, {extra} extra")
