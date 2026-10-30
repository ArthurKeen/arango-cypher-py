"""``arango_cypher.temporal``: arithmetic, comparison and ``duration.between``.
Cases marked "gateway" are ported from arango-gql-gateway's
``internal/temporal`` Go tests."""

from __future__ import annotations

import pytest

from arango_cypher import temporal as T


def c(kind: str, arg: object) -> T.Temporal:
    value = T.construct(kind, arg)
    assert value is not None
    return value


def dur(text: str) -> T.Duration:
    value = c("duration", text)
    assert isinstance(value, T.Duration)
    return value


# --- temporal ± duration -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "value", "duration", "plus", "minus"),
    [
        # month arithmetic clamps the day of month (gateway TestArithmeticAndJSON)
        ("date", "2015-01-31", "P1M", "2015-02-28", "2014-12-31"),
        ("date", "2016-02-29", "P1Y", "2017-02-28", "2015-02-28"),
        ("date", "2016-03-31", "P1M", "2016-04-30", "2016-02-29"),
        # whole days of the seconds, truncated toward zero; nanoseconds ignored
        ("date", "2015-01-01", "PT36H", "2015-01-02", "2014-12-31"),
        ("date", "2015-01-01", "PT-36H", "2014-12-31", "2015-01-02"),
        # times of day wrap around midnight; months and days are ignored
        ("localtime", "23:00", "P1M1DT2H", "01:00", "21:00"),
        ("time", "00:30+01:00", "PT-1H", "23:30+01:00", "01:30+01:00"),
        ("localdatetime", "2015-01-31T23:00", "P1MT2H", "2015-03-01T01:00", "2014-12-31T21:00"),
    ],
)
def test_add_and_subtract_a_duration(kind: str, value: str, duration: str, plus: str, minus: str) -> None:
    v = c(kind, value)
    d = dur(duration)
    assert str(T.add(v, d)) == plus
    assert str(T.add(d, v)) == plus
    assert str(T.subtract(v, d)) == minus


def test_fractional_duration_fields_spill_into_smaller_units() -> None:
    """TCK Temporal8 [5]: 12.5 years is 12 years 6 months, 5.5 months adds
    half an average month in days and seconds, and so on down."""
    d = c(
        "duration",
        {
            "years": 12.5,
            "months": 5.5,
            "days": 14.5,
            "hours": 16.5,
            "minutes": 12.5,
            "seconds": 70.5,
            "nanoseconds": 3,
        },
    )
    assert str(d) == "P12Y11M29DT33H58M13.500000003S"
    x = c(
        "datetime",
        {
            "year": 1984,
            "month": 10,
            "day": 11,
            "hour": 12,
            "minute": 31,
            "second": 14,
            "nanosecond": 1,
            "timezone": "+01:00",
        },
    )
    assert str(T.add(x, d)) == "1997-10-11T22:29:27.500000004+01:00"
    assert str(T.subtract(x, d)) == "1971-10-12T02:33:00.499999998+01:00"


@pytest.mark.parametrize(
    ("value", "duration", "expected"),
    [
        # wall-clock units resolve in the zone; the previous offset is kept when
        # still valid (gateway TestZoneResolution)
        ("2021-10-30T02:30+02:00[Europe/Berlin]", "P1D", "2021-10-31T02:30+02:00[Europe/Berlin]"),
        ("2021-10-31T02:30+01:00[Europe/Berlin]", "-P1D", "2021-10-30T02:30+02:00[Europe/Berlin]"),
        ("2021-10-31T02:30+01:00[Europe/Berlin]", "PT0S", "2021-10-31T02:30+01:00[Europe/Berlin]"),
        ("2021-03-27T12:00+01:00[Europe/Berlin]", "P1D", "2021-03-28T12:00+02:00[Europe/Berlin]"),
        # a wall clock in the gap moves forward by the gap
        ("2021-03-27T02:30+01:00[Europe/Berlin]", "P1D", "2021-03-28T03:30+02:00[Europe/Berlin]"),
        # seconds are exact time on the instant line
        ("2021-03-27T12:00+01:00[Europe/Berlin]", "PT24H", "2021-03-28T13:00+02:00[Europe/Berlin]"),
        ("2021-10-31T01:30+02:00[Europe/Berlin]", "PT1H", "2021-10-31T02:30+02:00[Europe/Berlin]"),
        ("2021-10-31T02:30+02:00[Europe/Berlin]", "PT1H", "2021-10-31T02:30+01:00[Europe/Berlin]"),
    ],
)
def test_datetime_arithmetic_across_daylight_saving(value: str, duration: str, expected: str) -> None:
    assert str(T.add(c("datetime", value), dur(duration))) == expected


