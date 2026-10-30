"""``duration.between`` / ``inMonths`` / ``inDays`` / ``inSeconds``.

The two values are first aligned to one shape, as Neo4j's
``TemporalValue.until`` does (a missing time of day is midnight, a missing
date is the other value's, a missing zone the other value's). Whole months and
days are then counted on wall clocks (java.time ``until``), and the rest is
the exact difference: of instants for zoned values, of wall clocks otherwise.
"""

from __future__ import annotations

from typing import Any

from ._calendar import NANOS_PER_SECOND, SECONDS_PER_DAY, civil_from_days, tdiv
from .arith import add
from .builders import _now_seconds
from .errors import InvalidTemporalType, InvalidTemporalValue
from .values import (
    Date,
    DateTime,
    Duration,
    LocalDateTime,
    LocalTime,
    Temporal,
    Time,
    datetime_in_zone,
    datetime_of_local,
    has_date,
    has_time,
    has_zone,
    is_temporal,
    make_duration,
    make_time,
)
from .zones import Zone

_UNITS = (None, "months", "days", "seconds")


def duration_between(a: Any, b: Any, unit: str | None = None) -> Duration | None:
    """``duration.between(a, b)`` (unit None), ``duration.inMonths`` ('months'),
    ``duration.inDays`` ('days') or ``duration.inSeconds`` ('seconds').
    A null operand gives None."""
    if unit not in _UNITS:
        raise InvalidTemporalValue(f"duration_between(): unknown unit {unit!r}, expected one of {_UNITS}")
    if a is None or b is None:
        return None
    for x in (a, b):
        if not is_temporal(x) or isinstance(x, Duration):
            name = f"duration.{'between' if unit is None else 'in' + unit.capitalize()}"
            raise InvalidTemporalType(f"{name}() needs two temporal values, got {x!r}")
    if unit is None:
        return _between(a, b)
    if unit == "months":
        return _in_months(a, b)
    if unit == "days":
        return _in_days(a, b)
    return _in_seconds(a, b)


def _between(a: Temporal, b: Temporal) -> Duration:
    months = days = 0
    if has_date(a) and has_date(b):
        x, y = _align(a, b, time_based=False)
        months = _until_months(x, y)
        a = _plus(a, months, 0)
        x, y = _align(a, b, time_based=False)
        days = _until_days(x, y)
        a = _plus(a, 0, days)
    x, y = _align(a, b, time_based=True)
    seconds, nanos = _until_nanos(x, y)
    return make_duration(months, days, seconds, nanos)


def _in_months(a: Temporal, b: Temporal) -> Duration:
    if not has_date(a) and not has_date(b):
        return make_duration(0, 0, 0, 0)
    x, y = _align(a, b, time_based=False)
    if not has_date(x):
        return make_duration(0, 0, 0, 0)
    return make_duration(_until_months(x, y), 0, 0, 0)


def _in_days(a: Temporal, b: Temporal) -> Duration:
    x, y = _align(a, b, time_based=False)
    if not has_date(x):
        return make_duration(0, 0, 0, 0)
    return make_duration(0, _until_days(x, y), 0, 0)


def _in_seconds(a: Temporal, b: Temporal) -> Duration:
    negate = False
    if has_zone(a) and not has_zone(b):
        a, b, negate = b, a, True
    x, y = _align(a, b, time_based=True)
    from_nanos = _nano_of_second(a)
    to_nanos = _nano_of_second(b)
    # The exact difference; Neo4j truncates seconds toward zero and then fixes
    # the sign of the nanosecond part.
    exact_seconds, exact_nanos = _until_nanos(x, y)
    seconds = exact_seconds + 1 if exact_seconds < 0 and exact_nanos > 0 else exact_seconds
    nanos = to_nanos - from_nanos
    less_than_one = (
        seconds == 0 and has_time(a) and has_time(b) and _second_of_minute(a) != _second_of_minute(b)
    )
    if nanos < 0 and (seconds > 0 or less_than_one):
        nanos += NANOS_PER_SECOND
    elif nanos > 0 and (seconds < 0 or less_than_one):
        nanos -= NANOS_PER_SECOND
    if negate:
        seconds, nanos = -seconds, -nanos
    return make_duration(0, 0, seconds, nanos)


def _nanos_of_day(v: Temporal) -> int:
    return v.nanos if isinstance(v, LocalTime | Time | LocalDateTime | DateTime) else 0


def _nano_of_second(v: Temporal) -> int:
    return _nanos_of_day(v) % NANOS_PER_SECOND if has_time(v) else 0


def _second_of_minute(v: Temporal) -> int:
    return _nanos_of_day(v) // NANOS_PER_SECOND % 60


