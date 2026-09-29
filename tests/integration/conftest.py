from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

try:
    from arango import ArangoClient
except ImportError:
    ArangoClient = None  # type: ignore[misc, assignment]


#: Keys that choose *which database* the tier talks to. Never taken from .env:
#: the repo .env points at a real cluster (it is what the service and the BYOC
#: deploy read), and integration fixtures create and drop databases. Following
#: the documented ``RUN_INTEGRATION=1 pytest -m integration`` with those keys
#: loaded aims the whole tier at that cluster — it failed safe only because
#: the tests read ARANGO_PASS while .env spells it ARANGO_PASSWORD. Set the
#: target explicitly in the environment to point the tier anywhere else.
_CONNECTION_TARGET_KEYS = frozenset(
    {
        "ARANGO_URL",
        "ARANGO_ENDPOINT",
        "ARANGO_HOST",
        "ARANGO_PORT",
        "ARANGO_USER",
        "ARANGO_USERNAME",
        "ARANGO_PASS",
        "ARANGO_PASSWORD",
        "ARANGO_DB",
        "ARANGO_DATABASE",
    }
)


def _load_dotenv_if_present(path: Path | None = None) -> None:
    """
    Minimal .env loader for local integration runs.
    We intentionally avoid introducing dotenv dependencies this early.

    Connection-target keys (:data:`_CONNECTION_TARGET_KEYS`) are skipped, so
    a credential file for a real cluster never becomes the test target.
    """
    p = path or Path(__file__).resolve().parents[2] / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and k not in os.environ and k not in _CONNECTION_TARGET_KEYS:
            os.environ[k] = v


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
    return host in _LOOPBACK_HOSTS


@pytest.fixture(scope="session", autouse=True)
def _integration_targets_a_local_database_only() -> None:
    """Refuse a remote ARANGO_URL for the integration tier unless RUN_LIVE=1.

    Skipping connection keys in the loader above is not enough on its own:
    ``arango_cypher.service`` calls ``load_dotenv()`` at import, and test
    collection imports it, so the repo .env's real-cluster ARANGO_URL can
    reach os.environ anyway. This checks the *effective* target, wherever it
    came from, before any fixture creates or drops a database. Session-scoped
    and autouse, so it runs ahead of every module fixture.
    """
    refusal = remote_target_refusal(os.environ)
    if refusal:
        pytest.fail(refusal, pytrace=False)


def remote_target_refusal(env: Any) -> str | None:
    """Why the integration tier must not run against *env*'s target, or None."""
    if env.get("RUN_INTEGRATION") != "1":
        return None
    url = env.get("ARANGO_URL", "")
    if url and not _is_loopback(url) and env.get("RUN_LIVE") != "1":
        return (
            f"integration tests would run against {url}, which is not a local database; "
            "these fixtures create and drop databases. Point ARANGO_URL at the docker "
            "compose instance (http://localhost:28529), or set RUN_LIVE=1 to target a "
            "remote cluster deliberately."
        )
    return None


def pytest_collection_modifyitems(config, items):
    _load_dotenv_if_present()
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="Set RUN_INTEGRATION=1 to run integration tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def arango_pytest_url() -> str:
    """
    Start ArangoDB via ``docker-compose.pytest.yml`` on host port **28530**, wait until
    healthy, yield base URL, then ``docker compose down`` (project ``arango_cypher_pytest``).

    Requires ``RUN_INTEGRATION=1`` and a working Docker daemon. Skips when unavailable.
    """
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")

    if ArangoClient is None:
        pytest.skip("python-arango not installed")

    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("Docker not available")

    root = Path(__file__).resolve().parents[2]
    compose_file = "docker-compose.pytest.yml"
    project = "arango_cypher_pytest"
    up = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "-p",
        project,
        "up",
        "-d",
    ]
    down = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "-p",
        project,
        "down",
    ]

    subprocess.run(up, cwd=root, check=True, capture_output=True, text=True)

    url = "http://127.0.0.1:28530"
    user, pw = "root", "openSesame"
    deadline = time.time() + 120
    last_err: BaseException | None = None
    while time.time() < deadline:
        try:
            client = ArangoClient(hosts=url)
            db = client.db("_system", username=user, password=pw)
            if db.version():
                break
        except Exception as e:
            last_err = e
        time.sleep(1)
    else:
        subprocess.run(down, cwd=root, capture_output=True, text=True)
        raise AssertionError(f"ArangoDB did not become ready at {url}") from last_err

    yield url

    subprocess.run(down, cwd=root, capture_output=True, text=True)
