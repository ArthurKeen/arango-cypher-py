"""Arithmetic, comparison and accessors on temporal values.

* temporal ± duration, duration ± duration, duration × / ÷ number
  (month arithmetic clamps the day of month; fractions spill into smaller
  units, as Neo4j's ``DurationValue`` does);
* ``compare`` / ``equals`` with Cypher's rules;
* ``get_property``: every accessor Neo4j supports (``d.week``,
  ``dt.offsetMinutes``, ``dur.minutesOfHour`` …).
"""

from __future__ import annotations

from typing import Any

from ._calendar import (
    MAX_YEAR,
    NANOS_PER_SECOND,
    SECONDS_PER_DAY,
    civil_from_days,
    day_of_quarter,
    iso_day_of_week,
    iso_week,
    ordinal_day,
    plus_months,
    quarter_of,
    tdiv,
    tmod,
)
from .builders import DURATION_LIMIT
from .errors import InvalidTemporalType, TemporalOverflow, UnsupportedTemporalUnit
from .parsing import approximate
from .values import (
    Date,
    DateTime,
    Duration,
    LocalDateTime,
    LocalTime,
    Temporal,
    Time,
    datetime_at_instant,
    datetime_of_local,
    is_temporal,
    make_duration,
    make_local_datetime,
    make_local_time,
    make_time,
)
from .zones import format_offset

# --- accessors ---------------------------------------------------------------------

DATE_ACCESSORS = (
    "year",
    "quarter",
    "month",
    "week",
    "weekYear",
    "day",
    "ordinalDay",
    "weekDay",
    "dayOfWeek",
    "dayOfQuarter",
)
TIME_ACCESSORS = ("hour", "minute", "second", "millisecond", "microsecond", "nanosecond")
ZONE_ACCESSORS = ("timezone", "offset", "offsetMinutes", "offsetSeconds")
INSTANT_ACCESSORS = ("epochMillis", "epochSeconds")
DURATION_ACCESSORS = (
    "years",
    "quarters",
    "months",
    "weeks",
    "days",
    "hours",
    "minutes",
    "seconds",
    "milliseconds",
    "microseconds",
    "nanoseconds",
    "quartersOfYear",
    "monthsOfQuarter",
    "monthsOfYear",
    "daysOfWeek",
    "minutesOfHour",
    "secondsOfMinute",
    "millisecondsOfSecond",
    "microsecondsOfSecond",
    "nanosecondsOfSecond",
)


def accessors(value: Temporal) -> tuple[str, ...]:
    """The accessor names a value supports."""
    if isinstance(value, Duration):
        return DURATION_ACCESSORS
    names: tuple[str, ...] = ()
    if isinstance(value, Date | LocalDateTime | DateTime):
        names += DATE_ACCESSORS
    if isinstance(value, LocalTime | Time | LocalDateTime | DateTime):
        names += TIME_ACCESSORS
    if isinstance(value, Time | DateTime):
        names += ZONE_ACCESSORS
    if isinstance(value, DateTime):
        names += INSTANT_ACCESSORS
    return names


def get_property(value: Temporal | None, name: str) -> int | str | None:
    """``value.name`` for a temporal value; None for a null value. An accessor
    the value does not have raises ``UnsupportedTemporalUnit``."""
    if value is None:
        return None
    if not is_temporal(value):
        raise InvalidTemporalType(f"{value!r} is not a temporal value")
    if isinstance(value, Duration):
        return _duration_field(value, name)
    if isinstance(value, Date | LocalDateTime | DateTime) and name in DATE_ACCESSORS:
        return _date_field(value.epoch_day, name)
    if isinstance(value, LocalTime | Time | LocalDateTime | DateTime) and name in TIME_ACCESSORS:
        return _time_field(value.nanos, name)
    if isinstance(value, Time | DateTime) and name in ZONE_ACCESSORS:
        if name == "timezone":
            if isinstance(value, DateTime) and value.zone is not None:
                return value.zone
            return format_offset(value.offset)
        if name == "offset":
            return format_offset(value.offset)
        if name == "offsetMinutes":
            return tdiv(value.offset, 60)
        return value.offset
    if isinstance(value, DateTime) and name in INSTANT_ACCESSORS:
        if name == "epochSeconds":
            return value.epoch_seconds
        return value.epoch_seconds * 1000 + value.nano_of_second // 1_000_000
    raise UnsupportedTemporalUnit(f"a {value.KIND} has no field {name!r}")