def _epoch_day(v: Temporal) -> int:
    assert isinstance(v, Date | LocalDateTime | DateTime)
    return v.epoch_day


def _plus(v: Temporal, months: int, days: int) -> Temporal:
    """Whole months or days added on the value's own terms."""
    out = add(v, make_duration(months, days, 0, 0))
    assert out is not None
    return out


def _align(a: Temporal, b: Temporal, time_based: bool) -> tuple[Temporal, Temporal]:
    """Attach the missing parts so both values have one shape: time of day
    (midnight), then date, then zone."""
    if time_based:
        a, b = _attach_time(a), _attach_time(b)
    elif has_time(a) and not has_time(b):
        b = _attach_time(b)
    elif has_time(b) and not has_time(a):
        a = _attach_time(a)
    if has_date(a) and not has_date(b):
        b = _attach_date(b, _epoch_day(a))
    elif has_date(b) and not has_date(a):
        a = _attach_date(a, _epoch_day(b))
    if has_zone(a) and not has_zone(b):
        b = _attach_zone(b, _zone_of(a))
    elif has_zone(b) and not has_zone(a):
        a = _attach_zone(a, _zone_of(b))
    return a, b


def _zone_of(v: Temporal) -> Zone:
    if isinstance(v, DateTime):
        return v.zone_rules()
    assert isinstance(v, Time)
    return Zone(None, v.offset)


def _attach_time(v: Temporal) -> Temporal:
    if isinstance(v, Date):
        return LocalDateTime(v.epoch_day, 0)
    return v


def _attach_date(v: Temporal, epoch_day: int) -> Temporal:
    if isinstance(v, LocalTime):
        return LocalDateTime(epoch_day, v.nanos)
    if isinstance(v, Time):
        return datetime_of_local(epoch_day, v.nanos, Zone(None, v.offset))
    return v


def _attach_zone(v: Temporal, zone: Zone) -> Temporal:
    if isinstance(v, LocalDateTime):
        return datetime_of_local(v.epoch_day, v.nanos, zone)
    if isinstance(v, LocalTime):
        return make_time(v.nanos, zone.offset_at(_now_seconds()))
    return v


def _local_parts(a: Temporal, b: Temporal) -> tuple[int, int, int, int]:
    """(epoch day, nanos of day) of both wall clocks; a DateTime ``b`` is
    first moved to ``a``'s zone (java.time ``until``)."""
    if isinstance(a, DateTime) and isinstance(b, DateTime):
        b = datetime_in_zone(b, a.zone_rules())
    return _epoch_day(a), _nanos_of_day(a), _epoch_day(b), _nanos_of_day(b)


def _until_months(a: Temporal, b: Temporal) -> int:
    day_a, nanos_a, day_b, nanos_b = _local_parts(a, b)
    end = day_b
    if has_time(a):
        if day_b > day_a and nanos_b < nanos_a:
            end -= 1
        elif day_b < day_a and nanos_b > nanos_a:
            end += 1
    ya, ma, da = civil_from_days(day_a)
    yb, mb, db = civil_from_days(end)
    packed_a = (ya * 12 + ma - 1) * 32 + da
    packed_b = (yb * 12 + mb - 1) * 32 + db
    return tdiv(packed_b - packed_a, 32)


def _until_days(a: Temporal, b: Temporal) -> int:
    day_a, nanos_a, day_b, nanos_b = _local_parts(a, b)
    if has_time(a):
        if day_b > day_a and nanos_b < nanos_a:
            day_b -= 1
        elif day_b < day_a and nanos_b > nanos_a:
            day_b += 1
    return day_b - day_a


def _until_nanos(a: Temporal, b: Temporal) -> tuple[int, int]:
    """The exact difference ``b - a`` as (seconds, nanos in [0, 1e9)):
    instants for zoned values, wall clocks otherwise."""
    if isinstance(a, LocalTime):
        return divmod(_nanos_of_day(b) - a.nanos, NANOS_PER_SECOND)
    if isinstance(a, Time):
        assert isinstance(b, Time)
        return divmod(b.utc_nanos - a.utc_nanos, NANOS_PER_SECOND)
    if isinstance(a, DateTime):
        assert isinstance(b, DateTime)
        return divmod(b.epoch_nanos - a.epoch_nanos, NANOS_PER_SECOND)
    day_a, nanos_a, day_b, nanos_b = _local_parts(a, b)
    return divmod((day_b - day_a) * SECONDS_PER_DAY * NANOS_PER_SECOND + nanos_b - nanos_a, NANOS_PER_SECOND)
