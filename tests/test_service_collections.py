"""``arango_cypher/service_collections.py``: the database view schema analysis
gets, without the transpilers' own collections (issue #19). The live
counterpart is ``tests/integration/test_service_collections_live.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_cypher.query_mining.store import COLLECTION_ENV
from arango_cypher.schema_acquire import _fallback_fingerprint
from arango_cypher.service_collections import hide_service_collections, service_collections


class FakeDb:
    """``collections()`` rows shaped as python-arango returns them."""

    def __init__(self, names: list[str]) -> None:
        self.name = "IAM"
        self._rows = [{"name": n, "type": "document", "system": n.startswith("_")} for n in names]
        self.counts = dict.fromkeys(names, 1)

    def collections(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def collection(self, name: str) -> Any:
        db = self

        class _Coll:
            def count(self) -> int:
                return db.counts[name]

        return _Coll()


ALL_FOUR = {
    "arango_cypher_schema_cache",
    "arango_cypher_examples",
    "arango_sparql_schema_cache",
    "arango_sparql_examples",
}


def test_the_set_is_both_apps_caches_and_examples() -> None:
    assert service_collections() == ALL_FOUR


def test_configured_names_join_the_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(COLLECTION_ENV, "my_examples")
    hidden = service_collections("my_cache")
    assert {"my_examples", "my_cache"} <= hidden and ALL_FOUR - {"arango_cypher_examples"} <= hidden


def test_the_view_lists_everything_else_and_delegates_the_rest() -> None:
    db = FakeDb(["Person", "_graphs", *ALL_FOUR])
    view = hide_service_collections(db)
    assert [r["name"] for r in view.collections()] == ["Person", "_graphs"]
    assert view.name == "IAM"
    assert view.collection("arango_cypher_schema_cache").count() == 1  # reading by name still works


def test_the_view_is_idempotent_and_widens_for_a_custom_cache() -> None:
    db = FakeDb(["Person", "custom_cache", *ALL_FOUR])
    view = hide_service_collections(db)
    assert hide_service_collections(view) is view
    wider = hide_service_collections(view, "custom_cache")
    assert [r["name"] for r in wider.collections()] == ["Person"]


def test_the_fallback_fingerprint_ignores_writes_to_them() -> None:
    db = FakeDb(["Person", *ALL_FOUR])
    before = _fallback_fingerprint(db, include_counts=True)  # type: ignore[arg-type]
    for name in ALL_FOUR:
        db.counts[name] += 5
    assert _fallback_fingerprint(db, include_counts=True) == before  # type: ignore[arg-type]
    db.counts["Person"] += 1
    assert _fallback_fingerprint(db, include_counts=True) != before  # type: ignore[arg-type]
