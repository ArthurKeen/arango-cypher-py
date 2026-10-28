"""``p = shortestPath((a)-[:R*]-(b))`` between bound nodes (LDBC plan WP-39).

A breadth-first traversal from ``a`` that stops at the first path reaching
``b`` is a shortest path::

    LET p = FIRST(FOR _sp_v, _sp_e, _sp_p IN 1..10 ANY a @@edges
                  OPTIONS {order: "bfs", uniqueVertices: "global"}
                  FILTER _sp_v._id == b._id LIMIT 1
                  RETURN {nodes: _sp_p.vertices, edges: _sp_p.edges})
    FILTER p != null

AQL's ``SHORTEST_PATH`` would be shorter but cannot filter edges by a type
field, which every LPG mapping needs; a traversal gets the same
variable-length type guard as any other (``_guard_varlength_edge_types``). As
in a MATCH, a pair with no path yields no row. The path is ``{nodes, edges}``,
as a named path in MATCH is, so ``length(p)`` and ``nodes(p)`` work on it.

Supported: one relationship, both endpoints bound earlier in the query
(another pattern part, or a WITH), any type and direction, ``*``,
``*1..n``, ``*0..n``. Refused (``NOT_IMPLEMENTED``): unbound endpoints,
several relationships, relationship properties (they would apply to every
edge of the path) and ``allShortestPaths``.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var


def _core(name: str) -> Any:
    from . import core

    return getattr(core, name)


def shortest_path_pattern(part: Any) -> CypherParser.OC_ShortestPathPatternContext | None:
    """The ``shortestPath(…)`` of a pattern part, or ``None``."""
    anon = part.oC_AnonymousPatternPart()
    return anon.oC_ShortestPathPattern() if anon is not None else None


def pattern_element(
    anon: CypherParser.OC_AnonymousPatternPartContext,
) -> CypherParser.OC_PatternElementContext:
    """A pattern part's element, through a ``shortestPath(…)`` wrapper."""
    sp = anon.oC_ShortestPathPattern()
    return _innermost(sp.oC_PatternElement()) if sp is not None else anon.oC_PatternElement()


def refuse_unbound_shortest_path(anon: CypherParser.OC_AnonymousPatternPartContext | None) -> None:
    """Refuse a ``shortestPath(…)`` where no earlier part or WITH binds its endpoints."""
    if anon is not None and anon.oC_ShortestPathPattern() is not None:
        raise CoreError(_UNBOUND, code="NOT_IMPLEMENTED")


_UNBOUND = (
    "shortestPath() between nodes bound earlier in the query is supported; bind both endpoints "
    "first, e.g. MATCH (a {…}), (b {…}), p = shortestPath((a)-[*]-(b))"
)


def refuse_shortest_path_in_writes(tree: Any) -> None:
    """``CREATE``/``MERGE`` of a ``shortestPath(…)`` is not Cypher (Neo4j refuses it)."""

    def walk(node: Any, in_write: bool) -> None:
        if isinstance(node, (CypherParser.OC_CreateContext, CypherParser.OC_MergeContext)):
            in_write = True
        if in_write and isinstance(node, CypherParser.OC_ShortestPathPatternContext):
            raise CoreError("shortestPath() cannot be created or merged", code="UNSUPPORTED")
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i), in_write)

    walk(tree, False)


def _innermost(elem: CypherParser.OC_PatternElementContext) -> CypherParser.OC_PatternElementContext:
    while elem.oC_NodePattern() is None and elem.oC_PatternElement() is not None:
        elem = elem.oC_PatternElement()
    return elem


def compile_shortest_path(
    part: Any,
    *,
    var_env: dict[str, str],
    forbidden: set[str],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    indent: str,
) -> list[str]:
    """The lines binding the path of a ``shortestPath`` pattern part (module docstring)."""
    sp = shortest_path_pattern(part)
    if sp is None:
        raise CoreError("Not a shortestPath pattern", code="INTERNAL_ERROR")
    if sp.ALLSHORTESTPATHS() is not None:
        raise CoreError("allShortestPaths() is not supported yet", code="NOT_IMPLEMENTED")
    elem = _innermost(sp.oC_PatternElement())
    chains = elem.oC_PatternElementChain() or []
    if len(chains) != 1:
        raise CoreError("shortestPath() takes exactly one relationship", code="NOT_IMPLEMENTED")
    start, end = elem.oC_NodePattern(), chains[0].oC_NodePattern()
    names = [n.oC_Variable().getText().strip() if n.oC_Variable() is not None else None for n in (start, end)]
    if any(name is None or name not in var_env for name in names):
        raise CoreError(_UNBOUND, code="NOT_IMPLEMENTED")
    a, b = (var_env[str(n)] for n in names)
    rel_pat = chains[0].oC_RelationshipPattern()
    detail = rel_pat.oC_RelationshipDetail()
    if detail is not None and detail.oC_Properties() is not None:
        raise CoreError("Relationship properties in shortestPath() are not supported", code="NOT_IMPLEMENTED")
    rel_type, _rel_var, (rmin, rmax) = _core("_extract_relationship_type_and_var")(
        rel_pat, default_var="_sp_r"
    )
    if rmin > 1:
        raise CoreError("shortestPath() needs a minimum length of 0 or 1", code="UNSUPPORTED")
    direction = _core("_relationship_direction")(rel_pat)
    r_map = _core("_resolve_relationship_for_pattern")(resolver, rel_type)

    v, e, p = (_pick_fresh_var(n, forbidden_vars=forbidden) for n in ("_sp_v", "_sp_e", "_sp_p"))
    path_var = part.oC_Variable().getText().strip() if part.oC_Variable() is not None else "_sp_path"
    path_aql = _pick_fresh_var(path_var, forbidden_vars=forbidden)
    var_env[path_var] = path_aql
    _core("_active_path_vars").get()[path_aql] = ([], [])

    edge_key = _pick_bind_key("@spEdge", bind_vars)
    bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    filters: list[str] = []
    if r_map.get("style") == "GENERIC_WITH_TYPE":
        tf = _pick_bind_key("spRelTF", bind_vars)
        tv = _pick_bind_key("spRelTV", bind_vars)
        bind_vars[tf], bind_vars[tv] = r_map.get("typeField"), r_map.get("typeValue")
        filters.append(f"{e}[@{tf}] == @{tv}")  # the variable-length guard extends it to every edge
    for node, var in ((start, a), (end, b)):
        if node.oC_NodeLabels() is not None:
            pred = _core("_compile_label_predicate")(var, node.oC_NodeLabels(), bind_vars)
            if pred is not None:
                filters.append(pred)
    filters.append(f"{v}._id == {b}._id")
    traversal = (
        f"FOR {v}, {e}, {p} IN {rmin}..{rmax} {direction} {a} {_aql_collection_ref(edge_key)} "
        'OPTIONS {order: "bfs", uniqueVertices: "global"} '
        + " ".join(f"FILTER {f}" for f in filters)
        + f" LIMIT 1 RETURN {{nodes: {p}.vertices, edges: {p}.edges}}"
    )
    return [f"{indent}LET {path_aql} = FIRST({traversal})", f"{indent}FILTER {path_aql} != null"]
