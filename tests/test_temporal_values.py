"""``arango_cypher.temporal``: parsing, formatting, construction, accessors,
truncation and errors. Cases marked "gateway" are ported from
arango-gql-gateway's ``internal/temporal`` Go tests, whose semantics this
library follows."""

from __future__ import annotations

import pytest

from arango_cypher import temporal as T
from arango_cypher.temporal import builders as build_module


def c(kind: str, arg: object) -> T.Temporal:
    value = T.construct(kind, arg)
    assert value is not None
    return value


# --- parsing and formatting -------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        # gateway TestParseFormatRoundTrip
        ("date", "2015-07-21", "2015-07-21"),
        ("date", "20150721", "2015-07-21"),
        ("date", "2015-W30-2", "2015-07-21"),
        ("date", "2015202", "2015-07-21"),
        ("date", "2015-Q3-21", "2015-07-21"),
        ("date", "+10000-01-01", "+10000-01-01"),
        ("localtime", "214032.142", "21:40:32.142"),
        ("localtime", "21:40", "21:40"),
        ("time", "21:40:32.142+0100", "21:40:32.142+01:00"),
        ("localdatetime", "2015-W30-2T214032.142", "2015-07-21T21:40:32.142"),
        ("datetime", "2015-07-21T21:40:32.142+01:00", "2015-07-21T21:40:32.142+01:00"),
        (
            "datetime",
            "1818-07-21T21:40:32.142[Europe/Stockholm]",
            "1818-07-21T21:40:32.142+00:53:28[Europe/Stockholm]",
        ),
        ("datetime", "2015-07-21", "2015-07-21T00:00Z"),
        ("duration", "P14DT16H12M", "P14DT16H12M"),
        ("duration", "PT70S", "PT1M10S"),
        ("duration", "P2012-02-02T14:37:21.545", "P2012Y2M2DT14H37M21.545S"),
        ("duration", "PT-0.001S", "PT-0.001S"),
        # gateway TestDisplayMatchesNeo4j5, stored (java.time) form
        ("localtime", "12:31:05.5", "12:31:05.500"),
        ("localtime", "12:31:05.000001", "12:31:05.000001"),
        ("time", "12:00Z", "12:00Z"),
        ("datetime", "1890-01-01T00:00[Europe/Berlin]", "1890-01-01T00:00+00:53:28[Europe/Berlin]"),
        ("duration", "PT1.120S", "PT1.12S"),
        # years outside 0000-9999 and negative years
        ("date", "-0005-03-01", "-0005-03-01"),
        ("date", "+999999999-12-31", "+999999999-12-31"),
        ("date", "-999999999-01-01", "-999999999-01-01"),
        # a comma as the decimal separator, lower-case designators
        ("localtime", "21:40:32,5", "21:40:32.500"),
        ("duration", "p1dt2h", "P1DT2H"),
        ("duration", "-P1DT2H", "P-1DT-2H"),
        ("duration", "P1Y2M3W4DT5H6M7.5S", "P1Y2M25DT5H6M7.5S"),
    ],
)
def test_parse_and_format(kind: str, text: str, expected: str) -> None:
    assert str(c(kind, text)) == expected


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("date", "2015-13-01"),
        ("date", "2015-02-30"),
        ("date", "2015-W54"),
        ("date", "2015-Q5"),
        ("date", "2015-366"),
        ("date", "15-07-21"),
        ("localtime", "24:00"),
        ("localtime", "12:00+01:00"),
        ("time", "12:00[Europe/Berlin]"),
        ("localdatetime", "2015-07-21T12:00Z"),
        ("datetime", "2015-07-21T12:00[Mars/Olympus]"),
        ("datetime", "2015-07-21T12:00+19:00"),
        ("duration", "P"),
        ("duration", "PT"),
        ("duration", "P1.5Y2M"),
        ("duration", "1D"),
    ],
)
def test_unparsable_strings_are_invalid_argument_values(kind: str, text: str) -> None:
    with pytest.raises(T.InvalidTemporalValue) as raised:
        T.construct(kind, text)
    assert raised.value.cypher_class == "InvalidArgumentValue"


