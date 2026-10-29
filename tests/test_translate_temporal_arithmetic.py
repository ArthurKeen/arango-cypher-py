"""Durations, temporal arithmetic and comparisons (LDBC plan WP-41, second half).

The behaviour tests run on ArangoDB in
``tests/integration/test_temporal_arithmetic_live.py``.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, mapping=None) -> str:
    return translate(cypher, mapping=mapping or mapping_bundle_for("ldbc_pg")).aql


def _with_encoding(encoding: str):
    bundle = mapping_bundle_for("ldbc_pg")
    physical = copy.deepcopy(bundle.physical_mapping)
    for section in ("entities", "relationships"):
        for spec in physical[section].values():
            for prop in (spec.get("properties") or {}).values():
                if isinstance(prop, dict) and prop.get("type") in ("datetime", "date"):
                    prop["encoding"] = encoding
    return dataclasses.replace(bundle, physical_mapping=physical)


def test_a_duration_is_an_iso_duration() -> None:
    assert 'CONCAT("P", ((1) * 12 + (2)), "M")' in _aql("RETURN duration({years: 1, months: 2}) AS d")
    assert '"T", (((36) * 3600000) / 1000), "S"' in _aql("RETURN duration({hours: 36}) AS d")


def test_adding_a_duration_adds_months_then_milliseconds() -> None:
    aql = _aql("RETURN datetime('2012-01-31') + duration({months: 1, days: 2}) AS t")
    assert "DATE_DAYS_IN_MONTH(" in aql  # the day is clamped to the target month
    assert "DATE_ADD(DATE_ISO8601(DATE_YEAR(" in aql
    assert ', ((2) * 86400000), "millisecond")' in aql


def test_subtracting_and_a_duration_first() -> None:
    assert ', -((3) * 3600000), "millisecond")' in _aql("RETURN datetime() - duration({hours: 3}) AS t")
    assert 'DATE_ADD(DATE_ISO8601(DATE_NOW()), ((1) * 86400000), "millisecond")' in _aql(
        "RETURN duration({days: 1}) + datetime() AS t"
    )


def test_a_null_field_or_value_makes_the_sum_null() -> None:
    aql = _aql("MATCH (m:Post) RETURN m.creationDate + duration({hours: $h}) AS t")
    assert "(m.creationDate == null OR @h == null) ? null :" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "WITH duration({days: 1}) AS d RETURN datetime() + d AS t",
        "RETURN datetime() + duration('P1D') AS t",
        "RETURN duration({days: 1}) + duration({days: 2}) AS t",
        "RETURN datetime() - datetime() AS t",
    ],
)
def test_unsupported_duration_forms_fail_closed(cypher: str) -> None:
    with pytest.raises(CoreError) as err:
        _aql(cypher)
    assert err.value.code in ("NOT_IMPLEMENTED", "UNSUPPORTED")


def test_unknown_duration_fields_are_refused() -> None:
    with pytest.raises(CoreError, match="fortnights") as err:
        _aql("RETURN duration({fortnights: 1}) AS d")
    assert err.value.code == "UNSUPPORTED"


_WINDOW = "MATCH (m:Post) WHERE m.creationDate < $date + duration({days: 100}) RETURN m.id"


def test_a_computed_datetime_and_a_stored_one_compare_in_epoch_milliseconds() -> None:
    aql = _aql(_WINDOW)
    assert "DATE_TIMESTAMP(m.creationDate) < DATE_TIMESTAMP(" in aql


def test_an_epoch_ms_property_keeps_its_plain_index_friendly_form() -> None:
    aql = _aql(_WINDOW, _with_encoding("epoch_ms"))
    assert "m.creationDate < DATE_TIMESTAMP(" in aql
    assert "DATE_TIMESTAMP(m.creationDate)" not in aql
    assert "DATE_TIMESTAMP(m.creationDate) < DATE_TIMESTAMP(" in _aql(_WINDOW, _with_encoding("iso8601"))


def test_comparisons_without_a_computed_temporal_are_unchanged() -> None:
    aql = _aql("MATCH (m:Post) WHERE $date <= m.creationDate RETURN m.id")
    assert "@date <= m.creationDate" in aql
    assert "DATE_TIMESTAMP" not in aql
