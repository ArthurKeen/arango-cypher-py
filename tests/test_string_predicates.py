"""STARTS WITH / ENDS WITH / CONTAINS / =~ (``_translate_v0.string_predicates``).

Live: TCK String8-10 through the execution ratchet (``CONTAINS ''`` is true,
``NOT … ENDS WITH`` negates the whole test, non-strings give null).
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.string_predicates import (
    compile_string_predicate,
    is_string_literal,
    string_test,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    ("kind", "rhs", "expected"),
    [
        ("starts", "'a'", "STARTS_WITH(x, 'a')"),
        ("ends", "'a'", "(RIGHT(x, LENGTH('a')) == 'a')"),
        ("contains", "''", "true"),
        ("contains", "'a'", "CONTAINS(x, 'a')"),
        ("contains", "@p", "(LENGTH(@p) == 0 OR CONTAINS(x, @p))"),
        ("regex", "'a.*'", "REGEX_TEST(x, 'a.*')"),
    ],
)
def test_string_test(kind: str, rhs: str, expected: str) -> None:
    assert string_test(kind, "x", rhs) == expected


def test_each_operand_not_shown_to_be_a_string_is_guarded() -> None:
    value = compile_string_predicate("starts", "x", "y", base_is_string=False, rhs_is_string=False, mode=None)
    assert value == "((IS_STRING(x) AND IS_STRING(y)) ? STARTS_WITH(x, y) : null)"
    holds = compile_string_predicate(
        "starts", "x", "'a'", base_is_string=False, rhs_is_string=True, mode="true"
    )
    assert holds == "(IS_STRING(x) AND STARTS_WITH(x, 'a'))"


def test_not_negates_the_whole_ends_with_test() -> None:
    fails = compile_string_predicate(
        "ends", "x", "'def'", base_is_string=False, rhs_is_string=True, mode="false"
    )
    assert fails == "(IS_STRING(x) AND NOT (RIGHT(x, LENGTH('def')) == 'def'))"


def test_a_shown_string_needs_no_guard() -> None:
    assert compile_string_predicate(
        "contains", "'abc'", "'b'", base_is_string=True, rhs_is_string=True, mode=None
    ) == ("(CONTAINS('abc', 'b'))")


@pytest.mark.parametrize(
    ("aql", "expected"), [("'a'", True), ('"a\\"b"', True), ("@p", False), ("x.name", False)]
)
def test_is_string_literal(aql: str, expected: bool) -> None:
    assert is_string_literal(aql) is expected


def test_translation_guards_a_property_and_keeps_a_literal_unguarded() -> None:
    aql = translate(
        "MATCH (n:User) WHERE NOT n.name ENDS WITH 'x' RETURN n.name", mapping=mapping_bundle_for("pg")
    ).aql
    assert "(IS_STRING(n.name) AND NOT (RIGHT(n.name, LENGTH('x')) == 'x'))" in aql
