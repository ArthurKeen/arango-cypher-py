"""Constructing temporal values: from a string, from a map of fields and
selections, by converting another value, by truncation, and from the epoch.

``date({year: 1984, week: 10, dayOfWeek: 3})``, ``time({time: t, timezone:
'+05:00'})``, ``datetime({date: d, time: t, day: 28})``,
``localdatetime.truncate('week', v, {dayOfWeek: 2})`` … follow Neo4j's
builders: a selected value (``date``, ``time``, ``datetime``) is the base the
other fields override; without one, ``year`` (date kinds) or ``hour`` (time
kinds) is required and fields must be given in order (no ``day`` without
``month``, no ``second`` without ``minute``).
"""

from __future__ import annotations

import time as _clock
from collections.abc import Mapping
from typing import Any

from ._calendar import (
    NANOS_PER_SECOND,
    calendar_date,
    check_year,
    civil_from_days,
    day_of_quarter,
    days_from_civil,
    iso_day_of_week,
    iso_week,
    ordinal_date,
    quarter_date,
    quarter_of,
    week_date,
    with_year_month,
)
from .errors import (
    InvalidTemporalType,
    InvalidTemporalValue,
    NotConstant,
    TemporalOverflow,
    UnsupportedTemporalUnit,
)
from .parsing import approximate, parse
from .values import (
    KINDS,
    Date,
    DateTime,
    Duration,
    LocalDateTime,
    LocalTime,
    Temporal,
    Time,
    datetime_at_instant,
    datetime_in_zone,
    datetime_of_local,
    has_date,
    has_time,
    is_temporal,
    kind_has_date,
    kind_has_zone,
    make_duration,
    make_local_datetime,
    make_local_time,
    make_time,
)
from .zones import UTC_ZONE, Zone, parse_zone

DATE_FIELDS = ("year", "month", "day", "week", "dayOfWeek", "quarter", "dayOfQuarter", "ordinalDay")
TIME_FIELDS = ("hour", "minute", "second", "millisecond", "microsecond", "nanosecond")
SUBSECOND_FIELDS = ("millisecond", "microsecond", "nanosecond")

# The four ways to name a day; a map may use only one of them.
_DATE_GROUPS: dict[str, tuple[str, ...]] = {
    "calendar": ("month", "day"),
    "week": ("week", "dayOfWeek"),
    "quarter": ("quarter", "dayOfQuarter"),
    "ordinal": ("ordinalDay",),
}

_DURATION_SCALE: dict[str, tuple[int, int]] = {  # component (months, days, seconds, nanos), factor
    "years": (0, 12),
    "quarters": (0, 3),
    "months": (0, 1),
    "weeks": (1, 7),
    "days": (1, 1),
    "hours": (2, 3600),
    "minutes": (2, 60),
    "seconds": (2, 1),
    "milliseconds": (3, 1_000_000),
    "microseconds": (3, 1000),
    "nanoseconds": (3, 1),
}

# Duration components stay inside a 64-bit integer.
DURATION_LIMIT = 9e18

_NO_ARGUMENT: Any = object()


def _now_seconds() -> int:
    """The current instant, used only where Neo4j itself reads the clock to
    pick a named zone's offset for a Time (a Time has no date)."""
    return int(_clock.time())


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise InvalidTemporalValue(f"unknown temporal type {kind!r}")
    return kind


def construct(kind: str, arg: Any = _NO_ARGUMENT) -> Temporal | None:
    """``date(x)``, ``localtime(x)``, ``time(x)``, ``localdatetime(x)``,
    ``datetime(x)`` and ``duration(x)`` of a known argument: a string, a map
    of fields (temporal selections already evaluated), or a temporal value.

    Null gives None. The clock forms (no argument, or a map holding only a
    ``timezone``) raise ``NotConstant``.
    """
    _check_kind(kind)
    if arg is _NO_ARGUMENT:
        if kind == "duration":
            raise InvalidTemporalType("duration() requires an argument")
        raise NotConstant(f"{kind}() reads the clock")
    if arg is None:
        return None
    if isinstance(arg, str):
        return parse(kind, arg)
    if is_temporal(arg):
        return convert(kind, arg)
    if isinstance(arg, Mapping):
        return build(kind, arg)
    raise InvalidTemporalType(f"{kind}() cannot be built from {type(arg).__name__} {arg!r}")


