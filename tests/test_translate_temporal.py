"""Temporal values, functions and accessors (LDBC plan WP-41, PRD §8.2).

``p.creationDate.year`` used to be copied to AQL as attribute access (``null``
on any stored timestamp), then refused; it is now ``DATE_YEAR(…)``. The
behaviour tests run on ArangoDB in ``tests/integration/test_temporal_live.py``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.temporal import INSTANT_ACCESSORS
from arango_cypher._translate_v0.temporal_guard import TEMPORAL_ACCESSORS, temporal_properties
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(params=["ldbc_pg", "ldbc_lpg"])
def ldbc(request):
    return mapping_bundle_for(request.param)


def _aql(cypher: str, mapping=None) -> str:
    return translate(cypher, mapping=mapping or mapping_bundle_for("ldbc_pg")).aql


@pytest.mark.parametrize(
    ("cypher", "lowered"),
    [
        ("MATCH (p:Person) RETURN p.creationDate.year AS y", "DATE_YEAR(p.creationDate)"),
        ("MATCH (p:Person) WHERE p.birthday.month = 5 RETURN p.id", "DATE_MONTH(p.birthday)"),
        ("MATCH (p:Person {id: 1}) RETURN p.creationDate.epochMillis AS e", "DATE_TIMESTAMP(p.creationDate)"),
        ("MATCH (a:Person)-[k:KNOWS]->(b:Person) RETURN k.creationDate.day AS d", "DATE_DAY(k.creationDate)"),
        ("MATCH (p:Post) WITH p ORDER BY p.creationDate.year RETURN p.id", "DATE_YEAR(p.creationDate)"),
    ],
)
def test_an_accessor_on_a_temporal_property_is_lowered(ldbc, cypher: str, lowered: str) -> None:
    aql = _aql(cypher, ldbc)
    assert lowered in aql
    # Null in, null out (AQL would return null with a warning).
    assert "== null) ? null :" in aql


def test_an_accessor_on_a_temporal_function_or_with_variable_is_lowered() -> None:
    assert "DATE_MONTH(DATE_ISO8601(" in _aql("RETURN datetime('2020-05-01').month AS m")
    aql = _aql(
        "MATCH (p:Person) WITH p, datetime({epochMillis: p.birthday}) AS b WHERE b.day >= 21 RETURN b.month AS m"
    )
    assert "DATE_DAY(b)" in aql
    assert "DATE_MONTH(b)" in aql


def test_an_accessor_on_a_parameter_is_decided_at_run_time() -> None:
    aql = _aql("RETURN $d.year AS y")
    assert "IS_OBJECT(@d) ? @d.year : " in aql
    assert "DATE_YEAR(@d)" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) RETURN p.creationDate AS c",
        "MATCH (p:Person) WHERE p.creationDate < $d RETURN p.id",
        # A sub-key that is not a temporal accessor is left alone.
        "MATCH (p:Person) RETURN p.creationDate.someField AS x",
        # A property the mapping does not declare.
        "MATCH (p:Person) RETURN p.address.year AS x",
    ],
)
def test_other_lookups_are_attribute_access(ldbc, cypher: str) -> None:
    assert "DATE_" not in translate(cypher, mapping=ldbc, params={"d": 0}).aql


@pytest.mark.parametrize(
    ("mapping", "cypher", "message"),
    [
        (
            "ldbc_pg",
            "MATCH (p:Person) RETURN p.firstName.year AS x",
            "'firstName', which the mapping types as string",
        ),
        (
            "movies_pg",
            "MATCH (m:Movie) RETURN m.released.year AS y",
            "'released', which the mapping types as number",
        ),
    ],
)
def test_an_accessor_on_a_property_typed_as_a_scalar_is_refused(
    mapping: str, cypher: str, message: str
) -> None:
    # A string or number has no ``.year``: an epoch-ms timestamp typed as a
    # number would otherwise read a missing attribute, null on every row.
    with pytest.raises(CoreError, match=message) as err:
        translate(cypher, mapping=mapping_bundle_for(mapping))
    assert err.value.code == "UNSUPPORTED"


def test_a_property_named_like_an_accessor_is_still_an_attribute() -> None:
    # ``m.year`` of a movie, and ``.year`` of a map property the mapping does not declare.
    assert "m.year" in _aql("MATCH (m:Movie) RETURN m.year AS y", mapping_bundle_for("movies_pg"))
    assert "m.meta.year" in _aql("MATCH (m:Movie) RETURN m.meta.year AS y", mapping_bundle_for("movies_pg"))


def test_an_unsupported_accessor_is_refused() -> None:
    with pytest.raises(CoreError, match="Temporal accessor '.timezone'") as err:
        _aql("MATCH (p:Person) RETURN p.creationDate.timezone AS z")
    assert err.value.code == "UNSUPPORTED"


@pytest.mark.parametrize(
    ("cypher", "aql"),
    [
        ("RETURN datetime('2020-01-02T03:04:05Z') AS d", "DATE_ISO8601('2020-01-02T03:04:05Z')"),
        ("RETURN datetime({epochMillis: $e}) AS d", "DATE_ISO8601(@e)"),
        ("RETURN datetime({epochSeconds: 10}) AS d", "DATE_ISO8601(10 * 1000)"),
        (
            "RETURN localdatetime({year: 2020, month: 2, hour: 7}) AS d",
            "DATE_ISO8601(2020, 2, 1, 7, 0, 0, 0)",
        ),
        ("RETURN date('2020-01-02') AS d", "LEFT(DATE_ISO8601('2020-01-02'), 10)"),
        ("RETURN date($d) AS d", "DATE_ISO8601(@d)"),
        ("RETURN datetime() AS d", "DATE_ISO8601(DATE_NOW())"),
    ],
)
def test_temporal_functions(cypher: str, aql: str) -> None:
    assert aql in _aql(cypher)


def test_date_of_a_temporal_property_truncates_to_the_day() -> None:
    aql = _aql("MATCH (m:Post) WHERE date(m.creationDate) = date($d) RETURN m.id")
    assert "LEFT(DATE_ISO8601(m.creationDate), 10)" in aql
    assert "LEFT(DATE_ISO8601(@d), 10)" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "RETURN date({date: date('2020-01-01'), day: 3}) AS d",
        "RETURN datetime({year: 2020, timezone: 'Europe/Paris'}) AS d",
        "RETURN date({month: 2}) AS d",
        "RETURN date({year: 2020, month: 1, hour: 3}) AS d",
        "RETURN date('a', 'b') AS d",
    ],
)
def test_unsupported_constructors_are_refused(cypher: str) -> None:
    with pytest.raises(CoreError) as err:
        _aql(cypher)
    assert err.value.code == "UNSUPPORTED"


def test_temporal_properties_collects_entities_and_relationships():
    temporal = temporal_properties(MappingResolver(mapping_bundle_for("ldbc_pg")))
    assert temporal["creationDate"] == "datetime"
    assert temporal["birthday"] == "date"
    assert "firstName" not in temporal


def test_every_lowered_accessor_is_a_cypher_accessor():
    assert {"year", "month", "day", "epochMillis"} <= set(INSTANT_ACCESSORS) <= TEMPORAL_ACCESSORS