def test_duration_plus_and_minus_duration() -> None:
    """TCK Temporal8 [6] rows, including fractions and negative components."""
    a = c(
        "duration",
        {"years": 12, "months": 5, "days": 14, "hours": 16, "minutes": 12, "seconds": 70, "nanoseconds": 1},
    )
    b = c("duration", {"months": 1, "days": -14, "hours": 16, "minutes": -12, "seconds": 70})
    f = c(
        "duration",
        {
            "years": 12.5,
            "months": 5.5,
            "days": 14.5,
            "hours": 16.5,
            "minutes": 12.5,
            "seconds": 70.5,
            "nanoseconds": 3,
        },
    )
    assert str(T.add(a, b)) == "P12Y6MT32H2M20.000000001S"
    assert str(T.subtract(a, b)) == "P12Y4M28DT24M0.000000001S"
    assert str(T.subtract(b, a)) == "P-12Y-4M-28DT-24M-0.000000001S"
    assert str(T.subtract(a, f)) == "P-6M-15DT-17H-45M-3.500000002S"
    assert str(T.subtract(a, a)) == "PT0S"


@pytest.mark.parametrize(
    ("factor", "product", "quotient"),
    [
        (1, "P12Y5M14DT16H13M10.000000001S", "P12Y5M14DT16H13M10.000000001S"),
        (2, "P24Y10M28DT32H26M20.000000002S", "P6Y2M22DT13H21M8S"),
        (0.5, "P6Y2M22DT13H21M8S", "P24Y10M28DT32H26M20.000000002S"),
        (-1, "P-12Y-5M-14DT-16H-13M-10.000000001S", "P-12Y-5M-14DT-16H-13M-10.000000001S"),
    ],
)
def test_multiply_and_divide(factor: int | float, product: str, quotient: str) -> None:
    """TCK Temporal8 [7], plus a negative factor."""
    d = c(
        "duration",
        {"years": 12, "months": 5, "days": 14, "hours": 16, "minutes": 12, "seconds": 70, "nanoseconds": 1},
    )
    assert str(T.multiply(d, factor)) == product
    assert str(T.multiply(factor, d)) == product
    assert str(T.divide(d, factor)) == quotient


def test_arithmetic_with_null_is_null() -> None:
    d = dur("P1D")
    assert T.add(None, d) is None
    assert T.subtract(c("date", "2015-01-01"), None) is None
    assert T.multiply(d, None) is None
    assert T.divide(None, 2) is None


def test_arithmetic_type_errors() -> None:
    date = c("date", "2015-01-01")
    for call in (
        lambda: T.add(date, date),
        lambda: T.subtract(dur("P1D"), date),
        lambda: T.add(date, 1),
        lambda: T.multiply(date, 2),
        lambda: T.multiply(dur("P1D"), "2"),
        lambda: T.multiply(dur("P1D"), True),
        lambda: T.divide(2, dur("P1D")),
    ):
        with pytest.raises(T.InvalidTemporalType):
            call()


def test_out_of_range_is_an_error() -> None:
    """gateway TestOutOfRangeIsAnError: these used to wrap silently."""
    d2020 = c("date", "2020-01-01")
    with pytest.raises(T.TemporalOverflow):
        T.add(d2020, dur("P999999999999999Y"))
    top = c("date", {"year": 999_999_999})
    with pytest.raises(T.TemporalOverflow):
        T.add(top, T.Duration(12, 0, 0, 0))
    with pytest.raises(T.TemporalOverflow):
        T.multiply(T.Duration(1 << 40, 0, 0, 0), 1 << 40)
    big = dur("PT999999999999999H")
    with pytest.raises(T.TemporalOverflow):
        T.multiply(big, 3.0)
    with pytest.raises(T.TemporalOverflow):
        T.divide(big, 0.3)
    with pytest.raises(T.TemporalOverflow):
        T.divide(dur("P1D"), 0)
    assert str(T.add(d2020, T.Duration(13, 2, 3600, 0))) == "2021-02-03"
    assert str(c("date", {"year": 999_999_999, "month": 1, "day": 1})) == "+999999999-01-01"


# --- comparison and equality ---------------------------------------------------------


def test_compare_same_kind() -> None:
    assert T.compare(c("date", "2015-01-01"), c("date", "2015-01-02")) == -1
    assert T.compare(c("localtime", "12:00"), c("localtime", "11:59:59.999999999")) == 1
    assert T.compare(c("localdatetime", "2015-01-01T12:00"), c("localdatetime", "2015-01-01T12:00")) == 0
    # Time orders by the instant on 1970-01-01, then by local time
    assert T.compare(c("time", "12:00+01:00"), c("time", "11:30Z")) == -1
    assert T.compare(c("time", "12:00+01:00"), c("time", "11:00Z")) == 1
    # DateTime orders by instant only
    assert (
        T.compare(
            c("datetime", "2015-07-21T21:40+01:00"), c("datetime", "2015-07-21T22:40+02:00[Europe/Berlin]")
        )
        == 0
    )


