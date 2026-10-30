"""The ArangoDB databases the LDBC suites load and query: one per data set × layout.

Connection settings come from the environment (``ARANGO_URL``, ``ARANGO_USER``,
``ARANGO_PASS``) with the integration tier's defaults; the integration
conftest refuses a non-local ``ARANGO_URL`` unless ``RUN_LIVE=1``.
"""

from __future__ import annotations

import os
from typing import Any

from arango import ArangoClient

from tests.ldbc.datasets import DATASETS, fetch
from tests.ldbc.loader import Layout, LoadReport, load


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def db_name(dataset: str, layout: str) -> str:
    """``sf0.003-iso`` + ``pg`` → ``ldbc_sf0_003_iso_pg``."""
    return f"ldbc_{dataset.replace('.', '_').replace('-', '_')}_{layout}"


def connect(name: str, url: str | None = None, *, request_timeout: float = 60) -> Any:
    """The database *name* on *url* (default: ``ARANGO_URL``), created if missing.

    *request_timeout* is the client's read timeout in seconds; a caller that
    sets a server-side ``max_runtime`` must give the client more.
    """
    client = ArangoClient(
        hosts=url or _env("ARANGO_URL", "http://localhost:8529"), request_timeout=request_timeout
    )
    user, pw = _env("ARANGO_USER", "root"), _env("ARANGO_PASS", "openSesame")
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(name):
        sys_db.create_database(name)
    return client.db(name, username=user, password=pw)


def load_fresh(
    dataset: str, layout: Layout, url: str | None = None, *, request_timeout: float = 60
) -> tuple[Any, LoadReport]:
    """Connect to the data set × layout database on *url* and (re)load it from scratch."""
    db = connect(db_name(dataset, layout), url, request_timeout=request_timeout)
    return db, load(db, fetch(dataset), layout=layout, encoding=DATASETS[dataset].encoding)
