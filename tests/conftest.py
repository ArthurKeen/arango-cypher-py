from __future__ import annotations

import os
from pathlib import Path

# Before any test module imports arango_cypher.service: the service loads the
# repo .env at import, which names a real cluster, and its ARANGO_DB /
# ARANGO_URL then became the target of tests that read those variables.
# Tests get connection settings only from the environment they are run with.
# Live runs say so explicitly: `set -a; source .env; set +a; RUN_LIVE=1 ...`.
os.environ.setdefault("ARANGO_CYPHER_NO_DOTENV", "1")

import pytest

from tests.helpers.corpus import CorpusCase, load_all_cases


@pytest.fixture(scope="session")
def corpus_cases() -> list[CorpusCase]:
    cases_dir = Path(__file__).parent / "fixtures" / "cases"
    return load_all_cases(cases_dir)


@pytest.fixture(autouse=True)
def _reset_tenant_plan_cache():
    """Isolate the module-global Layer-5 plan-shape cache (MT-6) per test.

    The certification LRU lives at module scope in
    ``arango_cypher.tenant_plan_validator``; without a reset a shape
    certified by one test could turn a later test's real-EXPLAIN into a
    silent cache hit (skipping the round-trip the later test may assert
    on). Clearing before each test keeps validator behaviour
    deterministic regardless of order.
    """
    from arango_cypher.tenant_plan_validator import reset_plan_cache

    reset_plan_cache()
    yield


@pytest.fixture(params=["legacy", "shared"])
def nl_engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once per NL→Cypher loop (issue #24): the original one and
    the shared ``NLQueryEngine``. Modules whose tests are the contract for both
    paths opt in with ``pytestmark = pytest.mark.usefixtures("nl_engine")``."""
    from arango_cypher.nl2cypher.engine_path import ENGINE_ENV

    monkeypatch.setenv(ENGINE_ENV, request.param)
    return str(request.param)
