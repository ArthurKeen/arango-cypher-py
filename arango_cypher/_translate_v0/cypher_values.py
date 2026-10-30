"""Cypher's value semantics over Python values, for compile-time evaluation.

``const_eval`` evaluates an expression whose inputs are all known at translate
time and emits the result as a literal.  That is only correct if evaluation
follows Cypher's rules exactly, which differ from AQL's (and Python's) in ways
that matter:

* equality is three-valued and recursive: ``[1, null] = [1, null]`` is null,
  ``[1, 2] = [1, null]`` is null, ``[1] = [1, null]`` is false;
* values of different types are unequal and *incomparable*: ``1 = '1'`` is
  false, ``1 < '1'`` is null; ``NaN`` is unequal to everything and every
  ordering comparison with it is false;
* integers are 64-bit and overflow is an error; ``/`` on integers truncates
  toward zero and ``%`` takes the dividend's sign (Java's rules);
* ``toString(1.0)`` is ``'1.0'`` and ``toString(1e20)`` is ``'1.0E20'``
  (Java's ``Double.toString``).

Python values model Cypher values: ``None`` (null), ``bool``, ``int``,
``float``, ``str``, ``list``, ``dict`` (map).  Errors raise
:class:`CypherEvalError` carrying the Neo4j error class.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class NotConstant(Exception):
    """The expression cannot be evaluated at translate time.

    Not an error: it depends on data, a parameter, a non-deterministic function
    (``rand()``), or a construct the evaluator does not model.  The translator
    then compiles it to AQL as usual.
    """


class CypherEvalError(Exception):
    """Evaluating a constant expression raised a Cypher error.

    ``cypher_class`` is the Neo4j / TCK error detail (``InvalidArgumentType``,
    ``ArithmeticError``, …); ``category`` is the TCK error kind
    (``TypeError``, ``ArgumentError``, ``ArithmeticError``, ``SemanticError``).
    """

    def __init__(self, category: str, cypher_class: str, message: str) -> None:
        super().__init__(f"{category} ({cypher_class}): {message}")
        self.category = category
        self.cypher_class = cypher_class
        self.message = message


def type_error(message: str, cypher_class: str = "InvalidArgumentType") -> CypherEvalError:
    return CypherEvalError("TypeError", cypher_class, message)


def type_name(v: Any) -> str:
    """Cypher's name for the type of *v*, as used in error messages."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "FLOAT"
    if isinstance(v, str):
        return "STRING"
    if isinstance(v, list):
        return "LIST"
    if isinstance(v, dict):
        return "MAP"
    name = getattr(v, "cypher_type_name", None)
    return name() if callable(name) else type(v).__name__.upper()


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_nan(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)


def check_int(v: int) -> int:
    """*v*, or an error when it does not fit a 64-bit integer."""
    if v < INT64_MIN or v > INT64_MAX:
        raise CypherEvalError("ArithmeticError", "IntegerOverflow", f"integer overflow: {v}")
    return v


# ---------------------------------------------------------------------------
# Hooks for other value kinds (temporal values register here)
# ---------------------------------------------------------------------------


class ValueKind:
    """How a non-primitive value kind (e.g. temporal) takes part in operators.

    Every hook returns ``NotImplemented`` when it does not apply, so several
    kinds can be registered without knowing about each other.
    """

    def owns(self, v: Any) -> bool:
        raise NotImplementedError

    def equal(self, a: Any, b: Any) -> bool | None | Any:
        return NotImplemented

    def compare(self, a: Any, b: Any) -> int | None | Any:
        return NotImplemented

    def add(self, a: Any, b: Any) -> Any:
        return NotImplemented

    def subtract(self, a: Any, b: Any) -> Any:
        return NotImplemented

    def multiply(self, a: Any, b: Any) -> Any:
        return NotImplemented

    def divide(self, a: Any, b: Any) -> Any:
        return NotImplemented

    def property(self, v: Any, key: str) -> Any:
        return NotImplemented

    def to_string(self, v: Any) -> Any:
        return NotImplemented

    def to_json(self, v: Any) -> Any:
        """The value as the query returns it (JSON-compatible)."""
        return NotImplemented


