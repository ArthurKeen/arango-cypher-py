"""``GET /examples``: verified mined examples served from the session's database."""

from __future__ import annotations

from typing import Any

import pytest
from arango.exceptions import AQLQueryExecuteError
from fastapi.testclient import TestClient

from arango_cypher.query_mining.store import DEFAULT_COLLECTION
from tests.helpers.fake_arango import FakeDb, server_error
from tests.helpers.service_reload import fresh_service


class _Client:
    def close(self) -> None:
        pass


def _open_session(db: FakeDb, graph: str | None = None) -> str:
    svc = fresh_service()
    token = "test-examples-token"
    session = svc._Session(token=token, db=db, client=_Client())
    session.graph_name = graph
    svc._sessions[token] = session
    return token


@pytest.fixture
def client() -> TestClient:
    return TestClient(fresh_service().app)


EXAMPLE = {
    "_key": "mined-1",
    "kind": "mined",
    "question": "Which roles trust each other?",
    "graph": "IAM_DEMO",
}


def test_lists_the_sessions_examples_scoped_to_its_graph(client: TestClient) -> None:
    seen: list[dict[str, Any]] = []

    def handler(q: str, b: dict[str, Any]) -> list[Any]:
        seen.append(b)
        return [EXAMPLE]

    token = _open_session(FakeDb({DEFAULT_COLLECTION: []}, handler=handler), graph="IAM_DEMO")
    resp = client.get("/examples?limit=20", headers={"X-Arango-Session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"examples": [EXAMPLE]}
    assert seen == [{"@c": DEFAULT_COLLECTION, "scope": "IAM_DEMO", "limit": 20}]


def test_a_database_never_mined_has_none(client: TestClient) -> None:
    token = _open_session(FakeDb({}))
    assert client.get("/examples", headers={"X-Arango-Session": token}).json() == {"examples": []}


def test_requires_a_session(client: TestClient) -> None:
    assert client.get("/examples").status_code == 401


def test_a_read_failure_is_a_502_naming_the_collection(client: TestClient) -> None:
    def handler(q: str, b: dict[str, Any]) -> list[Any]:
        raise server_error(AQLQueryExecuteError, 403, 11, "not authorized to execute this request")

    token = _open_session(FakeDb({DEFAULT_COLLECTION: []}, handler=handler))
    resp = client.get("/examples", headers={"X-Arango-Session": token})
    assert resp.status_code == 502
    assert DEFAULT_COLLECTION in resp.json()["detail"]["message"]
