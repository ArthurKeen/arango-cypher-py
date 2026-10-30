"""AmbiguousAggregationExpression: what an aggregating expression may mention.

In a ``WITH``/``RETURN`` that aggregates, the items without an aggregate are
the grouping keys. An item that mixes an aggregate with other parts
(``me.age + count(*)``) is evaluated once per group, so every part of it
outside the aggregate calls must have one value per group. Neo4j accepts:

- literals and parameters;
- a grouping key that is a variable (``WITH me, me.age + count(*)``) — and,
  here, a property of one, whose value is fixed by the variable;
- a grouping key that is a property access (``RETURN me.age, me.age +
  count(you.age)``, Return6 [19]; with an alias too, With6 [7]).

It rejects anything else (Return6 [20]: ``me.age`` is not a key) and a
compound key reused inside the aggregating item (Return6 [21]: ``me.age +
you.age`` is a key, but is not simple). The same holds for an ``ORDER BY``
item that aggregates after an aggregating projection, which may also name
the projection's columns (ReturnOrderBy6 [2]); a variable no key mentions is
not in scope there at all (``UndefinedVariable``, ReturnOrderBy6 [4]).

An aggregating item that binds variables of its own outside its aggregates
(a comprehension, quantifier, ``reduce()``, pattern, subquery) is not
checked: telling its local variables from outer ones is not worth a false
rejection.
"""

from __future__ import annotations

from antlr4 import ParserRuleContext

from .._antlr.CypherParser import CypherParser
from .aggregates import AGGREGATE_FUNCTIONS
from .naming import _strip_label_backticks
from .semantic_errors import semantic_error
from .semantic_expr import bare_variable, sole_operand, variable_name

_P = CypherParser

#: Every Cypher aggregate, including ones the translator does not lower:
#: missing one would make its item look like a grouping key.
_AGGREGATES = AGGREGATE_FUNCTIONS | {"stdev", "stdevp", "percentilecont", "percentiledisc"}

#: Constructs that bind variables of their own.
_SCOPING = (
    _P.OC_ListComprehensionContext,
    _P.OC_PatternComprehensionContext,
    _P.OC_ReduceExpressionContext,
    _P.OC_FilterExpressionContext,
    _P.OC_RelationshipsPatternContext,
    _P.OC_ExistsSubqueryContext,
    _P.OC_CountSubqueryContext,
)


class _Unchecked(Exception):
    """The expression binds variables of its own: leave it alone."""


def check_aggregation(
    items: list[CypherParser.OC_ProjectionItemContext],
    order: CypherParser.OC_OrderContext | None,
    *,
    columns: list[str],
    star_keys: list[str],
    keyword: str,
) -> None:
    """Raise ``AmbiguousAggregationExpression`` for a projection Neo4j rejects.

    *columns* are the projection's column names (visible to ``ORDER BY``);
    *star_keys* the variables a ``*`` projects, each a grouping key.
    """
    aggregating = [item for item in items if contains_aggregate(item.oC_Expression())]
    if not aggregating:
        return
    keys = [item.oC_Expression() for item in items if item not in aggregating]
    simple = set(star_keys)
    for key in keys:
        text = _simple_key(key)
        if text is not None:
            simple.add(text)
    mentioned = {name for key in keys for name in _variables(key)} | set(star_keys)
    for item in aggregating:
        _check(item.oC_Expression(), simple, set(), mentioned, clause=keyword, in_order=False)
    if order is None:
        return
    for sort_item in order.oC_SortItem():
        expr = sort_item.oC_Expression()
        if contains_aggregate(expr):
            _check(expr, simple, set(columns), mentioned, clause=f"{keyword} ... ORDER BY", in_order=True)


