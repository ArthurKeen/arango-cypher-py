"""Cypher's type conversion functions on top of AQL.

AQL's conversions are total where Cypher's return null: ``TO_NUMBER('abc')``
is 0 (Cypher's ``toInteger('abc')`` is null), ``TO_BOOL('false')`` is true
(any non-empty string is), and ``TO_STRING(null)`` is ``""``. ``TO_NUMBER``
also does not truncate: ``toInteger(3.7)`` is 3, ``TO_NUMBER(3.7)`` is 3.7.

Each conversion takes the argument's static type (see ``arithmetic``) and
emits only the branches that type allows.
"""

from __future__ import annotations

from .arithmetic import _NUMERIC, BOOL, INT, STRING
from .three_valued import _three_valued

# A string Cypher reads as a number: optional sign, digits with an optional
# fraction (or a bare fraction), optional exponent; surrounding spaces allowed.
_NUMERIC_STRING = "'^ *[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)? *$'"


def _guarded(expr: str, arg: str) -> str:
    return _three_valued(expr, expr, (arg,), None)


def _truncate(value: str) -> str:
    """Round toward zero, as Cypher's toInteger() does."""
    return f"(({value} >= 0) ? FLOOR({value}) : CEIL({value}))"


def _numeric_string(arg: str) -> str:
    return f"REGEX_TEST({arg}, {_NUMERIC_STRING})"


def compile_to_integer(arg: str, arg_type: str | None) -> str:
    """``toInteger(x)``: a number truncated; a numeric string parsed and
    truncated; a boolean 1 or 0; anything else null."""
    if arg_type == INT:
        return arg
    if arg_type in _NUMERIC:
        return _guarded(_truncate(arg), arg)
    from_string = f"({_numeric_string(arg)} ? {_truncate(f'TO_NUMBER({arg})')} : null)"
    if arg_type == STRING:
        return _guarded(from_string, arg)
    if arg_type == BOOL:
        return _guarded(f"TO_NUMBER({arg})", arg)
    return (
        f"(IS_NUMBER({arg}) ? {_truncate(arg)} : (IS_STRING({arg}) ? {from_string} "
        f": (IS_BOOL({arg}) ? TO_NUMBER({arg}) : null)))"
    )


def compile_to_float(arg: str, arg_type: str | None) -> str:
    """``toFloat(x)``: a number as is; a numeric string parsed; anything else null."""
    if arg_type in _NUMERIC:
        return arg
    from_string = f"({_numeric_string(arg)} ? TO_NUMBER({arg}) : null)"
    if arg_type == STRING:
        return _guarded(from_string, arg)
    return f"(IS_NUMBER({arg}) ? {arg} : (IS_STRING({arg}) ? {from_string} : null))"


def compile_to_string(arg: str, arg_type: str | None) -> str:
    """``toString(x)``: null stays null (AQL's ``TO_STRING(null)`` is "")."""
    if arg_type == STRING:
        return arg
    return _guarded(f"TO_STRING({arg})", arg)


def compile_to_boolean(arg: str, arg_type: str | None) -> str:
    """``toBoolean(x)``: 'true'/'false' in any case, a boolean as is, a
    number true unless 0; any other string null (AQL's ``TO_BOOL`` is true
    for every non-empty string, 'false' included)."""
    if arg_type == BOOL:
        return arg
    word = f"LOWER(TRIM({arg}))"
    from_string = f"(({word} == 'true') ? true : (({word} == 'false') ? false : null))"
    if arg_type == STRING:
        return _guarded(from_string, arg)
    if arg_type in _NUMERIC:
        return _guarded(f"({arg} != 0)", arg)
    return (
        f"(IS_BOOL({arg}) ? {arg} : (IS_STRING({arg}) ? {from_string} "
        f": (IS_NUMBER({arg}) ? ({arg} != 0) : null)))"
    )