VALUE_KINDS: list[ValueKind] = []


def _kind_of(*values: Any) -> ValueKind | None:
    for kind in VALUE_KINDS:
        if any(kind.owns(v) for v in values):
            return kind
    return None


def _primitive(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, float, str, list, dict))


# ---------------------------------------------------------------------------
# Equality, IN, ordering
# ---------------------------------------------------------------------------


def equal(a: Any, b: Any) -> bool | None:
    """Cypher ``a = b``: True, False, or None when a null makes it unknown."""
    if a is None or b is None:
        return None
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return False
        return _all_equal(zip(a, b, strict=True))
    if isinstance(a, dict):
        if not isinstance(b, dict) or a.keys() != b.keys():
            return False
        return _all_equal((a[k], b[k]) for k in a)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a):
        if not is_number(b):
            return False
        return a == b  # NaN != NaN, as in Cypher
    if isinstance(a, str):
        return isinstance(b, str) and a == b
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.equal(a, b)
        if result is not NotImplemented:
            return result
    if not _primitive(a) or not _primitive(b):
        return False if type(a) is not type(b) else a == b
    return False


def _all_equal(pairs: Any) -> bool | None:
    result: bool | None = True
    for x, y in pairs:
        r = equal(x, y)
        if r is False:
            return False
        if r is None:
            result = None
    return result


def not3(v: bool | None) -> bool | None:
    return None if v is None else not v


def in_list(x: Any, lst: Any) -> bool | None:
    """Cypher ``x IN lst``."""
    if lst is None:
        return None
    if not isinstance(lst, list):
        raise type_error(f"IN expects a list, got {type_name(lst)}")
    result: bool | None = False
    for e in lst:
        r = equal(x, e)
        if r is True:
            return True
        if r is None:
            result = None
    return result


def compare(a: Any, b: Any) -> int | None:
    """Order two values of a mutually comparable type: -1, 0 or 1.

    None when Cypher's answer is null: a null operand, or types that do not
    order against each other.  NaN orders as equal here; :func:`ordering`
    turns it into false.
    """
    if a is None or b is None:
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        if not (isinstance(a, bool) and isinstance(b, bool)):
            return None
        return (a > b) - (a < b)
    if is_number(a):
        if not is_number(b):
            return None
        return (a > b) - (a < b)
    if isinstance(a, str):
        if not isinstance(b, str):
            return None
        return (a > b) - (a < b)  # code point order, as Cypher
    if isinstance(a, list):
        if not isinstance(b, list):
            return None
        for x, y in zip(a, b, strict=False):
            c = compare(x, y)
            if c is None or c != 0:
                return c
        return (len(a) > len(b)) - (len(a) < len(b))
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.compare(a, b)
        if result is not NotImplemented:
            return result
    return None


def ordering(op: str, a: Any, b: Any) -> bool | None:
    """Cypher ``a <op> b`` for ``<``, ``<=``, ``>``, ``>=``."""
    c = compare(a, b)
    if c is None:
        return None
    if _contains_nan(a) or _contains_nan(b):
        return False
    if op == "<":
        return c < 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == ">=":
        return c >= 0
    raise ValueError(f"not an ordering operator: {op}")


def _contains_nan(v: Any) -> bool:
    if is_nan(v):
        return True
    if isinstance(v, list):
        return any(_contains_nan(x) for x in v)
    return False


# ---------------------------------------------------------------------------
# Boolean logic
# ---------------------------------------------------------------------------


def as_boolean(v: Any, operator: str) -> bool | None:
    if v is None or isinstance(v, bool):
        return v
    raise type_error(f"{operator} expects a boolean, got {type_name(v)}")


def and3(values: list[Any]) -> bool | None:
    result: bool | None = True
    for v in values:
        b = as_boolean(v, "AND")
        if b is False:
            result = False
        elif b is None and result is True:
            result = None
    return result


