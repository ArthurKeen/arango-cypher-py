"""``arango_cypher.temporal._calendar``: epoch-day arithmetic for any year,
checked against Python's ``datetime.date`` where the two overlap."""

from __future__ import annotations

import datetime as dt

import pytest

from arango_cypher.temporal import _calendar as cal
from arango_cypher.temporal.errors import InvalidTemporalValue

_EPOCH_ORDINAL = dt.date(1970, 1, 1).toordinal()


@pytest.mark.parametrize("step", [1, 7, 97, 1009])
def test_civil_round_trip_matches_python_dates(step: int) -> None:
    first, last = dt.date(1, 1, 1).toordinal(), dt.date(9999, 12, 31).toordinal()
    for ordinal in range(first, last + 1, step * 211):
        expected = dt.date.fromordinal(ordinal)
        epoch_day = ordinal - _EPOCH_ORDINAL
        assert cal.civil_from_days(epoch_day) == (expected.year, expected.month, expected.day)
        assert cal.days_from_civil(expected.year, expected.month, expected.day) == epoch_day
        assert cal.iso_day_of_week(epoch_day) == expected.isoweekday()
        iso = expected.isocalendar()
        assert cal.iso_week(epoch_day) == (iso.year, iso.week)
        assert cal.ordinal_day(epoch_day) == expected.timetuple().tm_yday


@pytest.mark.parametrize("year", [-999_999_999, -4713, -1, 0, 1, 1600, 1900, 2000, 9999, 10_000, 999_999_999])
def test_civil_round_trip_outside_python_range(year: int) -> None:
    for month, day in ((1, 1), (2, 28), (12, 31)):
        assert cal.civil_from_days(cal.days_from_civil(year, month, day)) == (year, month, day)
    assert cal.is_leap(year) == (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))


def test_truncating_division_matches_java() -> None:
    assert (cal.tdiv(-7, 2), cal.tmod(-7, 2)) == (-3, -1)
    assert (cal.tdiv(7, -2), cal.tmod(7, -2)) == (-3, 1)
    assert (cal.tdiv(7, 2), cal.tmod(7, 2)) == (3, 1)


def test_month_arithmetic_clamps_the_day() -> None:
    jan31 = cal.days_from_civil(2015, 1, 31)
    assert cal.civil_from_days(cal.plus_months(jan31, 1)) == (2015, 2, 28)
    assert cal.civil_from_days(cal.plus_months(jan31, -2)) == (2014, 11, 30)
    assert cal.civil_from_days(cal.plus_months(jan31, 13)) == (2016, 2, 29)


def test_week_quarter_and_ordinal_dates() -> None:
    assert cal.civil_from_days(cal.week_date(2015, 53, 7)) == (2016, 1, 3)
    assert cal.weeks_in_year(2015) == 53 and cal.weeks_in_year(2016) == 52
    assert cal.civil_from_days(cal.quarter_date(2016, 1, 91)) == (2016, 3, 31)
    assert cal.civil_from_days(cal.ordinal_date(2016, 366)) == (2016, 12, 31)
    for bad in (
        lambda: cal.week_date(2016, 53, 1),
        lambda: cal.quarter_date(2015, 1, 91),
        lambda: cal.ordinal_date(2015, 366),
    ):
        with pytest.raises(InvalidTemporalValue, match="invalid value"):
            bad()
