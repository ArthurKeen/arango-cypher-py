"""Time zones: fixed UTC offsets and IANA zones, resolved as java.time does.

Named zones come from ``zoneinfo.zip`` in this package, built from the
official IANA tz source *without* backzone (``scripts/build_tzdata.sh``; the
version is in ``zoneinfo.version``). That is the data Java, and so Neo4j, uses.
The host's tz database is not consulted: most Linux distributions build it
with backzone, which gives e.g. Europe/Stockholm in 1818 an offset of
+01:12:12 instead of Neo4j's +00:53:28, and results must not depend on the
machine the translator runs on.

``zoneinfo`` only resolves instants inside Python's ``datetime`` range (years
1 to 9999). Earlier instants take the zone's earliest offset (its local mean
time); later ones are moved back by whole 400-year Gregorian cycles, over
which the zone's final, rule-based offsets repeat exactly.
"""

from __future__ import annotations

import io
import threading
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

from .errors import InvalidTemporalValue

_ZONEINFO_ARCHIVE = "zoneinfo.zip"
_MAX_OFFSET = 18 * 3600

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Instants zoneinfo can resolve: a day inside each end of datetime's range,
# so the local time of the instant stays in range for every offset (±18h).
_MIN_INSTANT = int((datetime(1, 1, 2, tzinfo=UTC) - _EPOCH).total_seconds())
_MAX_INSTANT = int((datetime(9999, 12, 30, tzinfo=UTC) - _EPOCH).total_seconds())
_CYCLE_SECONDS = 146_097 * 86_400  # 400 Gregorian years


@dataclass(frozen=True, slots=True)
class Zone:
    """A resolved time zone: a fixed offset (``name`` is None) or an IANA zone."""

    name: str | None
    fixed_offset: int = 0

    def offset_at(self, instant: int) -> int:
        """UTC offset in seconds at an instant (epoch seconds)."""
        if self.name is None:
            return self.fixed_offset
        return _named_offset(self.name, instant)

    def resolve_local(self, local: int, preferred: int | None = None) -> int:
        """The instant (epoch seconds) a wall clock names in this zone, as
        java.time ``ZonedDateTime.ofLocal`` resolves it: the preferred offset
        when it is valid for the wall clock, else the earlier offset in an
        overlap; in a gap the instant of the wall clock read with the offset
        before the gap, which moves the wall clock forward by the gap's length.

        ``local`` is the wall clock as epoch seconds (as if it were UTC).
        """
        if self.name is None:
            return local - self.fixed_offset
        # Transitions are more than a day apart, so the offsets a day either
        # side are the two candidates.
        before = self.offset_at(local - 86_400)
        after = self.offset_at(local + 86_400)

        def valid(offset: int) -> bool:
            return self.offset_at(local - offset) == offset

        if preferred is not None and valid(preferred):
            return local - preferred
        if valid(before):
            return local - before
        if valid(after):
            return local - after
        return local - before


UTC_ZONE = Zone(None, 0)


def fixed_zone(offset: int) -> Zone:
    return Zone(None, offset)


def parse_zone(text: str) -> Zone:
    """``Z``, ``±HH``, ``±HHMM``, ``±HH:MM``, ``±HH:MM:SS`` or an IANA zone name."""
    if text in ("Z", "z"):
        return UTC_ZONE
    if text[:1] in ("+", "-"):
        offset = parse_offset(text)
        if offset is None:
            raise InvalidTemporalValue(f"invalid time zone offset {text!r}")
        return Zone(None, offset)
    if not _zone_exists(text):
        raise InvalidTemporalValue(f"invalid time zone {text!r}: unknown zone id")
    return Zone(text)


def parse_offset(text: str) -> int | None:
    """Seconds east of UTC of ``±HH[[:]MM[[:]SS]]``, or None when invalid."""
    if not text or text[0] not in "+-":
        return None
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    if len(digits) not in (2, 4, 6) or not digits.isascii() or not digits.isdigit():
        return None
    parts = [int(digits[i : i + 2]) for i in range(0, len(digits), 2)]
    parts += [0] * (3 - len(parts))
    hours, minutes, seconds = parts
    if minutes > 59 or seconds > 59:
        return None
    total = hours * 3600 + minutes * 60 + seconds
    if total > _MAX_OFFSET:
        return None
    return sign * total


def format_offset(offset: int) -> str:
    """java.time ``ZoneOffset.toString``: ``Z``, ``+01:00``, ``-02:05:07``."""
    if offset == 0:
        return "Z"
    sign = "-" if offset < 0 else "+"
    a = abs(offset)
    text = f"{sign}{a // 3600:02d}:{a // 60 % 60:02d}"
    if a % 60:
        text += f":{a % 60:02d}"
    return text


# --- the embedded tz database ------------------------------------------------

_archive_lock = threading.Lock()
_archive: zipfile.ZipFile | None = None


def _open_archive() -> zipfile.ZipFile:
    global _archive
    with _archive_lock:
        if _archive is None:
            data = resources.files(__package__).joinpath(_ZONEINFO_ARCHIVE).read_bytes()
            try:
                _archive = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile as exc:
                raise RuntimeError(
                    f"arango_cypher.temporal: embedded {_ZONEINFO_ARCHIVE} is corrupt"
                ) from exc
        return _archive


@cache
def zone_names() -> frozenset[str]:
    """Every IANA zone id the embedded database knows."""
    return frozenset(_open_archive().namelist())


def _zone_exists(name: str) -> bool:
    return name in zone_names()


@cache
def _zoneinfo(name: str) -> ZoneInfo:
    archive = _open_archive()
    with _archive_lock:
        data = archive.read(name)
    return ZoneInfo.from_file(io.BytesIO(data), key=name)


def _named_offset(name: str, instant: int) -> int:
    if instant < _MIN_INSTANT:
        instant = _MIN_INSTANT
    elif instant > _MAX_INSTANT:
        cycles = -(-(instant - _MAX_INSTANT) // _CYCLE_SECONDS)
        instant -= cycles * _CYCLE_SECONDS
    moment = (_EPOCH + timedelta(seconds=instant)).astimezone(_zoneinfo(name))
    delta = moment.utcoffset()
    if delta is None:  # a ZoneInfo always has an offset; guard the Optional type
        raise RuntimeError(f"time zone {name!r} has no UTC offset at {instant}")
    return int(delta.total_seconds())
