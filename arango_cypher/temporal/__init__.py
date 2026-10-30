"""Cypher's temporal types with Neo4j's java.time semantics, in pure Python.

The value library the translator calls to evaluate constant temporal
expressions at compile time (``RETURN date({year: 1816, week: 1})``,
``duration.between(...)``, ``d.week``, ``dt + duration('P1M')`` …) and emit
the result as a literal. It has no dependency on the translator.

Values (frozen, hashable; ``str(v)`` is the TCK's expected string form):
``Date``, ``LocalTime``, ``Time``, ``LocalDateTime``, ``DateTime``, ``Duration``.

Functions:

* ``construct(kind, arg)`` — ``date(x)`` … ``duration(x)`` of a string, a map
  or a temporal value; null gives None; the clock forms raise ``NotConstant``.
* ``truncate(kind, unit, value, overrides)`` — ``<kind>.truncate(...)``.
* ``from_epoch(seconds, nanos)``, ``from_epoch_millis(ms)``.
* ``duration_between(a, b, unit)`` — unit None / 'months' / 'days' / 'seconds'.
* ``add``, ``subtract``, ``multiply``, ``divide``.
* ``compare(a, b)`` (-1/0/1 or None), ``equals(a, b)`` (bool or None).
* ``get_property(value, name)`` — every accessor Neo4j supports.
* ``to_string(value)``, ``is_temporal(x)``, ``clock(kind, ...)``.

Errors are ``TemporalError`` subclasses carrying the Neo4j error class name
(``cypher_class``); ``NotConstant`` signals a clock-reading expression. The
semantics are ported from the arango-gql-gateway Go engine
(``internal/temporal``), which passes every TCK Temporal1–10 scenario.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .arith import (
    DATE_ACCESSORS,
    DURATION_ACCESSORS,
    INSTANT_ACCESSORS,
    TIME_ACCESSORS,
    ZONE_ACCESSORS,
    accessors,
    add,
    compare,
    divide,
    equals,
    get_property,
    multiply,
    subtract,
)
from .between import duration_between
from .builders import TRUNCATION_UNITS, build, construct, convert, from_epoch, from_epoch_millis, truncate
from .errors import (
    InvalidTemporalType,
    InvalidTemporalValue,
    NotConstant,
    TemporalError,
    TemporalOverflow,
    UnsupportedTemporalUnit,
)
from .parsing import parse
from .values import (
    KINDS,
    Date,
    DateTime,
    Duration,
    LocalDateTime,
    LocalTime,
    Temporal,
    Time,
    is_temporal,
)
from .zones import Zone, parse_zone, zone_names

CLOCK_SOURCES = ("statement", "transaction", "realtime")


def to_string(value: Temporal | None) -> str | None:
    """``toString(value)`` of a temporal value; None for null."""
    if value is None:
        return None
    if not is_temporal(value):
        raise InvalidTemporalType(f"to_string() needs a temporal value, got {value!r}")
    return str(value)


_NO_TIMEZONE: Any = object()


def clock(kind: str, source: str = "statement", timezone: Any = _NO_TIMEZONE) -> None:
    """``<kind>.statement()``, ``.transaction()``, ``.realtime()``, with or
    without a time zone argument: ``NotConstant``, as the clock is read at run
    time, except that a null argument gives null (``date.statement(null)``).
    An invalid kind, source or time zone is still reported as an error."""
    if kind not in KINDS or kind == "duration":
        raise InvalidTemporalValue(f"{kind!r} has no clock function")
    if source not in CLOCK_SOURCES:
        raise InvalidTemporalValue(f"unknown clock {source!r}, expected one of {CLOCK_SOURCES}")
    if timezone is None:
        return None
    if timezone is not _NO_TIMEZONE:
        if isinstance(timezone, str):
            parse_zone(timezone)
        elif not isinstance(timezone, Mapping):
            raise InvalidTemporalType(f"{kind}.{source}(): expected a time zone or a map, got {timezone!r}")
    raise NotConstant(f"{kind}.{source}() reads the clock")


__all__ = [
    "CLOCK_SOURCES",
    "DATE_ACCESSORS",
    "DURATION_ACCESSORS",
    "INSTANT_ACCESSORS",
    "KINDS",
    "TIME_ACCESSORS",
    "TRUNCATION_UNITS",
    "ZONE_ACCESSORS",
    "Date",
    "DateTime",
    "Duration",
    "InvalidTemporalType",
    "InvalidTemporalValue",
    "LocalDateTime",
    "LocalTime",
    "NotConstant",
    "Temporal",
    "TemporalError",
    "TemporalOverflow",
    "Time",
    "UnsupportedTemporalUnit",
    "Zone",
    "accessors",
    "add",
    "build",
    "clock",
    "compare",
    "construct",
    "convert",
    "divide",
    "duration_between",
    "equals",
    "from_epoch",
    "from_epoch_millis",
    "get_property",
    "is_temporal",
    "multiply",
    "parse",
    "parse_zone",
    "subtract",
    "to_string",
    "truncate",
    "zone_names",
]