# --- conversion ----------------------------------------------------------------


def convert(kind: str, value: Temporal) -> Temporal:
    """Project a temporal value onto ``kind``, as ``date(datetime(...))`` does."""
    _check_kind(kind)
    bad = InvalidTemporalType(f"cannot convert a {value.KIND} to a {kind}")
    if isinstance(value, Duration) or kind == "duration":
        if value.KIND == kind:
            return value
        raise bad
    if kind == "date":
        if isinstance(value, Date):
            return value
        if isinstance(value, LocalDateTime | DateTime):
            return Date(value.epoch_day)
        raise bad
    if kind == "localtime":
        if isinstance(value, LocalTime | Time | LocalDateTime | DateTime):
            return LocalTime(value.nanos)
        raise bad
    if kind == "time":
        if isinstance(value, Time):
            return value
        if isinstance(value, DateTime):
            return Time(value.nanos, value.offset)
        if isinstance(value, LocalTime | LocalDateTime):
            return Time(value.nanos, 0)
        raise bad
    if kind == "localdatetime":
        if isinstance(value, Date):
            return LocalDateTime(value.epoch_day, 0)
        if isinstance(value, LocalDateTime | DateTime):
            return LocalDateTime(value.epoch_day, value.nanos)
        raise bad
    # datetime
    if isinstance(value, DateTime):
        return value
    if isinstance(value, Date):
        return datetime_of_local(value.epoch_day, 0, UTC_ZONE)
    if isinstance(value, LocalDateTime):
        return datetime_of_local(value.epoch_day, value.nanos, UTC_ZONE)
    raise bad


# --- maps of fields --------------------------------------------------------------


