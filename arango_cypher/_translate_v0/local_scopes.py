"""Variables local to an expression: list comprehensions, quantifiers, ``reduce()``.

Two AQL facts shape their lowering:

* AQL keeps a ``FOR`` variable in scope to the end of the enclosing loop, while
  Cypher drops it at the next ``WITH``.  ``UNWIND l AS x WITH l … RETURN
  any(x IN l WHERE …)`` is valid Cypher, but an inner ``FOR x`` in AQL
  redeclares ``x`` (ERR 1511).  A local variable whose name is bound anywhere
  else in the query therefore gets a fresh AQL name.
* AQL's ``FILTER`` drops null and false alike, so a count-subquery quantifier
  answers false where Cypher answers null (``all(x IN [1, null] WHERE x > 0)``
  is null).  Quantifiers count true, false and null predicate values instead.
"""

from __future__ import annotations

from typing import Any

from .._antlr.CypherParser import CypherParser
from .naming import _rewrite_vars


def _root(ctx: Any) -> Any:
    while getattr(ctx, "parentCtx", None) is not None:
        ctx = ctx.parentCtx
    return ctx


def _variable_names(node: Any, *, skip: Any = None) -> set[str]:
    names: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur is skip:
            continue
        if isinstance(cur, CypherParser.OC_VariableContext):
            names.add(cur.getText().strip())
            continue
        stack.extend(getattr(cur, "children", None) or [])
    return names


def local_aql_name(name: str, owner: Any) -> str:
    """The AQL name for local variable *name* bound by *owner*.

    *name* itself unless it also occurs outside *owner* — then a fresh,
    deterministic name (``x_l<token index>``) that occurs nowhere in the query.
    """
    root = _root(owner)
    if name not in _variable_names(root, skip=owner):
        return name
    taken = _variable_names(root)
    candidate = f"{name}_l{owner.start.tokenIndex}"
    while candidate in taken:
        candidate += "_"
    return candidate


def rename_local(text: str, name: str, aql_name: str) -> str:
    """*text* (AQL compiled from the local scope) with *name* read as *aql_name*."""
    return text if name == aql_name else _rewrite_vars(text, {name: aql_name})


def quantifier_aql(kind: str, *, owner: Any, var: str, list_aql: str, predicate: str) -> str:
    """Cypher's three-valued ``<kind>(var IN list WHERE predicate)``.

    *predicate* is the predicate compiled as a value (true / false / null).
    A null list gives null.
    """
    n = owner.start.tokenIndex
    lst, results = f"q{n}_list", f"q{n}_values"
    trues = f"LENGTH({results}[* FILTER CURRENT == true])"
    falses = f"LENGTH({results}[* FILTER CURRENT == false])"
    nulls = f"LENGTH({results}[* FILTER CURRENT == null])"
    if kind == "any":
        verdict = f"({trues} > 0 ? true : ({nulls} > 0 ? null : false))"
    elif kind == "all":
        verdict = f"({falses} > 0 ? false : ({nulls} > 0 ? null : true))"
    elif kind == "none":
        verdict = f"({trues} > 0 ? false : ({nulls} > 0 ? null : true))"
    elif kind == "single":
        verdict = f"({trues} > 1 ? false : ({nulls} > 0 ? null : {trues} == 1))"
    else:
        raise ValueError(f"not a quantifier: {kind}")
    return (
        f"FIRST(FOR {lst} IN [{list_aql}] "
        f"LET {results} = (FOR {var} IN ({lst} == null ? [] : {lst}) RETURN ({predicate})) "
        f"RETURN ({lst} == null ? null : {verdict}))"
    )
