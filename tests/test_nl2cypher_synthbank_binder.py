"""Synthbank binder: pure helpers, the bank writer and the few-shot wiring.

The data-binding itself is exercised against real databases in
``tests/integration/test_synthbank_live.py``; these cover what is decidable
without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.fewshot import FewShotIndex

from arango_cypher.nl2cypher import _core
from arango_cypher.nl2cypher.synthbank_binder import (
    MIN_ANCHOR_DISTINCT_RATIO,
    _execution_nonempty,
    _strict_extremum,
    is_degenerate_value_label,
    profile_schema,
    write_bank,
)


class ScriptedExecutor:
    """An ``Executor`` answering by substring of the Cypher it is given."""

    def __init__(self, answers: dict[str, list[Any]]) -> None:
        self.answers = answers
        self.queries: list[str] = []

    def run(self, cypher: str) -> list[Any]:
        self.queries.append(cypher)
        for needle, rows in self.answers.items():
            if needle in cypher:
                return rows
        return []


# -- helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "degenerate"),
    [
        ("0,38 EUR", True),
        ("€0.38", True),
        ("12 %", True),
        ("42", False),
        ("Tom Hanks", False),
        ("R2 D2", False),
    ],
)
def test_degenerate_value_labels(label: str, degenerate: bool) -> None:
    assert is_degenerate_value_label(label) is degenerate


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], False),
        ([0], False),  # count() of nothing: one row, value 0
        ([{"result": 0}], False),
        ([3], True),
        ([{"result": 3}], True),
        ([{"result": None}], True),  # a real row whose property is null
        ([{"a": 0, "b": 0}], True),  # a multi-column row is a real row
        ([False], True),  # a boolean false is a value, not a zero count
        ([1, 2], True),
    ],
)
def test_execution_nonempty(rows: list[Any], expected: bool) -> None:
    assert _execution_nonempty(ScriptedExecutor({"MATCH": rows}), "MATCH ...") is expected


@pytest.mark.parametrize(
    ("values", "ok"),
    [([2003, 1999], True), ([2003, 2003], False), ([2003], True), (["2024-05-01", "2023-01-01"], True)],
)
def test_strict_extremum(values: list[Any], ok: bool) -> None:
    rows = [{"v": v} for v in values]

    assert _strict_extremum(ScriptedExecutor({"ORDER BY": rows}), "... ORDER BY ...")[0] is ok


# -- profiling -----------------------------------------------------------------


def _bundle(
    entities: list[dict[str, Any]], relationships: list[dict[str, Any]] | None = None
) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={"entities": entities, "relationships": relationships or []},
        physical_mapping={"entities": {}, "relationships": {}},
        metadata={},
    )


def test_an_anchor_must_be_near_unique() -> None:
    """On nine employees ``country`` classifies as a name; it must not anchor."""
    bundle = _bundle([{"name": "Employee", "properties": [{"name": "country"}, {"name": "lastName"}]}])
    countries = [{"v": c} for c in ["UK", "USA", "USA", "UK", "USA", "UK", "UK", "USA", "USA"]]
    names = [
        {"v": n}
        for n in [
            "Davolio Smith",
            "Fuller Jones",
            "Leverling Ann",
            "Peacock Lee",
            "Buchanan Roe",
            "Suyama Kim",
            "King Ray",
            "Callahan Fox",
            "Dodsworth May",
        ]
    ]
    executor = ScriptedExecutor({"x.country IS NOT NULL": countries, "x.lastName IS NOT NULL": names})

    profile = profile_schema(bundle, executor)

    assert profile.properties[("Employee", "country")].distinct_ratio < MIN_ANCHOR_DISTINCT_RATIO
    assert profile.anchor("Employee") == "lastName"


def test_numbers_are_orderable_numeric_strings_are_not() -> None:
    """``"10"`` sorts before ``"9"``: a top-N over numeric strings is wrong gold."""
    bundle = _bundle([{"name": "Movie", "properties": [{"name": "released"}, {"name": "code"}]}])
    executor = ScriptedExecutor(
        {
            "x.released IS NOT NULL": [{"v": y} for y in (1999, 2003, 1995)],
            "x.code IS NOT NULL": [{"v": s} for s in ("10", "9", "11")],
        }
    )

    profile = profile_schema(bundle, executor)

    assert profile.properties[("Movie", "released")].orderable
    assert not profile.properties[("Movie", "code")].orderable


def test_unsafe_identifiers_are_skipped_not_spliced() -> None:
    bundle = _bundle([{"name": "Movie", "properties": [{"name": "title) RETURN 1 //"}]}])
    executor = ScriptedExecutor({})

    profile = profile_schema(bundle, executor)

    assert any("not a plain identifier" in s for s in profile.skipped)
    assert not any("RETURN 1" in q for q in executor.queries)


def test_optional_relation_needs_instances_with_and_without() -> None:
    bundle = _bundle(
        [{"name": "Person", "properties": []}],
        [
            {"type": "DIRECTED", "fromEntity": "Person", "toEntity": "Movie"},
            {"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"},
        ],
    )
    executor = ScriptedExecutor(
        {
            "EXISTS { (x)-[:DIRECTED]->() }": [28],
            "EXISTS { (x)-[:ACTED_IN]->() }": [134],
            "RETURN count(x) AS n": [134],
        }
    )

    profile = profile_schema(bundle, executor)

    assert ("Person", "DIRECTED") in profile.optional_relations
    assert ("Person", "ACTED_IN") not in profile.optional_relations  # every Person acted


# -- writer + wiring ---------------------------------------------------------


BANK = {
    "version": 1,
    "examples": [
        {
            "question": "How many Person are there for Keanu Reeves?",
            "cypher": 'MATCH (m:Person)-[:FOLLOWS]->(c:Person {name: "Keanu Reeves"}) RETURN count(DISTINCT m) AS result',
            "shape": "scalar_count",
            "paraphrases": ["How many people follow Keanu Reeves?"],
        },
        {
            "question": "What is the born of Tom Hanks?",
            "cypher": 'MATCH (x:Person {name: "Tom Hanks"}) RETURN x.born AS result',
            "shape": "lookup",
        },
    ],
}


def test_paraphrases_are_flattened_into_retrievable_examples(tmp_path: Path) -> None:
    """``from_corpus_files`` reads only question + cypher, so a nested
    paraphrase list would never reach retrieval."""
    path = tmp_path / "bank.yml"

    written = write_bank(BANK, path, source="test")
    index = FewShotIndex.from_corpus_files([path], mode="bm25")

    assert written == 3
    questions = [q for q, _ in index.examples]
    assert "How many people follow Keanu Reeves?" in questions
    assert index.retrieve("how many people follow keanu reeves", k=1)[0][1] == BANK["examples"][0]["cypher"]
    assert path.read_text().startswith("# Generated by")


@pytest.fixture
def fresh_default_index():
    _core._invalidate_default_fewshot_index()
    yield
    _core._invalidate_default_fewshot_index()


def test_banks_are_opt_in(fresh_default_index, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "bank.yml"
    write_bank(BANK, path)
    monkeypatch.delenv(_core.FEWSHOT_BANKS_ENV, raising=False)

    index = _core._get_default_fewshot_index()

    assert index is None or "What is the born of Tom Hanks?" not in [q for q, _ in index.examples]


def test_a_configured_bank_reaches_the_prompt_section(
    fresh_default_index, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "bank.yml"
    write_bank(BANK, path)
    monkeypatch.setenv(_core.FEWSHOT_BANKS_ENV, str(path))

    index = _core._get_default_fewshot_index()

    assert index is not None
    section = index.format_prompt_section("What is the born of Tom Hanks?", k=1, language="cypher")
    assert section.startswith("## Examples")
    assert 'MATCH (x:Person {name: "Tom Hanks"})' in section


def test_a_missing_bank_path_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real = tmp_path / "bank.yml"
    write_bank(BANK, real)
    monkeypatch.setenv(_core.FEWSHOT_BANKS_ENV, f"{tmp_path / 'typo.yml'}{__import__('os').pathsep}{real}")

    with caplog.at_level("WARNING"):
        paths = _core._generated_bank_paths()

    assert paths == [real]
    assert "typo.yml" in caplog.text
