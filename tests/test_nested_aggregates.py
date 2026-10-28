"""Unit tests for nested-aggregate detection and the COLLECT INTO builder."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher._antlr.CypherParser import CypherParser
from arango_cypher._translate_v0.aggregates import nested_aggregates
from arango_cypher._translate_v0.core import _collect_into_rows
from arango_cypher.parser import parse_cypher


def _expression(expr: str) -> CypherParser.OC_ExpressionContext:
    tree = parse_cypher(f"RETURN {expr} AS x").tree
    query = tree.oC_Statement().oC_Query().oC_RegularQuery().oC_SingleQuery().oC_SinglePartQuery()
    return query.oC_Return().oC_ProjectionBody().oC_ProjectionItems().oC_ProjectionItem(0).oC_Expression()


@pytest.mark.parametrize(
    ("expr", "calls"),
    [
        ("count(*)", ["count(*)"]),
        ("100 * count(*) + count(n.x)", ["count(*)", "count(n.x)"]),
        ("head(collect(n))", ["collect(n)"]),
        ("[x IN collect(n.name) WHERE x <> 'a' | x]", ["collect(n.name)"]),
        ("reduce(s = 0, x IN collect(n.age) | s + x)", ["collect(n.age)"]),
        ("any(x IN collect(n.age) WHERE x > 1)", ["collect(n.age)"]),
        ("size(n.name)", []),
        ("COUNT { (n)-->() }", []),
        ("sum(count(*))", ["sum(count(*))"]),
    ],
)
def test_nested_aggregates_finds_the_aggregation_calls(expr: str, calls: list[str]) -> None:
    assert [node.getText() for node in nested_aggregates(_expression(expr))] == calls


@pytest.mark.parametrize(
    "expr",
    [
        "[x IN [1] | count(*)]",
        "[x IN [1] WHERE count(*) > 1 | x]",
        "all(x IN [1] WHERE count(*) > 1)",
        "reduce(s = 0, x IN [1] | s + count(*))",
        "[(n)-->(m) | count(m)]",
    ],
)
def test_an_aggregate_in_a_local_variable_scope_is_refused(expr: str) -> None:
    with pytest.raises(CoreError, match="Aggregation is not allowed"):
        nested_aggregates(_expression(expr))


def test_collect_into_rows_keeps_each_input_and_finishes_after_grouping() -> None:
    line, lets = _collect_into_rows(
        "city = n.city",
        [("c", "c", "COUNT_DISTINCT(n.x)"), ("k", "k", "COUNT(1)")],
        [("names", "names", {"inner": "n.name", "distinct": False, "slice": None})],
        "_rows",
    )
    assert line == "  COLLECT city = n.city INTO _rows = {names: n.name, _c_input: n.x}"
    assert lets == [
        ("names", "REMOVE_VALUE(_rows[*].names, null)"),
        (
            "c",
            "LENGTH(UNIQUE(FOR _agg_row IN _rows FILTER _agg_row._c_input != null RETURN _agg_row._c_input))",
        ),
        ("k", "LENGTH(_rows)"),
    ]


def test_collect_into_rows_without_grouping_keys() -> None:
    line, _ = _collect_into_rows(
        "", [], [("xs", "xs", {"inner": "n", "distinct": True, "slice": None})], "_rows"
    )
    assert line == "  COLLECT _collect_group = 1 INTO _rows = {xs: n}"
