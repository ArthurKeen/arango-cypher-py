"""Tests for the ``GET /tenants`` catalog endpoint.

This is the UI's "should I show the tenant selector?" probe — the
fast-path for single-tenant graphs (no `Tenant` collection → empty
list, ``detected: false``) has to stay fast and never 500.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from arango_cypher.service import _get_session, _Session, app

client = TestClient(app)


def _install_session(
    *,
    has_collection: bool,
    tenants: list[dict[str, Any]] | None = None,
    raise_on_query: bool = False,
    expected_collection: str = "Tenant",
) -> MagicMock:
    db = MagicMock()
    db.name = "testdb"
    db.has_collection.side_effect = lambda name: name == expected_collection and has_collection

    def _execute(aql: str, bind_vars: dict | None = None):  # noqa: ARG001
        if raise_on_query:
            raise RuntimeError("boom")
        return iter(tenants or [])

    db.aql.execute.side_effect = _execute

    session = _Session.__new__(_Session)
    session.token = "test-token"
    session.db = db
    session.client = MagicMock()
    session.created_at = 0.0
    session.last_used = 0.0
    app.dependency_overrides[_get_session] = lambda: session
    return db


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(_get_session, None)


class TestTenantsEndpoint:
    def test_requires_session(self):
        resp = client.get("/tenants")
        assert resp.status_code == 401

    def test_no_tenant_collection_returns_empty(self):
        _install_session(has_collection=False)
        resp = client.get("/tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is False
        assert body["tenants"] == []
        # Diagnostic fields are present so the UI can explain *why*
        # the catalog is empty (e.g. "looked for Tenant via heuristic").
        assert body["collection"] == "Tenant"
        assert body["source"] == "heuristic"

    def test_returns_catalog_when_collection_exists(self):
        _install_session(
            has_collection=True,
            tenants=[
                {
                    "id": "Tenant/t1",
                    "key": "t1",
                    "name": "Dagster Labs",
                    "subdomain": "dagster",
                    "hex_id": "abc123",
                },
                {
                    "id": "Tenant/t2",
                    "key": "t2",
                    "name": "Acme",
                    "subdomain": "acme",
                    "hex_id": "def456",
                },
            ],
        )
        resp = client.get("/tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is True
        assert len(body["tenants"]) == 2
        assert body["tenants"][0]["name"] == "Dagster Labs"
        assert body["tenants"][0]["hex_id"] == "abc123"
        # `id` (full _id) is the canonical tenant identifier the UI
        # binds the scope to. Must round-trip from the AQL projection.
        assert body["tenants"][0]["id"] == "Tenant/t1"
        assert body["tenants"][0]["key"] == "t1"
        assert body["collection"] == "Tenant"
        assert body["source"] == "heuristic"

    def test_query_failure_returns_500(self):
        _install_session(has_collection=True, raise_on_query=True)
        resp = client.get("/tenants")
        assert resp.status_code == 500
        assert "Tenant catalog query failed" in resp.json()["detail"]

    def test_empty_tenant_collection_is_detected_but_empty(self):
        _install_session(has_collection=True, tenants=[])
        resp = client.get("/tenants")
        body = resp.json()
        assert body["detected"] is True
        assert body["tenants"] == []

    def test_collection_query_param_uses_supplied_name(self):
        """The UI tells the server which collection backs the
        conceptual ``Tenant`` entity by passing it as a query param.
        Real schemas frequently rename the collection (``Tenants``,
        ``tenant_v2``, etc.) and the original literal-name probe
        silently returned an empty catalog."""
        _install_session(
            has_collection=True,
            expected_collection="Tenants",
            tenants=[
                {
                    "id": "Tenants/t1",
                    "key": "t1",
                    "name": "Dagster Labs",
                    "subdomain": "dagster",
                    "hex_id": "abc123",
                },
            ],
        )
        resp = client.get("/tenants?collection=Tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is True
        assert body["collection"] == "Tenants"
        assert body["source"] == "client"
        assert body["tenants"][0]["name"] == "Dagster Labs"

    def test_collection_query_param_not_found_reports_diagnostic(self):
        """When the client points us at a collection that doesn't
        exist, the server must report the resolved name back so the
        UI can show *what* it looked for, not just that nothing was
        found."""
        _install_session(has_collection=False, expected_collection="Tenants")
        resp = client.get("/tenants?collection=Tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is False
        assert body["collection"] == "Tenants"
        assert body["source"] == "client"


class TestTenantsCollectionNameValidation:
    """Guard against AQL identifier injection via the ``?collection=`` param.

    The resolved collection name is interpolated into the AQL string
    inside backticks (``FOR t IN `<name>` ``) — backticks are *not* a
    parameterisation boundary, so anything that isn't a valid ArangoDB
    collection identifier must be rejected at the edge, before the AQL
    is built. These tests pin the validator's rejection set so a future
    refactor (e.g. switching clients, moving the check downstream) can't
    silently re-open the injection surface.
    """

    @pytest.mark.parametrize(
        "bad_name",
        [
            "Tenant`+RETURN+1+//",  # backtick break-out (the original attack)
            "Tenant; DROP Tenant",  # punctuation + keywords
            "Tenant\nRETURN 1",  # embedded newline
            "Tenant RETURN 1",  # embedded space
            "1Tenant",  # starts with a digit
            "-Tenant",  # starts with a hyphen
            "Tenant/other",  # slash (collection separator char)
            "Tenant.other",  # dot
            "T" * 257,  # length cap (regex allows 1 + 255 trailing)
            "Tenant$",  # dollar sign
            "Tenant'",  # single quote
            'Tenant"',  # double quote
        ],
    )
    def test_rejects_non_identifier_collection_names(self, bad_name: str):
        db = _install_session(has_collection=True)
        resp = client.get("/tenants", params={"collection": bad_name})
        assert resp.status_code == 400, f"{bad_name!r} should be rejected"
        assert "Invalid collection name" in resp.json()["detail"]
        # Crucially: we never reached the DB. If we had, the backtick
        # escape would already be inside the AQL string.
        db.aql.execute.assert_not_called()
        db.has_collection.assert_not_called()

    @pytest.mark.parametrize(
        "ok_name",
        [
            "Tenant",  # bare happy path
            "Tenants",  # plural
            "tenant_v2",  # lowercase + underscore + digit
            "_system_tenants",  # leading underscore (system collections)
            "TENANT-CATALOG",  # hyphen + upper case
            "T" * 256,  # maximum length
            "a",  # minimum length
        ],
    )
    def test_accepts_valid_collection_names(self, ok_name: str):
        _install_session(has_collection=True, expected_collection=ok_name)
        resp = client.get("/tenants", params={"collection": ok_name})
        assert resp.status_code == 200, resp.json()
        assert resp.json()["collection"] == ok_name
