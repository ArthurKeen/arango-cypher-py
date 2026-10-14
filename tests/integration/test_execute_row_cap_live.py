"""``max_rows`` on ``/execute`` and ``/execute-aql``, against a real ArangoDB.

The service returns a result as one JSON document. On BYOC an unbounded
``RETURN a, b`` over IAM came to 156,861 rows, 257 MB and ten minutes on the
wire, and the browser gave up ("Failed to fetch"). The Workbench now asks for
at most 1,000 rows; a request without ``max_rows`` still gets every row.
2,500 rows is more than one server batch (1,000), so a cut result leaves a
server-side cursor that the service must close.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient

from tests.helpers.service_reload import fresh_service

pytestmark = pytest.mark.integration

_DB = "arango_cypher_row_cap_it"
_ROWS = 2500
_MAPPING = {
    "conceptualSchema": {
        "entities": [{"labels": ["User"], "name": "User", "properties": [{"name": "n"}]}],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {"User": {"collectionName": "users", "style": "COLLECTION"}},
        "relationships": {},
    },
    "metadata": {},
}


@pytest.fixture(scope="module")
def session() -> Iterator[tuple[TestClient, str]]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        db = client.db(_DB, username=user, password=password)
        db.create_collection("users").insert_many([{"n": i} for i in range(_ROWS)])
        svc = fresh_service()
        token = "row-cap-it-token"
        svc._sessions[token] = svc._Session(token=token, db=db, client=client)
        yield TestClient(svc.app), token
        svc._sessions.pop(token, None)
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _post(session: tuple[TestClient, str], path: str, body: dict[str, Any]) -> dict[str, Any]:
    client, token = session
    resp = client.post(path, json=body, headers={"X-Arango-Session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


CYPHER = "MATCH (u:User) RETURN u.n AS n"
AQL = "FOR u IN users RETURN u.n"


@pytest.mark.parametrize(
    ("path", "body"),
    [("/execute", {"cypher": CYPHER, "mapping": _MAPPING}), ("/execute-aql", {"aql": AQL})],
)
def test_a_capped_result_says_it_was_cut(
    session: tuple[TestClient, str], path: str, body: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
        out = _post(session, path, {**body, "max_rows": 1000})
    assert len(out["results"]) == 1000
    assert out["truncated"] is True and out["row_limit"] == 1000
    assert any("first 1,000 rows" in w["message"] for w in out["warnings"])
    # The server-side cursor holding the other 1,500 rows was closed cleanly.
    assert "closing a truncated cursor failed" not in caplog.text


@pytest.mark.parametrize(
    ("path", "body"),
    [("/execute", {"cypher": CYPHER, "mapping": _MAPPING}), ("/execute-aql", {"aql": AQL})],
)
def test_a_result_that_fits_is_not_marked(
    session: tuple[TestClient, str], path: str, body: dict[str, Any]
) -> None:
    out = _post(session, path, {**body, "max_rows": _ROWS})
    assert len(out["results"]) == _ROWS
    assert out["truncated"] is False
    assert not any("rows; the query returns more" in w["message"] for w in out["warnings"])


@pytest.mark.parametrize(
    ("path", "body"),
    [("/execute", {"cypher": CYPHER, "mapping": _MAPPING}), ("/execute-aql", {"aql": AQL})],
)
def test_without_max_rows_every_row_comes_back(
    session: tuple[TestClient, str], path: str, body: dict[str, Any]
) -> None:
    out = _post(session, path, body)
    assert len(out["results"]) == _ROWS
    assert out["truncated"] is False and out["row_limit"] is None


def test_max_rows_must_be_positive(session: tuple[TestClient, str]) -> None:
    client, token = session
    resp = client.post("/execute-aql", json={"aql": AQL, "max_rows": 0}, headers={"X-Arango-Session": token})
    assert resp.status_code == 422
