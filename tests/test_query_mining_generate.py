"""Query mining, slice 2: drafting question + Cypher and the verify/retry loop.

The provider is scripted but honours the real ``LLMProvider.generate(system,
user) -> (content, usage)`` contract; translation goes through the real
transpiler with the movies PG mapping, so a draft the transpiler refuses is
refused here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arango_query_core.mapping import MappingBundle

from arango_cypher.query_mining.generate import (
    Draft,
    GenerationError,
    build_user_prompt,
    parse_draft,
    physical_name_table,
)
from arango_cypher.query_mining.harvest import CANVAS_ACTIONS, SAVED_QUERIES, SavedQuery
from arango_cypher.query_mining.miner import (
    MAX_CONSECUTIVE_PROVIDER_FAILURES,
    _translation_feedback,
    _unlabeled_nodes,
    mine_database,
    mine_query,
)
from tests.helpers.fake_arango import FakeDb

MAPPINGS = Path(__file__).parent / "fixtures" / "mappings"


def _movies() -> MappingBundle:
    m = json.loads((MAPPINGS / "movies_pg.export.json").read_text())
    return MappingBundle(
        conceptual_schema=m["conceptualSchema"],
        physical_mapping=m["physicalMapping"],
        metadata=m.get("metadata", {}),
    )


class ScriptedProvider:
    """Replies in order; records every (system, user) it was sent."""

    def __init__(self, replies: list[str | Exception]):
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append((system, user))
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _reply(question: str, cypher: str, params: dict[str, Any] | None = None) -> str:
    return (
        "```json\n" + json.dumps({"question": question, "cypher": cypher, "params": params or {}}) + "\n```"
    )


KEANU = {"_id": "persons/1", "_key": "1", "name": "Keanu Reeves"}
MATRIX = {"_id": "movies/9", "_key": "9", "title": "The Matrix"}
EDGE = {"_id": "acted_in/5", "_from": "persons/1", "_to": "movies/9"}

SOURCE_AQL = "FOR m IN movies FILTER m.title == @t FOR p, e IN 1..1 INBOUND m acted_in RETURN e"
GOOD = "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN r"


def _db(*, candidate_rows: list[Any] | None = None) -> FakeDb:
    def handler(q: str, b: dict[str, Any]) -> list[Any]:
        if q == SOURCE_AQL:
            return [EDGE]
        return candidate_rows if candidate_rows is not None else [{"r": EDGE}]

    return FakeDb(handler=handler)


def _saved(**kw: Any) -> SavedQuery:
    base: dict[str, Any] = {
        "source": SAVED_QUERIES,
        "key": "q1",
        "name": "Cast of a movie",
        "description": "Everyone who acted in a given movie",
        "aql": SOURCE_AQL,
        "bind_vars": {"t": "The Matrix"},
        "graph": "movies",
    }
    base.update(kw)
    return SavedQuery(**base)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


class TestDraft:
    def test_parses_json_inside_fences_and_prose(self) -> None:
        d = parse_draft('Sure!\n```json\n{"question": "Who?", "cypher": "MATCH (n:A) RETURN n"}\n```')
        assert d == Draft("Who?", "MATCH (n:A) RETURN n", {})

    @pytest.mark.parametrize(
        ("reply", "message"),
        [
            ("no json here", "no JSON object"),
            ('{"question": "q", "cypher": "MATCH", ', "no JSON object"),
            ('{"question": "", "cypher": "MATCH (n:A) RETURN n"}', '"question"'),
            ('{"question": "q"}', '"cypher"'),
            ('{"question": "q", "cypher": "c", "params": [1]}', '"params"'),
        ],
    )
    def test_rejects_unusable_replies_with_feedback(self, reply: str, message: str) -> None:
        with pytest.raises(GenerationError, match=message):
            parse_draft(reply)

    def test_the_name_table_maps_physical_to_conceptual(self) -> None:
        table = physical_name_table(_movies())
        assert "- collection persons -> :Person" in table
        assert "- edge collection acted_in -> [:ACTED_IN]" in table

    def test_the_prompt_carries_the_source_and_the_selected_node(self) -> None:
        q = _saved(source=CANVAS_ACTIONS, bind_vars={"nodes": ["persons/1"]})
        user = build_user_prompt(q, q.bind_vars, start_properties={"name": "Keanu Reeves"})
        assert SOURCE_AQL in user
        assert "Everyone who acted in a given movie" in user
        assert 'match it by: name = "Keanu Reeves"' in user


class TestFeedback:
    def test_names_the_unlabeled_node_patterns(self) -> None:
        cypher = "MATCH (r:Role)<-[:CAN_ACCESS]-(principal) MATCH (v)-[:X*0..3]->(principal) RETURN count(*)"
        assert _unlabeled_nodes(cypher) == ["principal", "v"]
        fb = _translation_feedback("A single label is required in v0 subset", cypher)
        assert "These node patterns have no label: principal, v" in fb
        assert "UNION" in fb

    def test_other_translation_errors_pass_through(self) -> None:
        assert _translation_feedback("boom", "MATCH (a:A) RETURN a") == "the Cypher does not translate: boom"


# ---------------------------------------------------------------------------
# The verify / retry loop
# ---------------------------------------------------------------------------


class TestMineQuery:
    def test_retries_with_feedback_until_a_draft_verifies(self) -> None:
        provider = ScriptedProvider(
            [
                "I think the answer is MATCH ...",  # no JSON
                _reply("Who acted in it?", "MATCH (p:Person) CALL apoc.do.it(p) YIELD x RETURN x"),
                _reply("Who acted in The Matrix?", GOOD, {"t": "The Matrix"}),
            ]
        )
        outcome = mine_query(_db(), _movies(), _saved(), provider, schema_summary="(schema)")
        assert outcome.verified, outcome.reason
        ex = outcome.example
        assert ex is not None
        assert ex.attempts == 3
        assert ex.question == "Who acted in The Matrix?"
        assert ex.verdict.kind == "same_vertices" or ex.verdict.kind == "identical"
        assert "acted_in" in ex.bind_vars.values() or "@edgeCollection" in ex.bind_vars
        # Each retry carried the previous failure.
        assert "no JSON object" in provider.calls[1][1]
        assert "procedures (CALL ..., apoc.*) are not supported" in provider.calls[2][1]
        assert [a.failure.split(";")[0][:40] for a in outcome.failed_attempts] == [
            "the reply contained no JSON object",
            "the Cypher calls a procedure",
        ]

    def test_different_results_are_rejected_and_explained(self) -> None:
        other = {"_id": "acted_in/6", "_from": "persons/2", "_to": "movies/9"}
        provider = ScriptedProvider([_reply("q", GOOD, {"t": "The Matrix"})] * 3)
        outcome = mine_query(
            _db(candidate_rows=[{"r": other}]), _movies(), _saved(), provider, schema_summary=""
        )
        assert not outcome.verified
        assert (
            "its results differ from the saved query's (vertices differ: 1 missing, 1 extra" in outcome.reason
        )
        assert outcome.failed_attempts[0].cypher == GOOD

    def test_a_source_that_cannot_run_is_never_sent_to_the_model(self) -> None:
        provider = ScriptedProvider([])
        db = FakeDb(handler=lambda q, b: [])
        outcome = mine_query(db, _movies(), _saved(), provider, schema_summary="")
        assert outcome.reason == "source: returns no rows with its saved bind values"
        assert provider.calls == []

    def test_a_provider_failure_ends_the_query_without_retries(self) -> None:
        provider = ScriptedProvider([RuntimeError("401 Unauthorized"), _reply("q", GOOD)])
        outcome = mine_query(_db(), _movies(), _saved(), provider, schema_summary="")
        assert outcome.provider_failed
        assert outcome.reason == "LLM provider failed: RuntimeError: 401 Unauthorized"
        assert len(provider.calls) == 1


class TestMineDatabase:
    def _db(self, n: int) -> FakeDb:
        docs = [
            {
                "_key": f"q{i}",
                "name": f"Cast {i}",
                "description": "Everyone who acted in a given movie",
                "queryText": SOURCE_AQL + f" LIMIT {i + 1}",
                "bindVariables": {"t": "The Matrix"},
                "graphId": "movies",
            }
            for i in range(n)
        ]
        docs.append(
            {
                "_key": "builtin",
                "name": "Fetch edges of type @@edgeType (default)",
                "queryText": "FOR d IN @@edgeType LIMIT 100 RETURN d",
                "bindVariables": {"@edgeType": "acted_in"},
                "graphId": "movies",
            }
        )
        return FakeDb(
            {SAVED_QUERIES: docs},
            handler=lambda q, b: [EDGE],
            graphs={"movies": []},
        )

    def test_builtins_are_skipped_unless_asked(self) -> None:
        provider = ScriptedProvider([_reply("q", GOOD, {"t": "The Matrix"})])
        outcomes, skipped = mine_database(self._db(1), _movies(), provider, schema_summary="", graph="movies")
        assert [o.query.key for o in outcomes] == ["q0"]
        assert ("_queries/builtin", "visualizer built-in") in skipped

    def test_stops_calling_a_provider_that_keeps_failing(self) -> None:
        n = MAX_CONSECUTIVE_PROVIDER_FAILURES + 2
        provider = ScriptedProvider([RuntimeError("401 Unauthorized")] * n)
        outcomes, skipped = mine_database(self._db(n), _movies(), provider, schema_summary="", graph="movies")
        assert len(provider.calls) == MAX_CONSECUTIVE_PROVIDER_FAILURES
        assert sum(1 for _, why in skipped if why == "not attempted: the LLM provider kept failing") == 2
