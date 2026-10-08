"""The platform's injected CA and integration sidecar.

On BYOC the operator injects the endpoint's CA (``ARANGO_DEPLOYMENT_CA``) and
an integration sidecar (``INTEGRATION_HTTP_ADDRESS[_FULL]``) that names a
token's user (``/_integration/authn/v1/identity``) and mints tokens for a user
(``/_integration/authn/v1/createToken``). See
``arango_cypher/service/platform_auth.py``.

The sidecar here is a real HTTP server on localhost, so the client code runs
its real requests calls; the database handles for minted tokens are real
python-arango objects (which make no request until used).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient

from arango_cypher.service import platform_auth
from tests.helpers.service_reload import fresh_service, patched_arango_client
from tests.test_platform_session import ENDPOINT, MOUNT, _platform_jwt, _Recorder

JWT_ALICE = _platform_jwt("alice")


def _minted(user: str, lifetime_s: int) -> str:
    """A token python-arango's own pre-check refuses (it has no ``exp``
    claim), so only the unchecked path can use it. The fake sidecar knows its
    lifetime; nothing here depends on what claims a real minted token has."""
    return jwt.encode(
        {"iss": "arangodb", "preferred_username": user, "n": f"{user}-{lifetime_s}-{time.time_ns()}"},
        "sidecar-test-secret-padded-to-32-bytes!",
        algorithm="HS256",
    )


class FakeSidecar:
    """The two sidecar endpoints, plus ``/_api/version`` standing in for the
    coordinator. Tokens it knows: the forwarded login, and whatever it minted."""

    def __init__(self) -> None:
        self.users: dict[str, str] = {JWT_ALICE: "alice"}
        self.minted: list[dict[str, Any]] = []
        self.identity_calls = 0
        self.fail_create = False
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _caller(self) -> str | None:
                auth = self.headers.get("Authorization", "")
                return sidecar.users.get(auth[7:]) if auth.lower().startswith("bearer ") else None

            def do_GET(self) -> None:
                if self.path == "/_integration/authn/v1/identity":
                    sidecar.identity_calls += 1
                    user = self._caller()
                    self._send(200, {"user": user}) if user else self._send(401, {"error": "unknown token"})
                elif self.path == "/_api/version":
                    self._send(200, {"version": "3.12.4"}) if self._caller() else self._send(
                        401, {"error": True}
                    )
                else:
                    self._send(404, {})

            def do_POST(self) -> None:
                if self.path != "/_integration/authn/v1/createToken":
                    self._send(404, {})
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sidecar.minted.append(body)
                if sidecar.fail_create:
                    self._send(500, {"error": "boom"})
                    return
                token = _minted(body["user"], int(body["lifetime"].rstrip("s")))
                sidecar.users[token] = body["user"]
                self._send(200, {"token": token})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSidecar]:
    fake = FakeSidecar()
    monkeypatch.setenv("INTEGRATION_HTTP_ADDRESS_FULL", fake.address)
    monkeypatch.setattr(platform_auth, "_identities", {})
    yield fake
    fake.close()


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ARANGO_DEPLOYMENT_CA",
        "ARANGO_CYPHER_PLATFORM_CA_BUNDLE",
        "ARANGO_CYPHER_PLATFORM_VERIFY_TLS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", ENDPOINT + "/")
    monkeypatch.setenv("ROOT_PATH", MOUNT)
    monkeypatch.delenv("ARANGO_CYPHER_PLATFORM_AUTH", raising=False)


PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----"


class TestInjectedCa:
    def test_a_ca_file_verifies_the_injected_endpoint(
        self, platform_env: None, monkeypatch, tmp_path
    ) -> None:
        ca = tmp_path / "ca.crt"
        ca.write_text(PEM)
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", str(ca))
        assert platform_auth.platform_tls_verify() == str(ca)
        assert "injected CA" in platform_auth.describe_tls_verify(str(ca))

    def test_injected_pem_text_is_written_to_a_file_once(self, platform_env: None, monkeypatch) -> None:
        monkeypatch.setattr(platform_auth, "_ca_files", {})
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", PEM)
        first = platform_auth.platform_tls_verify()
        assert isinstance(first, str) and open(first).read() == PEM + "\n"
        assert platform_auth.platform_tls_verify() == first

    def test_a_path_to_nothing_leaves_the_endpoint_unverified(
        self, platform_env: None, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", str(tmp_path / "missing.crt"))
        assert platform_auth.platform_tls_verify() is False

    def test_no_injected_ca_keeps_the_old_default(self, platform_env: None) -> None:
        assert platform_auth.platform_tls_verify() is False

    @pytest.mark.parametrize(("switch", "expected"), [("off", False), ("on", True)])
    def test_the_switch_still_overrides(
        self, platform_env: None, monkeypatch, tmp_path, switch, expected
    ) -> None:
        ca = tmp_path / "ca.crt"
        ca.write_text(PEM)
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", str(ca))
        monkeypatch.setenv("ARANGO_CYPHER_PLATFORM_VERIFY_TLS", switch)
        assert platform_auth.platform_tls_verify() is expected

    def test_a_configured_bundle_still_wins(self, platform_env: None, monkeypatch, tmp_path) -> None:
        ca = tmp_path / "ca.crt"
        ca.write_text(PEM)
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", str(ca))
        monkeypatch.setenv("ARANGO_CYPHER_PLATFORM_CA_BUNDLE", "/etc/arango/ca.pem")
        assert platform_auth.platform_tls_verify() == "/etc/arango/ca.pem"

    def test_connect_opens_the_client_with_the_injected_ca(
        self, platform_env: None, monkeypatch, tmp_path
    ) -> None:
        ca = tmp_path / "ca.crt"
        ca.write_text(PEM)
        monkeypatch.setenv("ARANGO_DEPLOYMENT_CA", str(ca))
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            TestClient(fresh_service().app).post(
                "/connect/platform", json={}, headers={"Authorization": f"Bearer {JWT_ALICE}"}
            )
        assert rec.clients[0].verify_override == str(ca)


class TestSidecarClient:
    @pytest.mark.parametrize(
        ("name", "value", "expected"),
        [
            ("INTEGRATION_HTTP_ADDRESS_FULL", "http://127.0.0.1:9203/", "http://127.0.0.1:9203"),
            ("INTEGRATION_HTTP_ADDRESS", "127.0.0.1:9203", "http://127.0.0.1:9203"),
        ],
    )
    def test_the_address_comes_from_the_injected_env(self, monkeypatch, name, value, expected) -> None:
        for env in platform_auth.SIDECAR_ADDRESS_ENVS:
            monkeypatch.delenv(env, raising=False)
        monkeypatch.setenv(name, value)
        assert platform_auth.sidecar_address() == expected

    def test_off_the_platform_there_is_no_sidecar(self, monkeypatch) -> None:
        for env in platform_auth.SIDECAR_ADDRESS_ENVS:
            monkeypatch.delenv(env, raising=False)
        assert platform_auth.sidecar_address() is None
        assert platform_auth.sidecar_identity(JWT_ALICE) is None
        assert platform_auth.background_token("alice") is None

    def test_identity_names_the_user_and_is_cached(self, sidecar: FakeSidecar) -> None:
        assert platform_auth.sidecar_identity(JWT_ALICE) == "alice"
        assert platform_auth.sidecar_identity(JWT_ALICE) == "alice"
        assert sidecar.identity_calls == 1

    def test_an_unknown_token_has_no_identity(self, sidecar: FakeSidecar) -> None:
        assert platform_auth.sidecar_identity(_platform_jwt("mallory")) is None

    def test_a_token_is_minted_for_the_named_user(self, sidecar: FakeSidecar) -> None:
        token = platform_auth.sidecar_token("alice", 600)
        assert sidecar.minted == [{"lifetime": "600s", "user": "alice"}]
        assert sidecar.users[token] == "alice"

    def test_never_mints_without_a_user(self, sidecar: FakeSidecar) -> None:
        with pytest.raises(platform_auth.SidecarError, match="without a user"):
            platform_auth.sidecar_token("", 600)
        assert platform_auth.background_token(None) is None
        assert sidecar.minted == []

    def test_a_sidecar_failure_is_reported_without_a_token(self, sidecar: FakeSidecar) -> None:
        sidecar.fail_create = True
        with pytest.raises(platform_auth.SidecarError, match="HTTP 500"):
            platform_auth.sidecar_token("alice", 600)
        assert platform_auth.background_token("alice") is None

    def test_the_lifetime_is_configurable_with_a_floor(self, sidecar: FakeSidecar, monkeypatch) -> None:
        monkeypatch.setenv("ARANGO_CYPHER_SIDECAR_TOKEN_LIFETIME_S", "5")
        platform_auth.background_token("alice")
        assert sidecar.minted[-1]["lifetime"] == "60s"

    def test_a_minted_token_is_sent_as_is(self) -> None:
        # Real python-arango: the jwt path refuses a token without exp (with a
        # bare KeyError); the unchecked path sends it, and the server decides.
        token = _minted("alice", 600)
        with pytest.raises(KeyError):
            ArangoClient(hosts="http://127.0.0.1:9").db("IAM", auth_method="jwt", user_token=token)
        db = platform_auth.open_minted_database(ArangoClient(hosts="http://127.0.0.1:9"), "IAM", token)
        assert db.name == "IAM" and db.conn._auth_header == f"bearer {token}"

    def test_token_facts_show_shape_not_values(self) -> None:
        facts = platform_auth.token_facts(_minted("alice", 600))
        assert facts["claims"] == ["iss", "n", "preferred_username"]
        assert facts["python_arango_accepts"] is False
        assert "alice" not in json.dumps(facts)
        assert platform_auth.token_facts(JWT_ALICE)["python_arango_accepts"] is True


class TestUnusableForwardedToken:
    def test_a_login_without_exp_is_a_401_not_a_500(self, platform_env: None) -> None:
        token = jwt.encode({"iss": "arangodb", "preferred_username": "alice"}, "x" * 40, algorithm="HS256")
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            r = TestClient(fresh_service().app).post(
                "/connect/platform", json={}, headers={"Authorization": f"Bearer {token}"}
            )
        assert r.status_code == 401
        assert "no 'exp' claim" in r.json()["detail"]["message"]


class TestPlatformSessionUser:
    def test_connect_names_the_user_and_the_session_keeps_them(
        self, platform_env: None, sidecar: FakeSidecar
    ) -> None:
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            r = TestClient(fresh_service().app).post(
                "/connect/platform", json={}, headers={"Authorization": f"Bearer {JWT_ALICE}"}
            )
        assert r.status_code == 200 and r.json()["user"] == "alice"
        assert fresh_service()._sessions[r.json()["token"]].platform_user == "alice"

    def test_without_a_sidecar_the_user_is_unknown(self, platform_env: None, monkeypatch) -> None:
        for env in platform_auth.SIDECAR_ADDRESS_ENVS:
            monkeypatch.delenv(env, raising=False)
        rec = _Recorder()
        with patched_arango_client(rec.factory()):
            r = TestClient(fresh_service().app).post(
                "/connect/platform", json={}, headers={"Authorization": f"Bearer {JWT_ALICE}"}
            )
        assert r.status_code == 200 and r.json()["user"] is None


class TestBackgroundDatabase:
    def _session(self, *, user: str | None, platform: bool = True) -> Any:
        from arango_cypher.service.security import _Session

        client = ArangoClient(hosts="http://127.0.0.1:9")
        db = client.db("IAM", auth_method="jwt", user_token=JWT_ALICE)
        return _Session(
            token="s",
            db=db,
            client=client,
            platform_token=JWT_ALICE if platform else None,
            platform_user=user,
        )

    def test_a_known_user_gets_a_minted_token(self, sidecar: FakeSidecar) -> None:
        from arango_cypher.service.security import background_database

        session = self._session(user="alice")
        db = background_database(session)
        assert db is not session.db and db.name == "IAM"
        assert sidecar.minted == [{"lifetime": "3600s", "user": "alice"}]
        assert sidecar.users[db.conn._auth_header[7:]] == "alice"

    @pytest.mark.parametrize(("user", "platform"), [(None, True), ("alice", False)])
    def test_otherwise_the_session_handle_is_kept(self, sidecar: FakeSidecar, user, platform) -> None:
        from arango_cypher.service.security import background_database

        session = self._session(user=user, platform=platform)
        assert background_database(session) is session.db
        assert sidecar.minted == []

    def test_a_running_warm_does_not_mint_again(self, sidecar: FakeSidecar, monkeypatch) -> None:
        from arango_cypher.catalog import warm
        from arango_cypher.service.routes import schema

        started: list[Any] = []
        monkeypatch.setattr(warm, "schedule_warm", lambda db, graph=None: started.append(db) or True)
        monkeypatch.setattr(warm, "is_warming", lambda db_name, graph=None: bool(started))
        session = self._session(user="alice")
        assert schema._schedule_session_warm(session, None) is True
        assert schema._schedule_session_warm(session, None) is False
        assert len(sidecar.minted) == 1


class TestDiagnostics:
    def test_reports_what_the_platform_provides_without_secrets(
        self, platform_env: None, sidecar: FakeSidecar, monkeypatch
    ) -> None:
        monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", sidecar.address)
        r = TestClient(fresh_service().app).get(
            "/connect/platform/diagnostics", headers={"Authorization": f"Bearer {JWT_ALICE}"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["endpoint"]["injected"] is True
        assert body["tls"] == {
            "ca_injected": False,
            "ca_usable": False,
            "policy": "not verified",
            "direct_request": "HTTP 200",
        }
        assert body["forwarded_login"]["python_arango_accepts"] is True
        assert body["sidecar"]["identity_found"] is True
        assert body["sidecar"]["minted_token"]["claims"] == ["iss", "n", "preferred_username"]
        assert body["sidecar"]["minted_token_request"] == "HTTP 200"
        assert sidecar.minted == [{"lifetime": "60s", "user": "alice"}]
        text = r.text
        assert JWT_ALICE not in text and "alice" not in text

    def test_off_the_platform_it_reports_only_absences(self, monkeypatch) -> None:
        for env in (
            *platform_auth.SIDECAR_ADDRESS_ENVS,
            "ARANGO_DEPLOYMENT_ENDPOINT",
            "ARANGO_DEPLOYMENT_CA",
        ):
            monkeypatch.delenv(env, raising=False)
        body = TestClient(fresh_service().app).get("/connect/platform/diagnostics").json()
        assert body["endpoint"]["injected"] is False
        assert body["forwarded_login"] is None
        assert body["sidecar"] == {"address_injected": False}