def test_compare_is_undefined_across_kinds_and_for_durations() -> None:
    assert T.compare(c("date", "2015-01-01"), c("localdatetime", "2015-01-01T00:00")) is None
    assert T.compare(dur("P1D"), dur("P2D")) is None
    assert T.compare(c("date", "2015-01-01"), None) is None
    assert T.compare(c("date", "2015-01-01"), "2015-01-01") is None


def test_equality() -> None:
    a = c("datetime", "2015-07-21T21:40+01:00")
    same_instant_other_zone = c("datetime", "2015-07-21T22:40+02:00[Europe/Berlin]")
    assert T.equals(a, same_instant_other_zone) is False  # java.time equality: zone counts
    assert T.equals(a, c("datetime", "2015-07-21T21:40:00.000+01:00")) is True
    assert T.equals(c("time", "12:00+01:00"), c("time", "11:00Z")) is False
    assert T.equals(dur("PT70S"), dur("PT1M10S")) is True
    assert T.equals(dur("P1M"), dur("P30D")) is False
    assert T.equals(c("date", "2015-01-01"), c("localdatetime", "2015-01-01T00:00")) is False
    assert T.equals(c("date", "2015-01-01"), "2015-01-01") is False
    assert T.equals(c("date", "2015-01-01"), None) is None
    assert T.equals(None, None) is None


# --- duration.between and friends ---------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "unit", "expected"),
    [
        (("date", "1984-10-11"), ("date", "2015-07-21"), None, "P30Y9M10D"),  # gateway
        (
            ("date", "-999999999-01-01"),
            ("date", "+999999999-12-31"),
            None,
            "P1999999998Y11M30D",
        ),  # gateway, TCK Temporal10 [9]
        (("date", "1984-10-11"), ("datetime", "2015-07-21T21:40:32.142+0100"), "months", "P30Y9M"),
        (
            ("localdatetime", "2015-07-21T21:40:32.142"),
            ("date", "2015-06-24"),
            None,
            "P-27DT-21H-40M-32.142S",
        ),
        (("localtime", "14:30"), ("date", "2015-06-24"), None, "PT-14H-30M"),
        (("time", "14:30"), ("time", "16:30+0100"), None, "PT1H"),
        (("date", "2018-01-01"), ("date", "2018-02-02"), "days", "P32D"),
        (("localtime", "12:00"), ("localtime", "13:00"), "days", "PT0S"),
        (("localtime", "12:00"), ("localtime", "13:00"), "months", "PT0S"),
        # across a daylight-saving change: exact seconds (TCK Temporal10 [1], [8])
        (
            ("datetime", "2017-10-28T23:00+02:00[Europe/Stockholm]"),
            ("datetime", "2017-10-29T04:00+01:00[Europe/Stockholm]"),
            None,
            "PT6H",
        ),
        (
            ("datetime", "2017-10-29T00:00+02:00[Europe/Stockholm]"),
            ("datetime", "2017-10-30T00:00+01:00[Europe/Stockholm]"),
            "seconds",
            "PT25H",
        ),
        # seconds and sub-seconds of different signs (TCK Temporal10 [6], [11])
        (
            ("localdatetime", "2014-07-21T21:40:36.143"),
            ("localdatetime", "2014-07-21T21:40:36.142"),
            "seconds",
            "PT-0.001S",
        ),
        (("localtime", "12:34:54.7"), ("localtime", "12:34:54.3"), "seconds", "PT-0.4S"),
        (("localtime", "12:34:54.3"), ("localtime", "12:34:55.7"), "seconds", "PT1.4S"),
    ],
)
def test_duration_between(a: tuple[str, str], b: tuple[str, str], unit: str | None, expected: str) -> None:
    assert str(T.duration_between(c(*a), c(*b), unit)) == expected


def test_duration_between_arguments() -> None:
    d = c("date", "2015-01-01")
    assert T.duration_between(None, d) is None
    assert T.duration_between(d, None, "seconds") is None
    with pytest.raises(T.InvalidTemporalType):
        T.duration_between(d, dur("P1D"))
    with pytest.raises(T.InvalidTemporalType):
        T.duration_between(d, "2015-01-01", "days")
    with pytest.raises(T.InvalidTemporalValue):
        T.duration_between(d, d, "weeks")


def test_duration_between_and_back() -> None:
    """Adding the duration between two values to the first gives the second."""
    pairs = [
        ("date", "1984-10-11", "date", "2015-07-21"),
        ("localdatetime", "2015-01-31T23:59:59.999", "localdatetime", "2016-02-29T00:00"),
        (
            "datetime",
            "2021-03-27T02:30+01:00[Europe/Berlin]",
            "datetime",
            "2021-11-01T02:30+01:00[Europe/Berlin]",
        ),
    ]
    for ka, a, kb, b in pairs:
        x, y = c(ka, a), c(kb, b)
        between = T.duration_between(x, y)
        assert T.equals(T.add(x, between), y), (a, b, str(between))
