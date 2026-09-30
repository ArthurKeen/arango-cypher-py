"""The service must report the version pyproject declares.

It reported a hardcoded 0.1.0 while pyproject said 0.2.0, so /openapi.json and
/health misreported every deployed build, and the BYOC deploy's live-version
check (arango-byoc.toml version-probe) could never pass.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from arango_cypher.service import app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_openapi_reports_the_pyproject_version() -> None:
    assert TestClient(app).get("/openapi.json").json()["info"]["version"] == _declared()


def test_health_reports_the_pyproject_version() -> None:
    assert TestClient(app).get("/health").json()["version"] == _declared()
