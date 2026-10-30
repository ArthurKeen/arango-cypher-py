"""Proleptic Gregorian calendar arithmetic on epoch days, for any year.

Python's ``datetime`` covers years 1 to 9999 at microsecond precision; Cypher
covers ±999,999,999 at nanoseconds. Dates are therefore plain integers, the
number of days since 1970-01-01 (negative before), converted with Howard
Hinnant's ``days_from_civil`` / ``civil_from_days``, which are exact for every
integer year. Weeks are ISO 8601 weeks (Monday first, week 1 contains
January 4th), as in java.time.
"""

from __future__ import annotations

from .errors import InvalidTemporalValue, TemporalOverflow

NANOS_PER_SECOND = 1_000_000_000
SECONDS_PER_DAY = 86_400
NANOS_PER_DAY = SECONDS_PER_DAY * NANOS_PER_SECOND

# Neo4j's year range (java.time.Year.MIN_VALUE / MAX_VALUE).
MAX_YEAR = 999_999_999


def tdiv(a: int, b: int) -> int:
    """Integer division truncating toward zero (Java / Go ``/``)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def tmod(a: int, b: int) -> int:
    """Remainder with the sign of the dividend (Java / Go ``%``)."""
    return a - tdiv(a, b) * b


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def days_in_year(year: int) -> int:
    return 366 if is_leap(year) else 365


def days_from_civil(year: int, month: int, day: int) -> int:
    """Epoch day of a valid calendar date."""
    y = year - (1 if month <= 2 else 0)
    era = y // 400
    yoe = y - era * 400
    mp = (month + 9) % 12
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def civil_from_days(epoch_day: int) -> tuple[int, int, int]:
    """(year, month, day) of an epoch day."""
    z = epoch_day + 719_468
    era = z // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = yoe + era * 400 + (1 if month <= 2 else 0)
    return year, month, day


def year_of(epoch_day: int) -> int:
    return civil_from_days(epoch_day)[0]


def iso_day_of_week(epoch_day: int) -> int:
    """1 (Monday) to 7 (Sunday); 1970-01-01 was a Thursday."""
    return (epoch_day + 3) % 7 + 1


def iso_week(epoch_day: int) -> tuple[int, int]:
    """(week-based year, ISO week number) of an epoch day."""
    thursday = epoch_day - iso_day_of_week(epoch_day) + 4
    week_year = year_of(thursday)
    week = (thursday - days_from_civil(week_year, 1, 1)) // 7 + 1
    return week_year, week


def weeks_in_year(year: int) -> int:
    return iso_week(days_from_civil(year, 12, 28))[1]


def ordinal_day(epoch_day: int) -> int:
    """Day of the year, 1-based."""
    return epoch_day - days_from_civil(year_of(epoch_day), 1, 1) + 1


def quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def day_of_quarter(epoch_day: int) -> int:
    year, month, _ = civil_from_days(epoch_day)
    first = days_from_civil(year, (quarter_of(month) - 1) * 3 + 1, 1)
    return epoch_day - first + 1


def check_year(year: int) -> None:
    if year < -MAX_YEAR or year > MAX_YEAR:
        raise TemporalOverflow(f"year {year} is out of range (±{MAX_YEAR})")


def calendar_date(year: int, month: int, day: int) -> int:
    """Epoch day of year-month-day, rejecting invalid dates (no clamping)."""
    check_year(year)
    if month < 1 or month > 12:
        raise InvalidTemporalValue(f"invalid value for month: {month} (valid values 1 - 12)")
    if day < 1 or day > days_in_month(year, month):
        raise InvalidTemporalValue(
            f"invalid value for day of month: {day} "
            f"(valid values 1 - {days_in_month(year, month)} for {year}-{month:02d})"
        )
    return days_from_civil(year, month, day)


def week_date(year: int, week: int, day_of_week: int) -> int:
    """Epoch day of an ISO week date: Monday of week 1 is the Monday of the
    week containing January 4th."""
    check_year(year)
    if week < 1 or week > weeks_in_year(year):
        raise InvalidTemporalValue(
            f"invalid value for week of week-based year: {week} "
            f"(valid values 1 - {weeks_in_year(year)} for {year})"
        )
    if day_of_week < 1 or day_of_week > 7:
        raise InvalidTemporalValue(f"invalid value for day of week: {day_of_week} (valid values 1 - 7)")
    jan4 = days_from_civil(year, 1, 4)
    week1 = jan4 - (iso_day_of_week(jan4) - 1)
    return week1 + (week - 1) * 7 + day_of_week - 1


def quarter_date(year: int, quarter: int, day: int) -> int:
    check_year(year)
    if quarter < 1 or quarter > 4:
        raise InvalidTemporalValue(f"invalid value for quarter of year: {quarter} (valid values 1 - 4)")
    first = days_from_civil(year, (quarter - 1) * 3 + 1, 1)
    end_month = quarter * 3
    last = days_from_civil(year, end_month, days_in_month(year, end_month))
    length = last - first + 1
    if day < 1 or day > length:
        raise InvalidTemporalValue(
            f"invalid value for day of quarter: {day} (valid values 1 - {length} for {year}-Q{quarter})"
        )
    return first + day - 1


def ordinal_date(year: int, day: int) -> int:
    check_year(year)
    if day < 1 or day > days_in_year(year):
        raise InvalidTemporalValue(
            f"invalid value for day of year: {day} (valid values 1 - {days_in_year(year)} for {year})"
        )
    return days_from_civil(year, 1, 1) + day - 1


def plus_months(epoch_day: int, months: int) -> int:
    """java.time ``plusMonths``: the day of month is clamped to the new
    month's length (2015-01-31 plus one month is 2015-02-28)."""
    if months == 0:
        return epoch_day
    year, month, day = civil_from_days(epoch_day)
    total = year * 12 + month - 1 + months
    new_year, new_month = total // 12, total % 12 + 1
    return days_from_civil(new_year, new_month, min(day, days_in_month(new_year, new_month)))


def with_year_month(epoch_day: int, year: int | None, month: int | None) -> int:
    """java.time ``withYear`` / ``withMonth``: replace the year and/or month
    of a date, clamping the day of month."""
    y, m, d = civil_from_days(epoch_day)
    y = y if year is None else year
    m = m if month is None else month
    return days_from_civil(y, m, min(d, days_in_month(y, m)))
