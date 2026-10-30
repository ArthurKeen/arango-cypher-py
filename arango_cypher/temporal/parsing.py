"""Parsing the ISO 8601 string forms Neo4j accepts for each temporal type.

The patterns follow Neo4j's ``DateValue`` / ``TimeValue`` / ``DurationValue``
parsers: extended forms with separators (``2015-07-21``, ``21:40:32.142``)
and basic forms without (``20150721``, ``214032.142``), calendar, week
(``2015-W30-2``), quarter (``2015-Q3-21``) and ordinal (``2015-202``) dates,
offsets (``Z``, ``+01``, ``+0100``, ``+01:00``) and IANA zones in brackets,
and both duration forms (``P1Y2M3W4DT5H6M7.5S``, ``P2012-02-02T14:37:21.545``).
"""

from __future__ import annotations

import re

from ._calendar import (
    NANOS_PER_SECOND,
    calendar_date,
    ordinal_date,
    quarter_date,
    week_date,
)
from .errors import InvalidTemporalValue, TemporalError, TemporalOverflow
from .values import (
    Date,
    DateTime,
    Duration,
    Temporal,
    datetime_at_instant,
    datetime_of_local,
    make_duration,
    make_local_datetime,
    make_local_time,
    make_time,
)
from .zones import UTC_ZONE, Zone, parse_offset, parse_zone

_YEAR = r"(?P<year>[0-9]{4}|[+-][0-9]{1,9})"
_DATE = (
    _YEAR + r"(?:-(?P<lm>[0-9]{2})(?:-(?P<ld>[0-9]{2}))?"
    r"|(?P<sm>[0-9]{2})(?P<sd>[0-9]{2})?"
    r"|-W(?P<lw>[0-9]{2})(?:-(?P<lwd>[0-9]))?"
    r"|W(?P<sw>[0-9]{2})(?P<swd>[0-9])?"
    r"|-Q(?P<lq>[0-9])(?:-(?P<lqd>[0-9]{2}))?"
    r"|Q(?P<sq>[0-9])(?P<sqd>[0-9]{2})?"
    r"|-(?P<lo>[0-9]{3})"
    r"|(?P<so>[0-9]{3}))?"
)
_TIME = (
    r"(?:(?P<lh>[0-9]{1,2}):(?P<lmi>[0-9]{1,2})(?::(?P<ls>[0-9]{1,2})(?:[.,](?P<lf>[0-9]{1,9}))?)?"
    r"|(?P<sh>[0-9]{2})(?:(?P<smi>[0-9]{2})(?:(?P<ss>[0-9]{2})(?:[.,](?P<sf>[0-9]{1,9}))?)?)?)"
)
_ZONE = r"(?P<off>Z|z|[+-][0-9]{2}(?::?[0-9]{2}(?::?[0-9]{2})?)?)?(?:\[(?P<zone>[A-Za-z0-9~._ /+-]+)\])?"
_NUM = r"[-+]?[0-9]+(?:[.,][0-9]+)?"
_DURATION = (
    r"(?P<sign>[-+]?)P"
    rf"(?:(?P<y>{_NUM})Y)?(?:(?P<mo>{_NUM})M)?(?:(?P<w>{_NUM})W)?(?:(?P<d>{_NUM})D)?"
    rf"(?P<t>T(?:(?P<h>{_NUM})H)?(?:(?P<mi>{_NUM})M)?(?:(?P<s>[-+]?[0-9]+(?:[.,][0-9]{{0,9}})?)S)?)?"
)
_DURATION_ALT = (
    r"(?P<sign>[-+]?)P(?P<y>[0-9]{4})(?P<sep>-?)(?P<mo>[0-9]{2})-?(?P<d>[0-9]{2})"
    r"(?:T(?P<h>[0-9]{2}):?(?P<mi>[0-9]{2}):?(?P<s>[0-9]{2})(?:[.,](?P<f>[0-9]{1,9}))?)?"
)

_RE_DATE = re.compile(_DATE, re.IGNORECASE)
_RE_TIME = re.compile(_TIME + _ZONE, re.IGNORECASE)
_RE_DATETIME = re.compile(_DATE + r"(?:T" + _TIME + r")?" + _ZONE, re.IGNORECASE)
_RE_DURATION = re.compile(_DURATION, re.IGNORECASE)
_RE_DURATION_ALT = re.compile(_DURATION_ALT)

# Longer whole parts would overflow 64-bit years * 12 or hours * 3600.
_MAX_DURATION_DIGITS = 15


