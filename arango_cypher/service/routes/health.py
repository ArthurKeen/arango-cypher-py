"""Liveness / readiness probe endpoint."""

from __future__ import annotations

from functools import cache
from importlib.metadata import PackageNotFoundError, version

from ..app import app


@cache
def _analyzer_version() -> str | None:
    """The installed schema analyzer's version — what decides how a database's
    schema is read, so bug reports carry it. ``None`` when it is not installed."""
    try:
        return version("arangodb-schema-analyzer")
    except PackageNotFoundError:
        return None


# Liveness / readiness probe for container orchestrators (Arango Platform's
# Container Manager, Kubernetes, docker-compose healthchecks, etc.). Cheap,
# unauthenticated, no DB call -- returning 200 proves the process is up and
# the FastAPI event loop is serving. Actual DB reachability is tested per
# session via POST /connect, which is where a connection failure should
# surface (not at startup).
@app.get("/health")
def health() -> dict[str, str | None]:
    return {
        "status": "ok",
        "service": "arango-cypher-py",
        "version": app.version,
        "analyzer_version": _analyzer_version(),
    }
