"""Query mining, slice 1: harvesting saved queries, the read-only guard,
running the source AQL under caps, and the result signature.

The fake database mirrors python-arango's real signatures —
``AQL.execute(query, count, batch_size, ..., bind_vars, ..., max_runtime)``,
``AQL.explain(query, all_plans, max_plans, opt_rules, bind_vars)``,
``Cursor.close(ignore_missing)`` — and raises real ``AQLQueryExecuteError``
objects, so a driver change breaks these tests instead of passing them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from arango.exceptions import AQLQueryExecuteError, AQLQueryExplainError
from arango.request import Request
from arango.response import Response

from arango_cypher.query_mining import binding
from arango_cypher.query_mining.binding import (
    ROW_CAP,
    SourceRejected,
    TimedOut,
    TooLarge,
    rank_start_collections,
    run_read_only,
    run_source,
)
from arango_cypher.query_mining.harvest import (
    CANVAS_ACTIONS,
    EDITOR_SAVED_QUERIES,
    SAVED_QUERIES,
    SavedQuery,
    harvest_saved_queries,
    plan_writes,
)
from arango_cypher.query_mining.signature import compare, signature_of


def _server_error(cls: type, code: int, num: int, message: str) -> Exception:
    """A real python-arango server error, populated as BaseConnection.prep_response does."""
    body = {"error": True, "code": code, "errorNum": num, "errorMessage": message}
    resp = Response("post", "http://db/_api/cursor", {}, code, "error", json.dumps(body))
    resp.body = body
    resp.error_code = num
    resp.error_message = message
    resp.is_success = False
    return cls(resp, Request("post", "/_api/cursor"))


class FakeCursor:
    def __init__(self, rows: list[Any]):
        self._rows = rows
        self.closed = False

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)

    def close(self, ignore_missing: bool = False) -> bool | None:
        self.closed = True
        return True


class FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def all(self, skip: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return list(self._docs)


Handler = Callable[[str, dict[str, Any]], list[Any]]


class FakeAQL:
    def __init__(self, handler: Handler, plan_nodes: Callable[[str], list[str]]):
        self._handler = handler
        self._plan_nodes = plan_nodes
        self.executed: list[tuple[str, dict[str, Any], float | None]] = []

    def execute(
        self,
        query: str,
        count: bool = False,
        batch_size: int | None = None,
        ttl: int | None = None,
        bind_vars: dict[str, Any] | None = None,
        full_count: bool | None = None,
        max_plans: int | None = None,
        optimizer_rules: list[str] | None = None,
        cache: bool | None = None,
        memory_limit: int = 0,
        fail_on_warning: bool | None = None,
        profile: bool | None = None,
        max_transaction_size: int | None = None,
        max_warning_count: int | None = None,
        intermediate_commit_count: int | None = None,
        intermediate_commit_size: int | None = None,
        satellite_sync_wait: int | None = None,
        stream: bool | None = None,
        skip_inaccessible_cols: bool | None = None,
        max_runtime: float | None = None,
        **_more: Any,
    ) -> FakeCursor:
        self.executed.append((query, dict(bind_vars or {}), max_runtime))
        return FakeCursor(self._handler(query, dict(bind_vars or {})))

    def explain(
        self,
        query: str,
        all_plans: bool = False,
        max_plans: int | None = None,
        opt_rules: list[str] | None = None,
        bind_vars: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"nodes": [{"type": t} for t in self._plan_nodes(query)]}


class FakeDb:
    def __init__(
        self,
        collections: dict[str, list[dict[str, Any]]] | None = None,
        *,
        handler: Handler | None = None,
        plan_nodes: Callable[[str], list[str]] | None = None,
        name: str = "IAM",
    ):
        self._collections = collections or {}
        self._name = name
        self.aql = FakeAQL(
            handler or (lambda q, b: []), plan_nodes or (lambda q: ["SingletonNode", "ReturnNode"])
        )

    @property
    def name(self) -> str:
        return self._name

    def has_collection(self, name: str) -> bool:
        return name in self._collections

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._collections[name])


def _q(**overrides: Any) -> SavedQuery:
    base: dict[str, Any] = {
        "source": SAVED_QUERIES,
        "key": "1",
        "name": "Q",
        "description": "",
        "aql": "FOR d IN things RETURN d",
        "bind_vars": {},
        "graph": "IAM_DEMO",
    }
    base.update(overrides)
    return SavedQuery(**base)


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


class TestHarvest:
    def _db(self) -> FakeDb:
        return FakeDb(
            {
                SAVED_QUERIES: [
                    {
                        "_key": "q1",
                        "name": "IAM — Q13 Circular trust",
                        "description": "3-cycles of CAN_ASSUME",
                        "queryText": "FOR e IN CAN_ASSUME RETURN e",
                        "bindVariables": {},
                        "graphId": "IAM_DEMO",
                    },
                    {
                        "_key": "q2",
                        "name": "Fetch edges of type @@edgeType (default)",
                        "description": "Fetch all edges",
                        "queryText": "FOR doc IN @@edgeType LIMIT 100 RETURN doc",
                        "bindVariables": {"@edgeType": "ASSOCIATES"},
                        "graphId": "IAM_DEMO",
                    },
                    {"_key": "q3", "name": "empty", "queryText": "  ", "graphId": "IAM_DEMO"},
                    {
                        "_key": "q4",
                        "name": "other graph",
                        "queryText": "RETURN 1",
                        "bindVariables": {},
                        "graphId": "AWS_Security_Docs_CorpusGraph",
                    },
                ],
                CANVAS_ACTIONS: [
                    {
                        "_key": "a1",
                        "name": "Accessible EC2s",
                        "description": "Find all EC2 instances this user can access",
                        "queryText": "FOR v IN 1..2 OUTBOUND @nodes[0] GRAPH 'IAM_DEMO' RETURN v",
                        "bindVariables": {"nodes": ""},
                        "graphId": "IAM_DEMO",
                    }
                ],
                EDITOR_SAVED_QUERIES: [
                    {
                        "_key": "e1",
                        "title": "Untitled-2",
                        "content": "RETURN 2",
                        "bindVariables": "{}",
                        "databaseName": "IAM",
                    },
                    {
                        "_key": "e2",
                        "title": "Roles by account",
                        "content": "FOR r IN aws_iam_role RETURN r",
                        "bindVariables": '{"n": 5}',
                        "databaseName": "IAM",
                    },
                    {"_key": "e3", "title": "elsewhere", "content": "RETURN 3", "databaseName": "OTHER"},
                    {"_key": "e4", "title": "bad bind", "content": "RETURN 4", "bindVariables": "[1,2]"},
                ],
            }
        )

    def test_reads_all_three_sources_scoped_to_the_graph(self) -> None:
        report = harvest_saved_queries(self._db(), graph="IAM_DEMO")
        names = [q.name for q in report.queries]
        assert names == [
            "IAM — Q13 Circular trust",
            "Fetch edges of type @@edgeType (default)",
            "Accessible EC2s",
            "Untitled-2",
            "Roles by account",
        ]

    def test_parses_bind_variables_from_objects_and_json_strings(self) -> None:
        by_name = {q.name: q for q in harvest_saved_queries(self._db()).queries}
        assert by_name["Fetch edges of type @@edgeType (default)"].bind_vars == {"@edgeType": "ASSOCIATES"}
        assert by_name["Roles by account"].bind_vars == {"n": 5}

    def test_flags_builtins_and_canvas_actions(self) -> None:
        by_name = {q.name: q for q in harvest_saved_queries(self._db()).queries}
        assert by_name["Fetch edges of type @@edgeType (default)"].builtin is True
        assert by_name["IAM — Q13 Circular trust"].builtin is False
        assert by_name["Accessible EC2s"].is_canvas_action is True

    def test_untitled_editor_saves_carry_no_description(self) -> None:
        by_name = {q.name: q for q in harvest_saved_queries(self._db()).queries}
        assert by_name["Untitled-2"].description == ""
        assert by_name["Roles by account"].description == "Roles by account"

    def test_skips_with_reasons_rather_than_guessing(self) -> None:
        skipped = dict(harvest_saved_queries(self._db()).skipped)
        assert skipped[f"{SAVED_QUERIES}/q3"] == "no queryText"
        assert skipped[f"{EDITOR_SAVED_QUERIES}/e4"] == "bindVariables is not a JSON object"

    def test_other_databases_editor_saves_are_ignored(self) -> None:
        names = {q.name for q in harvest_saved_queries(self._db()).queries}
        assert "elsewhere" not in names

    def test_duplicates_are_reported_once(self) -> None:
        doc = {"name": "dup", "queryText": "RETURN 1", "bindVariables": {}, "graphId": "G"}
        db = FakeDb({SAVED_QUERIES: [{**doc, "_key": "a"}, {**doc, "_key": "b", "name": "renamed"}]})
        report = harvest_saved_queries(db)
        assert [q.key for q in report.queries] == ["a"]
        assert report.skipped == [(f"{SAVED_QUERIES}/b", "duplicate of an earlier saved query")]

    def test_a_database_without_the_visualizer_has_nothing(self) -> None:
        report = harvest_saved_queries(FakeDb({}))
        assert report.queries == [] and report.skipped == []


class TestPlanWrites:
    def test_a_read_plan_has_no_writes(self) -> None:
        assert plan_writes(FakeDb(), "FOR d IN x RETURN d", {}) == []

    def test_write_nodes_are_found_in_the_plan(self) -> None:
        db = FakeDb(plan_nodes=lambda q: ["SingletonNode", "EnumerateCollectionNode", "UpdateNode"])
        assert plan_writes(db, "FOR d IN x UPDATE d WITH {a: 1} IN x", {}) == ["UpdateNode"]


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def _v(i: str) -> dict[str, Any]:
    return {"_id": f"aws_iam_role/{i}", "_key": i, "name": f"role-{i}"}


def _e(i: str, a: str, b: str) -> dict[str, Any]:
    return {"_id": f"CAN_ASSUME/{i}", "_from": f"aws_iam_role/{a}", "_to": f"aws_iam_role/{b}"}


class TestSignature:
    def test_paths_nested_objects_and_edge_endpoints_are_collected(self) -> None:
        rows = [{"vertices": [_v("1"), _v("2")], "edges": [_e("x", "1", "2")]}, {"r": _v("3"), "n": 7}]
        sig = signature_of(rows)
        assert sig.vertices == {"aws_iam_role/1", "aws_iam_role/2", "aws_iam_role/3"}
        assert sig.edges == {"CAN_ASSUME/x"}
        assert sig.rows == 2

    def test_edges_alone_touch_their_endpoints(self) -> None:
        sig = signature_of([_e("x", "1", "2")])
        assert sig.vertices == {"aws_iam_role/1", "aws_iam_role/2"}

    def test_identical(self) -> None:
        paths = [{"vertices": [_v("1"), _v("2")], "edges": [_e("x", "1", "2")]}]
        flat = [{"a": _v("1"), "r": _e("x", "1", "2"), "b": _v("2")}]
        assert compare(signature_of(paths), signature_of(flat)).kind == "identical"

    def test_same_vertices_when_one_side_returns_no_edges(self) -> None:
        verdict = compare(signature_of([_e("x", "1", "2")]), signature_of([_v("1"), _v("2")]))
        assert verdict.kind == "same_vertices" and verdict.passed

    def test_different_vertices_fail_and_say_how(self) -> None:
        verdict = compare(signature_of([_v("1"), _v("2")]), signature_of([_v("1"), _v("9")]))
        assert not verdict.passed
        assert verdict.detail == "vertices differ: 1 missing, 1 extra"

    def test_scalar_results_compare_order_free_with_float_tolerance(self) -> None:
        a = signature_of([{"n": 1, "avg": 0.1 + 0.2}, {"n": 2, "avg": 1.0}])
        b = signature_of([{"avg": 1.0, "n": 2}, {"avg": 0.3, "n": 1}])
        assert compare(a, b).kind == "same_values"
        assert not compare(a, signature_of([{"n": 1, "avg": 0.3}])).passed


# ---------------------------------------------------------------------------
# Running the source under caps
# ---------------------------------------------------------------------------


class TestRunReadOnly:
    def test_refuses_a_writing_query_without_executing_it(self) -> None:
        db = FakeDb(plan_nodes=lambda q: ["InsertNode"])
        with pytest.raises(SourceRejected, match="writes"):
            run_read_only(db, "INSERT {} INTO x", {})
        assert db.aql.executed == []

    def test_is_bounded_by_runtime_and_rows(self) -> None:
        db = FakeDb(handler=lambda q, b: [{"n": i} for i in range(ROW_CAP + 5)])
        with pytest.raises(TooLarge):
            run_read_only(db, "FOR i IN 1..9999 RETURN {n: i}", {})
        assert db.aql.executed[0][2] == binding.MAX_RUNTIME_S

    def test_a_killed_query_is_a_timeout(self) -> None:
        def _kill(q: str, b: dict[str, Any]) -> list[Any]:
            raise _server_error(AQLQueryExecuteError, 410, 1500, "query killed")

        with pytest.raises(TimedOut):
            run_read_only(FakeDb(handler=_kill), "FOR ...", {})

    def test_a_broken_query_is_rejected_with_the_server_message(self) -> None:
        class _Db(FakeDb):
            pass

        db = _Db()

        def _explain(*a: Any, **k: Any) -> dict[str, Any]:
            raise _server_error(AQLQueryExplainError, 400, 1221, "while looking up graph ''")

        db.aql.explain = _explain  # type: ignore[method-assign]
        with pytest.raises(SourceRejected, match="does not run: while looking up graph"):
            run_read_only(db, 'FOR v IN 1..3 ANY x GRAPH "" RETURN v', {})


EDGE_DEFS = [
    {
        "edge_collection": "CAN_ACCESS",
        "from_vertex_collections": ["aws_iam_role", "aws_iam_user"],
        "to_vertex_collections": ["aws_ec2_instance", "aws_s3_bucket"],
    },
    {
        "edge_collection": "ASSOCIATES",
        "from_vertex_collections": ["aws_ec2_instance"],
        "to_vertex_collections": ["aws_iam_instance_profile"],
    },
]


class TestCanvasActions:
    def test_the_start_named_by_the_text_outranks_the_target(self) -> None:
        q = _q(
            source=CANVAS_ACTIONS,
            name="IAM — Blast radius: reachable EC2s",
            description="From a selected IAM role/user: every EC2 instance reachable via chained role assumptions.",
        )
        assert rank_start_collections(q, EDGE_DEFS)[:2] == ["aws_iam_role", "aws_iam_user"]

    def test_this_x_names_the_start(self) -> None:
        q = _q(
            source=CANVAS_ACTIONS,
            name="Accessible EC2s",
            description="Find all EC2 instances this user can access",
        )
        assert rank_start_collections(q, EDGE_DEFS)[0] == "aws_iam_user"

    def test_samples_connected_starts_and_skips_too_large_ones(self) -> None:
        def handler(q: str, b: dict[str, Any]) -> list[Any]:
            if "IS_SAME_COLLECTION" in q:
                return ["aws_iam_user/1", "aws_iam_user/1", "aws_iam_user/2"]
            if b.get("nodes") == ["aws_iam_user/1"]:
                return [{"n": i} for i in range(ROW_CAP + 1)]  # too large: try the next start
            return [_v("7")]

        db = FakeDb(handler=handler)
        q = _q(
            source=CANVAS_ACTIONS,
            name="Accessible EC2s",
            description="this user can access",
            aql="FOR v IN 1..3 OUTBOUND @nodes[0] GRAPH 'IAM_DEMO' RETURN v",
            bind_vars={"nodes": ""},
        )
        run = run_source(db, q, edge_definitions=EDGE_DEFS)
        assert run.start == "aws_iam_user/2"
        assert run.bind_vars["nodes"] == ["aws_iam_user/2"]
        probe = next(e for e in db.aql.executed if "IS_SAME_COLLECTION" in e[0])
        assert probe[1]["@ec"] == "CAN_ACCESS" and probe[1]["c"] == "aws_iam_user"

    def test_reports_why_no_start_worked(self) -> None:
        def handler(q: str, b: dict[str, Any]) -> list[Any]:
            if "IS_SAME_COLLECTION" in q:
                return ["aws_iam_role/1"]
            raise _server_error(AQLQueryExecuteError, 410, 1500, "query killed")

        q = _q(source=CANVAS_ACTIONS, name="x", aql="FOR v IN 1..9 OUTBOUND @nodes[0] GRAPH 'G' RETURN v")
        with pytest.raises(SourceRejected, match="timed out"):
            run_source(FakeDb(handler=handler), q, edge_definitions=EDGE_DEFS)

    def test_a_saved_query_returning_nothing_is_not_a_reference(self) -> None:
        with pytest.raises(SourceRejected, match="returns no rows"):
            run_source(FakeDb(handler=lambda q, b: []), _q())
