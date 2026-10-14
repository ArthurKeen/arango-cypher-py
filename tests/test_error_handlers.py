"""Which errors the service reports to the error inbox, through the real app.

Reported: generated AQL that ArangoDB rejects (``translator``), an exception
nothing handled (``crash``), and other 500s caused inside this service
(``server_error``). Not reported: the user's own mistakes (4xx) and failures
of the database or the network. The response of a reported error carries
``report`` next to ``detail``. The inbox here is real, writing to a fake
GitHub over HTTP; it is not started, so each test flushes it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from arango.exceptions import AQLQueryExecuteError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from arango_cypher.service.error_inbox import ErrorInbox, parse_body
from arango_cypher.service.github_issues import GitHubIssues
from tests.helpers.fake_arango import FakeDb, server_error
from tests.helpers.fake_github import REPO, TOKEN, FakeGitHub
from tests.helpers.service_reload import fresh_service

MAPPING = {
    "conceptualSchema": {
        "entities": [{"labels": ["User"], "name": "User", "properties": []}],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {"User": {"collectionName": "users", "style": "COLLECTION"}},
        "relationships": {},
    },
    "metadata": {},
}
SYNTAX = "syntax error, unexpected - operator near '-users' at position 1:6"


class _Client:
    def close(self) -> None:
        pass


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    yield fake
    fake.close()


@pytest.fixture
def svc() -> Any:
    return fresh_service()


@pytest.fixture
def client(svc: Any) -> TestClient:
    return TestClient(svc.app, raise_server_exceptions=False)


@pytest.fixture
def inbox(svc: Any, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> ErrorInbox:
    box = ErrorInbox(GitHubIssues(REPO, TOKEN, api=github.api))
    handler = svc.app.exception_handlers[StarletteHTTPException]
    monkeypatch.setitem(handler.__globals__, "get_inbox", lambda: box)
    return box


def open_session(svc: Any, db: FakeDb, *, user: str | None = "alice") -> dict[str, str]:
    token = f"inbox-test-{id(db)}"
    svc._sessions[token] = svc._Session(token=token, db=db, client=_Client(), platform_user=user)
    return {"X-Arango-Session": token}


def failing_db(exc: Exception) -> FakeDb:
    def handler(_q: str, _b: dict[str, Any]) -> list[Any]:
        raise exc

    return FakeDb({"users": []}, handler=handler)


def test_generated_aql_the_database_rejects_is_filed_as_a_translator_bug(
    svc: Any, client: TestClient, inbox: ErrorInbox, github: FakeGitHub
) -> None:
    headers = open_session(svc, failing_db(server_error(AQLQueryExecuteError, 400, 1501, SYNTAX)))
    resp = client.post(
        "/execute", json={"cypher": "MATCH (u:User) RETURN u", "mapping": MAPPING}, headers=headers
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["code"] == "GENERATED_AQL_REJECTED"
    assert body["report"]["seen"] == 1 and len(body["report"]["reference"]) == 8
    inbox.flush()
    issue = github.issues[1]
    assert {lb["name"] for lb in issue["labels"]} == {"auto-report", "translator"}
    assert "MATCH (u:User) RETURN u" in issue["body"] and '"@collection": "users"' in issue["body"]
    assert "`IAM`" in issue["body"]
    assert "alice" not in github.everything_written()  # users are counted, hashed
    assert parse_body(issue["body"])[1].users  # one hashed user


def test_a_crash_is_filed_with_where_it_happened(
    svc: Any, client: TestClient, inbox: ErrorInbox, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    endpoint = next(r.endpoint for r in svc.app.routes if getattr(r, "path", None) == "/translate")
    monkeypatch.setitem(endpoint.__globals__, "translate", boom)
    resp = client.post("/translate", json={"cypher": "MATCH (u:User) RETURN u", "mapping": MAPPING})
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["code"] == "INTERNAL_ERROR"
    assert "bug in Arango Cypher, not in your query" in body["detail"]["message"]
    assert body["report"]["status"] == "pending"
    inbox.flush()
    issue = github.issues[1]
    assert (
        issue["title"] == "[auto] Unexpected error: RuntimeError in service.routes.cypher.translate_endpoint"
    )
    assert "RuntimeError: boom" in issue["body"]


def test_a_database_outage_is_not_our_bug(
    svc: Any, client: TestClient, inbox: ErrorInbox, github: FakeGitHub
) -> None:
    headers = open_session(svc, failing_db(server_error(AQLQueryExecuteError, 503, 1, "cluster unavailable")))
    resp = client.post("/execute-aql", json={"aql": "FOR u IN users RETURN u"}, headers=headers)
    assert resp.status_code == 500 and "report" not in resp.json()
    inbox.flush()
    assert github.issues == {}


def test_an_error_inside_the_service_is_filed_as_a_server_error(
    svc: Any, client: TestClient, inbox: ErrorInbox, github: FakeGitHub
) -> None:
    headers = open_session(svc, failing_db(KeyError("_key")))
    resp = client.post("/execute-aql", json={"aql": "FOR u IN users RETURN u"}, headers=headers)
    assert resp.status_code == 500 and resp.json()["report"]["seen"] == 1
    inbox.flush()
    assert github.issues[1]["title"].startswith("[auto] Server error: KeyError in ")


def test_the_users_own_mistake_is_not_reported(
    client: TestClient, inbox: ErrorInbox, github: FakeGitHub
) -> None:
    resp = client.post("/translate", json={"cypher": "MATCH (u:User RETURN u", "mapping": MAPPING})
    assert resp.status_code == 422 and "report" not in resp.json()
    inbox.flush()
    assert github.issues == {}


def test_without_an_inbox_a_crash_still_explains_itself(
    svc: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = svc.app.exception_handlers[StarletteHTTPException]
    monkeypatch.setitem(handler.__globals__, "get_inbox", lambda: None)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    endpoint = next(r.endpoint for r in svc.app.routes if getattr(r, "path", None) == "/translate")
    monkeypatch.setitem(endpoint.__globals__, "translate", boom)
    resp = client.post("/translate", json={"cypher": "MATCH (u:User) RETURN u", "mapping": MAPPING})
    assert resp.status_code == 500
    assert resp.json() == {
        "detail": {
            "error": "internal_error",
            "code": "INTERNAL_ERROR",
            "message": "Arango Cypher hit an unexpected error (RuntimeError). "
            "This is a bug in Arango Cypher, not in your query.",
        }
    }


def test_diagnostics_show_where_errors_go_without_the_token(
    svc: Any, client: TestClient, inbox: ErrorInbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = next(
        r.endpoint for r in svc.app.routes if getattr(r, "path", None) == "/connect/platform/diagnostics"
    )
    monkeypatch.setitem(endpoint.__globals__, "get_inbox", lambda: inbox)
    resp = client.get("/connect/platform/diagnostics")
    assert resp.json()["error_inbox"] == {
        "enabled": True,
        "repository": REPO,
        "issues_known": 0,
        "pending": 0,
        "last_error": None,
    }
    assert TOKEN not in resp.text