def _groups(pattern: re.Pattern[str], text: str) -> dict[str, str] | None:
    match = pattern.fullmatch(text)
    if match is None:
        return None
    return {k: v for k, v in match.groupdict().items() if v}


def _int(text: str | None) -> int:
    return int(text) if text else 0


def _fraction_nanos(digits: str) -> int:
    return int((digits + "000000000")[:9])


def _bad(kind: str, text: str) -> InvalidTemporalValue:
    return InvalidTemporalValue(f"Text cannot be parsed to a {_TYPE_NAMES[kind]}: {text!r}")


_TYPE_NAMES = {
    "date": "Date",
    "localtime": "LocalTime",
    "time": "Time",
    "localdatetime": "LocalDateTime",
    "datetime": "DateTime",
    "duration": "Duration",
}


def parse(kind: str, text: str) -> Temporal:
    """Read the string form of a temporal value of ``kind``."""
    if kind == "date":
        g = _groups(_RE_DATE, text)
        if g is None:
            raise _bad(kind, text)
        return Date(_date_from_groups(g, kind, text))
    if kind in ("localtime", "time"):
        g = _groups(_RE_TIME, text)
        if g is None or "zone" in g or (kind == "localtime" and "off" in g):
            raise _bad(kind, text)
        nanos = _time_from_groups(g, kind, text)
        if kind == "localtime":
            return make_local_time(nanos)
        offset = 0
        if "off" in g:
            offset = _offset_or_bad(g["off"], kind, text)
        return make_time(nanos, offset)
    if kind in ("localdatetime", "datetime"):
        g = _groups(_RE_DATETIME, text)
        if g is None or (kind == "localdatetime" and ("off" in g or "zone" in g)):
            raise _bad(kind, text)
        epoch_day = _date_from_groups(g, kind, text)
        nanos = _time_from_groups(g, kind, text) if ("lh" in g or "sh" in g) else 0
        if kind == "localdatetime":
            return make_local_datetime(epoch_day, nanos)
        return _zoned_from_string(epoch_day, nanos, g.get("off"), g.get("zone"), text)
    if kind == "duration":
        return parse_duration(text)
    raise InvalidTemporalValue(f"unknown temporal type {kind!r}")


def _offset_or_bad(text: str, kind: str, source: str) -> int:
    if text in ("Z", "z"):
        return 0
    offset = parse_offset(text)
    if offset is None:
        raise _bad(kind, source)
    return offset


def _zoned_from_string(
    epoch_day: int, nanos: int, off: str | None, zone_id: str | None, text: str
) -> DateTime:
    """``<local><offset>[<zone>]``: the offset chooses between the two
    readings of a wall clock in a DST overlap and must be valid for the zone
    at that instant, as in Neo4j."""
    if zone_id is not None:
        zone = parse_zone(zone_id)
        if off is None:
            return datetime_of_local(epoch_day, nanos, zone)
        offset = _offset_or_bad(off, "datetime", text)
        local = epoch_day * 86_400 + nanos // NANOS_PER_SECOND
        instant = local - offset
        if zone.offset_at(instant) != offset:
            raise InvalidTemporalValue(f"Timezone and offset do not match: {text!r}")
        return datetime_at_instant(instant, nanos % NANOS_PER_SECOND, zone)
    zone_rules: Zone = UTC_ZONE
    if off is not None:
        zone_rules = Zone(None, _offset_or_bad(off, "datetime", text))
    return datetime_of_local(epoch_day, nanos, zone_rules)


def _date_from_groups(g: dict[str, str], kind: str, text: str) -> int:
    year = int(g["year"])
    try:
        if "lw" in g or "sw" in g:
            week = int(g.get("lw") or g["sw"])
            dow = int(g.get("lwd") or g.get("swd") or 1)
            return week_date(year, week, dow)
        if "lq" in g or "sq" in g:
            quarter = int(g.get("lq") or g["sq"])
            day = int(g.get("lqd") or g.get("sqd") or 1)
            return quarter_date(year, quarter, day)
        if "lo" in g or "so" in g:
            return ordinal_date(year, int(g.get("lo") or g["so"]))
        month = int(g.get("lm") or g.get("sm") or 1)
        day = int(g.get("ld") or g.get("sd") or 1)
        return calendar_date(year, month, day)
    except TemporalOverflow:
        raise
    except TemporalError as exc:
        raise InvalidTemporalValue(
            f"Text cannot be parsed to a {_TYPE_NAMES[kind]}: {text!r}: {exc}"
        ) from exc


