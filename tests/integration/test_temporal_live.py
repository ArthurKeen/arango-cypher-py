"""Temporal values, functions and accessors on ArangoDB (LDBC plan WP-41).

``creationDate`` is stored as ISO-8601 strings and ``birthday`` as epoch
milliseconds, the two encodings LDBC ships; the lowering reads both.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_temporal_it"

# 1989-12-23T10:00:00Z, 1990-01-21T23:30:00Z, 1991-12-02T00:00:00Z
_BIRTHDAYS = [630410400000, 632964600000, 691632000000]


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("person").import_bulk(
            [
                {
                    "_key": "1",
                    "id": 1,
                    "creationDate": "2010-01-03T15:10:31.499+00:00",
                    "birthday": _BIRTHDAYS[0],
                },
                {
                    "_key": "2",
                    "id": 2,
                    "creationDate": "2010-01-03T23:59:59.999+00:00",
                    "birthday": _BIRTHDAYS[1],
                },
                {
                    "_key": "3",
                    "id": 3,
                    "creationDate": "2012-02-29T00:00:00.000+00:00",
                    "birthday": _BIRTHDAYS[2],
                },
                {"_key": "4", "id": 4},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("ldbc_pg"), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_accessors_read_iso_and_epoch_values(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (p:Person) RETURN p.id AS id, p.creationDate.year AS y, p.creationDate.month AS m, "
        "p.creationDate.day AS d, p.birthday.year AS byear, p.birthday.month AS bm ORDER BY id",
    )
    assert rows == [
        {"id": 1, "y": 2010, "m": 1, "d": 3, "byear": 1989, "bm": 12},
        {"id": 2, "y": 2010, "m": 1, "d": 3, "byear": 1990, "bm": 1},
        {"id": 3, "y": 2012, "m": 2, "d": 29, "byear": 1991, "bm": 12},
        {"id": 4, "y": None, "m": None, "d": None, "byear": None, "bm": None},
    ]


def test_finer_accessors(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (p:Person {id: 1}) RETURN p.creationDate.hour AS h, p.creationDate.minute AS mi, "
        "p.creationDate.second AS s, p.creationDate.millisecond AS ms, p.creationDate.dayOfWeek AS dow, "
        "p.creationDate.ordinalDay AS od, p.creationDate.quarter AS q, p.creationDate.epochMillis AS e",
    )
    # 2010-01-03 is a Sunday: Cypher's dayOfWeek 7.
    assert rows == [{"h": 15, "mi": 10, "s": 31, "ms": 499, "dow": 7, "od": 3, "q": 1, "e": 1262531431499}]


def test_date_compares_days_whatever_the_time(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (p:Person) WHERE date(p.creationDate) = date($d) RETURN p.id AS id ORDER BY id",
        {"d": "2010-01-03T00:00:00.000+00:00"},
    )
    assert rows == [{"id": 1}, {"id": 2}]


def test_a_with_variable_bound_to_a_datetime_has_accessors(db: Any) -> None:
    # LDBC IC10: birthdays from the 21st of a month to the 21st of the next.
    rows = _run(
        db,
        "MATCH (p:Person) WITH p, datetime({epochMillis: p.birthday}) AS birthday "
        "WHERE (birthday.month = $month AND birthday.day >= 21) "
        "OR (birthday.month = ($month % 12) + 1 AND birthday.day < 22) "
        "RETURN p.id AS id ORDER BY id",
        {"month": 12},
    )
    assert rows == [{"id": 1}, {"id": 2}]


def test_an_accessor_on_a_parameter_reads_a_datetime_or_a_map(db: Any) -> None:
    # LDBC BI13 reads ``$endDate.year`` of a datetime parameter.
    cypher = "MATCH (p:Person {id: 3}) RETURN 12 * ($endDate.year - p.creationDate.year) + ($endDate.month - p.creationDate.month) AS months"
    assert _run(db, cypher, {"endDate": "2013-01-01T00:00:00.000+00:00"}) == [{"months": 11}]
    assert _run(db, cypher, {"endDate": {"year": 2013, "month": 1}}) == [{"months": 11}]


def test_temporal_constructors(db: Any) -> None:
    rows = _run(
        db,
        "RETURN date({year: 1984, week: 10, dayOfWeek: 3}) AS w, date({year: 1984, ordinalDay: 202}) AS o, "
        "datetime({year: 1984, month: 10, day: 11, hour: 12, minute: 31, second: 14, millisecond: 645}) AS dt, "
        "datetime({epochMillis: 1262531431499}) AS e, date(null) AS n",
    )
    assert rows == [
        {
            "w": "1984-03-07",
            "o": "1984-07-20",
            "dt": "1984-10-11T12:31:14.645Z",
            "e": "2010-01-03T15:10:31.499Z",
            "n": None,
        }
    ]
