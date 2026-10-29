"""Fail closed on temporal accessors the transpiler cannot lower yet (PRD §8.4).

In Cypher, ``n.creationDate.year`` is the year component of a temporal value.
Copied to AQL as attribute access, it reads ``.year`` of the stored number or
string and yields ``null`` — AQL that runs, looks plausible, and returns wrong
rows. Until temporal values are lowered (docs/ldbc_benchmark_plan.md WP-41),
translation refuses an accessor on a property the mapping declares temporal.

The check needs both signals — a property declared ``date``/``datetime``/…
*and* a Cypher temporal accessor name after it — so a nested map property
(``n.address.zip``) is never affected, and a mapping without temporal
declarations translates exactly as before.
"""

from __future__ import annotations

import re

from arango_query_core import CoreError, MappingResolver

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


def reject_temporal_accessor(keys: list[str], resolver: MappingResolver | None) -> None:
    """Raise ``UNSUPPORTED`` if *keys* (a property lookup chain) applies a
    temporal accessor to a property the mapping declares temporal."""
    if resolver is None or len(keys) < 2:
        return
    temporal = temporal_properties(resolver)
    if not temporal:
        return
    for prop, sub in zip(keys, keys[1:], strict=False):
        declared = temporal.get(prop)
        if declared is not None and sub in TEMPORAL_ACCESSORS:
            raise CoreError(
                f"Temporal accessor '.{sub}' on property '{prop}' is not supported yet: the mapping "
                f"declares '{prop}' as {declared}, and copying the accessor to AQL would read an "
                f"attribute of the stored value and return null. Temporal functions are planned "
                f"(docs/ldbc_benchmark_plan.md WP-41).",
                code="UNSUPPORTED",
            )


_PROPERTY_ACCESS = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")


def reject_temporal_conversion(function: str, compiled_arg: str, resolver: MappingResolver | None) -> None:
    """Raise ``UNSUPPORTED`` for ``date(n.p)`` / ``localdatetime(n.p)`` over a
    property the mapping declares temporal.

    ``date(x)`` lowers to ``DATE_ISO8601(x)``, which keeps the time of day, so
    ``date(a.creationDate) = date($d)`` compares whole timestamps and matches
    almost nothing (LDBC BI16). Converting a literal or a parameter is
    unaffected.
    """
    if resolver is None:
        return
    temporal = temporal_properties(resolver)
    for prop in _PROPERTY_ACCESS.findall(compiled_arg):
        declared = temporal.get(prop)
        if declared is not None:
            raise CoreError(
                f"{function}() of property '{prop}' is not supported yet: the mapping declares "
                f"'{prop}' as {declared}, and its stored encoding is not known, so the converted "
                f"value would not truncate or compare as Cypher's does. Temporal functions are "
                f"planned (docs/ldbc_benchmark_plan.md WP-41).",
                code="UNSUPPORTED",
            )
