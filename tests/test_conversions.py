"""Unit tests for the conversion helpers (``_translate_v0.conversions``)."""

from __future__ import annotations

import pytest

from arango_cypher._translate_v0.arithmetic import BOOL, FLOAT, INT, NUMBER, STRING
from arango_cypher._translate_v0.conversions import (
    compile_to_boolean,
    compile_to_float,
    compile_to_integer,
    compile_to_string,
)

_NUMERIC = "'^ *[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)? *$'"


@pytest.mark.parametrize(
    ("arg_type", "expected"),
    [
        (INT, "x"),
        (NUMBER, "((x == null) ? null : ((x >= 0) ? FLOOR(x) : CEIL(x)))"),
        (FLOAT, "((x == null) ? null : ((x >= 0) ? FLOOR(x) : CEIL(x)))"),
        (BOOL, "((x == null) ? null : TO_NUMBER(x))"),
        (
            STRING,
            f"((x == null) ? null : (REGEX_TEST(x, {_NUMERIC}) ? ((TO_NUMBER(x) >= 0) ? FLOOR(TO_NUMBER(x)) "
            ": CEIL(TO_NUMBER(x))) : null))",
        ),
    ],
)
def test_to_integer_by_type(arg_type: str, expected: str) -> None:
    assert compile_to_integer("x", arg_type) == expected


def test_to_integer_of_an_unknown_type_dispatches() -> None:
    aql = compile_to_integer("x", None)
    assert aql.startswith("(IS_NUMBER(x) ? ((x >= 0) ? FLOOR(x) : CEIL(x)) : (IS_STRING(x) ?")
    assert aql.endswith(": (IS_BOOL(x) ? TO_NUMBER(x) : null)))")


def test_to_float_by_type() -> None:
    assert compile_to_float("x", NUMBER) == "x"
    assert (
        compile_to_float("x", STRING)
        == f"((x == null) ? null : (REGEX_TEST(x, {_NUMERIC}) ? TO_NUMBER(x) : null))"
    )
    assert compile_to_float("x", None).startswith("(IS_NUMBER(x) ? x : (IS_STRING(x) ?")


def test_to_string_keeps_null() -> None:
    assert compile_to_string("x", STRING) == "x"
    assert compile_to_string("x", None) == "((x == null) ? null : TO_STRING(x))"
    assert compile_to_string("12", INT) == "TO_STRING(12)"


def test_to_boolean_by_type() -> None:
    assert compile_to_boolean("x", BOOL) == "x"
    assert compile_to_boolean("x", NUMBER) == "((x == null) ? null : (x != 0))"
    assert compile_to_boolean("x", STRING) == (
        "((x == null) ? null : ((LOWER(TRIM(x)) == 'true') ? true : ((LOWER(TRIM(x)) == 'false') ? false : null)))"
    )
    assert compile_to_boolean("x", None).startswith("(IS_BOOL(x) ? x : (IS_STRING(x) ?")