def _time_from_groups(g: dict[str, str], kind: str, text: str) -> int:
    hour = _int(g.get("lh") or g.get("sh"))
    minute = _int(g.get("lmi") or g.get("smi"))
    second = _int(g.get("ls") or g.get("ss"))
    fraction = g.get("lf") or g.get("sf") or ""
    if hour > 23 or minute > 59 or second > 59:
        raise _bad(kind, text)
    nanos = _fraction_nanos(fraction) if fraction else 0
    return (hour * 3600 + minute * 60 + second) * NANOS_PER_SECOND + nanos


# --- durations -----------------------------------------------------------------

AVG_DAYS_PER_MONTH = 365.2425 / 12


def approximate(months: float, days: float, seconds: float, nanos: float) -> Duration:
    """Cascade fractions down, as Neo4j does: months into days (the average
    Gregorian month), days into seconds, seconds into nanoseconds, each
    truncated toward zero."""
    m = float(int(months))
    days += AVG_DAYS_PER_MONTH * (months - m)
    d = float(int(days))
    seconds += 86_400 * (days - d)
    s = float(int(seconds))
    nanos += 1e9 * (seconds - s)
    return make_duration(int(m), int(d), int(s), int(nanos))


def parse_duration(text: str) -> Duration:
    g = _groups(_RE_DURATION_ALT, text)
    if g is not None:
        sign = -1 if g.get("sign") == "-" else 1
        months = _int(g.get("y")) * 12 + _int(g.get("mo"))
        seconds = _int(g.get("h")) * 3600 + _int(g.get("mi")) * 60 + _int(g.get("s"))
        nanos = _fraction_nanos(g["f"]) if "f" in g else 0
        return make_duration(sign * months, sign * _int(g.get("d")), sign * seconds, sign * nanos)
    g = _groups(_RE_DURATION, text)
    if g is None:
        raise _bad("duration", text)
    t_part = g.get("t")
    if not any(k in g for k in ("y", "mo", "w", "d")) and t_part is None:
        raise _bad("duration", text)
    if t_part is not None and t_part.upper() == "T":
        raise _bad("duration", text)
    for key in ("y", "mo", "w", "d", "h", "mi", "s"):
        whole = re.split(r"[.,]", g.get(key, "").lstrip("+-"))[0]
        if len(whole) > _MAX_DURATION_DIGITS:
            raise TemporalOverflow(f"duration {text!r} is out of range")

    sign = -1 if g.get("sign") == "-" else 1

    def frac(key: str) -> bool:
        return key in g and ("." in g[key] or "," in g[key])

    def flt(key: str) -> float:
        return float(g[key].replace(",", ".", 1)) if key in g else 0.0

    def num(key: str) -> int:
        return int(g[key]) if key in g else 0

    def later(*keys: str) -> bool:
        return any(k in g for k in keys)

    # A fractional component must be the last one given.
    if frac("y"):
        if later("mo", "w", "d", "h", "mi", "s"):
            raise _bad("duration", text)
        return approximate(sign * flt("y") * 12, 0, 0, 0)
    if frac("mo"):
        if later("w", "d", "h", "mi", "s"):
            raise _bad("duration", text)
        return approximate(sign * (float(num("y") * 12) + flt("mo")), 0, 0, 0)
    months = num("y") * 12 + num("mo")
    if frac("w"):
        if later("d", "h", "mi", "s"):
            raise _bad("duration", text)
        return approximate(sign * float(months), sign * flt("w") * 7, 0, 0)
    if frac("d"):
        if later("h", "mi", "s"):
            raise _bad("duration", text)
        return approximate(sign * float(months), sign * (float(num("w") * 7) + flt("d")), 0, 0)
    days = num("w") * 7 + num("d")
    if frac("h"):
        if later("mi", "s"):
            raise _bad("duration", text)
        return approximate(sign * float(months), sign * float(days), sign * flt("h") * 3600, 0)
    if frac("mi"):
        if later("s"):
            raise _bad("duration", text)
        return approximate(
            sign * float(months), sign * float(days), sign * (float(num("h") * 3600) + flt("mi") * 60), 0
        )
    second_text, nano_text = g.get("s", ""), ""
    split = re.search(r"[.,]", second_text)
    if split is not None:
        second_text, nano_text = second_text[: split.start()], second_text[split.end() :]
    seconds = num("h") * 3600 + num("mi") * 60 + _int(second_text if second_text not in ("+", "-") else "")
    nanos = 0
    if nano_text:
        nanos = _fraction_nanos(nano_text)
        if second_text.startswith("-"):
            nanos = -nanos
    return make_duration(sign * months, sign * days, sign * seconds, sign * nanos)
