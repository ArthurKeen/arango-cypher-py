"""Shape and byte-identity tests for :class:`PromptBuilder`.

These tests pin the contract between the NL→Cypher pipeline and the
prompt layer. The zero-shot rendering MUST remain byte-identical to
the pre-refactor ``_SYSTEM_PROMPT.format(schema=...)`` output so that
provider-side prefix caching keeps working and Wave 4a sub-agents can
layer on top without regressing the baseline prompt.
"""
from __future__ import annotations

from arango_cypher.nl2cypher import _SYSTEM_PROMPT, PromptBuilder

FROZEN_SYSTEM_PROMPT = (
    "You are a Cypher query expert."
    " Given a natural language question and a graph schema,"
    " generate a valid Cypher query.\n"
    "\n"
    "Rules:\n"
    "- Use only node labels and relationship types from the schema\n"
    "- Use property names from the schema\n"
    "- Return a single Cypher query (no explanation)\n"
    "- Use standard Cypher syntax (MATCH, WHERE, RETURN, ORDER BY, LIMIT, etc.)\n"
    "- For counts, use count()\n"
    "- For aggregations, use collect(), sum(), avg(), min(), max()\n"
    "- Wrap the query in ```cypher``` code block\n"
    "\n"
    "{schema}"
)


class TestZeroShotByteIdentity:
    def test_matches_pre_refactor_format_call(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        assert builder.render_system() == FROZEN_SYSTEM_PROMPT.format(schema="X")

    def test_matches_current_system_prompt_constant(self) -> None:
        builder = PromptBuilder(schema_summary="Graph:\n  Node :Person (name)")
        expected = _SYSTEM_PROMPT.replace(
            "{schema}", "Graph:\n  Node :Person (name)",
        )
        assert builder.render_system() == expected

    def test_frozen_prompt_matches_module_constant(self) -> None:
        assert FROZEN_SYSTEM_PROMPT == _SYSTEM_PROMPT

    def test_empty_schema_is_valid(self) -> None:
        builder = PromptBuilder(schema_summary="")
        assert builder.render_system() == FROZEN_SYSTEM_PROMPT.format(schema="")


class TestRenderUser:
    def test_no_retry_context_returns_question_unchanged(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        assert builder.render_user("find all people") == "find all people"

    def test_retry_context_appends_same_wording_as_legacy_loop(self) -> None:
        builder = PromptBuilder(schema_summary="X", retry_context="ERR")
        expected = (
            "q\n\n"
            "Your previous Cypher was invalid: ERR. Please fix it."
        )
        assert builder.render_user("q") == expected

    def test_retry_context_cleared_between_uses(self) -> None:
        builder = PromptBuilder(schema_summary="X")
        builder.retry_context = "syntax error"
        first = builder.render_user("q")
        builder.retry_context = ""
        second = builder.render_user("q")
        assert "syntax error" in first
        assert second == "q"


class TestFewShotSection:
    def test_few_shot_examples_appear_after_schema(self) -> None:
        nl = "who directed The Matrix"
        cy = "MATCH (p:Person)-[:DIRECTED]->(m:Movie) RETURN p"
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[(nl, cy)],
        )
        rendered = builder.render_system()
        schema_idx = rendered.index("SCHEMA")
        examples_idx = rendered.index("Examples")
        assert schema_idx < examples_idx
        assert nl in rendered
        assert cy in rendered

    def test_zero_shot_has_no_examples_section(self) -> None:
        builder = PromptBuilder(schema_summary="SCHEMA")
        assert "Examples" not in builder.render_system()

    def test_multiple_examples_render_in_order(self) -> None:
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[
                ("q1", "MATCH (n) RETURN n"),
                ("q2", "MATCH (m:Movie) RETURN m"),
            ],
        )
        rendered = builder.render_system()
        assert rendered.index("q1") < rendered.index("q2")


class TestResolvedEntitiesSection:
    def test_resolved_entities_appear_after_schema(self) -> None:
        builder = PromptBuilder(
            schema_summary="SCHEMA",
            resolved_entities=["'Tom Hanks' -> Person {name: 'Tom Hanks'}"],
        )
        rendered = builder.render_system()
        assert "Resolved entities" in rendered
        assert "Tom Hanks" in rendered
        assert rendered.index("SCHEMA") < rendered.index("Resolved entities")

    def test_zero_shot_has_no_resolved_entities_section(self) -> None:
        builder = PromptBuilder(schema_summary="SCHEMA")
        assert "Resolved entities" not in builder.render_system()


class TestExtensionsDoNotBreakSystemPrefix:
    def test_system_prefix_is_preserved_with_extensions(self) -> None:
        """The schema-first prefix MUST remain byte-stable when extensions
        are added, so providers can still cache the prefix across calls."""
        bare = PromptBuilder(schema_summary="SCHEMA").render_system()
        with_examples = PromptBuilder(
            schema_summary="SCHEMA",
            few_shot_examples=[("q", "MATCH (n) RETURN n")],
        ).render_system()
        assert with_examples.startswith(bare)