def test_duration_too_large_to_hold_is_out_of_range() -> None:
    """gateway TestOutOfRangeIsAnError: P2^63Y used to wrap."""
    with pytest.raises(T.TemporalOverflow):
        T.construct("duration", "P9223372036854775807Y")
    for text in ("P999999999999999Y", "PT1H30M", "P1Y2M3DT4H5M6.5S"):
        T.construct("duration", text)


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (T.Duration(0, 0, 0, 0), "PT0S"),
        (T.Duration(-13, 0, 0, 0), "P-1Y-1M"),
        (T.Duration(0, -1, 1, 0), "P-1DT1S"),
        (T.Duration(0, 0, -86400, 100_000_000), "PT-23H-59M-59.9S"),
        (T.Duration(0, 0, -1, 999_000_000), "PT-0.001S"),
        (T.Duration(0, 0, 0, 1), "PT0.000000001S"),
        (T.Duration(0, 0, 3600, 0), "PT1H"),
    ],
)
def test_duration_string_form(duration: T.Duration, expected: str) -> None:
    assert str(duration) == expected
    assert T.to_string(duration) == expected


def test_to_string_of_null_and_non_temporal() -> None:
    assert T.to_string(None) is None
    with pytest.raises(T.InvalidTemporalType):
        T.to_string("2015-07-21")  # type: ignore[arg-type]


# --- time zones -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # gateway TestZoneResolution: the earlier offset in an overlap, forward
        # by the gap's length in a gap, an explicit offset kept when valid
        ("2021-10-31T02:30[Europe/Berlin]", "2021-10-31T02:30+02:00[Europe/Berlin]"),
        ("2021-10-31T02:30+01:00[Europe/Berlin]", "2021-10-31T02:30+01:00[Europe/Berlin]"),
        ("2021-03-28T02:30[Europe/Berlin]", "2021-03-28T03:30+02:00[Europe/Berlin]"),
        ("2021-04-04T02:30[Australia/Sydney]", "2021-04-04T02:30+11:00[Australia/Sydney]"),
        ("2021-10-03T02:30[Australia/Sydney]", "2021-10-03T03:30+11:00[Australia/Sydney]"),
        # the embedded database has no backzone: Java's (Neo4j's) pre-1970 offsets
        ("1818-07-21T12:00[Europe/Stockholm]", "1818-07-21T12:00+00:53:28[Europe/Stockholm]"),
        # outside Python's datetime range: local mean time before year 1, the
        # 400-year cycle of the zone's rules after 9999
        ("-0100-07-01T12:00[Europe/Berlin]", "-0100-07-01T12:00+00:53:28[Europe/Berlin]"),
        ("+12000-07-01T12:00[Europe/Berlin]", "+12000-07-01T12:00+02:00[Europe/Berlin]"),
        ("+12000-01-01T12:00[Europe/Berlin]", "+12000-01-01T12:00+01:00[Europe/Berlin]"),
        ("2015-07-21T12:00[UTC]", "2015-07-21T12:00Z[UTC]"),
    ],
)
def test_wall_clock_resolution_in_a_zone(text: str, expected: str) -> None:
    assert str(c("datetime", text)) == expected


def test_offset_that_does_not_match_its_zone_is_rejected() -> None:
    with pytest.raises(T.InvalidTemporalValue, match="do not match"):
        T.construct("datetime", "2021-10-30T02:30+01:00[Europe/Berlin]")


def test_zone_parsing() -> None:
    assert T.parse_zone("Z").offset_at(0) == 0
    assert T.parse_zone("+05:30").offset_at(0) == 19800
    assert T.parse_zone("-0207").offset_at(0) == -(2 * 3600 + 7 * 60)
    assert T.parse_zone("+02:05:59").offset_at(0) == 2 * 3600 + 5 * 60 + 59
    assert "Europe/Stockholm" in T.zone_names()
    for bad in ("+19:00", "+1", "+01:60", "Europe/Nowhere", "europe/stockholm"):
        with pytest.raises(T.InvalidTemporalValue):
            T.parse_zone(bad)


