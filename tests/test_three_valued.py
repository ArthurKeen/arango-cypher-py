"""Unit tests for the three-valued-logic helpers (``_translate_v0.three_valued``)."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher._antlr.CypherParser import CypherParser
from arango_cypher._translate_v0.three_valued import (
    _compile_comparison,
    _guard_preference,
    _is_two_valued,
    _negate_value,
    _test_truth,
    _three_valued,
)
from arango_cypher.parser import parse_cypher


@pytest.mark.parametrize(
    ("left", "op", "right", "mode", "expected"),
    [
        # A filter's equality needs one guard, on the parameter or loop variable.
        ("n.a", "=", "@p", "true", "(@p != null AND n.a == @p)"),
        ("n.a", "=", "x", "true", "(x != null AND n.a == x)"),
        ("n.a", "=", "m.b", "true", "(n.a != null AND n.a == m.b)"),
        ("n.a", "=", "'x'", "true", "(n.a == 'x')"),
        ("n.a", "=", "null", "true", "(null != null AND n.a == null)"),
        # Inequality and ordering need a guard on every nullable operand.
        ("n.a", "<>", "@p", "true", "(n.a != null AND @p != null AND n.a != @p)"),
        ("n.a", "<", "3", "true", "(n.a != null AND n.a < 3)"),
        # "Is false" negates the operator.
        ("n.a", "=", "'x'", "false", "(n.a != null AND n.a != 'x')"),
        ("n.a", ">", "3", "false", "(n.a != null AND n.a <= 3)"),
        ("1", "<", "2", "false", "(1 >= 2)"),
        # A value keeps null.
        ("n.a", "=", "'x'", None, "((n.a == null) ? null : (n.a == 'x'))"),
        ("n.a", ">=", "m.b", None, "((n.a == null OR m.b == null) ? null : (n.a >= m.b))"),
        ("1", "=", "1", None, "(1 == 1)"),
    ],
)
def test_compile_comparison(left: str, op: str, right: str, mode: str | None, expected: str) -> None:
    assert _compile_comparison(left, op, right, mode) == expected


def test_compile_comparison_rejects_an_unknown_operator() -> None:
    with pytest.raises(CoreError, match="Unsupported comparison op"):
        _compile_comparison("a", "=~", "b", None)


def test_three_valued_leaves_a_call_unparenthesised() -> None:
    assert _three_valued("CONTAINS('ab', 'a')", "NOT CONTAINS('ab', 'a')", ("'ab'", "'a'"), "true") == (
        "CONTAINS('ab', 'a')"
    )
    assert _three_valued("CONTAINS(n.a, 'a')", "NOT CONTAINS(n.a, 'a')", ("n.a", "'a'"), "false") == (
        "(n.a != null AND NOT CONTAINS(n.a, 'a'))"
    )


def test_guard_preference_orders_parameter_variable_expression() -> None:
    assert _guard_preference("@p") < _guard_preference("x") < _guard_preference("n.a")
    assert _guard_preference("`a b`") == _guard_preference("x")


@pytest.mark.parametrize(
    ("mode", "two_valued", "expected"),
    [
        (None, False, "v"),
        ("true", False, "v"),
        ("false", False, "(v == false)"),
        ("false", True, "(NOT v)"),
    ],
)
def test_test_truth(mode: str | None, two_valued: bool, expected: str) -> None:
    assert _test_truth("v", mode, two_valued=two_valued) == expected


def test_negate_value_keeps_null_unless_two_valued() -> None:
    assert _negate_value("v", two_valued=False) == "(v == null ? null : (NOT v))"
    assert _negate_value("v", two_valued=True) == "(NOT v)"


def _where_comparison(where: str) -> CypherParser.OC_ComparisonExpressionContext:
    tree = parse_cypher(f"MATCH (n) WHERE {where} RETURN n").tree
    found: list[CypherParser.OC_ComparisonExpressionContext] = []

    def walk(node: object) -> None:
        if isinstance(node, CypherParser.OC_ComparisonExpressionContext):
            found.append(node)
            return
        for i in range(node.getChildCount()):
            child = node.getChild(i)
            if hasattr(child, "getChildCount"):
                walk(child)

    walk(tree)
    return found[0]


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("n:User", True),
        ("n.a IS NULL", True),
        ("(n)-->()", True),
        ("exists(n.a)", True),
        ("any(x IN n.xs WHERE x = 1)", True),
        ("true", True),
        ("n.active", False),
        ("n.a = 1", False),
        ("n.a IN [1]", False),
        ("n.a STARTS WITH 'x'", False),
        ("-n.a", False),
        ("n.xs[0]", False),
    ],
)
def test_is_two_valued(where: str, expected: bool) -> None:
    assert _is_two_valued(_where_comparison(where)) is expected


def test_three_valued_wraps_only_bare_operator_expressions() -> None:
    assert _three_valued("REGEX_TEST('a b', ' ')", "x", ("'a b'",), None) == "REGEX_TEST('a b', ' ')"
    assert (
        _three_valued("RIGHT('ab', LENGTH('b')) == 'b'", "x", ("'ab'",), None)
        == "(RIGHT('ab', LENGTH('b')) == 'b')"
    )