def or3(values: list[Any]) -> bool | None:
    result: bool | None = False
    for v in values:
        b = as_boolean(v, "OR")
        if b is True:
            result = True
        elif b is None and result is False:
            result = None
    return result


def xor3(a: Any, b: Any) -> bool | None:
    x, y = as_boolean(a, "XOR"), as_boolean(b, "XOR")
    if x is None or y is None:
        return None
    return x != y


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def _numeric(a: Any, b: Any, op: str) -> tuple[Any, Any]:
    if not is_number(a) or not is_number(b):
        raise type_error(f"cannot apply {op} to {type_name(a)} and {type_name(b)}")
    return a, b


def add(a: Any, b: Any) -> Any:
    # Null first, as Neo4j's CypherMath.add: [1] + null is null, not [1, null].
    if a is None or b is None:
        return None
    if isinstance(a, list) or isinstance(b, list):
        # List concatenation and element append/prepend.
        left = a if isinstance(a, list) else [a]
        right = b if isinstance(b, list) else [b]
        return left + right
    if isinstance(a, str) and isinstance(b, str):
        return a + b
    if isinstance(a, str) and (is_number(b) or isinstance(b, bool)):
        return a + _text(b)
    if isinstance(b, str) and (is_number(a) or isinstance(a, bool)):
        return _text(a) + b
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.add(a, b)
        if result is not NotImplemented:
            return result
        if isinstance(a, str) or isinstance(b, str):
            return _text(a) + _text(b)
    _numeric(a, b, "+")
    return _arith_result(a + b, a, b)


def _text(v: Any) -> str:
    """``toString`` of a value already known not to be null."""
    text = to_string(v)
    if text is None:
        raise ValueError("_text() of null")
    return text


