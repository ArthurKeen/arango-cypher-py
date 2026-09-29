"""Helpers for opt-in live-execution tests (WP-V1).

Live tests execute translated AQL against a *real* remote ArangoDB so we can
assert result *shape* (path vs scalar vs grouped row), not just transpile
success. They run only on explicit consent: :func:`require_live_db` skips unless
``RUN_LIVE=1``, matching the repo's ``RUN_INTEGRATION`` / ``RUN_CROSS`` /
``RUN_TCK`` convention. It then also skips when ``ARANGO_URL`` is unset or the
server is unreachable.

Credential *presence* is deliberately not consent. ``arango_cypher.service``
calls ``load_dotenv()`` at import time, so any test that imports the service
inherits a developer's repo-root ``.env``. Before this gate, configuring real
credentials for an unrelated purpose — a BYOC deploy, say — silently switched
these tests on against whatever database ``.env`` named, where the FinReflectKG
data does not exist, and "the suite is green" came to depend on who ran it.

Run them with, e.g.::

    set -a; source .env; set +a
    RUN_LIVE=1 .venv/bin/python -m pytest tests/test_live_finreflectkg_execution.py -q
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Server-side cap so a pathological query can never hang the suite. Every live
# query here is LIMIT-bounded with an unlabeled/abundant endpoint and is
# expected to finish well under a second, so this only fires on genuine trouble.
DEFAULT_MAX_RUNTIME_SECONDS = 15.0


def _verify_ssl() -> bool:
    raw = os.environ.get("ARANGO_VERIFY_SSL", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def live_opted_in() -> bool:
    """True only when the caller has explicitly asked for live tests."""
    return os.environ.get("RUN_LIVE", "").strip() == "1"


def require_live_db(database: str):
    """Return a connected ``StandardDatabase`` or skip the test.

    Skips (never fails) unless ``RUN_LIVE=1``, and then when ``ARANGO_URL`` is
    absent or the server/database is unreachable — so the default suite behaves
    identically whether or not a developer has credentials configured.
    """
    if not live_opted_in():
        pytest.skip("live tests are opt-in: set RUN_LIVE=1")
    url = os.environ.get("ARANGO_URL")
    if not url:
        pytest.skip("ARANGO_URL not set; live execution tests skipped")

    try:
        from arango import ArangoClient
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"python-arango unavailable: {exc}")

    client = ArangoClient(hosts=url)
    db = client.db(
        database,
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASSWORD", ""),
        verify=_verify_ssl(),
    )
    try:
        db.version()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live ArangoDB {url}/{database} unreachable: {exc}")
    return db


def execute_translated(
    db: Any,
    cypher: str,
    bundle: Any,
    *,
    max_runtime: float = DEFAULT_MAX_RUNTIME_SECONDS,
) -> tuple[list[Any], str]:
    """Translate ``cypher`` with ``bundle`` and run the AQL on ``db``.

    Returns ``(rows, aql)``. The AQL is returned too so failing assertions can
    report exactly what executed.
    """
    from arango_cypher.api import translate

    result = translate(cypher, mapping=bundle)
    try:
        cursor = db.aql.execute(result.aql, bind_vars=result.bind_vars, max_runtime=max_runtime)
    except TypeError:
        # Older python-arango without ``max_runtime`` support.
        cursor = db.aql.execute(result.aql, bind_vars=result.bind_vars)
    return list(cursor), result.aql
