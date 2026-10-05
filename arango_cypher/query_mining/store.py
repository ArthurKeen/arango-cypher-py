"""Verified examples, stored in the database they were mined from.

A user collection (:data:`DEFAULT_COLLECTION`) beside the data, like the
schema cache: it survives service redeploys, belongs to exactly one database,
and the deployed service reads it without an LLM key — mining runs offline,
serving does not need to.

One document per saved query (keyed by its source collection and key), so
re-mining replaces an example in place and a query that no longer verifies
loses its example. Each document records the mapping it was verified against
(:func:`arango_query_core.mapping_hash`) and the source query's fingerprint,
so a reader can tell an example from a schema or a saved query that has since
changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from .._arango_sync import bind, sync
from .miner import MinedExample, Outcome

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "arango_cypher_examples"
COLLECTION_ENV = "ARANGO_CYPHER_EXAMPLES_COLLECTION"

#: Examples the service lists at most per request.
MAX_LIST = 200


def collection_name() -> str:
    return os.getenv(COLLECTION_ENV, "").strip() or DEFAULT_COLLECTION


def example_key(source_collection: str, source_key: str) -> str:
    """Stable ``_key`` for the example mined from one saved query."""
    digest = hashlib.sha256(f"{source_collection}/{source_key}".encode()).hexdigest()[:20]
    return f"mined-{digest}"


def to_document(example: MinedExample, *, mapping_hash: str, model: str) -> dict[str, Any]:
    q = example.source
    return {
        "_key": example_key(q.source, q.key),
        "kind": "mined",
        "question": example.question,
        "cypher": example.cypher,
        "params": example.params,
        "aql": example.aql,
        "graph": q.graph,
        "source": {
            "collection": q.source,
            "key": q.key,
            "name": q.name,
            "description": q.description,
            "aql": q.aql,
            "fingerprint": q.fingerprint,
        },
        "verification": {
            "verdict": example.verdict.kind,
            "attempts": example.attempts,
            "rows": example.source_signature.rows,
            "vertices": len(example.source_signature.vertices),
            "edges": len(example.source_signature.edges),
            "mapping_hash": mapping_hash,
            "model": model,
            "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }


def ensure_collection(db: Any) -> Any:
    name = collection_name()
    if db.has_collection(name):
        return db.collection(name)
    logger.info("creating examples collection %s in %s", name, db.name)
    return db.create_collection(name)


def save_outcomes(db: Any, outcomes: list[Outcome], *, mapping_hash: str, model: str) -> dict[str, int]:
    """Write verified examples; drop the stored example of any query that no
    longer verifies. Queries that were not judged (provider failure) keep
    whatever they had. Returns counts by action."""
    col = ensure_collection(db)
    counts = {"saved": 0, "removed": 0, "kept": 0}
    for outcome in outcomes:
        key = example_key(outcome.query.source, outcome.query.key)
        if outcome.example is not None:
            col.insert(to_document(outcome.example, mapping_hash=mapping_hash, model=model), overwrite=True)
            counts["saved"] += 1
        elif outcome.provider_failed:
            counts["kept"] += 1
        elif col.has(key):
            col.delete(key)
            counts["removed"] += 1
    return counts


def list_examples(db: Any, *, graph: str | None = None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Stored examples, those for *graph* (and graph-less ones) when given."""
    name = collection_name()
    if not db.has_collection(name):
        return []
    query = (
        "FOR e IN @@c FILTER e.kind == 'mined' "
        # e["graph"], not e.graph: GRAPH is an AQL keyword, even after a dot.
        'FILTER @scope == null OR e["graph"] == null OR e["graph"] == @scope '
        "SORT e.source.name LIMIT @limit RETURN UNSET(e, '_id', '_rev')"
    )
    cursor = db.aql.execute(
        query, bind_vars=bind({"@c": name, "scope": graph, "limit": max(1, min(limit, MAX_LIST))})
    )
    return list(sync(cursor))