# --- construction from maps --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("date", {"year": 1817, "week": 1}, "1816-12-30"),  # gateway TestBuildAndAccessors
        ("date", {"year": 2015, "week": 53, "dayOfWeek": 7}, "2016-01-03"),
        ("date", {"year": 1984, "quarter": 3, "dayOfQuarter": 45}, "1984-08-14"),
        ("date", {"year": 1984, "ordinalDay": 366}, "1984-12-31"),
        ("date", {"year": 1984.0, "month": 2.0, "day": 29}, "1984-02-29"),
        # a selected date keeps the fields not overridden, clamping the day
        ("date", {"date": T.parse("date", "2016-01-31"), "month": 2}, "2016-02-29"),
        ("localtime", {"hour": 12, "minute": 31, "second": 14, "millisecond": 645}, "12:31:14.645"),
        (
            "localtime",
            {
                "hour": 12,
                "minute": 31,
                "second": 14,
                "nanosecond": 789,
                "millisecond": 123,
                "microsecond": 456,
            },
            "12:31:14.123456789",
        ),
        ("time", {"hour": 12, "timezone": "-02:05:07"}, "12:00-02:05:07"),
        (
            "datetime",
            {"year": 1984, "month": 10, "day": 11, "timezone": "Europe/Stockholm"},
            "1984-10-11T00:00+01:00[Europe/Stockholm]",
        ),
        ("duration", {"months": 0.75}, "P22DT19H51M49.5S"),
        ("duration", {"weeks": 2.5}, "P17DT12H"),
        ("duration", {"minutes": 1.5, "seconds": 1}, "PT1M31S"),
        ("duration", {"seconds": -1.5}, "PT-1.5S"),
        ("duration", {"days": 14, "seconds": 70, "nanoseconds": 1}, "P14DT1M10.000000001S"),
        ("duration", {}, "PT0S"),
    ],
)
def test_build_from_fields(kind: str, fields: dict[str, object], expected: str) -> None:
    assert str(c(kind, fields)) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # gateway TestEpochFields: the instant is the base the other fields override
        ({"epochMillis": 1500, "nanosecond": 5}, "1970-01-01T00:00:01.000000005Z"),
        ({"epochSeconds": 1, "millisecond": 5}, "1970-01-01T00:00:01.005Z"),
        (
            {"epochSeconds": 1, "nanosecond": 5, "timezone": "Europe/Berlin"},
            "1970-01-01T01:00:01.000000005+01:00[Europe/Berlin]",
        ),
        ({"epochSeconds": -1, "nanosecond": 5}, "1969-12-31T23:59:59.000000005Z"),
        ({"epochSeconds": 1, "year": 2000}, "2000-01-01T00:00:01Z"),
    ],
)
def test_datetime_from_epoch_fields(fields: dict[str, object], expected: str) -> None:
    assert str(c("datetime", fields)) == expected


def test_from_epoch_functions() -> None:
    assert str(T.from_epoch(416779, 999999999)) == "1970-01-05T19:46:19.999999999Z"
    assert str(T.from_epoch_millis(237821673987)) == "1977-07-15T13:34:33.987Z"
    assert str(T.from_epoch_millis(-1)) == "1969-12-31T23:59:59.999Z"
    with pytest.raises(T.InvalidTemporalType):
        T.from_epoch(1.5, 0)  # type: ignore[arg-type]
    with pytest.raises(T.TemporalOverflow):
        T.from_epoch(10**20, 0)


