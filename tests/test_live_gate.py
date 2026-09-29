"""The live-test gate: credential presence must never be taken as consent.

``arango_cypher.service`` calls ``load_dotenv()`` at import, so a developer's
repo-root ``.env`` leaks into every test process that imports the service.
Before ``RUN_LIVE`` existed, configuring real credentials for an unrelated
reason silently turned five skipped tests into five failures against whatever
database ``.env`` named. These tests pin the gate so that cannot recur.
"""

from __future__ import annotations

import pytest

from tests.helpers import live_db


def test_credentials_without_opt_in_skip_before_any_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARANGO_URL set, RUN_LIVE unset: skip, and never touch the network."""
    monkeypatch.setenv("ARANGO_URL", "https://example.invalid:8529")
    monkeypatch.delenv("RUN_LIVE", raising=False)

    def _no_network(*_a, **_k):  # pragma: no cover - reaching here is the failure
        raise AssertionError("require_live_db connected without RUN_LIVE=1")

    monkeypatch.setattr("arango.ArangoClient", _no_network)

    with pytest.raises(pytest.skip.Exception, match="RUN_LIVE=1"):
        live_db.require_live_db("anything")


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
def test_only_the_exact_value_one_opts_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A deliberate switch, not a truthiness guess: only "1" enables live tests.

    Mirrors RUN_INTEGRATION / RUN_CROSS / RUN_TCK, all of which compare to "1".
    """
    monkeypatch.setenv("RUN_LIVE", value)

    assert live_db.live_opted_in() is False


def test_opt_in_with_value_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_LIVE", "1")

    assert live_db.live_opted_in() is True


def test_opt_in_without_a_url_still_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consent is necessary, not sufficient — no target still means skip."""
    monkeypatch.setenv("RUN_LIVE", "1")
    monkeypatch.delenv("ARANGO_URL", raising=False)

    with pytest.raises(pytest.skip.Exception, match="ARANGO_URL"):
        live_db.require_live_db("anything")


# -- the integration tier's target ----------------------------------------------


@pytest.mark.parametrize(
    ("env", "refused"),
    [
        ({"RUN_INTEGRATION": "1", "ARANGO_URL": "https://prod.example:8529"}, True),
        ({"RUN_INTEGRATION": "1", "ARANGO_URL": "https://prod.example:8529", "RUN_LIVE": "1"}, False),
        ({"RUN_INTEGRATION": "1", "ARANGO_URL": "http://localhost:28529"}, False),
        ({"RUN_INTEGRATION": "1", "ARANGO_URL": "http://127.0.0.1:28529"}, False),
        ({"RUN_INTEGRATION": "1"}, False),  # unset: tests default to localhost
        ({"ARANGO_URL": "https://prod.example:8529"}, False),  # tier not running
    ],
)
def test_integration_tier_refuses_a_remote_target_without_opt_in(env: dict[str, str], refused: bool) -> None:
    """The repo .env names a real cluster; integration fixtures drop databases."""
    from tests.integration.conftest import remote_target_refusal

    assert (remote_target_refusal(env) is not None) is refused


def test_dotenv_loader_never_imports_the_connection_target(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration.conftest import _CONNECTION_TARGET_KEYS, _load_dotenv_if_present

    for key in _CONNECTION_TARGET_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SOME_FLAG", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("ARANGO_URL=https://prod.example:8529\nARANGO_PASSWORD=secret\nSOME_FLAG=1\n")

    _load_dotenv_if_present(dotenv)

    import os

    assert "ARANGO_URL" not in os.environ
    assert "ARANGO_PASSWORD" not in os.environ
    assert os.environ.get("SOME_FLAG") == "1"
    monkeypatch.delenv("SOME_FLAG", raising=False)