def subtract(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.subtract(a, b)
        if result is not NotImplemented:
            return result
    _numeric(a, b, "-")
    return _arith_result(a - b, a, b)


def multiply(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.multiply(a, b)
        if result is not NotImplemented:
            return result
    _numeric(a, b, "*")
    return _arith_result(a * b, a, b)


def divide(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    kind = _kind_of(a, b)
    if kind is not None:
        result = kind.divide(a, b)
        if result is not NotImplemented:
            return result
    _numeric(a, b, "/")
    if isinstance(a, int) and isinstance(b, int):
        if b == 0:
            raise CypherEvalError("ArithmeticError", "DivisionByZero", "/ by zero")
        q = abs(a) // abs(b)
        return check_int(q if (a >= 0) == (b >= 0) else -q)
    return _float_divide(float(a), float(b))


def _float_divide(a: float, b: float) -> float:
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return math.nan
        negative = (a < 0) != (math.copysign(1.0, b) < 0)
        return -math.inf if negative else math.inf
    return a / b


def modulo(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    _numeric(a, b, "%")
    if isinstance(a, int) and isinstance(b, int):
        if b == 0:
            raise CypherEvalError("ArithmeticError", "DivisionByZero", "% by zero")
        return int(math.fmod(a, b)) if abs(a) < 2**53 and abs(b) < 2**53 else _java_int_mod(a, b)
    if float(b) == 0.0:
        return math.nan
    return math.fmod(float(a), float(b))


def _java_int_mod(a: int, b: int) -> int:
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def power(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    _numeric(a, b, "^")
    try:
        return float(a) ** float(b)
    except OverflowError:
        return math.inf
    except ZeroDivisionError:
        return math.inf


def negate(a: Any) -> Any:
    if a is None:
        return None
    kind = _kind_of(a)
    if kind is not None:
        result = kind.multiply(a, -1)
        if result is not NotImplemented:
            return result
    if not is_number(a):
        raise type_error(f"cannot negate {type_name(a)}")
    return check_int(-a) if isinstance(a, int) else -a


def _arith_result(result: Any, a: Any, b: Any) -> Any:
    if isinstance(a, int) and isinstance(b, int):
        return check_int(result)
    return float(result)


# ---------------------------------------------------------------------------
# String conversion (Java's rules, as Neo4j)
# ---------------------------------------------------------------------------


def format_float(f: float) -> str:
    """Java ``Double.toString``: ``1.0``, ``0.001``, ``1.0E7``, ``1.0E-4``, ``NaN``.

    Plain notation for magnitudes in [1e-3, 1e7), otherwise ``d.dddE<n>``;
    always at least one fractional digit; the shortest digits that round-trip.
    """
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    if f == 0.0:
        return "-0.0" if math.copysign(1.0, f) < 0 else "0.0"
    sign = "-" if f < 0 else ""
    mag = abs(f)
    exact = Decimal(repr(mag))
    if 1e-3 <= mag < 1e7:
        text = format(exact, "f")
        if "." not in text:
            text += ".0"
        return sign + text
    _, digit_tuple, exponent = exact.as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    assert isinstance(exponent, int)
    sci_exponent = len(digit_tuple) + exponent - 1
    return f"{sign}{digits[0]}.{digits[1:] or '0'}E{sci_exponent}"


def to_string(v: Any) -> str | None:
    """Cypher ``toString`` of a scalar."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format_float(v)
    if isinstance(v, str):
        return v
    kind = _kind_of(v)
    if kind is not None:
        result = kind.to_string(v)
        if result is not NotImplemented:
            return result
    raise type_error(f"toString() cannot convert {type_name(v)}")


# ---------------------------------------------------------------------------
# Indexing, slicing, property access
# ---------------------------------------------------------------------------


def index(container: Any, idx: Any) -> Any:
    """Cypher ``container[idx]``: list by integer, map by string."""
    if container is None or idx is None:
        return None
    if isinstance(container, list):
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise type_error(f"list index must be an INTEGER, got {type_name(idx)}")
        n = len(container)
        i = idx + n if idx < 0 else idx
        return container[i] if 0 <= i < n else None
    if isinstance(container, dict):
        if not isinstance(idx, str):
            raise type_error(f"map key must be a STRING, got {type_name(idx)}")
        return container.get(idx)
    kind = _kind_of(container)
    if kind is not None and isinstance(idx, str):
        result = kind.property(container, idx)
        if result is not NotImplemented:
            return result
    raise type_error(f"cannot index {type_name(container)}")


def slice_list(lst: Any, start: Any, end: Any, *, has_start: bool, has_end: bool) -> Any:
    """Cypher ``lst[start..end]``; an omitted bound is open, a null bound gives null."""
    if lst is None:
        return None
    if (has_start and start is None) or (has_end and end is None):
        return None
    if not isinstance(lst, list):
        raise type_error(f"cannot slice {type_name(lst)}")
    for bound in (start, end):
        if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool)):
            raise type_error(f"list slice bound must be an INTEGER, got {type_name(bound)}")
    n = len(lst)
    lo = 0 if start is None else (max(start + n, 0) if start < 0 else min(start, n))
    hi = n if end is None else (max(end + n, 0) if end < 0 else min(end, n))
    return lst[lo:hi] if lo < hi else []


def get_property(v: Any, key: str) -> Any:
    """Cypher ``v.key``."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get(key)
    kind = _kind_of(v)
    if kind is not None:
        result = kind.property(v, key)
        if result is not NotImplemented:
            return result
    raise type_error(f"type mismatch: expected a map but was {type_name(v)}")


def to_json(v: Any) -> Any:
    """The value as an AQL bind variable / query result carries it."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, list):
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    kind = _kind_of(v)
    if kind is not None:
        result = kind.to_json(v)
        if result is not NotImplemented:
            return result
    raise NotConstant(f"no JSON form for {type_name(v)}")


def representable(v: Any) -> bool:
    """Whether AQL can carry *v* exactly (no NaN/Infinity, which JSON lacks)."""
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, list):
        return all(representable(x) for x in v)
    if isinstance(v, dict):
        return all(representable(x) for x in v.values())
    return True


#: Function name (lower case) → implementation over evaluated arguments.
FunctionImpl = Callable[[list[Any]], Any]
