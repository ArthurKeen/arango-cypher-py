"""Platform sessions — connect as the user the ArangoDB platform signed in.

On the platform the gateway forwards the browser's platform JWT as
``Authorization: Bearer`` and the operator injects the coordinator as
``ARANGO_DEPLOYMENT_ENDPOINT``; the Workbench opens a session from those two
instead of showing a login form (``arango_cypher/service/platform_auth.py``).

The fakes mirror python-arango's real signatures — ``ArangoClient(hosts=…)``
and ``ArangoClient.db(name, username, password, verify, auth_method,
user_token, superuser_token)`` — and the failures are real python-arango
exception objects, so a driver change breaks these tests rather than passing
them against an imagined API.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
import requests
from arango import ArangoClient
from arango.exceptions import ServerVersionError
from arango.request import Request
from arango.response import Response
from fastapi.testclient import TestClient

from arango_cypher.service import platform_auth
from tests.helpers.service_reload import fresh_service, patched_arango_client

ENDPOINT = "http://coordinator.cluster.svc:8529"
MOUNT = "/_service/uds/_db/AIM/arango-cypher-py"


def _platform_jwt(user: str, *, ttl: int = 3600, issuer: str = "arangodb") -> str:
    """A token shaped like the ones ArangoDB issues (``iss``/``iat``/``exp``).

    The signing secret is irrelevant: python-arango decodes without
    verifying, and the coordinator — faked here — is what checks it.
    """
    now = int(time.time())
    claims = {"iss": issuer, "iat": now - 10, "exp": now + ttl, "preferred_username": user}
    return jwt.encode(claims, "test-only-secret-padded-to-32-bytes!", algorithm="HS256")


JWT_A = _platform_jwt("alice")
JWT_B = _platform_jwt("alice-rotated")


def _server_error(status: int, error_num: int, message: str) -> ServerVersionError:
    body = f'{{"error":true,"code":{status},"errorNum":{error_num},"errorMessage":"{message}"}}'
    resp = Response("get", f"{ENDPOINT}/_api/version", {}, status, message, body)
    return ServerVersionError(resp, Request("get", "/_api/version"))


class _FakeDb:
    """The slice of ``StandardDatabase`` the platform path and ``/graphs`` touch."""

    def __init__(self, name: str, *, fail: BaseException | None, accessible: list[str]):
        self._name = name
        self._fail = fail
        self._accessible = accessible

    @property
    def name(self) -> str:
        return self._name

    def version(self) -> str:
        if self._fail is not None:
            raise self._fail
        return "3.12.4"

    def databases_accessible_to_user(self) -> list[str]:
        return list(self._accessible)

    def graphs(self) -> list[dict[str, Any]]:
        return []


class _Recorder:
    """Every client built and every ``db()`` opened, in order."""

    def __init__(self, *, fail: BaseException | None = None, accessible: list[str] | None = None):
        self.fail = fail
        self.accessible = accessible if accessible is not None else ["AIM", "_system", "FinReflectKG"]
        self.clients: list[Any] = []
        self.opened: list[dict[str, Any]] = []

    def factory(self) -> type:
        recorder = self

        class _FakeClient:
            def __init__(self, hosts: str | list[str] = "http://127.0.0.1:8529", **_kwargs: Any):
                self.hosts = hosts
                self.closed = False
                recorder.clients.append(self)

            def db(
                self,
                name: str = "_system",
                username: str = "root",
                password: str = "",
                verify: bool = False,
                auth_method: str = "basic",
                user_token: str | None = None,
                superuser_token: str | None = None,
            ) -> _FakeDb:
                if auth_method == "jwt":
                    # The real client's local token check — decode only, no
                    # request — so a malformed or expired token fails here
                    # exactly as it does in production.
                    ArangoClient(hosts="http://127.0.0.1:9").db(
                        name, auth_method="jwt", user_token=user_token
                    )
                recorder.opened.append(
                    {
                        "name": name,
                        "username": username,
                        "password": password,
                        "auth_method": auth_method,
                        "user_token": user_token,
                    }
                )
                return _FakeDb(name, fail=recorder.fail, accessible=recorder.accessible)

            def close(self) -> None:
                self.closed = True

        return _FakeClient


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", ENDPOINT + "/")
    monkeypatch.setenv("ROOT_PATH", MOUNT)
    monkeypatch.delenv("ARANGO_CYPHER_PLATFORM_AUTH", raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(fresh_service().app)


def _sessions() -> dict[str, Any]:
    return fresh_service()._sessions


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# platform_auth helpers
# ---------------------------------------------------------------------------


class TestPlatformEndpoint:
    def test_operator_endpoint_wins_over_arango_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", ENDPOINT + "/")
        monkeypatch.setenv("ARANGO_URL", "https://public.example:8529")
        assert platform_auth.platform_endpoint() == ENDPOINT

    def test_falls_back_to_arango_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARANGO_DEPLOYMENT_ENDPOINT", raising=False)
        monkeypatch.setenv("ARANGO_URL", "https://public.example:8529/")
        assert platform_auth.platform_endpoint() == "https://public.example:8529"

    def test_none_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARANGO_DEPLOYMENT_ENDPOINT", raising=False)
        monkeypatch.setenv("ARANGO_URL", "   ")
        assert platform_auth.platform_endpoint() is None

    @pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
    def test_switch_disables_it(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", ENDPOINT)
        monkeypatch.setenv("ARANGO_CYPHER_PLATFORM_AUTH", value)
        assert platform_auth.platform_endpoint() is None


class TestMountDatabase:
    @pytest.mark.parametrize(
        ("root_path", "expected"),
        [
            ("/_service/uds/_db/AIM/arango-cypher-py", "AIM"),
            ("/_service/uds/_db/AIM", "AIM"),
            ("/_service/uds/_db/my%20db/app/", "my db"),
            ("/_service/uds/_global/arango-cypher-py", None),
            ("", None),
            ("/somewhere/_service/uds/_db/AIM/app", None),
        ],
    )
    def test_parses_the_mount(self, root_path: str, expected: str | None) -> None:
        assert platform_auth.mount_database(root_path) == expected

    def test_mount_database_wins_then_arango_db_then_system(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROOT_PATH", MOUNT)
        monkeypatch.setenv("ARANGO_DB", "Other")
        assert platform_auth.default_database() == "AIM"
        monkeypatch.setenv("ROOT_PATH", "/_service/uds/_global/app")
        assert platform_auth.default_database() == "Other"
        monkeypatch.delenv("ARANGO_DB")
        assert platform_auth.default_database() == "_system"


class TestChooseDatabase:
    @pytest.mark.parametrize(
        ("accessible", "expected"),
        [
            (None, "AIM"),  # listing failed: nothing better is known
            (["AIM", "_system"], "AIM"),
            (["IAM", "_system"], "_system"),
            (["IAM", "JLR"], "IAM"),
            ([], "AIM"),
        ],
    )
    def test_prefers_the_mount_database_the_user_can_open(
        self, monkeypatch: pytest.MonkeyPatch, accessible: list[str] | None, expected: str
    ) -> None:
        monkeypatch.setenv("ROOT_PATH", MOUNT)
        assert platform_auth.choose_database(accessible) == expected


# ---------------------------------------------------------------------------
# GET /connect/platform
# ---------------------------------------------------------------------------


class TestPlatformStatus:
    def test_available_behind_the_gateway(self, client: TestClient, platform_env: None) -> None:
        body = client.get("/connect/platform", headers=_bearer(JWT_A)).json()
        assert body == {"available": True, "database": "AIM", "reason": None}

    def test_discloses_neither_endpoint_nor_token(self, client: TestClient, platform_env: None) -> None:
        text = client.get("/connect/platform", headers=_bearer(JWT_A)).text
        assert "coordinator" not in text
        assert JWT_A not in text

    def test_unavailable_without_a_forwarded_login(self, client: TestClient, platform_env: None) -> None:
        body = client.get("/connect/platform").json()
        assert body["available"] is False
        assert "no platform login" in body["reason"]

    def test_unavailable_without_an_endpoint(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ARANGO_DEPLOYMENT_ENDPOINT", raising=False)
        monkeypatch.delenv("ARANGO_URL", raising=False)
        body = client.get("/connect/platform", headers=_bearer(JWT_A)).json()
        assert body["available"] is False
        assert "ARANGO_DEPLOYMENT_ENDPOINT" in body["reason"]

    def test_unavailable_when_switched_off(
        self, client: TestClient, platform_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_PLATFORM_AUTH", "off")
        body = client.get("/connect/platform", headers=_bearer(JWT_A)).json()
        assert body["available"] is False
        assert "disabled" in body["reason"]


# ---------------------------------------------------------------------------
# POST /connect/platform
# ---------------------------------------------------------------------------


class TestPlatformConnect:
    def test_opens_the_mount_database_as_the_forwarded_user(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert rec.clients[0].hosts == ENDPOINT
        # The user's databases are listed from _system first, then the chosen
        # one is opened — every handle authenticated with the forwarded JWT.
        assert rec.opened == [
            {
                "name": "_system",
                "username": "root",
                "password": "",
                "auth_method": "jwt",
                "user_token": JWT_A,
            },
            {"name": "AIM", "username": "root", "password": "", "auth_method": "jwt", "user_token": JWT_A},
        ]
        # The user's own database list, sorted — not _system.databases().
        assert body["databases"] == ["AIM", "FinReflectKG", "_system"]
        assert body["database"] == "AIM"
        session = _sessions()[body["token"]]
        assert session.platform_token == JWT_A
        assert session.db.name == "AIM"

    def test_a_mount_database_the_user_cannot_open_falls_back_to_system(
        self, client: TestClient, platform_env: None
    ) -> None:
        # Seen on prod.demo: the instance is mounted in a database that does
        # not exist, so opening it would fail the session outright.
        rec = _Recorder(accessible=["IAM", "_system"])
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.status_code == 200, resp.text
        assert resp.json()["database"] == "_system"
        assert rec.opened[-1]["name"] == "_system"

    def test_without_system_access_the_first_database_is_opened(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder(accessible=["IAM", "JLR"])
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.json()["database"] == "IAM"

    def test_a_named_database_is_opened_as_is(self, client: TestClient, platform_env: None) -> None:
        # No fallback for an explicit choice: the user asked for that one.
        rec = _Recorder(accessible=["IAM"])
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={"database": "JLR"}, headers=_bearer(JWT_A))
        assert resp.json()["database"] == "JLR"
        assert [o["name"] for o in rec.opened] == ["JLR"]

    def test_opens_the_requested_database(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={"database": "FinReflectKG"}, headers=_bearer(JWT_A))
        assert resp.status_code == 200, resp.text
        assert rec.opened[0]["name"] == "FinReflectKG"

    def test_current_database_is_listed_even_if_the_listing_omits_it(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder(accessible=[])
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.json()["databases"] == ["AIM"]

    def test_refused_without_a_forwarded_login(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "platform_session_unavailable"
        assert rec.clients == []

    def test_rejected_login_is_a_401_and_leaves_no_session(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder(fail=_server_error(401, 11, "not authorized to execute this request"))
        before = set(_sessions())
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.status_code == 401
        assert set(_sessions()) == before
        assert rec.clients[0].closed is True

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            ("forged.token.value", "not a usable ArangoDB token"),
            (_platform_jwt("alice", ttl=-60), "expired"),
        ],
        ids=["malformed", "expired"],
    )
    def test_unusable_token_is_a_401_not_a_crash(
        self, client: TestClient, platform_env: None, token: str, reason: str
    ) -> None:
        # Found live: a forged bearer made python-arango's local decode raise
        # outside the handler — an unhandled 500.
        rec = _Recorder()
        before = set(_sessions())
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(token))
        assert resp.status_code == 401, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "platform_login_rejected"
        assert reason in detail["message"]
        assert token not in resp.text
        assert set(_sessions()) == before
        assert rec.clients[0].closed is True

    def test_a_foreign_issuer_is_left_to_the_coordinator(
        self, client: TestClient, platform_env: None
    ) -> None:
        # python-arango decodes without verifying the signature, and PyJWT
        # then skips the issuer check too: whether the platform's token is
        # acceptable is the coordinator's call (its version() here), not ours.
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            resp = client.post(
                "/connect/platform", json={}, headers=_bearer(_platform_jwt("alice", issuer="platform"))
            )
        assert resp.status_code == 200, resp.text

    def test_unknown_database_is_a_404(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder(fail=_server_error(404, 1228, "database not found"))
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={"database": "Nope"}, headers=_bearer(JWT_A))
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_database"
        assert "Nope" in resp.json()["detail"]["message"]

    def test_unreachable_cluster_is_a_502_that_names_the_cause(
        self, client: TestClient, platform_env: None
    ) -> None:
        cause = requests.exceptions.ConnectionError("Name or service not known")
        failure = ConnectionAbortedError("Can't connect to host(s) within limit (3)")
        failure.__cause__ = cause
        rec = _Recorder(fail=failure)
        with patched_arango_client(rec.factory()):
            resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["error"] == "cluster_unreachable"
        assert "Name or service not known" in detail["message"]
        assert JWT_A not in resp.text


# ---------------------------------------------------------------------------
# A platform session follows the caller's current JWT
# ---------------------------------------------------------------------------


class TestPlatformIdentity:
    def _connect(self, client: TestClient, rec: _Recorder) -> str:
        resp = client.post("/connect/platform", json={}, headers=_bearer(JWT_A))
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    def test_rebinds_to_a_rotated_jwt(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            resp = client.get("/graphs", headers={"X-Arango-Session": token, **_bearer(JWT_B)})
        assert resp.status_code == 200, resp.text
        assert rec.opened[-1] == {
            "name": "AIM",
            "username": "root",
            "password": "",
            "auth_method": "jwt",
            "user_token": JWT_B,
        }
        assert _sessions()[token].platform_token == JWT_B

    def test_same_jwt_reuses_the_handle(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            opened_at_connect = len(rec.opened)
            client.get("/graphs", headers={"X-Arango-Session": token, **_bearer(JWT_A)})
        assert len(rec.opened) == opened_at_connect

    def test_request_without_the_jwt_is_refused(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            resp = client.get("/graphs", headers={"X-Arango-Session": token})
        assert resp.status_code == 401
        assert resp.json()["detail"] == fresh_service().security.PLATFORM_TOKEN_MISSING

    def test_an_unusable_rotated_token_is_refused_and_not_adopted(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder()
        expired = _platform_jwt("alice", ttl=-60)
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            resp = client.get("/graphs", headers={"X-Arango-Session": token, **_bearer(expired)})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"]
        assert _sessions()[token].platform_token == JWT_A

    def test_session_token_as_bearer_leaves_no_room_for_the_jwt(
        self, client: TestClient, platform_env: None
    ) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            resp = client.get("/graphs", headers=_bearer(token))
        assert resp.status_code == 401

    def test_body_session_token_cannot_reach_a_platform_session(
        self, client: TestClient, platform_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arango_cypher.nl2cypher as nl2cypher

        seen: list[Any] = []

        class _Stop(Exception):
            pass

        def _capture(question: str, **kwargs: Any) -> Any:
            seen.append(kwargs.get("db"))
            raise _Stop

        monkeypatch.setattr(nl2cypher, "nl_to_cypher", _capture)
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            token = self._connect(client, rec)
            with pytest.raises(_Stop):
                client.post("/nl2cypher", json={"question": "who?", "use_llm": False, "session_token": token})
        assert seen == [None]

    def test_password_sessions_are_unaffected(self, client: TestClient, platform_env: None) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            resp = client.post(
                "/connect",
                json={"url": "http://127.0.0.1:8529", "database": "AIM", "username": "u", "password": "p"},
            )
            assert resp.status_code == 200, resp.text
            token = resp.json()["token"]
            graphs = client.get("/graphs", headers={"X-Arango-Session": token})
        assert graphs.status_code == 200
        assert _sessions()[token].platform_token is None