def _date_field(epoch_day: int, name: str) -> int:
    year, month, day = civil_from_days(epoch_day)
    if name == "year":
        return year
    if name == "quarter":
        return quarter_of(month)
    if name == "month":
        return month
    if name == "week":
        return iso_week(epoch_day)[1]
    if name == "weekYear":
        return iso_week(epoch_day)[0]
    if name == "day":
        return day
    if name == "ordinalDay":
        return ordinal_day(epoch_day)
    if name in ("weekDay", "dayOfWeek"):
        return iso_day_of_week(epoch_day)
    return day_of_quarter(epoch_day)


def _time_field(nanos: int, name: str) -> int:
    seconds, fraction = divmod(nanos, NANOS_PER_SECOND)
    return {
        "hour": seconds // 3600,
        "minute": seconds // 60 % 60,
        "second": seconds % 60,
        "millisecond": fraction // 1_000_000,
        "microsecond": fraction // 1000,
        "nanosecond": fraction,
    }[name]


def _duration_field(d: Duration, name: str) -> int:
    if name not in DURATION_ACCESSORS:
        raise UnsupportedTemporalUnit(f"a duration has no field {name!r}")
    return {
        "years": tdiv(d.months, 12),
        "quarters": tdiv(d.months, 3),
        "months": d.months,
        "weeks": tdiv(d.days, 7),
        "days": d.days,
        "hours": tdiv(d.seconds, 3600),
        "minutes": tdiv(d.seconds, 60),
        "seconds": d.seconds,
        "milliseconds": d.seconds * 1000 + d.nanos // 1_000_000,
        "microseconds": d.seconds * 1_000_000 + d.nanos // 1000,
        "nanoseconds": d.seconds * NANOS_PER_SECOND + d.nanos,
        "quartersOfYear": tdiv(tmod(d.months, 12), 3),
        "monthsOfQuarter": tmod(d.months, 3),
        "monthsOfYear": tmod(d.months, 12),
        "daysOfWeek": tmod(d.days, 7),
        "minutesOfHour": tmod(tdiv(d.seconds, 60), 60),
        "secondsOfMinute": tmod(d.seconds, 60),
        "millisecondsOfSecond": d.nanos // 1_000_000,
        "microsecondsOfSecond": d.nanos // 1000,
        "nanosecondsOfSecond": d.nanos,
    }[name]


# --- comparison ----------------------------------------------------------------------


def _cmp(a: int, b: int) -> int:
    return (a > b) - (a < b)


def compare(a: Any, b: Any) -> int | None:
    """-1, 0 or 1 ordering two values of the same temporal type; None when
    Cypher has no order between them (a null, different types, durations)."""
    if not is_temporal(a) or not is_temporal(b) or type(a) is not type(b) or isinstance(a, Duration):
        return None
    if isinstance(a, Time):
        assert isinstance(b, Time)
        return _cmp(a.utc_nanos, b.utc_nanos) or _cmp(a.nanos, b.nanos)
    if isinstance(a, DateTime):
        assert isinstance(b, DateTime)
        return _cmp(a.epoch_nanos, b.epoch_nanos)
    if isinstance(a, Date):
        assert isinstance(b, Date)
        return _cmp(a.epoch_day, b.epoch_day)
    if isinstance(a, LocalTime):
        assert isinstance(b, LocalTime)
        return _cmp(a.nanos, b.nanos)
    assert isinstance(a, LocalDateTime) and isinstance(b, LocalDateTime)
    return _cmp(a.epoch_day, b.epoch_day) or _cmp(a.nanos, b.nanos)


def equals(a: Any, b: Any) -> bool | None:
    """Cypher's ``=`` with a temporal operand: None when either side is null,
    False for different types, else java.time equality (a DateTime needs the
    same instant, offset and zone; a Time the same time and offset)."""
    if a is None or b is None:
        return None
    if not is_temporal(a) and not is_temporal(b):
        raise InvalidTemporalType(f"equals() needs a temporal operand, got {a!r} and {b!r}")
    if type(a) is not type(b):
        return False
    return bool(a == b)


# --- arithmetic --------------------------------------------------------------------------


def _check_number(x: Any, operation: str) -> int | float:
    if isinstance(x, bool) or not isinstance(x, int | float):
        raise InvalidTemporalType(f"cannot {operation} a duration by {x!r}")
    return x


def _in_range(*parts: float) -> None:
    for x in parts:
        if x != x or abs(x) >= DURATION_LIMIT:
            raise TemporalOverflow("duration out of range")


def add(a: Any, b: Any) -> Temporal | None:
    """``a + b``: temporal + duration, duration + temporal, duration + duration."""
    if a is None or b is None:
        return None
    if isinstance(a, Duration) and is_temporal(b) and not isinstance(b, Duration):
        a, b = b, a
    return _add_duration(a, b, 1, "add")


