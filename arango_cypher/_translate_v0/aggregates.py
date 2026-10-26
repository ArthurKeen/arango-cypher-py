"""Null semantics of Cypher aggregates lowered to AQL ``COLLECT … AGGREGATE``.

Cypher aggregates skip nulls; several AQL aggregates do not. On ArangoDB 3.11:

- ``COUNT(x)`` and ``COUNT_DISTINCT(x)`` count null values, so a null-extended
  OPTIONAL MATCH row or a missing property is counted;
- ``SUM`` over no rows is null, where Cypher's ``sum()`` is 0;
- ``AVG``, ``MIN`` and ``MAX`` already ignore nulls.

``count(*)`` (``COUNT(1)``) counts rows, nulls included, and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_NULL_COUNTING = re.compile(r"(COUNT|COUNT_DISTINCT|SUM)\((.*)\)", re.DOTALL)


def lower_aggregate(aql: str, agg_expr: str, reserve: Callable[[str], str]) -> tuple[list[str], str | None]:
    """Lower one compiled Cypher aggregate for a ``COLLECT … AGGREGATE`` clause.

    Returns the ``name = expr`` assignments for the AGGREGATE clause and, when
    they bind helper variables instead of *aql*, the expression a post-COLLECT
    ``LET {aql} = …`` must evaluate. *reserve* returns a fresh variable name and
    marks it used.

    - ``count(x)`` sums non-null indicators: ``SUM(x == null ? 0 : 1)``;
    - ``count(DISTINCT x)`` subtracts the one distinct null that
      ``COUNT_DISTINCT`` counted, if any value was null;
    - ``sum(x)`` falls back to 0 over no rows.
    """
    expr = agg_expr.strip()
    m = _NULL_COUNTING.fullmatch(expr)
    if m is None or expr == "COUNT(1)":
        return [f"{aql} = {expr}"], None
    fn, inner = m.group(1), m.group(2).strip()
    if fn == "COUNT":
        nonnull = reserve(f"_{aql}_nonnull")
        return [f"{nonnull} = SUM(({inner}) == null ? 0 : 1)"], f"NOT_NULL({nonnull}, 0)"
    if fn == "SUM":
        total = reserve(f"_{aql}_sum")
        return [f"{total} = SUM({inner})"], f"NOT_NULL({total}, 0)"
    distinct = reserve(f"_{aql}_distinct")
    has_null = reserve(f"_{aql}_has_null")
    return (
        [f"{distinct} = COUNT_DISTINCT({inner})", f"{has_null} = MAX(({inner}) == null ? 1 : 0)"],
        f"{distinct} - NOT_NULL({has_null}, 0)",
    )
