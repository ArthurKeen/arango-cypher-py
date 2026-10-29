"""``CALL { … }`` subqueries (LDBC plan WP-42).

For every incoming row the subquery runs once, and the row continues once per
row the subquery returns, with the subquery's columns added; a row whose
subquery returns nothing is dropped, as in a MATCH. An AQL subquery that
references the outer variables does exactly that::

    MATCH (p:Person)
    CALL { WITH p  MATCH (p)-[:KNOWS]->(f) RETURN count(f) AS friends }
    RETURN p.id, friends

    FOR p IN @@collection
      FOR _call IN (FOR … RETURN {friends: …})
        LET friends = _call["friends"]
        RETURN {id: p.id, friends: friends}

* **Imports.** A leading ``WITH a, b`` imports outer variables: the body
  compiles with them already bound to the outer AQL names. Only plain
  variables can be imported (Cypher's rule). A body without one is
  uncorrelated.
* **Columns.** Every ``RETURN`` item is a variable or aliased (Cypher's rule),
  and must not rebind a variable already in scope. The body returns its rows
  as objects (``_object_rows``, even for one column) and each column is read
  by name.
* **UNION / UNION ALL** of bodies compile to ``UNION_DISTINCT()`` / ``UNION()``
  of the branches, which must return the same columns.
* **Names.** AQL refuses a subquery that declares a name already in scope
  (ERR 1511), so body names that clash with outer ones are renamed. The body's
  bind variables are merged as a UNION branch's are (``_merge_bind_vars``).

Writes inside the body and ``CALL { … } IN TRANSACTIONS`` are refused.
"""

from __future__ import annotations

import json
import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .bind_names import _merge_bind_vars
from .naming import _declared_aql_vars, _pick_fresh_var, _rewrite_vars, _strip_label_backticks
from .state import _active_user_params, _active_user_vars, _object_rows

