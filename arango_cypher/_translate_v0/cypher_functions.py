"""Cypher's scalar, list, string, math and conversion functions over Python
values, for compile-time evaluation (``const_eval``).

Each implementation takes the evaluated argument list and follows Neo4j's
behaviour: null in → null out (unless the function is defined on null),
a wrong argument type raises a ``TypeError`` (InvalidArgumentType), an
argument out of range an ``ArgumentError``.  Functions that read data or the
clock, or are random (``rand()``, ``timestamp()``), are deliberately absent:
they are not constant.  ``register()`` adds more (the temporal functions).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from .cypher_values import (
    CypherEvalError,
    FunctionImpl,
    NotConstant,
    check_int,
    is_number,
    to_string,
    type_error,
    type_name,
)

FUNCTIONS: dict[str, FunctionImpl] = {}


def register(name: str, impl: FunctionImpl) -> None:
    FUNCTIONS[name.lower()] = impl


def _fn(name: str, arity: int | tuple[int, int]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    lo, hi = (arity, arity) if isinstance(arity, int) else arity

    def deco(f: Callable[..., Any]) -> Callable[..., Any]:
        def impl(args: list[Any]) -> Any:
            if not lo <= len(args) <= hi:
                raise CypherEvalError(
                    "SyntaxError",
                    "InvalidNumberOfArguments",
                    f"{name}() takes {lo if lo == hi else f'{lo} to {hi}'} argument(s), got {len(args)}",
                )
            return f(*args)

        register(name, impl)
        return f

    return deco


def _arg_error(name: str, value: Any, expected: str) -> CypherEvalError:
    return type_error(f"{name}() expects {expected}, got {type_name(value)}")


def _int_arg(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _arg_error(name, value, "an INTEGER")
    return value


# ---------------------------------------------------------------------------
# Predicates and string operators
# ---------------------------------------------------------------------------


def string_predicate(value: Any, other: Any, test: Callable[[str, str], bool]) -> bool | None:
    """STARTS WITH / ENDS WITH / CONTAINS: null unless both are strings."""
    if not isinstance(value, str) or not isinstance(other, str):
        return None
    return test(value, other)


def regex_match(value: Any, pattern: Any) -> bool | None:
    if not isinstance(value, str) or not isinstance(pattern, str):
        return None
    try:
        return re.fullmatch(pattern, value, flags=re.DOTALL if "(?s)" in pattern else 0) is not None
    except re.error as e:
        raise CypherEvalError(
            "ArgumentError", "InvalidArgumentValue", f"invalid regex {pattern!r}: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Scalar / list functions
# ---------------------------------------------------------------------------


@_fn("size", 1)
def _size(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (list, str)):
        return len(v)
    raise _arg_error("size", v, "a LIST or STRING")


@_fn("head", 1)
def _head(v: Any) -> Any:
    if v is None:
        return None
    if not isinstance(v, list):
        raise _arg_error("head", v, "a LIST")
    return v[0] if v else None


@_fn("last", 1)
def _last(v: Any) -> Any:
    if v is None:
        return None
    if not isinstance(v, list):
        raise _arg_error("last", v, "a LIST")
    return v[-1] if v else None


@_fn("tail", 1)
def _tail(v: Any) -> Any:
    if v is None:
        return None
    if not isinstance(v, list):
        raise _arg_error("tail", v, "a LIST")
    return v[1:]


@_fn("reverse", 1)
def _reverse(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (list, str)):
        return v[::-1]
    raise _arg_error("reverse", v, "a LIST or STRING")


@_fn("range", (2, 3))
def _range(start: Any, end: Any, step: Any = 1) -> Any:
    start, end, step = _int_arg("range", start), _int_arg("range", end), _int_arg("range", step)
    if step == 0:
        raise CypherEvalError("ArgumentError", "NumberOutOfRange", "range() step must not be zero")
    count = (end - start) // step + 1
    if count > 1_000_000:
        raise NotConstant(f"range() of {count} elements is evaluated at run time")
    return list(range(start, end + (1 if step > 0 else -1), step))


@_fn("keys", 1)
def _keys(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, dict):
        return list(v.keys())
    raise _arg_error("keys", v, "a MAP, NODE or RELATIONSHIP")


@_fn("coalesce", (1, 1 << 16))
def _coalesce(*args: Any) -> Any:
    return next((a for a in args if a is not None), None)


@_fn("isempty", 1)
def _is_empty(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (list, str, dict)):
        return len(v) == 0
    raise _arg_error("isEmpty", v, "a LIST, MAP or STRING")


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*$")


def _parse_number_string(s: str) -> int | float | None:
    if re.fullmatch(r"\s*[+-]?\d+\s*", s):
        return int(s)
    if re.fullmatch(r"\s*0[xX][0-9a-fA-F]+\s*", s):
        return int(s, 16)
    if _NUMBER_RE.match(s):
        return float(s)
    return None


@_fn("tointeger", 1)
def _to_integer(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise CypherEvalError(
                "ArgumentError", "InvalidArgumentValue", f"cannot convert {v} to an integer"
            )
        return check_int(int(v))
    if isinstance(v, str):
        parsed = _parse_number_string(v)
        if parsed is None:
            return None
        if isinstance(parsed, float):
            if math.isnan(parsed) or math.isinf(parsed):
                return None
            return check_int(int(parsed))
        return check_int(parsed)
    raise _arg_error("toInteger", v, "a STRING, INTEGER, FLOAT or BOOLEAN")


@_fn("tofloat", 1)
def _to_float(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        raise _arg_error("toFloat", v, "a STRING, INTEGER or FLOAT")
    if is_number(v):
        return float(v)
    if isinstance(v, str):
        parsed = _parse_number_string(v)
        return None if parsed is None else float(parsed)
    raise _arg_error("toFloat", v, "a STRING, INTEGER or FLOAT")


@_fn("toboolean", 1)
def _to_boolean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        t = v.strip().lower()
        return True if t == "true" else False if t == "false" else None
    raise _arg_error("toBoolean", v, "a STRING, INTEGER or BOOLEAN")


@_fn("tostring", 1)
def _to_string(v: Any) -> Any:
    if isinstance(v, (list, dict)):
        raise _arg_error("toString", v, "a scalar")
    return to_string(v)


def _or_null(f: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def impl(v: Any) -> Any:
        try:
            return f(v)
        except CypherEvalError:
            return None

    return impl


_fn("tointegerornull", 1)(_or_null(_to_integer))
_fn("tofloatornull", 1)(_or_null(_to_float))
_fn("tobooleanornull", 1)(_or_null(_to_boolean))
_fn("tostringornull", 1)(_or_null(_to_string))


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _number_arg(name: str, v: Any) -> float | int:
    if not is_number(v):
        raise _arg_error(name, v, "a number")
    return v


@_fn("abs", 1)
def _abs(v: Any) -> Any:
    if v is None:
        return None
    n = _number_arg("abs", v)
    return check_int(abs(n)) if isinstance(n, int) else abs(n)


@_fn("sign", 1)
def _sign(v: Any) -> Any:
    if v is None:
        return None
    n = _number_arg("sign", v)
    if isinstance(n, float) and math.isnan(n):
        return 0
    return (n > 0) - (n < 0)


def _float_fn(name: str, f: Callable[[float], float]) -> None:
    def impl(v: Any) -> Any:
        if v is None:
            return None
        n = float(_number_arg(name, v))
        try:
            return f(n)
        except (ValueError, OverflowError):
            return math.nan

    _fn(name, 1)(impl)


_float_fn("ceil", lambda n: float(math.ceil(n)) if math.isfinite(n) else n)
_float_fn("floor", lambda n: float(math.floor(n)) if math.isfinite(n) else n)
_float_fn("sqrt", lambda n: math.sqrt(n) if n >= 0 else math.nan)
_float_fn("exp", math.exp)
_float_fn("log", lambda n: math.log(n) if n > 0 else (-math.inf if n == 0 else math.nan))
_float_fn("log10", lambda n: math.log10(n) if n > 0 else (-math.inf if n == 0 else math.nan))
_float_fn("sin", math.sin)
_float_fn("cos", math.cos)
_float_fn("tan", math.tan)
_float_fn("asin", math.asin)
_float_fn("acos", math.acos)
_float_fn("atan", math.atan)
_float_fn("degrees", math.degrees)
_float_fn("radians", math.radians)


@_fn("atan2", 2)
def _atan2(y: Any, x: Any) -> Any:
    if y is None or x is None:
        return None
    return math.atan2(float(_number_arg("atan2", y)), float(_number_arg("atan2", x)))


@_fn("round", (1, 3))
def _round(v: Any, precision: Any = None, mode: Any = None) -> Any:
    if v is None:
        return None
    if mode is not None:
        raise NotConstant("round() with a rounding mode")
    n = float(_number_arg("round", v))
    if not math.isfinite(n):
        return n
    if precision is None:
        # Java Math.round: half up (toward positive infinity).
        return float(math.floor(n + 0.5))
    p = _int_arg("round", precision)
    factor = 10.0**p
    return math.floor(abs(n) * factor + 0.5) / factor * (1 if n >= 0 else -1)


@_fn("e", 0)
def _e() -> float:
    return math.e


@_fn("pi", 0)
def _pi() -> float:
    return math.pi


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------


def _str_arg(name: str, v: Any) -> str:
    if not isinstance(v, str):
        raise _arg_error(name, v, "a STRING")
    return v


def _string_fn(name: str, f: Callable[[str], str]) -> None:
    def impl(v: Any) -> Any:
        return None if v is None else f(_str_arg(name, v))

    _fn(name, 1)(impl)


_string_fn("tolower", str.lower)
_string_fn("toupper", str.upper)
_string_fn("lower", str.lower)
_string_fn("upper", str.upper)
_string_fn("trim", lambda s: s.strip(" \t\n\r\f\v"))
_string_fn("ltrim", lambda s: s.lstrip(" \t\n\r\f\v"))
_string_fn("rtrim", lambda s: s.rstrip(" \t\n\r\f\v"))


@_fn("replace", 3)
def _replace(s: Any, search: Any, repl: Any) -> Any:
    if s is None or search is None or repl is None:
        return None
    return _str_arg("replace", s).replace(_str_arg("replace", search), _str_arg("replace", repl))


@_fn("substring", (2, 3))
def _substring(s: Any, start: Any, length: Any = None) -> Any:
    if s is None:
        return None
    text, begin = _str_arg("substring", s), _int_arg("substring", start)
    if begin < 0:
        raise CypherEvalError("ArgumentError", "NumberOutOfRange", "substring() start must not be negative")
    if length is None:
        return text[begin:]
    n = _int_arg("substring", length)
    if n < 0:
        raise CypherEvalError("ArgumentError", "NumberOutOfRange", "substring() length must not be negative")
    return text[begin : begin + n]


@_fn("left", 2)
def _left(s: Any, n: Any) -> Any:
    if s is None:
        return None
    count = _int_arg("left", n)
    if count < 0:
        raise CypherEvalError("ArgumentError", "NumberOutOfRange", "left() length must not be negative")
    return _str_arg("left", s)[:count]


@_fn("right", 2)
def _right(s: Any, n: Any) -> Any:
    if s is None:
        return None
    count = _int_arg("right", n)
    if count < 0:
        raise CypherEvalError("ArgumentError", "NumberOutOfRange", "right() length must not be negative")
    text = _str_arg("right", s)
    return text[len(text) - count :] if count else ""


@_fn("split", 2)
def _split(s: Any, sep: Any) -> Any:
    if s is None or sep is None:
        return None
    text = _str_arg("split", s)
    if isinstance(sep, list):
        pattern = "|".join(re.escape(_str_arg("split", x)) for x in sep)
        return re.split(pattern, text) if pattern else list(text)
    delimiter = _str_arg("split", sep)
    return text.split(delimiter) if delimiter else list(text)