def subtract(a: Any, b: Any) -> Temporal | None:
    """``a - b``: temporal - duration, duration - duration."""
    if a is None or b is None:
        return None
    return _add_duration(a, b, -1, "subtract")


def _add_duration(value: Any, d: Any, sign: int, operation: str) -> Temporal:
    if not is_temporal(value) or not isinstance(d, Duration):
        raise InvalidTemporalType(f"cannot {operation} {_describe(value)} and {_describe(d)}")
    if isinstance(value, Duration):
        _in_range(
            value.months + sign * d.months,
            value.days + sign * d.days,
            value.seconds + sign * d.seconds,
        )
        return make_duration(
            value.months + sign * d.months,
            value.days + sign * d.days,
            value.seconds + sign * d.seconds,
            value.nanos + sign * d.nanos,
        )
    months, days, seconds, nanos = sign * d.months, sign * d.days, sign * d.seconds, sign * d.nanos
    # Far past the year range the result is out of range whatever the value is;
    # a span of 2 * MAX_YEAR itself is valid (duration.between of the extremes).
    span = 4 * MAX_YEAR
    if abs(months) > span * 12 or abs(days) > span * 366 or abs(tdiv(seconds, 86_400)) > span * 366:
        raise TemporalOverflow(f"adding {d} is out of range")
    out = _add_parts(value, months, days, seconds, nanos)
    if isinstance(out, Date | LocalDateTime | DateTime):
        year = civil_from_days(out.epoch_day)[0]
        if not -MAX_YEAR <= year <= MAX_YEAR:
            raise TemporalOverflow(f"year {year} is out of range (±{MAX_YEAR})")
    return out


def _describe(x: Any) -> str:
    return f"a {x.KIND}" if is_temporal(x) else repr(x)


def _add_parts(value: Temporal, months: int, days: int, seconds: int, nanos: int) -> Temporal:
    if isinstance(value, Date):
        return Date(plus_months(value.epoch_day, months) + days + tdiv(seconds, SECONDS_PER_DAY))
    if isinstance(value, LocalTime):
        return make_local_time(value.nanos + (seconds % SECONDS_PER_DAY) * NANOS_PER_SECOND + nanos)
    if isinstance(value, Time):
        return make_time(value.nanos + (seconds % SECONDS_PER_DAY) * NANOS_PER_SECOND + nanos, value.offset)
    if isinstance(value, LocalDateTime):
        day = plus_months(value.epoch_day, months) + days
        return make_local_datetime(day, value.nanos + seconds * NANOS_PER_SECOND + nanos)
    if isinstance(value, DateTime):
        zone = value.zone_rules()
        day = plus_months(value.epoch_day, months) + days
        local = datetime_of_local(day, value.nanos, zone, preferred=value.offset)
        return datetime_at_instant(local.epoch_seconds + seconds, local.nano_of_second + nanos, zone)
    raise InvalidTemporalType(f"cannot add a duration to {_describe(value)}")


def multiply(d: Any, factor: Any) -> Duration | None:
    """``duration * number`` (or ``number * duration``)."""
    if d is None or factor is None:
        return None
    if not isinstance(d, Duration) and isinstance(factor, Duration):
        d, factor = factor, d
    if not isinstance(d, Duration):
        raise InvalidTemporalType(f"cannot multiply {_describe(d)} by {_describe(factor)}")
    number = _check_number(factor, "multiply")
    if isinstance(number, int):
        for x in (d.months, d.days, d.seconds, d.nanos):
            if abs(float(x) * float(number)) >= DURATION_LIMIT:
                raise TemporalOverflow(f"duration multiplied by {number} is out of range")
        return make_duration(d.months * number, d.days * number, d.seconds * number, d.nanos * number)
    _in_range(d.months * number, d.days * number, d.seconds * number)
    return approximate(d.months * number, d.days * number, d.seconds * number, d.nanos * number)


def divide(d: Any, divisor: Any) -> Duration | None:
    """``duration / number``."""
    if d is None or divisor is None:
        return None
    if not isinstance(d, Duration):
        raise InvalidTemporalType(f"cannot divide {_describe(d)} by {_describe(divisor)}")
    number = float(_check_number(divisor, "divide"))
    if number == 0:
        raise TemporalOverflow("division of a duration by zero")
    _in_range(d.months / number, d.days / number, d.seconds / number)
    return approximate(d.months / number, d.days / number, d.seconds / number, d.nanos / number)