_BARE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|`[^`]+`")


def _core(name: str) -> Any:
    from . import core

    return getattr(core, name)


def subquery_call(rc: CypherParser.OC_ReadingClauseContext) -> CypherParser.OC_SubqueryCallContext | None:
    """The ``CALL { … }`` of a reading clause, or ``None``."""
    return rc.oC_SubqueryCall()


def has_subquery_call(reading: list[CypherParser.OC_ReadingClauseContext]) -> bool:
    return any(subquery_call(rc) is not None for rc in reading)


def refuse_unsupported_subquery_calls(tree: Any) -> None:
    """Refuse a ``CALL { … }`` where only the read pipeline would see it.

    The write translators and ``EXISTS { … }`` / ``COUNT { … }`` walk their
    reading clauses for MATCH and UNWIND; a subquery call among them would be
    skipped, and the query would run without it.
    """

    def walk(node: Any, in_subquery_expression: bool) -> None:
        if isinstance(node, CypherParser.OC_SubqueryBodyContext):
            in_subquery_expression = True
        if isinstance(node, CypherParser.OC_SubqueryCallContext):
            if in_subquery_expression:
                raise CoreError(
                    "CALL { … } inside EXISTS { … } or COUNT { … } is not supported", code="NOT_IMPLEMENTED"
                )
            if _query_writes(node):
                raise CoreError("CALL { … } in a query that writes is not supported", code="NOT_IMPLEMENTED")
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i), in_subquery_expression)

    walk(tree, False)


def _query_writes(call: CypherParser.OC_SubqueryCallContext) -> bool:
    """Whether the query the ``CALL`` stands in (not its body) has a write clause."""
    node = call.parentCtx
    while node is not None and not isinstance(node, CypherParser.OC_SingleQueryContext):
        node = node.parentCtx
    if node is None:
        return False
    parts = [node.oC_SinglePartQuery()] if node.oC_SinglePartQuery() is not None else []
    mpq = node.oC_MultiPartQuery()
    if mpq is not None:
        parts.append(mpq.oC_SinglePartQuery())
        if mpq.oC_UpdatingClause():
            return True
    return any(p is not None and p.oC_UpdatingClause() for p in parts)


def _branches(
    call: CypherParser.OC_SubqueryCallContext,
) -> tuple[list[CypherParser.OC_SingleQueryContext], bool]:
    """The bodies of a ``CALL``, and whether their union keeps duplicates."""
    regular = call.oC_RegularQuery()
    unions = regular.oC_Union() or []
    return [regular.oC_SingleQuery(), *(u.oC_SingleQuery() for u in unions)], all(
        u.ALL() is not None for u in unions
    )


def _importing_with(branch: CypherParser.OC_SingleQueryContext) -> CypherParser.OC_WithContext | None:
    """The body's leading ``WITH`` (its imports), when nothing precedes it."""
    mpq = branch.oC_MultiPartQuery()
    if mpq is None:
        return None
    first = next(
        (
            c
            for c in mpq.getChildren()
            if isinstance(c, (CypherParser.OC_ReadingClauseContext, CypherParser.OC_WithContext))
        ),
        None,
    )
    return first if isinstance(first, CypherParser.OC_WithContext) else None


def _imports(with_ctx: CypherParser.OC_WithContext, var_env: dict[str, str]) -> dict[str, str]:
    """Imported Cypher variable → outer AQL name, for ``WITH a, b``."""
    body = with_ctx.oC_ProjectionBody()
    simple = (
        with_ctx.oC_Where() is None
        and body.DISTINCT() is None
        and body.oC_Order() is None
        and body.oC_Skip() is None
        and body.oC_Limit() is None
    )
    items_ctx = body.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem() or []
    if not simple or items_ctx.getText().startswith("*"):
        raise CoreError(
            "The importing WITH of a CALL subquery may only list variables (WITH a, b)",
            code="NOT_IMPLEMENTED",
        )
    imports: dict[str, str] = {}
    for item in items:
        name = item.oC_Expression().getText().strip()
        alias = item.oC_Variable()
        if not _BARE_VARIABLE.fullmatch(name) or (alias is not None and alias.getText().strip() != name):
            raise CoreError(
                f"The importing WITH of a CALL subquery may only list variables, not {name!r}",
                code="UNSUPPORTED",
            )
        if name not in var_env:
            raise CoreError(f"Variable `{_strip_label_backticks(name)}` not defined", code="UNSUPPORTED")
        imports[name] = var_env[name]
    return imports


def _columns(branch: CypherParser.OC_SingleQueryContext) -> list[str]:
    """The body's column names."""
    spq = branch.oC_SinglePartQuery()
    if spq is None:
        spq = branch.oC_MultiPartQuery().oC_SinglePartQuery()
    if spq.oC_UpdatingClause():
        raise CoreError("Writes inside a CALL subquery are not supported", code="NOT_IMPLEMENTED")
    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("A CALL subquery must end with RETURN", code="UNSUPPORTED")
    items_ctx = ret.oC_ProjectionBody().oC_ProjectionItems()
    if items_ctx.getText().startswith("*"):
        raise CoreError("RETURN * in a CALL subquery is not supported", code="NOT_IMPLEMENTED")
    items = items_ctx.oC_ProjectionItem() or []
    names: list[str] = []
    for item in items:
        alias = item.oC_Variable()
        name = alias.getText().strip() if alias is not None else item.oC_Expression().getText().strip()
        if alias is None and not _BARE_VARIABLE.fullmatch(name):
            raise CoreError(
                f"Expression in CALL {{ RETURN … }} must be aliased (use AS): {name}", code="UNSUPPORTED"
            )
        names.append(name)
    return names


def _compile_branch(
    branch: CypherParser.OC_SingleQueryContext,
    *,
    var_env: dict[str, str],
    outer_names: set[str],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """One body as AQL text with its own bind variables, its names clear of the outer ones."""
    # Seeded with the user's parameters, as a UNION branch's are, so names the
    # body picks for itself avoid them.
    branch_bv = {k: bind_vars[k] for k in _active_user_params.get() if k in bind_vars}
    importing = _importing_with(branch)
    user_vars = _active_user_vars.get()
    rows_token = _object_rows.set(True)  # read every column by name, even a single one
    try:
        if importing is not None:
            imports = _imports(importing, var_env)
            query = _core("_translate_multi_part_query")(
                branch.oC_MultiPartQuery(),
                resolver=resolver,
                bind_vars=branch_bv,
                imports=imports,
                outer_names=frozenset(outer_names),
            )
        else:
            imports = {}
            query = _core("_translate_single_query")(branch, resolver=resolver, bind_vars=branch_bv)
    finally:
        _object_rows.reset(rows_token)
        _active_user_vars.set(user_vars)
    clashes = _declared_aql_vars(query.text.splitlines()) & outer_names
    if clashes & set(imports.values()):
        raise CoreError(
            f"CALL subquery redeclares imported variable(s) {sorted(clashes & set(imports.values()))}",
            code="INTERNAL_ERROR",
        )
    taken = outer_names | _declared_aql_vars(query.text.splitlines())
    renames = {name: _pick_fresh_var(name, forbidden_vars=taken) for name in sorted(clashes)}
    return _rewrite_vars(query.text, renames), query.bind_vars


def compile_subquery_call(
    call: CypherParser.OC_SubqueryCallContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
    forbidden_vars: set[str],
    lines: list[str],
) -> dict[str, str]:
    """Append the lines of a ``CALL { … }`` and return the extended variable environment."""
    branches, keep_duplicates = _branches(call)
    columns: list[str] | None = None
    outer_names = set(forbidden_vars) | _declared_aql_vars(lines) | set(var_env.values())
    sources: list[str] = []
    for index, branch in enumerate(branches):
        names = _columns(branch)
        rebound = [c for c in names if c in var_env]
        if rebound:
            raise CoreError(
                f"Variable `{_strip_label_backticks(rebound[0])}` already declared: a CALL subquery "
                "cannot return a variable that is already in scope",
                code="UNSUPPORTED",
            )
        if columns is None:
            columns = names
        elif names != columns:
            raise CoreError(
                "All subqueries in a UNION must return the same columns "
                f"({', '.join(columns)} vs {', '.join(names)})",
                code="UNSUPPORTED",
            )
        text, branch_bv = _compile_branch(
            branch, var_env=var_env, outer_names=outer_names, resolver=resolver, bind_vars=bind_vars
        )
        text = _merge_bind_vars(
            bind_vars, branch_bv, text, branch_index=index, user_params=_active_user_params.get()
        )
        sources.append(f"(\n{text}\n)")
    assert columns is not None

    if len(sources) == 1:
        source = sources[0]
    else:
        fn = "UNION" if keep_duplicates else "UNION_DISTINCT"
        source = f"{fn}(" + ", ".join(sources) + ")"

    out_env = dict(var_env)
    row_var = _pick_fresh_var("_call", forbidden_vars=forbidden_vars)
    lines.append(f"  FOR {row_var} IN {source}")
    for column in columns:
        aql_var = _pick_fresh_var(column, forbidden_vars=forbidden_vars)
        lines.append(f"    LET {aql_var} = {row_var}[{_attribute(column)}]")
        out_env[column] = aql_var
    user_vars = list(_active_user_vars.get())
    _active_user_vars.set(user_vars + [c for c in columns if c not in user_vars])
    return out_env


def _attribute(column: str) -> str:
    """A column's attribute name as an AQL string literal."""
    return json.dumps(_strip_label_backticks(column))


def translate_single_part_with_calls(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """``MATCH … CALL { … } … RETURN …`` with no ``WITH``: the clauses compile in order."""
    core = _core
    lines: list[str] = []
    forbidden_vars: set[str] = set()
    var_env: dict[str, str] = {}
    rel_type_exprs: dict[str, str] = {}
    bound = False
    for rc in spq.oC_ReadingClause() or []:
        call = subquery_call(rc)
        match = rc.oC_Match()
        unwind = rc.oC_Unwind()
        if call is not None:
            var_env = compile_subquery_call(
                call,
                resolver=resolver,
                bind_vars=bind_vars,
                var_env=var_env,
                forbidden_vars=forbidden_vars,
                lines=lines,
            )
        elif unwind is not None:
            var_env = core("_compile_pipeline_unwind")(
                unwind, bind_vars=bind_vars, forbidden_vars=forbidden_vars, var_env=var_env, lines=lines
            )
        elif match is not None and not bound:
            lines, forbidden_vars, var_env = core("_compile_leading_match")(
                match, resolver=resolver, bind_vars=bind_vars
            )
            core("_set_user_vars_from_parts")(match.oC_Pattern().oC_PatternPart() or [])
        elif match is not None:
            var_env, part_rel_type_exprs = core("_compile_following_match")(
                match,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                var_env=var_env,
                lines=lines,
            )
            rel_type_exprs.update(part_rel_type_exprs)
            core("_set_user_vars_from_parts")(match.oC_Pattern().oC_PatternPart() or [])
        else:
            raise CoreError(
                "A procedure CALL next to a CALL subquery is not supported", code="NOT_IMPLEMENTED"
            )
        bound = True
    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    core("_append_return")(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env=var_env,
        rel_type_exprs=rel_type_exprs,
    )
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)
