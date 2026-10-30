"""Unit tests for the LDBC benchmark runner and its report (no database)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import requests

from tests.ldbc.bench import (
    CLIENT_TIMEOUT_MARGIN_S,
    Settings,
    environment,
    load_databases,
    percentiles,
    plan_shape,
    time_execution,
)
from tests.ldbc.report import ATTRIBUTION, DISCLAIMER, check, compare, render


def test_percentiles_are_nearest_rank():
    stats = percentiles([float(x) for x in range(1, 101)])
    assert stats == {"n": 100, "p50": 50.0, "p95": 95.0, "p99": 99.0, "mean": 50.5, "min": 1.0, "max": 100.0}
    assert percentiles([]) is None
    assert percentiles([7.0])["p99"] == 7.0  # type: ignore[index]


_PLAN = {
    "nodes": [
        {"id": 1, "type": "SingletonNode", "estimatedCost": 1},
        {
            "id": 2,
            "type": "IndexNode",
            "collection": "person",
            "indexes": [{"name": "idx_person_id"}],
            "estimatedNrItems": 3,
        },
        {"id": 3, "type": "TraversalNode", "options": {"order": "bfs", "uniqueVertices": "global"}},
        {"id": 4, "type": "ReturnNode"},
    ],
    "rules": ["use-indexes", "move-filters-up"],
}


def test_a_plan_digest_ignores_estimates_and_ids():
    noisy = copy.deepcopy(_PLAN)
    noisy["nodes"][1]["estimatedNrItems"] = 999
    noisy["nodes"][0]["id"] = 42
    assert plan_shape(noisy)["digest"] == plan_shape(_PLAN)["digest"]


def test_a_plan_digest_changes_with_the_plan():
    scan = copy.deepcopy(_PLAN)
    scan["nodes"][1] = {"id": 2, "type": "EnumerateCollectionNode", "collection": "person"}
    shape = plan_shape(scan)
    assert shape["full_scans"] == ["person"]
    assert shape["digest"] != plan_shape(_PLAN)["digest"]
    assert plan_shape(_PLAN)["indexes"] == ["person:idx_person_id"]


class _Db:
    """Mirrors ``arango.database.Database.version(self, details: bool = False)``
    and ``Database.engine(self)``, as ArangoDB 3.12 answers them: the version
    details no longer name the storage engine."""

    def version(self, details: bool = False) -> Any:
        if not details:
            return "3.12.12"
        return {
            "server": "arango",
            "version": "3.12.12",
            "license": "community",
            "details": {"mode": "server"},
        }

    def engine(self) -> Any:
        return {"name": "rocksdb", "supports": {}}


def test_the_environment_records_what_the_numbers_depend_on():
    env = environment(_Db(), Settings(), {"bi": "sf0.003-iso"})
    assert env["server"]["version"] == "3.12.12"
    assert env["server"]["engine"] == "rocksdb"
    assert env["data"]["datasets"]["bi"]["content_digest"]
    assert set(env["mappings"]) == {"pg", "lpg"}
    assert len(env["git"]["sha"]) >= 7
    assert env["settings"]["repeats"] == Settings().repeats


def _result(p50: float = 12.5, digest: str = "aaaa", outcome: str = "ok") -> dict[str, Any]:
    cell: dict[str, Any] = {"operation": "IC2", "layout": "pg", "outcome": outcome, "bindings": 2}
    if outcome == "ok":
        cell |= {
            "rows": [20, 0],
            "server_ms": {
                "n": 10,
                "p50": p50,
                "p95": 20.0,
                "p99": 25.0,
                "mean": 13.0,
                "min": 9.0,
                "max": 25.0,
            },
            "client_ms": {
                "n": 10,
                "p50": p50 + 1,
                "p95": 21.0,
                "p99": 26.0,
                "mean": 14.0,
                "min": 10.0,
                "max": 26.0,
            },
            "translation_ms": {"cold": {"p50": 3.2}, "warm": {"p50": 0.01}},
            "plan": {"digest": digest},
        }
    env = environment(_Db(), Settings(), {"interactive_v2": "sf0.003-epoch-ms"})
    return {
        "schema_version": 1,
        "environment": env,
        "cells": [cell, {"operation": "IC14", "layout": "pg", "outcome": "translate_failed"}],
    }


def test_the_report_carries_the_disclaimer_and_conforms():
    result = _result()
    md = render(result)
    assert DISCLAIMER in md and ATTRIBUTION in md
    assert "| IC2 | pg | ok | 20/0 | 12.500 | 20.000 | 25.000 | 13.500 | 3.200 | 0.010 | aaaa |" in md
    assert check(md, result) == []


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda md: md.replace("12.500", "9.000", 1), "row not generated from the run"),
        (lambda md: md.replace(DISCLAIMER, ""), "disclaimer is missing"),
        (
            lambda md: "\n".join(line for line in md.splitlines() if "IC14" not in line),
            "row of the run missing",
        ),
    ],
)
def test_a_hand_edited_report_does_not_conform(edit, problem):
    result = _result()
    assert any(problem in p for p in check(edit(render(result)), result))


def test_compare_flags_plan_changes_p50_moves_and_outcomes():
    assert compare(_result(), _result()) == []
    assert compare(_result(), _result(p50=12.6)) == []  # within the factor
    assert any("plan changed" in f for f in compare(_result(), _result(digest="bbbb")))
    assert any("server p50" in f for f in compare(_result(), _result(p50=40.0)))
    assert any("outcome ok -> TIMEOUT" in f for f in compare(_result(), _result(outcome="TIMEOUT")))
    # Moves below the noise floor are not flagged.
    assert compare(_result(p50=0.5), _result(p50=2.0)) == []


class _TimingOutAql:
    """Mirrors ``arango.aql.AQL.execute(self, query, count=False, ..., bind_vars=None, ..., max_runtime=None,
    ..., cache=None)``; the HTTP read times out as python-arango's ``requests`` session reports it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(self, query: str, count: bool = False, bind_vars: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"query": query, "count": count, "bind_vars": bind_vars, **kwargs})
        raise requests.exceptions.ReadTimeout("HTTPConnectionPool(host='127.0.0.1'): Read timed out.")


class _SlowDb:
    def __init__(self) -> None:
        self.aql = _TimingOutAql()


def test_a_client_side_timeout_is_one_cells_timeout_not_the_runs_end():
    db = _SlowDb()
    out = time_execution(db, "FOR x IN c RETURN x", {}, warmup=1, repeats=3, timeout_s=60.0)
    assert out["outcome"] == "TIMEOUT" and out["timeout_s"] == 60.0
    assert out["detail"].startswith("client: ")
    assert len(db.aql.calls) == 1, "a timed-out binding is not retried"
    assert db.aql.calls[0]["max_runtime"] == 60.0 and db.aql.calls[0]["cache"] is False


def test_the_client_waits_longer_than_the_servers_max_runtime(monkeypatch):
    from tests.ldbc import arango_dbs

    seen: list[tuple[str, str, float]] = []

    def fake_load_fresh(
        dataset: str, layout: str, url: str | None = None, *, request_timeout: float = 60
    ) -> Any:
        seen.append((dataset, layout, request_timeout))
        return object(), None

    monkeypatch.setattr(arango_dbs, "load_fresh", fake_load_fresh)
    settings = Settings(layouts=("pg",), timeout_s=45.0)
    load_databases("http://127.0.0.1:1", settings)
    assert seen and all(t == 45.0 + CLIENT_TIMEOUT_MARGIN_S for _, _, t in seen)
    assert CLIENT_TIMEOUT_MARGIN_S > 0
