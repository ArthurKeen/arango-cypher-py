"""``… DELETE`` / ``… DETACH DELETE``: read every matched row, then write.

AQL cannot read a collection after it modified it in the same query
(ERR 1579), and a node reached twice is removed twice (ERR 1202). Deleting
inside the loop that matches — ``FOR c IN 0..10 INBOUND … REMOVE c …`` with a
``DETACH`` subquery that walks the same edges — fails on both counts as soon
as the pattern matches more than one row (LDBC DEL6, DEL7), and the loop
compiled only the first ``MATCH``, so a delete after ``OPTIONAL MATCH`` was
refused (DEL1, DEL4).

So the reading clauses compile through the read translator, as
``<reading clauses> RETURN <every pattern variable>``, into a list of
matched rows. Then:

1. the ids of every deleted node and relationship are collected (nulls of an
   unmatched OPTIONAL MATCH dropped, duplicates merged);
2. for ``DETACH DELETE``, the ids of every edge touching a deleted node, in
   every edge collection;
3. each collection is written once: one ``REMOVE`` per edge collection, then
   one per node collection, each over the ids that live in it;
4. a ``RETURN`` reads the matched rows, as Cypher's does. A row that reads a
   property or the labels of an entity the query deleted fails, as Neo4j's
   ``DeletedEntityAccess`` does: at run time, row by row, so a match with no
   rows still succeeds (``_deleted_entities_read``).

Every read happens before the first write, and no collection is written
twice.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from ..parser import parse_cypher
from .literals import _aql_string_literal
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks
from .semantic_expr import bare_variable

_BARE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|`[^`]+`")

#: Functions that read a node's or relationship's stored data (``type()``,
#: ``id()`` and returning the entity itself do not, and stay allowed).
_DATA_READING_FUNCTIONS = frozenset({"labels", "properties", "keys"})
_DELETED_ENTITY_ACCESS = _aql_string_literal(
    "DeletedEntityAccess: the query reads a property or the labels of a node or relationship it deleted"
)


def _core(name: str) -> Any:
    from . import core

    return getattr(core, name)


def _pattern_variables(
    reading_clauses: list[Any],
) -> tuple[dict[str, list[str]], dict[str, bool], dict[str, list[str]]]:
    """Node variables with their labels; relationship variables with whether
    they are variable-length, and with their types; named in *reading_clauses*."""
    nodes: dict[str, list[str]] = {}
    rels: dict[str, bool] = {}
    rel_types: dict[str, list[str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, CypherParser.OC_NodePatternContext) and node.oC_Variable() is not None:
            labels = nodes.setdefault(node.oC_Variable().getText().strip(), [])
            if node.oC_NodeLabels() is not None:
                labels.extend(
                    _strip_label_backticks(nl.oC_LabelName().getText().strip())
                    for nl in node.oC_NodeLabels().oC_NodeLabel()
                )
        elif isinstance(node, CypherParser.OC_RelationshipDetailContext) and node.oC_Variable() is not None:
            name = node.oC_Variable().getText().strip()
            rels[name] = node.oC_RangeLiteral() is not None
            types = node.oC_RelationshipTypes()
            names = types.oC_RelTypeName() if types is not None else []
            rel_types[name] = [_strip_label_backticks(t.getText().strip()) for t in names]
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i))

    for rc in reading_clauses:
        walk(rc)
    return nodes, rels, rel_types


def _edge_collections(types: list[str], resolver: MappingResolver) -> list[str]:
    """Edge collections of relationships of *types*; every one when untyped."""
    if not types:
        return resolver.all_edge_collections()
    out: list[str] = []
    for t in types:
        rmap = resolver.resolve_relationship(t)
        ec = rmap.get("edgeCollectionName") or rmap.get("collectionName")
        if isinstance(ec, str) and ec and ec not in out:
            out.append(ec)
    return out


def _entity_collections(label: str, resolver: MappingResolver) -> list[str]:
    emap = resolver.resolve_entity(label)
    if emap.get("style") == "UNION":
        out: list[str] = []
        for member in emap.get("members") or []:
            out.extend(c for c in _entity_collections(member, resolver) if c not in out)
        return out
    coll = emap.get("collectionName")
    return [coll] if isinstance(coll, str) and coll else []


def _node_collections(labels: list[str], resolver: MappingResolver) -> list[str]:
    """Collections a node with *labels* may live in: its primary label's, or
    every node collection when it has none."""
    if labels:
        return _entity_collections(labels[0], resolver)
    out: list[str] = []
    for label in resolver.all_entity_labels():
        out.extend(c for c in _entity_collections(label, resolver) if c not in out)
    return out


def translate_delete_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    delete_clauses: list[CypherParser.OC_DeleteContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate ``<reading clauses> [DETACH] DELETE … [RETURN …]`` (module docstring)."""
    reading_clauses = spq.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("DELETE needs a MATCH that binds what it deletes", code="UNSUPPORTED")
    nodes, rels, rel_types = _pattern_variables(reading_clauses)

    deleted_nodes: list[str] = []
    deleted_rels: list[str] = []
    detach = False
    for dc in delete_clauses:
        detach = detach or dc.DETACH() is not None
        for expr in dc.oC_Expression() or []:
            name = expr.getText().strip()
            if not _BARE_VARIABLE.fullmatch(name):
                raise CoreError("DELETE only supports a variable bound by MATCH", code="UNSUPPORTED")
            if name in rels:
                deleted_rels.append(name)
            elif name in nodes:
                deleted_nodes.append(name)
            else:
                raise CoreError(f"DELETE targets {name!r}, which no MATCH binds", code="UNSUPPORTED")

    # 1. The matched rows, through the read translator.
    reads = " ".join(rc.getText() for rc in reading_clauses)
    # Every pattern variable, named; ``_row`` keeps a single column an object.
    columns = ", ".join(f"{name} AS {name}" for name in [*nodes, *rels])
    read_tree = parse_cypher(f"{reads} RETURN {columns}, true AS _row").tree
    read_query = read_tree.oC_Statement().oC_Query().oC_RegularQuery().oC_SingleQuery()
    read_aql = _core("_translate_single_query")(read_query, resolver=resolver, bind_vars=bind_vars).text
    head: list[str] = []
    body = read_aql.splitlines()
    while body and body[0].startswith("WITH "):
        head.append(body.pop(0))  # a WITH declaration must open the whole query
    lines = [*head, "LET _matched = (", *(f"  {line}" for line in body), ")"]

    # 2. The ids to remove.
    def ids_of(names: list[str], *, flatten: bool) -> str:
        parts = [
            f"_matched[*].{n}[*]._id" if flatten and rels.get(n) else f"_matched[*].{n}._id" for n in names
        ]
        joined = parts[0] if len(parts) == 1 else f"UNION({', '.join(parts)})"
        return f"UNIQUE(REMOVE_VALUE(FLATTEN({joined}), null))"

    edge_parts: list[str] = []
    if deleted_nodes:
        lines.append(f"LET _deleted_nodes = {ids_of(deleted_nodes, flatten=False)}")
    if deleted_rels:
        lines.append(f"LET _deleted_rels = {ids_of(deleted_rels, flatten=True)}")
        edge_parts.append("_deleted_rels")
    edge_collections: list[str] = []
    if detach and deleted_nodes:
        edge_collections = resolver.all_edge_collections()
    else:
        for name in deleted_rels:
            edge_collections.extend(
                c for c in _edge_collections(rel_types[name], resolver) if c not in edge_collections
            )
    edge_keys: list[tuple[str, str]] = []
    for i, ec in enumerate(edge_collections):
        ec_key = _pick_bind_key("@deleteEdges", bind_vars)
        bind_vars[ec_key] = ec
        edge_keys.append((ec, ec_key))
        if detach and deleted_nodes:
            lines.append(
                f"LET _detach{i} = (FOR _n IN _deleted_nodes FOR _x, _e IN 1..1 ANY _n "
                f"{_aql_collection_ref(ec_key)} RETURN _e._id)"
            )
            edge_parts.append(f"_detach{i}")

    # 3. One REMOVE per collection, edges first.
    removals: list[str] = []
    if edge_parts:
        joined = edge_parts[0] if len(edge_parts) == 1 else f"UNION({', '.join(edge_parts)})"
        lines.append(f"LET _deleted_edges = UNIQUE({joined})")
        for ec, ec_key in edge_keys:
            removals.append(_removal("_deleted_edges", ec, ec_key, bind_vars))
    if deleted_nodes:
        node_collections: list[str] = []
        for name in deleted_nodes:
            node_collections.extend(
                c for c in _node_collections(nodes[name], resolver) if c not in node_collections
            )
        for coll in node_collections:
            coll_key = _pick_bind_key("@deleteNodes", bind_vars)
            bind_vars[coll_key] = coll
            removals.append(_removal("_deleted_nodes", coll, coll_key, bind_vars))

    ret = spq.oC_Return()
    if ret is None:
        # A query cannot end on a LET: the last removal is the statement.
        lines += [f"LET _removed{i} = ({r})" for i, r in enumerate(removals[:-1])]
        lines.append(removals[-1])
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    # 4. RETURN over the matched rows.
    lines += [f"LET _removed{i} = ({r})" for i, r in enumerate(removals)]
    lines.append("FOR _row IN _matched")
    for name in [*nodes, *rels]:
        lines.append(f"  LET {name} = _row.{name}")
    read = _deleted_entities_read(ret.oC_ProjectionBody(), {*deleted_nodes, *deleted_rels})
    for name in [*nodes, *rels]:
        if _strip_label_backticks(name) in read:
            # null: an OPTIONAL MATCH that matched nothing, which Neo4j reads as null.
            lines.append(f"  FILTER {name} == null OR FAIL({_DELETED_ENTITY_ACCESS})")
    _core("_append_return")(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _deleted_entities_read(body: Any, deleted: set[str]) -> set[str]:
    """The deleted variables whose properties or labels *body* reads.

    A property lookup (``n.name``) or ``labels(n)``/``properties(n)``/
    ``keys(n)`` of a deleted variable is Neo4j's ``DeletedEntityAccess``;
    returning ``n`` itself or ``type(r)`` is not (TCK Return2 [14]).
    """
    names = {_strip_label_backticks(name) for name in deleted}
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, CypherParser.OC_PropertyOrLabelsExpressionContext) and node.oC_PropertyLookup():
            variable = node.oC_Atom().oC_Variable()
            name = _strip_label_backticks(variable.getText().strip()) if variable is not None else None
            if name in names:
                found.add(name)
        elif isinstance(node, CypherParser.OC_FunctionInvocationContext):
            args = node.oC_Expression()
            function = node.oC_FunctionName().getText().strip().lower()
            if function in _DATA_READING_FUNCTIONS and len(args) == 1:
                name = bare_variable(args[0])
                if name in names:
                    found.add(name)
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i))

    walk(body)
    return found


def _removal(ids: str, collection: str, coll_key: str, bind_vars: dict[str, Any]) -> str:
    """Remove the documents of *ids* that live in *collection*."""
    name_key = _pick_bind_key(f"{coll_key[1:]}Name", bind_vars)
    bind_vars[name_key] = collection
    return (
        f"FOR _id IN {ids} FILTER PARSE_IDENTIFIER(_id).collection == @{name_key} "
        f"REMOVE PARSE_IDENTIFIER(_id).key IN {_aql_collection_ref(coll_key)}"
    )