class _Fields:
    """A constructor map, with typed access to its numeric fields."""

    def __init__(self, kind: str, fields: Mapping[str, Any]) -> None:
        self.kind = kind
        self.map = dict(fields)
        for key in self.map:
            if not isinstance(key, str):
                raise InvalidTemporalType(f"{kind}(): field names must be strings, got {key!r}")

    def has(self, *keys: str) -> bool:
        return any(k in self.map for k in keys)

    def num(self, key: str) -> int | None:
        """The integer value of a field, None when absent; a fractional or
        non-numeric value is an error."""
        if key not in self.map:
            return None
        value = self.map[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidTemporalType(f"{self.kind}(): {key} must be an integer, got {value!r}")
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")) or value != int(value):
                raise InvalidTemporalValue(f"{self.kind}(): {key} must be an integer, got {value!r}")
            return int(value)
        return value

    def base(self, key: str) -> Temporal | None:
        value = self.map.get(key)
        if value is None:
            return None
        if not is_temporal(value):
            raise InvalidTemporalType(f"{self.kind}(): {key} must be a temporal value, got {value!r}")
        return value


def build(kind: str, fields: Mapping[str, Any]) -> Temporal:
    """Construct a value of ``kind`` from a map of components and selections."""
    _check_kind(kind)
    f = _Fields(kind, fields)
    if kind == "duration":
        return _build_duration(f)
    if set(f.map) == {"timezone"}:
        raise NotConstant(f"{kind}({{timezone: …}}) reads the clock")
    if not f.map:
        raise InvalidTemporalValue(f"{kind}(): at least one temporal unit must be specified")
    if kind == "datetime" and f.has("epochSeconds", "epochMillis"):
        return _build_from_epoch(f)
    for key in f.map:
        if not _valid_key(kind, key):
            raise UnsupportedTemporalUnit(f"{kind}(): unsupported field {key!r}")
    zone = _zone_field(f)
    if kind == "date":
        return Date(_build_date(f, None))
    if kind == "localtime":
        nanos, _ = _build_time(f)
        return make_local_time(nanos)
    if kind == "time":
        return _build_time_value(f, zone)
    if kind == "localdatetime":
        dt = f.base("datetime")
        if dt is not None:
            if not isinstance(dt, Date | LocalDateTime | DateTime):
                raise InvalidTemporalType(f"localdatetime(): datetime must have a date, got a {dt.KIND}")
            day = _build_date(f, dt.epoch_day)
            return make_local_datetime(day, _apply_time(f, _nanos_of(dt)))
        day = _build_date(f, None)
        nanos, _ = _build_time(f)
        return make_local_datetime(day, nanos)
    return _build_datetime(f, zone)


def _nanos_of(value: Temporal) -> int:
    return value.nanos if isinstance(value, LocalTime | Time | LocalDateTime | DateTime) else 0


def _valid_key(kind: str, key: str) -> bool:
    if kind == "date":
        return key in DATE_FIELDS or key == "date"
    if kind == "localtime":
        return key in TIME_FIELDS or key == "time"
    if kind == "time":
        return key in TIME_FIELDS or key in ("time", "timezone")
    if kind == "localdatetime":
        return key in DATE_FIELDS or key in TIME_FIELDS or key in ("date", "time", "datetime")
    return key in DATE_FIELDS or key in TIME_FIELDS or key in ("date", "time", "datetime", "timezone")


def _zone_field(f: _Fields) -> Zone | None:
    value = f.map.get("timezone")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidTemporalType(f"{f.kind}(): timezone must be a string, got {value!r}")
    return parse_zone(value)


def _check_date_fields(f: _Fields, has_base: bool) -> None:
    """Neo4j's rules for the date fields of a map: one way of naming the day,
    and without a base date ``year`` first and the fields in order."""
    groups = [name for name, keys in _DATE_GROUPS.items() if f.has(*keys)]
    if len(groups) > 1:
        raise UnsupportedTemporalUnit(
            f"{f.kind}(): cannot mix {' and '.join(groups)} fields in one date: {sorted(f.map)}"
        )
    if has_base:
        return
    if not f.has("year"):
        raise UnsupportedTemporalUnit(f"{f.kind}(): year must be specified")
    for first, second in (("month", "day"), ("week", "dayOfWeek"), ("quarter", "dayOfQuarter")):
        if f.has(second) and not f.has(first):
            raise UnsupportedTemporalUnit(f"{f.kind}(): {second} cannot be specified without {first}")


def _build_date(f: _Fields, base: int | None) -> int:
    """The date part, as an epoch day: from a ``date`` selection (or the given
    base) with field overrides, or from the fields alone."""
    selected = f.base("date")
    if selected is not None:
        if not isinstance(selected, Date | LocalDateTime | DateTime):
            raise InvalidTemporalType(f"{f.kind}(): date must have a date part, got a {selected.KIND}")
        base = selected.epoch_day
    values = {k: f.num(k) for k in DATE_FIELDS}
    _check_date_fields(f, base is not None)
    year = values["year"]
    if year is not None:
        check_year(year)

    def given(key: str, default: int) -> int:
        value = values[key]
        return default if value is None else value

    if base is None:
        assert year is not None  # _check_date_fields requires it
        if f.has("week"):
            return week_date(year, given("week", 1), given("dayOfWeek", 1))
        if f.has("quarter"):
            return quarter_date(year, given("quarter", 1), given("dayOfQuarter", 1))
        if f.has("ordinalDay"):
            return ordinal_date(year, given("ordinalDay", 1))
        return calendar_date(year, given("month", 1), given("day", 1))

    base_year, base_month, base_day = civil_from_days(base)
    if f.has("week") or f.has("dayOfWeek"):
        week_year, week = iso_week(base)
        return week_date(
            given("year", week_year), given("week", week), given("dayOfWeek", iso_day_of_week(base))
        )
    if f.has("quarter") or f.has("dayOfQuarter"):
        return quarter_date(
            given("year", base_year),
            given("quarter", quarter_of(base_month)),
            given("dayOfQuarter", day_of_quarter(base)),
        )
    if f.has("ordinalDay"):
        return ordinal_date(given("year", base_year), given("ordinalDay", 1))
    y = given("year", base_year)
    m = given("month", base_month)
    if values["day"] is None:
        # java.time withYear / withMonth clamp the day of month
        if not 1 <= m <= 12:
            raise InvalidTemporalValue(f"invalid value for month: {m} (valid values 1 - 12)")
        return with_year_month(base, y, m)
    return calendar_date(y, m, given("day", base_day))


def _build_time(f: _Fields) -> tuple[int, Temporal | None]:
    """The time part: from a ``time`` selection with overrides, or from the
    fields alone. Returns the selected source so callers can see its zone."""
    selected = f.base("time")
    if selected is not None:
        if not has_time(selected):
            raise InvalidTemporalType(f"{f.kind}(): time must have a time part, got a {selected.KIND}")
        return _apply_time(f, _nanos_of(selected)), selected
    if f.has(*TIME_FIELDS):
        _check_time_order(f)
    return _apply_time(f, 0), None


def _check_time_order(f: _Fields) -> None:
    """Without a base time, Neo4j requires the time fields in order: hour,
    then minute, then second, then a sub-second field."""
    missing = None
    for name, present in (
        ("hour", f.has("hour")),
        ("minute", f.has("minute")),
        ("second", f.has("second")),
        ("millisecond, microsecond or nanosecond", f.has(*SUBSECOND_FIELDS)),
    ):
        if not present:
            missing = missing or name
        elif missing is not None:
            raise UnsupportedTemporalUnit(f"{f.kind}(): {name} cannot be specified without {missing}")


def _apply_time(f: _Fields, base: int) -> int:
    """Override the components of a time of day with the given fields."""
    values = {k: f.num(k) for k in TIME_FIELDS}
    seconds, nanos = divmod(base, NANOS_PER_SECOND)
    hour, minute, second = seconds // 3600, seconds // 60 % 60, seconds % 60
    hour = hour if values["hour"] is None else values["hour"]
    minute = minute if values["minute"] is None else values["minute"]
    second = second if values["second"] is None else values["second"]
    if f.has(*SUBSECOND_FIELDS):
        ms = values["millisecond"] or 0
        us = values["microsecond"] or 0
        ns = values["nanosecond"] or 0
        if f.has("millisecond") and not 0 <= ms <= 999:
            raise InvalidTemporalValue(f"invalid value for millisecond: {ms} (valid values 0 - 999)")
        if f.has("millisecond") and (us > 999 or ns > 999_999):
            raise InvalidTemporalValue(f"{f.kind}(): sub-second fields out of range: {ms}, {us}, {ns}")
        if f.has("microsecond") and (us > 999_999 or (ns > 999)):
            raise InvalidTemporalValue(f"{f.kind}(): sub-second fields out of range: {us}, {ns}")
        nanos = ms * 1_000_000 + us * 1000 + ns
    if not 0 <= hour <= 23:
        raise InvalidTemporalValue(f"invalid value for hour: {hour} (valid values 0 - 23)")
    if not 0 <= minute <= 59:
        raise InvalidTemporalValue(f"invalid value for minute: {minute} (valid values 0 - 59)")
    if not 0 <= second <= 59:
        raise InvalidTemporalValue(f"invalid value for second: {second} (valid values 0 - 59)")
    if not 0 <= nanos <= 999_999_999:
        raise InvalidTemporalValue(f"invalid value for nanosecond: {nanos} (valid values 0 - 999999999)")
    return (hour * 3600 + minute * 60 + second) * NANOS_PER_SECOND + nanos


def _build_time_value(f: _Fields, zone: Zone | None) -> Time:
    nanos, source = _build_time(f)
    offset = 0
    if isinstance(source, Time | DateTime):
        offset = source.offset
        if zone is not None:
            # a zoned source moves to the new zone at the same instant
            # (its instant on 1970-01-01, as Neo4j's TimeValue does)
            new_offset = zone.offset_at(nanos // NANOS_PER_SECOND - offset)
            return make_time(nanos + (new_offset - offset) * NANOS_PER_SECOND, new_offset)
    if zone is not None:
        offset = zone.offset_at(_now_seconds())
    return make_time(nanos, offset)


def _build_datetime(f: _Fields, zone: Zone | None) -> DateTime:
    dt = f.base("datetime")
    if dt is not None:
        if not isinstance(dt, Date | LocalDateTime | DateTime):
            raise InvalidTemporalType(f"datetime(): datetime must have a date, got a {dt.KIND}")
        if isinstance(dt, DateTime):
            source = datetime_in_zone(dt, zone) if zone is not None else dt
        else:
            source = datetime_of_local(dt.epoch_day, _nanos_of(dt), zone or UTC_ZONE)
        day = _build_date(f, source.epoch_day)
        nanos = _apply_time(f, source.nanos)
        return datetime_of_local(day, nanos, source.zone_rules())
    day = _build_date(f, None)
    nanos, time_source = _build_time(f)
    if isinstance(time_source, Time | DateTime):
        zone_of_source = (
            time_source.zone_rules() if isinstance(time_source, DateTime) else Zone(None, time_source.offset)
        )
        value = datetime_of_local(day, nanos, zone_of_source)
        if zone is not None:
            value = datetime_in_zone(value, zone)
        return value
    return datetime_of_local(day, nanos, zone or UTC_ZONE)


def _build_from_epoch(f: _Fields) -> DateTime:
    """``datetime({epochSeconds | epochMillis, ...})``: the instant, in the
    ``timezone`` field's zone (UTC by default), as the base the remaining
    fields override; ``{epochMillis: 1500, nanosecond: 5}`` keeps the second
    and replaces its fraction, as Neo4j does."""
    if f.has("epochSeconds") and f.has("epochMillis"):
        raise UnsupportedTemporalUnit("datetime(): epochSeconds and epochMillis cannot be combined")
    zone = _zone_field(f) or UTC_ZONE
    seconds = f.num("epochSeconds")
    if seconds is not None:
        value = datetime_at_instant(seconds, 0, zone)
    else:
        millis = f.num("epochMillis")
        assert millis is not None  # the caller checked one of the two is present
        value = datetime_at_instant(millis // 1000, millis % 1000 * 1_000_000, zone)
    _check_instant(value)
    rest = {k: v for k, v in f.map.items() if k not in ("epochSeconds", "epochMillis", "timezone")}
    if not rest:
        return value
    rest["datetime"] = value
    result = build("datetime", rest)
    assert isinstance(result, DateTime)
    return result


def _check_instant(value: DateTime) -> None:
    check_year(civil_from_days(value.epoch_day)[0])


def _build_duration(f: _Fields) -> Duration:
    ints = [0, 0, 0, 0]
    floats = [0.0, 0.0, 0.0, 0.0]
    approx = False
    for key, value in f.map.items():
        scale = _DURATION_SCALE.get(key)
        if scale is None:
            raise UnsupportedTemporalUnit(f"duration(): unsupported field {key!r}")
        index, factor = scale
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidTemporalType(f"duration(): {key} must be a number, got {value!r}")
        if isinstance(value, float):
            approx = True
        else:
            ints[index] += value * factor
        floats[index] += value * factor
    for x in floats:
        if abs(x) >= DURATION_LIMIT or x != x:
            raise TemporalOverflow(f"duration(): value out of range: {f.map}")
    if approx:
        return approximate(*floats)
    return make_duration(*ints)


def from_epoch(seconds: int, nanos: int) -> DateTime:
    """``datetime.fromepoch(seconds, nanos)``, in UTC."""
    for name, number in (("seconds", seconds), ("nanoseconds", nanos)):
        if isinstance(number, bool) or not isinstance(number, int):
            raise InvalidTemporalType(f"datetime.fromepoch(): {name} must be an integer, got {number!r}")
    value = datetime_at_instant(seconds, nanos, UTC_ZONE)
    _check_instant(value)
    return value


def from_epoch_millis(millis: int) -> DateTime:
    """``datetime.fromepochmillis(ms)``, in UTC."""
    if isinstance(millis, bool) or not isinstance(millis, int):
        raise InvalidTemporalType(
            f"datetime.fromepochmillis(): milliseconds must be an integer, got {millis!r}"
        )
    value = datetime_at_instant(millis // 1000, millis % 1000 * 1_000_000, UTC_ZONE)
    _check_instant(value)
    return value


# --- truncation -------------------------------------------------------------------

TRUNCATION_UNITS = (
    "millennium",
    "century",
    "decade",
    "year",
    "weekyear",
    "quarter",
    "month",
    "week",
    "day",
    "hour",
    "minute",
    "second",
    "millisecond",
    "microsecond",
)
_UNIT_INDEX = {name: i for i, name in enumerate(TRUNCATION_UNITS)}
_DAY = _UNIT_INDEX["day"]
_MILLISECOND = _UNIT_INDEX["millisecond"]
_MICROSECOND = _UNIT_INDEX["microsecond"]
_TIME_UNIT_NANOS = {
    _UNIT_INDEX["hour"]: 3600 * NANOS_PER_SECOND,
    _UNIT_INDEX["minute"]: 60 * NANOS_PER_SECOND,
    _UNIT_INDEX["second"]: NANOS_PER_SECOND,
    _MILLISECOND: 1_000_000,
    _MICROSECOND: 1000,
}


def truncate(
    kind: str, unit: str, value: Temporal | None, overrides: Mapping[str, Any] | None = None
) -> Temporal | None:
    """``<kind>.truncate(unit, value, overrides)``. A null value gives None."""
    _check_kind(kind)
    if value is None:
        return None
    if not isinstance(unit, str):
        raise InvalidTemporalType(f"{kind}.truncate(): unit must be a string, got {unit!r}")
    if not is_temporal(value):
        raise InvalidTemporalType(f"{kind}.truncate(): cannot truncate {value!r}")
    u = _UNIT_INDEX.get(unit.lower())
    if u is None:
        raise UnsupportedTemporalUnit(f"{kind}.truncate(): unsupported unit {unit!r}")
    if kind == "duration" or isinstance(value, Duration):
        raise InvalidTemporalType("a duration cannot be truncated")
    if kind_has_date(kind) and not has_date(value):
        raise InvalidTemporalType(f"cannot truncate a {value.KIND} to a {kind}")
    if not kind_has_date(kind) and not has_time(value):
        raise InvalidTemporalType(f"cannot truncate a {value.KIND} to a {kind}")
    if kind == "date" and u > _DAY:
        raise UnsupportedTemporalUnit(f"a date cannot be truncated to {unit!r}")
    if not kind_has_date(kind) and u < _DAY:
        raise UnsupportedTemporalUnit(f"a {kind} cannot be truncated to {unit!r}")
    f = _Fields(kind, overrides or {})

    day = _truncate_date(value.epoch_day, u) if isinstance(value, Date | LocalDateTime | DateTime) else 0
    nanos = 0
    if has_time(value) and u > _DAY:
        n = _nanos_of(value)
        nanos = n - n % _TIME_UNIT_NANOS[u]
    nanos = _subsecond_conflict(f, u, nanos)

    out: Temporal
    if kind == "date":
        out = Date(day)
    elif kind == "localtime":
        out = make_local_time(nanos)
    elif kind == "time":
        out = make_time(nanos, value.offset if isinstance(value, Time | DateTime) else 0)
    elif kind == "localdatetime":
        out = make_local_datetime(day, nanos)
    else:
        out = datetime_of_local(day, nanos, value.zone_rules() if isinstance(value, DateTime) else UTC_ZONE)
    if not f.map:
        return out

    rest = {k: v for k, v in f.map.items() if k != "timezone"}
    if "timezone" in f.map and kind_has_zone(kind):
        # the truncated local value moves to the new zone, keeping its wall clock
        zone = _zone_field(f)
        if zone is not None:
            if isinstance(out, Time):
                out = make_time(out.nanos, zone.offset_at(_now_seconds()))
            elif isinstance(out, DateTime):
                out = datetime_of_local(out.epoch_day, out.nanos, zone)
    if _subsecond_handled(f, u):
        for key in SUBSECOND_FIELDS:
            rest.pop(key, None)
    if not rest:
        return out
    rest[{"date": "date", "localtime": "time", "time": "time"}.get(kind, "datetime")] = out
    return build(kind, rest)


def _subsecond_conflict(f: _Fields, u: int, nanos: int) -> int:
    """Truncating to milliseconds (or microseconds) and then giving finer
    sub-second fields adds them to what the truncation kept."""
    if not _subsecond_handled(f, u):
        return nanos
    us = f.num("microsecond") or 0
    ns = f.num("nanosecond") or 0
    if u == _MILLISECOND:
        return nanos + us * 1000 + ns
    return nanos + ns


def _subsecond_handled(f: _Fields, u: int) -> bool:
    if u == _MILLISECOND:
        return not f.has("millisecond") and f.has("microsecond", "nanosecond")
    if u == _MICROSECOND:
        return not f.has("millisecond", "microsecond") and f.has("nanosecond")
    return False


def _truncate_date(epoch_day: int, u: int) -> int:
    year, month, day = civil_from_days(epoch_day)
    if u == 0:
        return days_from_civil(year - year % 1000, 1, 1)
    if u == 1:
        return days_from_civil(year - year % 100, 1, 1)
    if u == 2:
        return days_from_civil(year - year % 10, 1, 1)
    if u == 3:
        return days_from_civil(year, 1, 1)
    if u == 4:
        return week_date(iso_week(epoch_day)[0], 1, 1)
    if u == 5:
        return days_from_civil(year, (quarter_of(month) - 1) * 3 + 1, 1)
    if u == 6:
        return days_from_civil(year, month, 1)
    if u == 7:
        return epoch_day - (iso_day_of_week(epoch_day) - 1)
    return epoch_day
