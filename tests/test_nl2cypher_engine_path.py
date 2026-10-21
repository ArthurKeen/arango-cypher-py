"""NL→Cypher on the shared engine (issue #24): ``nl_to_cypher`` runs
arango-query-core's ``NLQueryEngine``, and keeps the contract its own loop had.

The scripted provider mirrors ``LLMProvider.generate(system, user) ->
(text, usage)``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from arango_cypher.nl2cypher import engine_path, nl_to_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


class ScriptedProvider:
    """Answers in order; an ``Exception`` in the script is raised instead."""

    def __init__(self, *answers: str | Exception) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append((system, user))
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "cached_tokens": 40,
        }


@pytest.fixture
def movies() -> Any:
    return mapping_bundle_for("movies_pg")


def ask(movies: Any, provider: ScriptedProvider, **kwargs: Any) -> Any:
    return nl_to_cypher(
        "Who acted in The Matrix?",
        mapping=movies,
        llm_provider=provider,
        use_fewshot=False,
        use_entity_resolution=False,
        **kwargs,
    )


class TestEngine:
    def test_nl_to_cypher_runs_the_engine(self, movies: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from arango_query_core.nl import NLQueryEngine

        calls: list[str] = []
        real = NLQueryEngine.generate

        def spy(self: Any, question: str, **kwargs: Any) -> Any:
            calls.append(question)
            return real(self, question, **kwargs)

        monkeypatch.setattr(NLQueryEngine, "generate", spy)
        provider = ScriptedProvider("```cypher\nMATCH (p:Person) RETURN p.name\n```")
        assert ask(movies, provider).method == "llm"
        assert calls == ["Who acted in The Matrix?"]


class TestContract:
    def test_prose_before_the_fence_costs_no_retry(self, movies: Any) -> None:
        provider = ScriptedProvider(
            "Here is the query:\n```cypher\nMATCH (p:Person) RETURN p.name\n```\nIt lists people."
        )
        res = ask(movies, provider)
        assert (res.method, res.cypher, res.retries) == ("llm", "MATCH (p:Person) RETURN p.name", 0)
        assert len(provider.calls) == 1

    def test_a_hallucinated_label_is_repaired_not_retried(self, movies: Any) -> None:
        provider = ScriptedProvider("```cypher\nMATCH (a:Actor)-[:ACTED_IN]->(m:Movie) RETURN a.name\n```")
        res = ask(movies, provider)
        assert res.method == "llm" and "(a:Person)" in res.cypher and "Actor" not in res.cypher
        assert len(provider.calls) == 1

    def test_a_provider_call_that_fails_once_is_retried(self, movies: Any) -> None:
        provider = ScriptedProvider(
            TimeoutError("LLM API timed out"), "```cypher\nMATCH (p:Person) RETURN p\n```"
        )
        res = ask(movies, provider)
        assert res.method == "llm" and res.cypher == "MATCH (p:Person) RETURN p"

    def test_every_call_failing_falls_back_to_the_rule_based_translator(self, movies: Any) -> None:
        provider = ScriptedProvider(*(RuntimeError("down") for _ in range(6)))
        res = ask(movies, provider)
        assert res.method == "rule_based"
        assert len(provider.calls) == 3  # failed calls spend the one budget: 1 + max_retries

    def test_a_failed_call_after_a_bad_candidate_fails_closed_quoting_it(self, movies: Any) -> None:
        provider = ScriptedProvider("```cypher\nMATCH (p:Nope RETURN p\n```", TimeoutError("timed out"))
        res = ask(movies, provider, max_retries=1)
        assert res.method == "validation_failed" and res.cypher == ""
        assert "Last attempted Cypher was:\n\nMATCH (p:Nope RETURN p" in res.explanation
        assert res.prompt_tokens == 100  # the call that answered

    def test_prose_before_an_unfenced_query_costs_no_retry(self, movies: Any) -> None:
        provider = ScriptedProvider("Here is the query:\nMATCH (p:Person) RETURN p.name")
        res = ask(movies, provider)
        assert (res.method, res.cypher, res.retries) == ("llm", "MATCH (p:Person) RETURN p.name", 0)

    def test_the_callers_hint_rides_on_the_first_message_only(self, movies: Any) -> None:
        provider = ScriptedProvider(
            "```cypher\nMATCH (p:Nope RETURN p\n```", "```cypher\nMATCH (p:Person) RETURN p\n```"
        )
        res = ask(movies, provider, retry_context="it timed out")
        assert res.method == "llm"
        first, second = (user for _, user in provider.calls)
        assert (
            first
            == "Who acted in The Matrix?\n\nYour previous Cypher was invalid: it timed out. Please fix it."
        )
        assert "it timed out" not in second and "MATCH (p:Nope RETURN p" in second

    def test_a_few_shot_index_that_raises_asks_without_examples(
        self, movies: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class BrokenIndex:
            def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
                raise OSError("embedding model unavailable")

        monkeypatch.setattr(engine_path, "_get_default_fewshot_index", lambda: BrokenIndex())
        provider = ScriptedProvider("```cypher\nMATCH (p:Person) RETURN p\n```")
        with caplog.at_level(logging.WARNING):
            res = nl_to_cypher("Who acted in The Matrix?", mapping=movies, llm_provider=provider)
        assert res.method == "llm" and res.cypher == "MATCH (p:Person) RETURN p"
        assert "## Examples" not in provider.calls[0][0]
        assert "embedding model unavailable" in caplog.text

    def test_the_explanation_is_the_models_answer_and_usage_adds_up(self, movies: Any) -> None:
        answer = "```cypher\nMATCH (p:Person) RETURN p\n```\nPeople."
        res = ask(movies, ScriptedProvider("```cypher\nMATCH (p:Nope RETURN p\n```", answer))
        assert res.explanation == answer
        assert (res.retries, res.prompt_tokens, res.cached_tokens) == (1, 200, 80)

    def test_an_exhausted_budget_fails_closed_quoting_the_last_attempt(self, movies: Any) -> None:
        bad = "```cypher\nMATCH (p:Nope RETURN p\n```"
        res = ask(movies, ScriptedProvider(bad, bad, bad))
        assert res.method == "validation_failed" and res.cypher == ""
        assert "Last attempted Cypher was:\n\nMATCH (p:Nope RETURN p" in res.explanation

    def test_resolved_entities_follow_the_examples(self, movies: Any) -> None:
        class Resolver:
            advisories: list[Any] = []

            def resolve(self, question: str) -> list[str]:
                return ["hit"]

            def format_prompt_section(self, hits: list[str]) -> list[str]:
                return ['"The Matrix" -> Movie.title = "The Matrix" (score 1.00)']

        provider = ScriptedProvider("```cypher\nMATCH (m:Movie) RETURN m\n```")
        nl_to_cypher(
            "Who acted in The Matrix?",
            mapping=movies,
            llm_provider=provider,
            use_fewshot=True,
            entity_resolver=Resolver(),
        )
        system = provider.calls[0][0]
        assert "## Resolved entities" in system and 'Movie.title = "The Matrix"' in system
        if "## Examples" in system:
            assert system.index("## Examples") < system.index("## Resolved entities")