@pytest.mark.parametrize(
    ("kind", "fields", "error"),
    [
        ("date", {"year": 2015, "month": 13}, T.InvalidTemporalValue),
        ("date", {"year": 2015, "month": 2, "day": 29}, T.InvalidTemporalValue),
        ("date", {"year": 2015, "week": 54}, T.InvalidTemporalValue),
        ("date", {"year": 2015, "day": 3}, T.UnsupportedTemporalUnit),
        ("date", {"year": 2015, "dayOfWeek": 3}, T.UnsupportedTemporalUnit),
        ("date", {"year": 2015, "week": 3, "month": 2}, T.UnsupportedTemporalUnit),
        ("date", {"month": 2}, T.UnsupportedTemporalUnit),
        ("date", {"year": 2015, "hour": 2}, T.UnsupportedTemporalUnit),
        ("date", {"year": 2015.5}, T.InvalidTemporalValue),
        ("date", {"year": "2015"}, T.InvalidTemporalType),
        ("date", {"year": True}, T.InvalidTemporalType),
        ("date", {"year": 2**40}, T.TemporalOverflow),  # gateway TestOutOfRangeIsAnError
        ("date", {"date": "2015-01-01"}, T.InvalidTemporalType),
        ("date", {"date": T.LocalTime(0)}, T.InvalidTemporalType),
        ("date", {}, T.InvalidTemporalValue),
        ("localtime", {"hour": 12, "second": 3}, T.UnsupportedTemporalUnit),
        ("localtime", {"minute": 3}, T.UnsupportedTemporalUnit),
        ("localtime", {"hour": 24}, T.InvalidTemporalValue),
        (
            "localtime",
            {"hour": 1, "minute": 1, "second": 1, "millisecond": 1, "microsecond": 1000},
            T.InvalidTemporalValue,
        ),
        ("time", {"hour": 1, "timezone": 1}, T.InvalidTemporalType),
        ("datetime", {"year": 2015, "timezone": "Mars/Olympus"}, T.InvalidTemporalValue),
        ("datetime", {"epochSeconds": 1, "epochMillis": 1}, T.UnsupportedTemporalUnit),
        ("datetime", {"epochSeconds": 1.5}, T.InvalidTemporalValue),
        ("duration", {"fortnights": 1}, T.UnsupportedTemporalUnit),
        ("duration", {"days": "1"}, T.InvalidTemporalType),
        ("duration", {"years": 2**62}, T.TemporalOverflow),
    ],
)
def test_invalid_fields(kind: str, fields: dict[str, object], error: type[T.TemporalError]) -> None:
    with pytest.raises(error):
        T.construct(kind, fields)


def test_error_classes_carry_the_neo4j_error_name() -> None:
    assert T.InvalidTemporalValue("x").cypher_class == "InvalidArgumentValue"
    assert T.InvalidTemporalType("x").cypher_class == "InvalidArgumentType"
    unit = T.UnsupportedTemporalUnit("x")
    assert unit.cypher_class == "UnsupportedTemporalUnit"
    assert unit.tck_detail == "InvalidArgumentValue"
    assert T.TemporalOverflow("x").cypher_class == "NumberOutOfRange"
    assert all(
        issubclass(e, T.TemporalError)
        for e in (
            T.InvalidTemporalValue,
            T.InvalidTemporalType,
            T.UnsupportedTemporalUnit,
            T.TemporalOverflow,
        )
    )
    assert not issubclass(T.NotConstant, T.TemporalError)


def test_construct_arguments() -> None:
    assert T.construct("date", None) is None
    with pytest.raises(T.InvalidTemporalType):
        T.construct("date", 20150721)
    with pytest.raises(T.InvalidTemporalValue):
        T.construct("instant", "2015-07-21")
    with pytest.raises(T.InvalidTemporalType):
        T.construct("duration")


@pytest.mark.parametrize(
    "call",
    [
        lambda: T.construct("date"),
        lambda: T.construct("datetime"),
        lambda: T.construct("localtime", {"timezone": "Europe/Berlin"}),
        lambda: T.clock("datetime", "statement"),
        lambda: T.clock("date", "realtime", "Europe/Berlin"),
        lambda: T.clock("time", "transaction", {"timezone": "+01:00"}),
    ],
)
def test_clock_forms_are_not_constant(call: object) -> None:
    with pytest.raises(T.NotConstant):
        call()  # type: ignore[operator]


def test_clock_of_null_is_null_and_bad_clock_is_an_error() -> None:
    assert T.clock("date", "statement", None) is None
    with pytest.raises(T.InvalidTemporalValue):
        T.clock("duration", "statement")
    with pytest.raises(T.InvalidTemporalValue):
        T.clock("date", "yesterday")
    with pytest.raises(T.InvalidTemporalValue):
        T.clock("date", "statement", "Mars/Olympus")


