"""A served schema notices the database changed, without "Refresh schema".

``/schema/introspect`` serves the cached schema without looking at the
database, and on BYOC nothing else re-analyzes it: after #23 the IAM schema
kept its leaked classes until someone clicked Refresh schema. Now a served
schema is rechecked in the background (at most every
``ARANGO_CYPHER_SCHEMA_RECHECK_S``), and re-analyzed only when the database's
shape changed. Real analyzer, real background thread, real ArangoDB.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient

from tests.helpers.service_reload import fresh_service

pytestmark = pytest.mark.integration

_DB = "arango_cypher_schema_recheck_it"


@pytest.fixture
def setup(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Any, TestClient, dict[str, str], dict[str, Any]]]:
    pytest.importorskip("schema_analyzer")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        db = client.db(_DB, username=user, password=password)
        db.create_collection("Person").insert_many([{"name": "Tom"}, {"name": "Meg"}])
        svc = fresh_service()
        token = "schema-recheck-it"
        svc._sessions[token] = svc._Session(token=token, db=db, client=client)
        endpoint = next(
            r.endpoint for r in svc.app.routes if getattr(r, "path", None) == "/schema/introspect"
        )
        endpoint.__globals__["_last_recheck"].clear()
        yield db, TestClient(svc.app), {"X-Arango-Session": token}, endpoint.__globals__
        svc._sessions.pop(token, None)
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _labels(body: dict[str, Any]) -> set[str]:
    return {e["label"] for e in body["entities"]}


def test_a_new_collection_reaches_the_served_schema_on_its_own(
    setup: tuple[Any, TestClient, dict[str, str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, client, headers, _ = setup
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "1")
    first = client.get("/schema/introspect?force=true", headers=headers).json()
    assert first["status"] == "ready" and _labels(first) == {"Person"}
    assert first["rechecking"] is False  # just analyzed

    db.create_collection("Movie").insert({"title": "Big"})
    time.sleep(1.2)  # the recheck interval has passed

    served = client.get("/schema/introspect", headers=headers).json()
    assert _labels(served) == {"Person"} and served["rechecking"] is True  # old schema, recheck running
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "3600")  # no further rechecks while polling

    deadline = time.monotonic() + 120
    while served["rechecking"] and time.monotonic() < deadline:
        time.sleep(0.5)
        served = client.get("/schema/introspect", headers=headers).json()
    assert served["rechecking"] is False
    assert _labels(served) == {"Person", "Movie"}
    assert served["schema_hash"] != first["schema_hash"]


def test_an_unchanged_database_keeps_its_schema(
    setup: tuple[Any, TestClient, dict[str, str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, headers, _ = setup
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "1")
    first = client.get("/schema/introspect?force=true", headers=headers).json()
    time.sleep(1.2)
    served = client.get("/schema/introspect", headers=headers).json()
    assert served["rechecking"] is True
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "3600")
    deadline = time.monotonic() + 60
    while served["rechecking"] and time.monotonic() < deadline:
        time.sleep(0.5)
        served = client.get("/schema/introspect", headers=headers).json()
    assert served["rechecking"] is False and served["schema_hash"] == first["schema_hash"]


def test_rechecks_are_throttled(
    setup: tuple[Any, TestClient, dict[str, str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, headers, _ = setup
    monkeypatch.setenv("ARANGO_CYPHER_SCHEMA_RECHECK_S", "3600")
    client.get("/schema/introspect?force=true", headers=headers)
    for _ in range(3):
        assert client.get("/schema/introspect", headers=headers).json()["rechecking"] is False
