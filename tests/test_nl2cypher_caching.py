"""Unit tests for WP-25.4 prompt caching.

Covers:

* Section ordering: schema block precedes few-shot and resolved-entities
  sections so providers can cache the schema-first prefix.
* ``cached_tokens`` propagation: OpenAI's ``prompt_tokens_details.cached_tokens``
  surfaces through :class:`NL2CypherResult.cached_tokens`.
* Default ``cached_tokens`` is ``0`` when the provider doesn't report it.
* Anthropic provider stub produces a cache-control split with the
  schema-first prefix marked ephemeral and the per-question suffix
  uncached.

All tests run offline.  The OpenAI path is exercised via a mocked
``requests.post`` response — no network.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from arango_cypher.nl2cypher import (
    AnthropicProvider,
    NL2CypherResult,
    OpenAIProvider,
    PromptBuilder,
    nl_to_cypher,
    split_system_for_anthropic_cache,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture
def movies_mapping():
    return mapping_bundle_for("movies_pg")


class TestSectionOrdering:
    def test_schema_before_examples_and_resolved_entities(self) -> None:
        """The schema block must appear before both extension sections.

        This is the precondition for provider-side prefix caching: if
        the schema drifts away from the top, each new few-shot retrieval
        or resolved-entity set invalidates the cache.
        """
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q1", "MATCH (n) RETURN n")],
            resolved_entities=["'x' -> Label {name: 'x'}"],
        )
        rendered = builder.render_system()
        schema_idx = rendered.index("SCHEMA")
        examples_idx = rendered.index("## Examples")
        resolved_idx = rendered.index("## Resolved entities")
        assert schema_idx < examples_idx
        assert examples_idx < resolved_idx

    def test_zero_shot_prefix_stable(self) -> None:
        """The schema-first prefix must stay byte-stable when extensions are added."""
        bare = PromptBuilder(schema_summary="SCHEMA").render_system()
        with_few = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
        ).render_system()
        with_both = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
            resolved_entities=["x"],
        ).render_system()
        assert with_few.startswith(bare)
        assert with_both.startswith(bare)


class TestCachedTokensPropagation:
    def test_openai_cached_tokens_surface(self) -> None:
        """OpenAI's ``prompt_tokens_details.cached_tokens`` propagates end-to-end."""
        provider = OpenAIProvider(api_key="fake", model="gpt-4o-mini")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {
                        "prompt_tokens": 1024,
                        "completion_tokens": 64,
                        "total_tokens": 1088,
                        "prompt_tokens_details": {"cached_tokens": 512},
                    },
                }

        with patch(
            "requests.post", return_value=_Resp(),
        ):
            content, usage = provider.generate("system", "user")
        assert content == "OK"
        assert usage["cached_tokens"] == 512
        assert usage["prompt_tokens"] == 1024

    def test_cached_tokens_default_zero_when_absent(self) -> None:
        """Providers that don't report cached_tokens yield 0, not a crash."""
        provider = OpenAIProvider(api_key="fake", model="gpt-4o-mini")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "total_tokens": 110,
                    },
                }

        with patch("requests.post", return_value=_Resp()):
            _, usage = provider.generate("system", "user")
        assert usage["cached_tokens"] == 0

    def test_cached_tokens_propagate_into_nl2cypher_result(self, movies_mapping) -> None:
        class _P:
            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                return (
                    "```cypher\nMATCH (p:Person) RETURN p\n```",
                    {
                        "prompt_tokens": 800,
                        "completion_tokens": 40,
                        "total_tokens": 840,
                        "cached_tokens": 640,
                    },
                )

        res: NL2CypherResult = nl_to_cypher(
            "q",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_P(),
        )
        assert res.cached_tokens == 640
        assert res.prompt_tokens == 800

    def test_cached_tokens_accumulates_across_retries(self, movies_mapping) -> None:
        """Retries add up: each attempt's cached_tokens contributes to the total."""
        responses = [
            ("```\nnot valid cypher\n```", 100),
            ("```cypher\nMATCH (p:Person) RETURN p\n```", 200),
        ]

        class _P:
            def __init__(self) -> None:
                self._i = 0

            def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
                content, cached = responses[self._i]
                self._i += 1
                return content, {
                    "prompt_tokens": 1000,
                    "completion_tokens": 10,
                    "total_tokens": 1010,
                    "cached_tokens": cached,
                }

        res = nl_to_cypher(
            "q",
            mapping=movies_mapping,
            use_fewshot=False,
            use_entity_resolution=False,
            llm_provider=_P(),
            max_retries=2,
        )
        assert res.cached_tokens == 300
        assert res.retries == 1


class TestAnthropicCacheControl:
    def test_split_without_breakpoint_returns_single_cached_block(self) -> None:
        """Pure schema-only prompts become one cached block."""
        blocks = split_system_for_anthropic_cache(
            "You are a Cypher expert.\n\nSCHEMA:\n  Node :Person",
        )
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "SCHEMA" in blocks[0]["text"]

    def test_split_with_examples_breakpoint(self) -> None:
        """Schema is cached; examples/resolved-entities form the uncached suffix."""
        system = (
            "Prelude\n\nSCHEMA:\n  Node :Person\n\n"
            "## Examples\nQ: who?\n```cypher\nMATCH (n) RETURN n\n```"
        )
        blocks = split_system_for_anthropic_cache(system)
        assert len(blocks) == 2
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "Prelude" in blocks[0]["text"]
        assert "SCHEMA" in blocks[0]["text"]
        assert "Examples" not in blocks[0]["text"]
        assert "cache_control" not in blocks[1]
        assert "Examples" in blocks[1]["text"]

    def test_split_empty_system_safe(self) -> None:
        blocks = split_system_for_anthropic_cache("")
        assert len(blocks) == 1
        assert blocks[0]["text"] == ""

    def test_provider_stub_build_system_blocks(self) -> None:
        provider = AnthropicProvider(api_key="fake")
        system = "Prelude\n\nSCHEMA\n\n## Examples\nQ: x"
        blocks = provider.build_system_blocks(system)
        assert len(blocks) == 2
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "SCHEMA" in blocks[0]["text"]
        assert "Examples" in blocks[1]["text"]

    def test_provider_generate_is_not_implemented(self) -> None:
        """The HTTP path is deliberately unwired — downstream tests should skip."""
        provider = AnthropicProvider(api_key="fake")
        with pytest.raises(NotImplementedError):
            provider.generate("s", "u")


class TestCachedTokensSerializationShape:
    """Smoke-test that the HTTP response keys are stable for the UI."""

    def test_http_response_includes_cached_tokens_key(self, movies_mapping) -> None:
        from fastapi.testclient import TestClient

        from arango_cypher.service import app

        client = TestClient(app)
        resp = client.post(
            "/nl2cypher",
            json={
                "question": "find people",
                "mapping": {
                    "conceptualSchema": movies_mapping.conceptual_schema,
                    "physicalMapping": movies_mapping.physical_mapping,
                    "metadata": {},
                },
                "use_llm": False,
            },
        )
        assert resp.status_code == 200, resp.text
        body = json.loads(resp.text)
        assert "cached_tokens" in body
        assert body["cached_tokens"] == 0