def test_time_in_a_named_zone_takes_the_offset_of_the_current_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Time has no date, so Neo4j picks a named zone's offset at the current
    instant; the library reads the clock only there."""
    summer = 1_500_000_000  # 2017-07-14
    winter = 1_484_000_000  # 2017-01-09
    monkeypatch.setattr(build_module, "_now_seconds", lambda: summer)
    assert str(c("time", {"hour": 12, "timezone": "Europe/Berlin"})) == "12:00+02:00"
    monkeypatch.setattr(build_module, "_now_seconds", lambda: winter)
    assert str(c("time", {"hour": 12, "timezone": "Europe/Berlin"})) == "12:00+01:00"


# --- conversion -------------------------------------------------------------------


def test_conversion_between_kinds() -> None:
    dt = c("datetime", "2015-07-21T21:40:32.142+01:00")
    assert str(T.construct("date", dt)) == "2015-07-21"
    assert str(T.construct("localtime", dt)) == "21:40:32.142"
    assert str(T.construct("time", dt)) == "21:40:32.142+01:00"
    assert str(T.construct("localdatetime", dt)) == "2015-07-21T21:40:32.142"
    assert str(T.construct("datetime", c("date", "2015-07-21"))) == "2015-07-21T00:00Z"
    assert str(T.construct("time", c("localtime", "12:00"))) == "12:00Z"
    for kind, value in (
        ("date", c("localtime", "12:00")),
        ("datetime", c("time", "12:00Z")),
        ("duration", dt),
    ):
        with pytest.raises(T.InvalidTemporalType):
            T.construct(kind, value)


# --- accessors ---------------------------------------------------------------------


def test_date_accessors() -> None:
    d = c("date", {"year": 1984, "month": 10, "day": 11})
    got = {name: T.get_property(d, name) for name in T.DATE_ACCESSORS}
    assert got == {
        "year": 1984,
        "quarter": 4,
        "month": 10,
        "week": 41,
        "weekYear": 1984,
        "day": 11,
        "ordinalDay": 285,
        "weekDay": 4,
        "dayOfWeek": 4,
        "dayOfQuarter": 11,
    }


@pytest.mark.parametrize(
    ("text", "week_year", "week"),
    [
        ("1984-01-01", 1983, 52),  # in the last week of the previous week year
        ("2016-01-03", 2015, 53),
        ("2019-12-30", 2020, 1),  # in the first week of the next week year
        ("2015-12-28", 2015, 53),
    ],
)
def test_iso_week_years(text: str, week_year: int, week: int) -> None:
    d = c("date", text)
    assert (T.get_property(d, "weekYear"), T.get_property(d, "week")) == (week_year, week)


def test_zoned_accessors() -> None:
    dt = c(
        "datetime",
        {
            "year": 1984,
            "month": 11,
            "day": 11,
            "hour": 12,
            "minute": 31,
            "second": 14,
            "nanosecond": 645876123,
            "timezone": "Europe/Stockholm",
        },
    )
    assert T.get_property(dt, "timezone") == "Europe/Stockholm"
    assert T.get_property(dt, "offset") == "+01:00"
    assert T.get_property(dt, "offsetMinutes") == 60
    assert T.get_property(dt, "offsetSeconds") == 3600
    assert T.get_property(dt, "epochSeconds") == 469020674
    assert T.get_property(dt, "epochMillis") == 469020674645
    assert T.get_property(dt, "millisecond") == 645
    assert T.get_property(dt, "microsecond") == 645876
    assert T.get_property(dt, "nanosecond") == 645876123
    t = c("time", "12:00-02:30")
    assert T.get_property(t, "timezone") == "-02:30"
    assert T.get_property(t, "offsetMinutes") == -150
    assert T.get_property(c("time", "12:00Z"), "offset") == "Z"


def test_duration_accessors() -> None:
    """gateway TestBuildAndAccessors and TCK Temporal5 [7]."""
    d = c(
        "duration",
        {
            "years": 1,
            "months": 4,
            "days": 10,
            "hours": 1,
            "minutes": 1,
            "seconds": 1,
            "nanoseconds": 111111111,
        },
    )
    got = {name: T.get_property(d, name) for name in T.DURATION_ACCESSORS}
    assert got == {
        "years": 1,
        "quarters": 5,
        "months": 16,
        "weeks": 1,
        "days": 10,
        "hours": 1,
        "minutes": 61,
        "seconds": 3661,
        "milliseconds": 3661111,
        "microseconds": 3661111111,
        "nanoseconds": 3661111111111,
        "quartersOfYear": 1,
        "monthsOfQuarter": 1,
        "monthsOfYear": 4,
        "daysOfWeek": 3,
        "minutesOfHour": 1,
        "secondsOfMinute": 1,
        "millisecondsOfSecond": 111,
        "microsecondsOfSecond": 111111,
        "nanosecondsOfSecond": 111111111,
    }


def test_negative_duration_accessors_truncate_toward_zero() -> None:
    d = c("duration", "P-1Y-5M-10DT-25H")
    assert T.get_property(d, "years") == -1
    assert T.get_property(d, "monthsOfYear") == -5
    assert T.get_property(d, "weeks") == -1
    assert T.get_property(d, "hours") == -25
    assert T.get_property(d, "minutesOfHour") == 0


def test_unknown_accessors() -> None:
    assert T.get_property(None, "year") is None
    for value, name in (
        (c("date", "2015-01-01"), "hour"),
        (c("localtime", "12:00"), "timezone"),
        (c("localdatetime", "2015-01-01T12:00"), "epochSeconds"),
        (c("duration", "P1D"), "year"),
        (c("date", "2015-01-01"), "quarterDay"),
    ):
        with pytest.raises(T.UnsupportedTemporalUnit):
            T.get_property(value, name)
    assert set(T.accessors(c("datetime", "2015-01-01T00:00Z"))) >= {"week", "hour", "offset", "epochMillis"}


# --- truncation --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "unit", "source", "value", "overrides", "expected"),
    [
        ("date", "millennium", "date", "2017-10-11", None, "2000-01-01"),
        ("date", "weekYear", "date", "2016-01-02", None, "2014-12-29"),
        ("date", "week", "date", "2017-10-11", {"dayOfWeek": 2}, "2017-10-10"),
        ("date", "decade", "date", "-0015-06-01", None, "-0020-01-01"),
        (
            "datetime",
            "day",
            "datetime",
            "2017-10-11T12:31:14.645876123+01:00",
            {"timezone": "Europe/Stockholm"},
            "2017-10-11T00:00+02:00[Europe/Stockholm]",
        ),
        (
            "localdatetime",
            "millennium",
            "localdatetime",
            "2017-10-11T12:31:14.645876123",
            {"day": 2},
            "2000-01-02T00:00",
        ),
        (
            "localtime",
            "millisecond",
            "localtime",
            "12:31:14.645876123",
            {"nanosecond": 2},
            "12:31:14.645000002",
        ),
        ("localtime", "day", "localtime", "12:31:14", None, "00:00"),
        ("time", "hour", "time", "12:31:14+01:00", None, "12:00+01:00"),
        ("time", "minute", "localdatetime", "2017-10-11T12:31:14", None, "12:31Z"),
    ],
)
def test_truncate(
    kind: str, unit: str, source: str, value: str, overrides: dict[str, object] | None, expected: str
) -> None:
    assert str(T.truncate(kind, unit, c(source, value), overrides)) == expected


def test_truncate_errors() -> None:
    d = c("date", "2015-01-01")
    assert T.truncate("date", "day", None, None) is None
    with pytest.raises(T.UnsupportedTemporalUnit):
        T.truncate("date", "hour", d, None)
    with pytest.raises(T.UnsupportedTemporalUnit):
        T.truncate("date", "fortnight", d, None)
    with pytest.raises(T.UnsupportedTemporalUnit):
        T.truncate("localtime", "week", c("localtime", "12:00"), None)
    with pytest.raises(T.InvalidTemporalType):
        T.truncate("datetime", "day", c("localtime", "12:00"), None)
    with pytest.raises(T.InvalidTemporalType):
        T.truncate("localtime", "hour", d, None)
    with pytest.raises(T.InvalidTemporalType):
        T.truncate("date", "day", c("duration", "P1D"), None)


def test_values_are_frozen_and_hashable() -> None:
    a = c("datetime", "2015-07-21T21:40:32.142+01:00")
    b = c("datetime", "2015-07-21T21:40:32.142+01:00")
    assert a == b and hash(a) == hash(b)
    assert len({a, b, c("date", "2015-07-21"), T.Date(16637)}) == 2
    with pytest.raises(AttributeError):
        a.offset = 0  # type: ignore[misc]
    assert T.is_temporal(a) and not T.is_temporal("2015-07-21") and not T.is_temporal(None)
