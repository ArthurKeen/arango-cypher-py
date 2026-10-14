"""AQL formatting and post-processing helpers for the v0 translator."""

from __future__ import annotations

import re

from arango_query_core import AqlQuery, MappingResolver

_INDENT = "  "
_FOR_RE = re.compile(r"^\s*FOR\b")
_TERMINAL_RE = re.compile(r"^\s*(?:RETURN|SORT|LIMIT|COLLECT)\b")
_FILTER_LET_RE = re.compile(r"^\s*(?:FILTER|LET|PRUNE)\b")


_WITH_BIND_KEY = "@withCollection"


def _with_collection_refs(
    vertex_collections: set[str], bind_vars: dict[str, object]
) -> tuple[list[str], dict[str, object]]:
    """``@@`` references for *vertex_collections*, and the bind vars they need.

    Collection names come from the mapping, which callers supply with the
    request, so they are never spliced into the AQL text: a name such as
    ``IAM-AWS-DOCS-DEMO_Chunks`` does not parse as a bare identifier, and a
    crafted one could inject AQL. A collection the query already binds reuses
    that bind variable; the rest get fresh ``@withCollectionN`` names that
    clash with no existing bind variable. Ordered by collection name, so the
    output is deterministic.
    """
    existing: dict[str, str] = {}
    for key, val in bind_vars.items():
        if key.startswith("@") and isinstance(val, str) and val not in existing:
            existing[val] = key
    added: dict[str, object] = {}
    refs: list[str] = []
    n = 0
    for coll in sorted(vertex_collections):
        bound = existing.get(coll)
        if bound is None:
            while f"{_WITH_BIND_KEY}{n}" in bind_vars:
                n += 1
            bound = f"{_WITH_BIND_KEY}{n}"
            n += 1
            added[bound] = coll
        refs.append("@" + bound)
    return refs, {**bind_vars, **added}


def _prepend_with_collections(result: AqlQuery, resolver: MappingResolver) -> AqlQuery:
    """Prepend ``WITH @@c1, @@c2, ...`` listing all vertex collections.

    ArangoDB requires a leading ``WITH`` declaration of all vertex collections
    accessed during graph traversals. This is mandatory in cluster deployments
    and harmless in single-server mode. Edge collections are excluded. The
    collections are bind parameters (:func:`_with_collection_refs`).
    """
    has_traversal = re.search(
        r"\b(?:OUTBOUND|INBOUND|ANY|SHORTEST_PATH|ALL_SHORTEST_PATHS)\b",
        result.text,
    )
    if not has_traversal:
        return result

    edge_collections: set[str] = set()
    rels = resolver.bundle.physical_mapping.get("relationships", {})
    if isinstance(rels, dict):
        for rmap in rels.values():
            ec = rmap.get("edgeCollectionName") or rmap.get("collectionName")
            if isinstance(ec, str) and ec:
                edge_collections.add(ec)

    vertex_collections: set[str] = set()

    for key, val in result.bind_vars.items():
        if key.startswith("@") and isinstance(val, str) and val:
            if val not in edge_collections:
                vertex_collections.add(val)

    entities = resolver.bundle.physical_mapping.get("entities", {})
    if isinstance(entities, dict):
        for emap in entities.values():
            coll = emap.get("collectionName")
            if isinstance(coll, str) and coll and coll not in edge_collections:
                vertex_collections.add(coll)

    if not vertex_collections:
        return result

    refs, bind_vars = _with_collection_refs(vertex_collections, dict(result.bind_vars))
    with_line = "WITH " + ", ".join(refs)
    return AqlQuery(
        text=with_line + "\n" + result.text,
        bind_vars=bind_vars,
        debug=result.debug,
        warnings=result.warnings,
    )


def _reindent_aql(text: str) -> str:
    """Re-indent AQL to reflect the nesting structure of FOR loops."""
    raw_lines = text.split("\n")
    out: list[str] = []
    depth = 0
    first_for_depth = -1

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue

        if stripped.startswith("WITH "):
            out.append(stripped)
            continue

        if _FOR_RE.match(stripped):
            indent = _INDENT * depth
            out.append(indent + stripped)
            if first_for_depth < 0:
                first_for_depth = depth
            depth += 1
        elif _TERMINAL_RE.match(stripped):
            out.append(_INDENT * depth + stripped)
        elif _FILTER_LET_RE.match(stripped):
            out.append(_INDENT * depth + stripped)
        else:
            out.append(_INDENT * depth + stripped)

    return "\n".join(out)
