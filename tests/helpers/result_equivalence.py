"""Result-set equivalence between a reference Cypher engine and translated AQL.

Shared by the Neo4j cross-validation suites (Movies, Northwind) and the LDBC
execution tier, which compares against recorded Neo4j snapshots. Lifted from
``tests/integration/test_movies_crossvalidate.py`` unchanged in behaviour.

Rows are compared **positionally**, not by column name: Neo4j keeps the raw
dotted expression as a column name (``p.name``) where AQL must rename it.
"""

from __future__ import annotations

import json
from typing import Any


def normalize_scalar(v: Any) -> Any:
    """Normalize Neo4j vs AQL scalar differences.

    - Integers and floats with zero fractional part are treated as equal
      to their integer form (Neo4j returns int, AQL may return float for
      SUM/AVG/COUNT, etc.).
    - ``None`` and missing-key both compare equal to ``None``.
    - Nested dicts/lists are normalized recursively.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if v == int(v) and not (v != v):  # finite, integral
            return int(v)
        return v
    if isinstance(v, list):
        return [normalize_scalar(x) for x in v]
    if isinstance(v, dict):
        return {k: normalize_scalar(val) for k, val in v.items()}
    return v


def row_values(row: Any) -> list[Any]:
    """Return row values in RETURN-clause order, normalized.

    Accepts a ``dict`` (a Neo4j record after coercion, or an AQL
    multi-projection; both preserve RETURN order), a ``list`` (one element per
    column), or a scalar (a single-column AQL ``RETURN expr``).
    """
    if isinstance(row, dict):
        return [normalize_scalar(v) for v in row.values()]
    if isinstance(row, list):
        return [normalize_scalar(v) for v in row]
    return [normalize_scalar(row)]


def row_names(row: Any) -> list[str]:
    if isinstance(row, dict):
        return list(row.keys())
    return ["<scalar>"]


def has_order_by(cypher: str) -> bool:
    return "ORDER BY" in cypher.upper()


def sort_list_columns(values: list[Any], positions: frozenset[int]) -> list[Any]:
    """*values* with the lists at *positions* put in a canonical element order."""
    return [
        sorted(v, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        if i in positions and isinstance(v, list)
        else v
        for i, v in enumerate(values)
    ]


def _sort_key(values: list[Any]) -> str:
    """Deterministic key for comparing unordered results as multisets."""
    return json.dumps(values, sort_keys=True, default=str)


def result_difference(
    expected_rows: list[Any],
    actual_rows: list[Any],
    *,
    ordered: bool,
    label: str = "<query>",
    cypher: str = "",
    unordered_positions: frozenset[int] = frozenset(),
) -> str | None:
    """The first difference between two result sets, or ``None`` if equivalent.

    - Column **count** must match (names need not — see :func:`row_values`).
    - Row count must match exactly.
    - *ordered* compares rows position-wise; otherwise as multisets.
    - Columns at *unordered_positions* hold lists whose element order is
      undefined (Cypher ``collect()``); their elements compare as multisets.
    """
    exp_vals = [sort_list_columns(row_values(r), unordered_positions) for r in expected_rows]
    act_vals = [sort_list_columns(row_values(r), unordered_positions) for r in actual_rows]
    exp_names = row_names(expected_rows[0]) if expected_rows else []
    act_names = row_names(actual_rows[0]) if actual_rows else []

    exp_widths = {len(r) for r in exp_vals}
    act_widths = {len(r) for r in act_vals}
    if exp_widths != act_widths:
        return (
            f"[{label}] column-count mismatch: neo4j widths={sorted(exp_widths)} "
            f"vs aql widths={sorted(act_widths)}\n"
            f"  neo4j columns: {exp_names}\n"
            f"  aql columns:   {act_names}"
        )
    if len(exp_vals) != len(act_vals):
        return (
            f"[{label}] row-count mismatch: neo4j={len(exp_vals)} vs aql={len(act_vals)}\n"
            f"  cypher: {cypher!r}\n"
            f"  neo4j columns: {exp_names}; sample: {exp_vals[:3]!r}\n"
            f"  aql   columns: {act_names}; sample: {act_vals[:3]!r}"
        )
    if ordered:
        pairs = zip(exp_vals, act_vals, strict=True)
        kind = "ordered"
    else:
        pairs = zip(sorted(exp_vals, key=_sort_key), sorted(act_vals, key=_sort_key), strict=True)
        kind = "unordered"
    for i, (a, b) in enumerate(pairs):
        if a != b:
            return (
                f"[{label}] row #{i} differs ({kind}, position-wise):\n"
                f"  neo4j ({exp_names}): {a!r}\n"
                f"  aql   ({act_names}): {b!r}"
            )
    return None


def assert_result_equivalent(
    neo4j_rows: list[dict[str, Any]],
    aql_rows: list[dict[str, Any]],
    query: dict[str, Any],
) -> None:
    """Assert two result sets are equivalent; ordered iff the Cypher has ``ORDER BY``."""
    cypher = query.get("cypher", "")
    diff = result_difference(
        neo4j_rows,
        aql_rows,
        ordered=has_order_by(cypher),
        label=query.get("id", "<unknown>"),
        cypher=cypher,
    )
    assert diff is None, diff
