"""``DISTINCT`` that keeps the order of the rows it reads.

``… ORDER BY numberOfMembers DESC WITH DISTINCT forum LIMIT 100`` keeps the
first 100 forums in member order (Neo4j keeps each value's first row, in
order). AQL's ``COLLECT forum = forum_1`` sorts its groups by the group
value, so the ``LIMIT`` kept the 100 lowest forums instead (LDBC BI4).

When a ``SORT`` still orders the rows reaching the DISTINCT, each group keeps
its sort keys, the first under that order is chosen, and the groups are sorted
by it again::

    SORT k1 DESC, k2
    COLLECT forum = forum_1 INTO _distinct_rows = [k1, k2]
    LET _distinct_first = FIRST(FOR _d IN _distinct_rows SORT _d[0] DESC, _d[1] ASC RETURN _d)
    SORT _distinct_first[0] DESC, _distinct_first[1] ASC

With no ``SORT`` in force the order is unspecified, and the plain ``COLLECT``
is kept.

AQL's ``RETURN DISTINCT`` keeps the first of each value in order, but a
``LIMIT`` before it limits the rows *before* they are made distinct
(``RETURN DISTINCT a, b LIMIT 2`` could return one row). So a sliced
``RETURN DISTINCT`` reads the distinct rows of a subquery
(:func:`append_distinct_return`).
"""

from __future__ import annotations

from collections.abc import Callable

from .naming import _declared_aql_vars, _pick_fresh_var
from .three_valued import _unquoted_chars

#: Clauses that keep the order of the rows they pass on.
_ORDER_KEEPING = ("LET ", "FILTER ", "LIMIT ")


def live_sort_terms(lines: list[str]) -> list[tuple[str, str]] | None:
    """The ``(expression, direction)`` terms of the SORT that orders the rows
    at the end of *lines*, or ``None`` when no SORT does.

    Conservative: anything but LET, FILTER or LIMIT after the SORT (a FOR,
    a COLLECT, a multi-line subquery) means the order is not known.
    """
    for entry in reversed(lines):
        for line in reversed(entry.split("\n")):
            stripped = line.strip()
            if stripped.startswith("SORT "):
                return _sort_terms(stripped[len("SORT ") :])
            if not stripped.startswith(_ORDER_KEEPING):
                return None
    return None


def _sort_terms(body: str) -> list[tuple[str, str]]:
    terms: list[str] = []
    start = 0
    for i, ch, depth in _unquoted_chars(body):
        if ch == "," and depth == 0:
            terms.append(body[start:i])
            start = i + 1
    terms.append(body[start:])
    out: list[tuple[str, str]] = []
    for term in (t.strip() for t in terms):
        for direction in ("DESC", "ASC"):
            if term.upper().endswith(" " + direction):
                out.append((term[: -len(direction)].rstrip(), direction))
                break
        else:
            out.append((term, "ASC"))
    return out


def order_keeping_collect(
    collect_parts: str, terms: list[tuple[str, str]], *, forbidden_vars: set[str]
) -> tuple[list[str], list[str]]:
    """The COLLECT that groups the DISTINCT keys (and the first row's sort
    keys), and the SORT lines that restore the order after it."""
    rows = _pick_fresh_var("_distinct_rows", forbidden_vars=forbidden_vars)
    first = _pick_fresh_var("_distinct_first", forbidden_vars=forbidden_vars)
    row = _pick_fresh_var("_d", forbidden_vars=forbidden_vars)
    keys = ", ".join(expr for expr, _ in terms)
    inner = ", ".join(f"{row}[{i}] {direction}" for i, (_, direction) in enumerate(terms))
    outer = ", ".join(f"{first}[{i}] {direction}" for i, (_, direction) in enumerate(terms))
    collect = [
        f"  COLLECT {collect_parts} INTO {rows} = [{keys}]",
        f"  LET {first} = FIRST(FOR {row} IN {rows} SORT {inner} RETURN {row})",
    ]
    return collect, [f"  SORT {outer}"]


def append_distinct_return(
    lines: list[str],
    projection: str,
    *,
    skip_value: str | None,
    limit_value: str | None,
    append_skip_limit: Callable[[list[str], str | None, str | None], None],
) -> None:
    """``RETURN DISTINCT projection``, with SKIP/LIMIT applied to the distinct rows."""
    if skip_value is None and limit_value is None:
        lines.append(f"  RETURN DISTINCT {projection}")
        return
    row = _pick_fresh_var("_distinct_row", forbidden_vars=_declared_aql_vars(lines))
    body = "\n".join([*lines, f"  RETURN DISTINCT {projection}"])
    lines[:] = [f"FOR {row} IN (\n{body}\n)"]
    append_skip_limit(lines, skip_value, limit_value)
    lines.append(f"  RETURN {row}")
