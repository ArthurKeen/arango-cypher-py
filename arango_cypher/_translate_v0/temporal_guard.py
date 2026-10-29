"""The temporal vocabulary: mapping types, accessor names, declared properties.

``n.creationDate.year`` is the year of a temporal value; copied to AQL as
attribute access it would read ``.year`` of the stored number or string and
yield ``null``. ``temporal.py`` lowers it when the mapping declares the
property temporal (``TEMPORAL_TYPES``), and refuses an accessor it cannot
lower; a nested map property (``n.address.zip``) is never affected.
"""

from __future__ import annotations

from arango_query_core import MappingResolver

#: Mapping property types that hold Cypher temporal values.
TEMPORAL_TYPES = frozenset({"date", "datetime", "localdatetime", "time", "localtime", "duration"})

#: Component accessors of Cypher temporal instants and durations.
TEMPORAL_ACCESSORS = frozenset(
    {
        # instants
        "year",
        "quarter",
        "month",
        "week",
        "weekYear",
        "dayOfQuarter",
        "quarterDay",
        "day",
        "ordinalDay",
        "dayOfWeek",
        "weekDay",
        "hour",
        "minute",
        "second",
        "millisecond",
        "microsecond",
        "nanosecond",
        "timezone",
        "offset",
        "offsetMinutes",
        "offsetSeconds",
        "epochMillis",
        "epochSeconds",
        # durations
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
    }
)


def temporal_properties(resolver: MappingResolver) -> dict[str, str]:
    """Property name → declared temporal type, over every entity and relationship."""
    out: dict[str, str] = {}
    for name in [*resolver.all_entity_labels(), *resolver.all_relationship_types()]:
        for prop, info in resolver.resolve_properties(name).items():
            declared = str(info.type or "").lower()
            if declared in TEMPORAL_TYPES:
                out.setdefault(prop, declared)
    return out
