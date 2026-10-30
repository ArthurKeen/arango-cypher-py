"""Cypher's six temporal value types and their string forms.

Values are frozen and hashable, and hold their components as plain integers so
that nanoseconds and years beyond Python's ``datetime`` range are exact:

* ``Date``          — ``epoch_day`` (days since 1970-01-01)
* ``LocalTime``     — ``nanos`` (nanoseconds since midnight)
* ``Time``          — ``nanos`` and ``offset`` (seconds east of UTC)
* ``LocalDateTime`` — ``epoch_day`` and ``nanos`` of the wall clock
* ``DateTime``      — the wall clock (``epoch_day``, ``nanos``), its
  ``offset`` and the IANA ``zone`` (None for a fixed offset)
* ``Duration``      — ``months``, ``days``, ``seconds`` and ``nanos``,
  with ``nanos`` normalised to [0, 1e9)

Dataclass equality is Cypher's ``=`` on temporal values (java.time equality,
which Neo4j uses): a Time needs the same offset, a DateTime the same instant,
offset and zone, a Duration the same four components.

``str(value)`` is java.time's ``toString``, the form the openCypher TCK
expects: seconds are dropped when zero (``12:31``), a fraction is written in
groups of three digits (``12:31:05.500``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._calendar import NANOS_PER_DAY, NANOS_PER_SECOND, SECONDS_PER_DAY, civil_from_days, tdiv, tmod
from .zones import Zone, fixed_zone, format_offset


@dataclass(frozen=True, slots=True)
class Date:
    epoch_day: int
    KIND: ClassVar[str] = "date"

    @property
    def ymd(self) -> tuple[int, int, int]:
        return civil_from_days(self.epoch_day)

    def __str__(self) -> str:
        return format_date(self.epoch_day)


@dataclass(frozen=True, slots=True)
class LocalTime:
    nanos: int
    KIND: ClassVar[str] = "localtime"

    def __str__(self) -> str:
        return format_time_of_day(self.nanos)


@dataclass(frozen=True, slots=True)
class Time:
    nanos: int
    offset: int
    KIND: ClassVar[str] = "time"

    @property
    def utc_nanos(self) -> int:
        """Nanoseconds since midnight UTC, unwrapped (may leave [0, 1 day))."""
        return self.nanos - self.offset * NANOS_PER_SECOND

    def __str__(self) -> str:
        return format_time_of_day(self.nanos) + format_offset(self.offset)


@dataclass(frozen=True, slots=True)
class LocalDateTime:
    epoch_day: int
    nanos: int
    KIND: ClassVar[str] = "localdatetime"

    @property
    def ymd(self) -> tuple[int, int, int]:
        return civil_from_days(self.epoch_day)

    def __str__(self) -> str:
        return format_date(self.epoch_day) + "T" + format_time_of_day(self.nanos)


@dataclass(frozen=True, slots=True)
class DateTime:
    epoch_day: int
    nanos: int
    offset: int
    zone: str | None = None
    KIND: ClassVar[str] = "datetime"

    @property
    def ymd(self) -> tuple[int, int, int]:
        return civil_from_days(self.epoch_day)

    @property
    def local_seconds(self) -> int:
        """The wall clock as epoch seconds (as if it were UTC)."""
        return self.epoch_day * SECONDS_PER_DAY + self.nanos // NANOS_PER_SECOND

    @property
    def epoch_seconds(self) -> int:
        """The instant, in whole seconds since the epoch."""
        return self.local_seconds - self.offset

    @property
    def nano_of_second(self) -> int:
        return self.nanos % NANOS_PER_SECOND

    @property
    def epoch_nanos(self) -> int:
        return self.epoch_seconds * NANOS_PER_SECOND + self.nano_of_second

    def zone_rules(self) -> Zone:
        """The zone the value lives in: its IANA zone, or its fixed offset."""
        return Zone(self.zone) if self.zone is not None else fixed_zone(self.offset)

    def __str__(self) -> str:
        text = format_date(self.epoch_day) + "T" + format_time_of_day(self.nanos) + format_offset(self.offset)
        if self.zone is not None:
            text += f"[{self.zone}]"
        return text


@dataclass(frozen=True, slots=True)
class Duration:
    months: int
    days: int
    seconds: int
    nanos: int
    KIND: ClassVar[str] = "duration"

    def __str__(self) -> str:
        return format_duration(self)


Temporal = Date | LocalTime | Time | LocalDateTime | DateTime | Duration
TEMPORAL_TYPES: tuple[type, ...] = (Date, LocalTime, Time, LocalDateTime, DateTime, Duration)
KINDS: dict[str, type] = {cls.KIND: cls for cls in TEMPORAL_TYPES}  # type: ignore[attr-defined]


def is_temporal(value: object) -> bool:
    return isinstance(value, TEMPORAL_TYPES)


def has_date(value: object) -> bool:
    return isinstance(value, Date | LocalDateTime | DateTime)


def has_time(value: object) -> bool:
    return isinstance(value, LocalTime | Time | LocalDateTime | DateTime)


def has_zone(value: object) -> bool:
    return isinstance(value, Time | DateTime)


def kind_has_date(kind: str) -> bool:
    return kind in ("date", "localdatetime", "datetime")


def kind_has_time(kind: str) -> bool:
    return kind in ("localtime", "time", "localdatetime", "datetime")


def kind_has_zone(kind: str) -> bool:
    return kind in ("time", "datetime")


# --- construction helpers ----------------------------------------------------


def make_duration(months: int, days: int, seconds: int, nanos: int) -> Duration:
    """A duration with nanos carried into seconds and normalised to [0, 1e9)."""
    carry, nanos = divmod(nanos, NANOS_PER_SECOND)
    return Duration(months, days, seconds + carry, nanos)


def make_local_time(nanos: int) -> LocalTime:
    return LocalTime(nanos % NANOS_PER_DAY)


def make_time(nanos: int, offset: int) -> Time:
    return Time(nanos % NANOS_PER_DAY, offset)


def make_local_datetime(epoch_day: int, nanos: int) -> LocalDateTime:
    carry, nanos = divmod(nanos, NANOS_PER_DAY)
    return LocalDateTime(epoch_day + carry, nanos)


def datetime_at_instant(seconds: int, nanos: int, zone: Zone) -> DateTime:
    """The DateTime of an instant (epoch seconds plus nanos) in a zone."""
    carry, nanos = divmod(nanos, NANOS_PER_SECOND)
    seconds += carry
    offset = zone.offset_at(seconds)
    day, second_of_day = divmod(seconds + offset, SECONDS_PER_DAY)
    return DateTime(day, second_of_day * NANOS_PER_SECOND + nanos, offset, zone.name)


def datetime_of_local(epoch_day: int, nanos: int, zone: Zone, preferred: int | None = None) -> DateTime:
    """A wall clock in a zone, resolved as java.time ``ZonedDateTime.ofLocal``
    (see ``Zone.resolve_local``)."""
    carry, nanos = divmod(nanos, NANOS_PER_DAY)
    epoch_day += carry
    local = epoch_day * SECONDS_PER_DAY + nanos // NANOS_PER_SECOND
    instant = zone.resolve_local(local, preferred)
    return datetime_at_instant(instant, nanos % NANOS_PER_SECOND, zone)


def datetime_in_zone(value: DateTime, zone: Zone) -> DateTime:
    """The same instant as ``value`` in another zone."""
    return datetime_at_instant(value.epoch_seconds, value.nano_of_second, zone)


# --- formatting (java.time toString) -----------------------------------------


def format_year(year: int) -> str:
    if 0 <= year <= 9999:
        return f"{year:04d}"
    if -9999 <= year < 0:
        return f"-{-year:04d}"
    if year > 9999:
        return f"+{year}"
    return str(year)


def format_date(epoch_day: int) -> str:
    year, month, day = civil_from_days(epoch_day)
    return f"{format_year(year)}-{month:02d}-{day:02d}"


def format_time_of_day(nanos: int) -> str:
    seconds, fraction = divmod(nanos, NANOS_PER_SECOND)
    text = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}"
    second = seconds % 60
    if second or fraction:
        text += f":{second:02d}"
        if fraction == 0:
            pass
        elif fraction % 1_000_000 == 0:
            text += f".{fraction // 1_000_000:03d}"
        elif fraction % 1000 == 0:
            text += f".{fraction // 1000:06d}"
        else:
            text += f".{fraction:09d}"
    return text


def format_duration(d: Duration) -> str:
    """Neo4j's duration string: ``P1Y2M3DT4H5M6.5S``; components share the
    sign of the seconds they come from (``PT-23H-59M-59.9S``)."""
    if d.months == 0 and d.days == 0 and d.seconds == 0 and d.nanos == 0:
        return "PT0S"
    parts = ["P"]

    def unit(quantity: int, suffix: str) -> None:
        if quantity:
            parts.append(f"{quantity}{suffix}")

    unit(tdiv(d.months, 12), "Y")
    unit(tmod(d.months, 12), "M")
    unit(d.days, "D")
    if d.seconds or d.nanos:
        s, n = d.seconds, d.nanos
        if s < 0 and n != 0:
            s += 1
            n -= NANOS_PER_SECOND
        parts.append("T")
        unit(tdiv(s, 3600), "H")
        s = tmod(s, 3600)
        unit(tdiv(s, 60), "M")
        s = tmod(s, 60)
        if s or n:
            if s == 0 and n < 0:
                parts.append("-")
            parts.append(str(s))
            if n:
                parts.append("." + f"{abs(n):09d}".rstrip("0"))
            parts.append("S")
    if len(parts) == 1:
        parts.append("T0S")
    return "".join(parts)
