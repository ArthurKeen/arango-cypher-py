"""When ``/schema/introspect`` rechecks a served schema in the background.

The live behaviour (old schema served while ``rechecking``, then the new one)
is in ``tests/integration/test_schema_recheck_live.py``; this pins the
schedule. ``schedule_warm`` / ``is_warming`` are replaced with fakes of the
same signatures (``arango_cypher/catalog/warm.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_cypher.catalog import warm
from arango_cypher.service.routes import schema as schema_routes


class _Db:
    name = "IAM"


class _Session:
    db = _Db()
    platform_token = None


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def schedule_warm(db: Any, graph_name: str | None = None, *, force: bool = True) -> bool:
        calls.append({"graph": graph_name, "force": force})
        return True

    def is_warming(db_name: str, graph_name: str | None = None) -> bool:
        return False

    monkeypatch.setattr(warm, "schedule_warm", schedule_warm)
    monkeypatch.setattr(warm, "is_warming", is_warming)
    monkeypatch.setattr(schema_routes, "background_database", lambda session: session.db)
    schema_routes._last_recheck.clear()
    return calls


def test_the_first_serve_rechecks_then_waits_for_the_interval(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", raising=False)
    assert schema_routes._maybe_recheck(_Session(), "g") is True
    assert schema_routes._maybe_recheck(_Session(), "g") is False
    assert scheduled == [{"graph": "g", "force": False}]  # a recheck, never a forced rebuild


def test_a_fresh_analysis_postpones_the_recheck(scheduled: list[dict[str, Any]]) -> None:
    schema_routes._note_fresh("IAM", None)
    assert schema_routes._maybe_recheck(_Session(), None) is False
    assert scheduled == []


def test_zero_turns_rechecks_off(scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "0")
    assert schema_routes._maybe_recheck(_Session(), None) is False
    assert scheduled == []


def test_a_bad_interval_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "soon")
    assert schema_routes._recheck_interval_s() == schema_routes._DEFAULT_RECHECK_S
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "-5")
    assert schema_routes._recheck_interval_s() == 0.0
