"""Null semantics of Cypher aggregates lowered to AQL ``COLLECT … AGGREGATE``.

Cypher aggregates skip nulls; several AQL aggregates do not. On ArangoDB 3.11:

- ``COUNT(x)`` and ``COUNT_DISTINCT(x)`` count null values, so a null-extended
  OPTIONAL MATCH row or a missing property is counted;
- ``SUM`` over no rows is null, where Cypher's ``sum()`` is 0;
- ``AVG``, ``MIN`` and ``MAX`` already ignore nulls.

``count(*)`` counts rows, nulls included. It is ``SUM(1)``, not ``COUNT(1)``:
after a ``COLLECT`` with no group keys that counts, ArangoDB (3.11.14, 3.12.12)
evaluates a later uncorrelated subquery to null (arangodb/arangodb#23553), so
``WITH count(*) AS c RETURN c, [x IN range(1, 3) | x]`` lost its list and a
``MATCH`` after it lost its rows. ``SUM`` is not affected.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser
from .naming import _affix_name

_NULL_COUNTING = re.compile(r"(COUNT|COUNT_DISTINCT|SUM)\((.*)\)", re.DOTALL)


def lower_aggregate(aql: str, agg_expr: str, reserve: Callable[[str], str]) -> tuple[list[str], str | None]:
    """Lower one compiled Cypher aggregate for a ``COLLECT … AGGREGATE`` clause.

    Returns the ``name = expr`` assignments for the AGGREGATE clause and, when
    they bind helper variables instead of *aql*, the expression a post-COLLECT
    ``LET {aql} = …`` must evaluate. *reserve* returns a fresh variable name and
    marks it used.

    - ``count(*)`` sums ones, ``SUM(1)`` (the module docstring says why not
      ``COUNT(1)``), falling back to 0 over no rows;
    - ``count(x)`` sums non-null indicators: ``SUM(x == null ? 0 : 1)``;
    - ``count(DISTINCT x)`` subtracts the one distinct null that
      ``COUNT_DISTINCT`` counted, if any value was null;
    - ``sum(x)`` falls back to 0 over no rows.
    """
    expr = agg_expr.strip()
    m = _NULL_COUNTING.fullmatch(expr)
    if expr == "COUNT(1)":
        rows = reserve(_affix_name(aql, prefix="_", suffix="_rows"))
        return [f"{rows} = SUM(1)"], f"NOT_NULL({rows}, 0)"
    if m is None:
        return [f"{aql} = {expr}"], None
    fn, inner = m.group(1), m.group(2).strip()
    if fn == "COUNT":
        nonnull = reserve(_affix_name(aql, prefix="_", suffix="_nonnull"))
        return [f"{nonnull} = SUM(({inner}) == null ? 0 : 1)"], f"NOT_NULL({nonnull}, 0)"
    if fn == "SUM":
        total = reserve(_affix_name(aql, prefix="_", suffix="_sum"))
        return [f"{total} = SUM({inner})"], f"NOT_NULL({total}, 0)"
    distinct = reserve(_affix_name(aql, prefix="_", suffix="_distinct"))
    has_null = reserve(_affix_name(aql, prefix="_", suffix="_has_null"))
    return (
        [f"{distinct} = COUNT_DISTINCT({inner})", f"{has_null} = MAX(({inner}) == null ? 1 : 0)"],
        f"{distinct} - NOT_NULL({has_null}, 0)",
    )


#: Cypher aggregate functions the translator lowers (see ``_compile_agg_expr``
#: and ``_parse_collect`` in ``core``).
AGGREGATE_FUNCTIONS = frozenset(
    {"count", "sum", "avg", "min", "max", "collect", "percentiledisc", "percentilecont"}
)

# While a projection that nests aggregates compiles, each aggregate call's
# parse-tree node (by ``id``) maps to the variable the COLLECT binds it to.
_AGGREGATE_VARS: ContextVar[dict[int, str] | None] = ContextVar("_AGGREGATE_VARS", default=None)


def _is_aggregate_call(node: Any) -> bool:
    if isinstance(node, CypherParser.OC_FunctionInvocationContext):
        return node.oC_FunctionName().getText().strip().lower() in AGGREGATE_FUNCTIONS
    return isinstance(node, CypherParser.OC_AtomContext) and node.COUNT() is not None


def nested_aggregates(expr_ctx: Any) -> list[Any]:
    """The aggregate calls inside *expr_ctx*, in source order.

    ``count(*)`` is an atom of its own in the grammar; every other aggregate
    is a function invocation. A subquery (``EXISTS {}``, ``COUNT {}``) is
    its own scope and is not searched, nor is an aggregate's argument.

    Raises ``UNSUPPORTED`` for an aggregate where a comprehension, quantifier
    or ``reduce()`` binds its own variable (``[x IN xs | count(*)]``): Cypher
    rejects that as InvalidAggregation. The list such a construct iterates
    over may aggregate (``[x IN collect(n.name) | toUpper(x)]``).
    """
    found: list[Any] = []

    def walk(node: Any, local: bool) -> None:
        if _is_aggregate_call(node):
            if local:
                raise CoreError(
                    f"Aggregation is not allowed inside a construct that binds its own variable: {node.getText()}",
                    code="UNSUPPORTED",
                )
            found.append(node)
            return
        if isinstance(node, (CypherParser.OC_ExistsSubqueryContext, CypherParser.OC_CountSubqueryContext)):
            return
        if isinstance(node, CypherParser.OC_PatternComprehensionContext):
            local = True
        if isinstance(node, CypherParser.OC_IdInCollContext):
            walk(node.oC_Expression(), local)  # the iterated list, outside the variable's scope
            return
        if isinstance(node, CypherParser.OC_ListComprehensionContext):
            walk(node.oC_FilterExpression(), local)
            if node.oC_Expression() is not None:
                walk(node.oC_Expression(), True)
            return
        if isinstance(node, CypherParser.OC_ReduceExpressionContext):
            init, body = node.oC_Expression()
            walk(init, local)
            walk(node.oC_IdInColl(), local)
            walk(body, True)
            return
        if isinstance(node, CypherParser.OC_FilterExpressionContext):
            walk(node.oC_IdInColl(), local)
            if node.oC_Where() is not None:
                walk(node.oC_Where(), True)
            return
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i), local)

    walk(expr_ctx, False)
    return found


@contextmanager
def aggregate_variables(variables: dict[int, str]) -> Iterator[None]:
    """Compile with each aggregate node in *variables* standing for its variable."""
    token = _AGGREGATE_VARS.set(variables)
    try:
        yield
    finally:
        _AGGREGATE_VARS.reset(token)


def aggregate_variable(node: Any) -> str | None:
    """The COLLECT variable that stands for aggregate *node*, if one does."""
    variables = _AGGREGATE_VARS.get()
    return variables.get(id(node)) if variables else None


def aggregate_key(text: str) -> str:
    """How an aggregate's Cypher text is matched: without whitespace."""
    return "".join(text.split())


def sort_aggregates(expr_ctx: Any, aggregates: dict[str, str] | None) -> dict[int, str]:
    """The aggregate calls in an ORDER BY item, each standing for the variable
    of the projection item that computes it (``RETURN max(x) AS m ORDER BY
    max(x) + 1``). An aggregate the projection does not compute is refused:
    its group would need an aggregate of its own.
    """
    variables: dict[int, str] = {}
    for node in nested_aggregates(expr_ctx):
        variable = (aggregates or {}).get(aggregate_key(node.getText()))
        if variable is None:
            raise CoreError(
                f"ORDER BY {node.getText()}: an aggregate the projection does not compute is not supported",
                code="UNSUPPORTED",
            )
        variables[id(node)] = variable
    return variables