def contains_aggregate(ctx: ParserRuleContext) -> bool:
    """Whether *ctx* calls an aggregate outside any subquery.

    Iterative: a literal nested 40 deep is several hundred contexts deep.
    """
    stack = [ctx]
    while stack:
        node = stack.pop()
        if _is_aggregate(node):
            return True
        if isinstance(node, (_P.OC_ExistsSubqueryContext, _P.OC_CountSubqueryContext)):
            continue
        stack.extend(c for c in node.getChildren() if isinstance(c, ParserRuleContext))
    return False


def _is_aggregate(ctx: ParserRuleContext) -> bool:
    if isinstance(ctx, _P.OC_FunctionInvocationContext):
        return ctx.oC_FunctionName().getText().strip().lower() in _AGGREGATES
    return isinstance(ctx, _P.OC_AtomContext) and ctx.COUNT() is not None  # count(*)


def _check(
    expr: ParserRuleContext,
    simple: set[str],
    columns: set[str],
    mentioned: set[str],
    *,
    clause: str,
    in_order: bool,
) -> None:
    try:
        offending = _first_non_key(expr, simple, columns)
    except _Unchecked:
        return
    if offending is None:
        return
    variable, text = offending
    shown = " ".join(expr.getText().split())
    if in_order and variable not in mentioned:
        raise semantic_error(
            "UndefinedVariable",
            f"Variable `{variable}` not defined: after an aggregating {clause.split()[0]} only its "
            f"grouping keys and columns are in scope (`{shown}`)",
        )
    raise semantic_error(
        "AmbiguousAggregationExpression",
        f"`{text}` in `{shown}` ({clause}) is neither a grouping key nor inside an aggregate; an "
        "expression that aggregates may only mention grouping keys that are variables or property accesses",
    )


def _first_non_key(node: ParserRuleContext, simple: set[str], columns: set[str]) -> tuple[str, str] | None:
    """The first (variable, reference) outside an aggregate that is not a key."""
    if _is_aggregate(node):
        return None
    if isinstance(node, _SCOPING):
        raise _Unchecked
    if isinstance(node, _P.OC_PropertyOrLabelsExpressionContext):
        variable = node.oC_Atom().oC_Variable()
        if variable is not None:
            name = variable_name(variable)
            chain = _chain(name, node)
            if name in simple or name in columns or any(prefix in simple for prefix in chain):
                return None
            return name, chain[-1] if chain else name
    for child in node.getChildren():
        if isinstance(child, ParserRuleContext):
            found = _first_non_key(child, simple, columns)
            if found is not None:
                return found
    return None


def _chain(name: str, node: CypherParser.OC_PropertyOrLabelsExpressionContext) -> list[str]:
    """``me.a.b`` -> ``["me.a", "me.a.b"]``: each property access on the variable."""
    prefixes: list[str] = []
    text = name
    for lookup in node.oC_PropertyLookup():
        text = f"{text}.{_key(lookup)}"
        prefixes.append(text)
    return prefixes


def _key(lookup: CypherParser.OC_PropertyLookupContext) -> str:
    return _strip_label_backticks(lookup.oC_PropertyKeyName().getText().strip())


def _simple_key(expr: ParserRuleContext) -> str | None:
    """A grouping key that is a variable or a property access on one, as text."""
    name = bare_variable(expr)
    if name is not None:
        return name
    node = sole_operand(expr, _P.OC_PropertyOrLabelsExpressionContext)
    if not isinstance(node, _P.OC_PropertyOrLabelsExpressionContext):
        return None
    variable = node.oC_Atom().oC_Variable()
    if variable is None or node.oC_NodeLabels() is not None or not node.oC_PropertyLookup():
        return None
    return _chain(variable_name(variable), node)[-1]


def _variables(expr: ParserRuleContext) -> set[str]:
    """Every variable *expr* reads."""
    found: set[str] = set()
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, _P.OC_AtomContext) and node.oC_Variable() is not None:
            found.add(variable_name(node.oC_Variable()))
            continue
        stack.extend(c for c in node.getChildren() if isinstance(c, ParserRuleContext))
    return found
