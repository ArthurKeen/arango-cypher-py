"""Collections the transpilers keep in a customer database for themselves.

arango-cypher-py and arango-sparql-py each store a schema cache and mined
examples in the databases they serve, in ordinary (not ``_``-prefixed)
collections. They are never user data, so schema analysis must not see them:
the analyzer skips only system collections and cannot be told to skip any
other, so it turned them into classes, an invented ``SchemaCache`` shard
family, statistics, sharding-profile entries and OWL classes (issue #19).

:func:`hide_service_collections` gives the analysis a view of the database
whose ``collections()`` leaves them out. Every analysis pass enumerates
collections through that one call (the analyzer's snapshot and statistics,
the fingerprints, the heuristic detector and mapping builder), so filtering it
keeps them out of every derived artifact at once. The same set is excluded
from the fingerprints, so one app's cache writes never make the other
re-analyze. Mirrors arango-sparql-py's ``schema/acquire.py``.
"""

from __future__ import annotations

from typing import Any

from .schema_cache import DEFAULT_CACHE_COLLECTION

#: Both transpilers' schema caches, under their default names.
SCHEMA_CACHE_COLLECTIONS = frozenset({DEFAULT_CACHE_COLLECTION, "arango_sparql_schema_cache"})
#: arango-sparql-py's examples collection (its default name).
SPARQL_EXAMPLES_COLLECTION = "arango_sparql_examples"


def service_collections(cache_collection: str | None = None) -> frozenset[str]:
    """Both schema caches and both examples collections, with this service's
    as configured (``cache_collection``, ``ARANGO_CYPHER_EXAMPLES_COLLECTION``)."""
    # Imported here: query_mining imports the translator, which imports the
    # schema code that imports this module.
    from .query_mining.store import collection_name as examples_collection

    names = set(SCHEMA_CACHE_COLLECTIONS) | {SPARQL_EXAMPLES_COLLECTION, examples_collection()}
    if cache_collection:
        names.add(cache_collection)
    return frozenset(names)


class _ServiceCollectionsHidden:
    """A database handle whose ``collections()`` leaves out the service
    collections; everything else is delegated to the wrapped handle unchanged
    (reading one of them by name with ``collection()`` still works)."""

    def __init__(self, db: Any, hidden: frozenset[str]) -> None:
        self._db = db
        self._hidden = hidden

    def collections(self) -> Any:
        rows = self._db.collections()
        if not isinstance(rows, list):
            return rows
        return [row for row in rows if not (isinstance(row, dict) and row.get("name") in self._hidden)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def hide_service_collections(db: Any, cache_collection: str | None = None) -> Any:
    """*db* seen without :func:`service_collections` (idempotent: an already
    hidden view is returned as is, with *cache_collection* added if new)."""
    hidden = service_collections(cache_collection)
    if isinstance(db, _ServiceCollectionsHidden):
        if hidden <= db._hidden:
            return db
        return _ServiceCollectionsHidden(db._db, db._hidden | hidden)
    return _ServiceCollectionsHidden(db, hidden)
