"""Cypher's global sort order as an AQL sort key.

Cypher orders values of different types by type first — maps, nodes,
relationships, lists, paths, strings, booleans, numbers, and null last when
ascending — and lists element by element in that same order. AQL's own order
is null < boolean < number < string < array < object, so ``ORDER BY`` over a
value whose type is not known (an ``UNWIND`` variable, a list, a projected
expression) sorted ``[null, 1]`` before ``['a']`` and strings after numbers.

:func:`cypher_sort_key` wraps such a value in ``[rank, value]``: AQL compares
arrays element by element, so the rank orders the types and the value orders
within one. A string keeps AQL's order, as AQL's ``<`` compares an untyped
value (``_code_point_operands``): sorting and comparing must agree (TCK
WithOrderBy1 [45]). A list's elements get the same key, one
level deep (a list nested in a list keeps AQL's order inside it). Graph
elements are told apart by shape: an edge has ``_from``, a node ``_id``, a
path is the translator's ``{nodes, edges}``.

The key is only for a value the query shows to be a list, or an element of a
list of mixed types (the semantic pass records each ORDER BY item's type):
any other sort, a stored property above all (``ORDER BY n.name``, which an
index may serve), keeps the plain key — see :func:`needs_cypher_sort_key`.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

from .semantic_types import LIST, SType

#: ORDER BY sort-item contexts (by ``id``) -> the type the semantic pass
#: shows for them, for the translation running in this context.
SORT_VALUE_TYPES: ContextVar[dict[int, SType] | None] = ContextVar("SORT_VALUE_TYPES", default=None)

_RANK_MAP, _RANK_NODE, _RANK_REL, _RANK_LIST, _RANK_PATH = 0, 1, 2, 3, 4
_RANK_STRING, _RANK_BOOL, _RANK_NUMBER, _RANK_NULL = 5, 6, 7, 8


def record_sort_type(ctx: Any, value_type: SType) -> None:
    """Note the type the semantic pass shows for an ORDER BY sort item."""
    recorded = SORT_VALUE_TYPES.get()
    if recorded is not None:
        recorded[id(ctx)] = value_type


def needs_cypher_sort_key(ctx: Any) -> bool:
    """Whether sorting the sort item *ctx* needs the type-ranked key.

    Only for a value the query shows to be a list, or to be one element of a
    list of mixed types. Everything else — a property, an aggregate, a value
    of one type — keeps the plain key: AQL orders one type as Cypher does
    (strings get the code-point key elsewhere), and a plain key on a property
    lets an index serve the sort.
    """
    value_type = (SORT_VALUE_TYPES.get() or {}).get(id(ctx))
    return value_type is not None and (value_type.kind == LIST or value_type.mixed)


#: AQL's TYPENAME() of a value that is not an object -> its Cypher rank.
_RANK_OF_TYPENAME = (
    f'{{"null": {_RANK_NULL}, "bool": {_RANK_BOOL}, "number": {_RANK_NUMBER}, '
    f'"string": {_RANK_STRING}, "array": {_RANK_LIST}}}'
)


def _rank(value: str) -> str:
    """The Cypher type rank of *value*; an object's rank comes from its shape."""
    return (
        f"(IS_OBJECT({value}) ? (HAS({value}, '_from') ? {_RANK_REL} : HAS({value}, '_id') ? {_RANK_NODE}"
        f" : (HAS({value}, 'nodes') AND HAS({value}, 'edges')) ? {_RANK_PATH} : {_RANK_MAP})"
        f" : TRANSLATE(TYPENAME({value}), {_RANK_OF_TYPENAME}, {_RANK_MAP}))"
    )


def _within_rank(value: str) -> str:
    """What orders two values of one rank: a node or relationship by its id."""
    return f"((IS_OBJECT({value}) AND HAS({value}, '_id')) ? {value}._id : {value})"


def cypher_sort_key(expr: str) -> str:
    """An AQL expression that sorts *expr* in Cypher's order (module docstring).

    A list's elements are keyed with an inline projection (``[* RETURN …]``),
    not a subquery: AQL runs a subquery before the ternary around it, over a
    value that may not be a list.
    """
    value = f"({expr})"
    elements = f"{value}[* RETURN [{_rank('CURRENT')}, {_within_rank('CURRENT')}]]"
    return f"[{_rank(value)}, (IS_ARRAY({value}) ? {elements} : {_within_rank(value)})]"


def read_grouping_keys(expr: str, grouped: dict[str, str]) -> str:
    """*expr* with each grouping key's source expression read from the key.

    After ``COLLECT name = a.name`` the sort ``a.name`` (or ``a.name + 'C'``)
    must read ``name``: ``a`` is gone. Longer sources are replaced first, and
    only where they stand as a whole operand (not inside ``a.name2`` or
    ``b.a.name``).
    """
    for source in sorted(grouped, key=len, reverse=True):
        pattern = rf"(?<![\w.$@`]){re.escape(source)}(?![\w`])"
        key = grouped[source]
        expr = re.sub(pattern, lambda _match: key, expr)  # noqa: B023 - used at once
    return expr
