"""SET / REMOVE / DELETE inside a MATCH … WITH pipeline.

A query part may write the rows it matched and go on: ``MATCH (n:N) SET
n.num = n.num + 1 WITH n WHERE n.num % 2 = 0 RETURN n.num`` (TCK Set6),
``MATCH (n) WITH n, n.num AS num DELETE n WITH num … RETURN num`` (Delete6).
The pipeline's FOR loops are already in ``lines``; the writes nest inside
them, one ``UPDATE``/``REPLACE``/``REMOVE`` per target, and what follows
reads a written variable as the document after the write (``LET n_new =
NEW``), as Cypher's later clauses see the new values.

AQL refuses to read a collection after writing it in the same query
(ERR 1579), and to write one collection twice. So after a write the query
may filter, project, aggregate and ``UNWIND`` the rows it has — but a later
``MATCH``, ``CALL`` or second write is refused here, with the reason, rather
than handed to ArangoDB.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var
from .property_writes import keep_fields_for

#: What a query may not do after it wrote mid-pipeline, and why.
AFTER_WRITE_REFUSAL = (
    "{what} after SET/REMOVE/DELETE in the same query is not supported: AQL cannot read a "
    "collection after writing it (ERR 1579), nor write one twice; run it as a separate statement"
)


def emit_pipeline_writes(
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list[Any],
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
    rebind_to: set[str] | None = None,
) -> dict[str, str]:
    """Append the writes to *lines*; return the variable environment after them.

    With *rebind_to* (the AQL names in scope, extended in place) each written
    variable is rebound to its document after the write (``LET x_new =
    NEW``), for clauses that follow; without it *var_env* comes back as is.
    """
    from .writes import (
        _compile_expression,
        _infer_var_collections_from_pipeline,
        _property_writes,
        _refuse_repeated_collections,
        _rewrite_vars,
        _stored_write_line,
    )

    if not (set_clauses or delete_clauses or remove_clauses):
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires at least one mutating clause",
            code="UNSUPPORTED",
        )

    var_collections = _infer_var_collections_from_pipeline(lines)
    if not var_collections:
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires a document variable from MATCH",
            code="NOT_IMPLEMENTED",
        )

    def _coll_key_for(target_var: str) -> str:
        aql_var = var_env.get(target_var, target_var)
        key = var_collections.get(aql_var) or var_collections.get(target_var)
        if key is None:
            raise CoreError(
                f"SET/DELETE/REMOVE after WITH targets {target_var!r}, which is not a "
                "MATCH-bound document variable",
                code="NOT_IMPLEMENTED",
            )
        if key not in bind_vars:
            raise CoreError(
                f"Missing collection bind for SET/DELETE/REMOVE target {target_var!r}",
                code="UNSUPPORTED",
            )
        return key

    def _rewrite_target(name: str) -> str:
        return var_env.get(name, name)

    writes = _property_writes(
        set_clauses,
        remove_clauses,
        bind_vars=bind_vars,
        target_of=_rewrite_target,
        rewrite_value=(lambda value: _rewrite_vars(value, var_env)) if var_env else None,
    )
    physical = resolver.bundle.physical_mapping
    written: list[tuple[str, str]] = []
    env = dict(var_env)
    for target_var in writes.ops:
        key = _coll_key_for(target_var)
        write = writes.stored_write(target_var, keep_fields_for(bind_vars[key], physical))
        lines.append(_stored_write_line(target_var, _aql_collection_ref(key), write))
        written.append((f"SET/REMOVE on {target_var}", bind_vars[key]))
        if rebind_to is not None:
            updated = _pick_fresh_var(f"{target_var}_new", forbidden_vars=rebind_to)
            lines.append(f"  LET {updated} = NEW")
            for name, aql in list(env.items()):
                if aql == target_var:
                    env[name] = updated
            if target_var not in var_env.values():
                env[target_var] = updated

    for dc in delete_clauses:
        is_detach = dc.DETACH() is not None
        for de in dc.oC_Expression() or []:
            del_var = _compile_expression(de, bind_vars)
            if var_env:
                del_var = _rewrite_vars(del_var, var_env)
            # Expression must resolve to a simple variable for collection lookup.
            simple = del_var.strip()
            if not re.fullmatch(r"\w+", simple):
                raise CoreError(
                    "DELETE after WITH only supports a simple MATCH-bound variable",
                    code="NOT_IMPLEMENTED",
                )
            if is_detach:
                for idx, ec in enumerate(resolver.all_edge_collections()):
                    ec_key = _pick_bind_key("@detachEdge", bind_vars)
                    bind_vars[ec_key] = ec
                    ec_ref = _aql_collection_ref(ec_key)
                    de_edge = f"_de{idx}"
                    lines.append(
                        f"  LET _edgeRm{idx} = (FOR {de_edge} IN 1..1 ANY {simple} {ec_ref} "
                        f"REMOVE {de_edge} IN {ec_ref})"
                    )
                    written.append((f"DETACH DELETE {simple} (its {ec} edges)", ec))
            lines.append(f"  REMOVE {simple} IN {_aql_collection_ref(_coll_key_for(simple))}")
            written.append((f"DELETE {simple}", bind_vars[_coll_key_for(simple)]))

    _refuse_repeated_collections(written)
    return env


def split_writes(
    clauses: list[CypherParser.OC_UpdatingClauseContext],
) -> tuple[list[CypherParser.OC_SetContext], list[CypherParser.OC_DeleteContext], list[Any]]:
    """The SET, DELETE and REMOVE clauses of *clauses*; anything else is refused."""
    sets, deletes, removes = [], [], []
    for clause in clauses:
        if clause.oC_Set() is not None:
            sets.append(clause.oC_Set())
        elif clause.oC_Delete() is not None:
            deletes.append(clause.oC_Delete())
        elif clause.oC_Remove() is not None:
            removes.append(clause.oC_Remove())
        else:
            kind = type(clause.getChild(0)).__name__.removeprefix("OC_").removesuffix("Context").upper()
            raise CoreError(
                f"{kind} before WITH is not supported here: only SET, REMOVE and DELETE may write "
                "before a WITH, unless every write of the query is a CREATE",
                code="UNSUPPORTED",
            )
    return sets, deletes, removes


def append_midquery_writes(
    clauses: list[CypherParser.OC_UpdatingClauseContext],
    *,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
    forbidden_vars: set[str],
    already_wrote: bool,
) -> dict[str, str]:
    """The writes of one query part that a WITH follows; the environment after them."""
    if already_wrote:
        raise CoreError(AFTER_WRITE_REFUSAL.format(what="A second write"), code="UNSUPPORTED")
    sets, deletes, removes = split_writes(clauses)
    return emit_pipeline_writes(
        set_clauses=sets,
        delete_clauses=deletes,
        remove_clauses=removes,
        lines=lines,
        var_env=var_env,
        bind_vars=bind_vars,
        resolver=resolver,
        rebind_to=forbidden_vars,
    )


def refuse_after_write(what: str) -> CoreError:
    """The refusal for a clause that would read or write after a mid-pipeline write."""
    return CoreError(AFTER_WRITE_REFUSAL.format(what=what), code="UNSUPPORTED")
