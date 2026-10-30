"""Duration arithmetic and temporal comparisons on ArangoDB (LDBC plan WP-41).

``person.creationDate`` is stored as epoch milliseconds, and
``post.creationDate`` and ``comment.creationDate`` as ISO-8601 strings; the
mapping declares no encoding, or (``_epoch_mapping``) ``epoch_ms`` for every
``creationDate``, for the queries that read only people. ``$date + duration({days: 100})`` is an ISO
string in UTC; a comparison with either stored form is made in epoch
milliseconds, and keeps an index on the ``epoch_ms`` property usable.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle, MappingResolver

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_temporal_arithmetic_it"


def _mapping() -> MappingBundle:
    return mapping_bundle_for("ldbc_pg")


def _epoch_mapping() -> MappingBundle:
    """Every ``creationDate`` declared ``epoch_ms``: an encoding is used only
    when every declaration of the property agrees on it."""
    bundle = mapping_bundle_for("ldbc_pg")
    physical = copy.deepcopy(bundle.physical_mapping)
    for section in ("entities", "relationships"):
        for spec in physical[section].values():
            prop = (spec.get("properties") or {}).get("creationDate")
            if isinstance(prop, dict):
                prop["encoding"] = "epoch_ms"
    return dataclasses.replace(bundle, physical_mapping=physical)


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
        # A traversal's WITH clause names every collection of the mapping.
        resolver = MappingResolver(_mapping())
        for label in resolver.all_entity_labels():
            name = resolver.resolve_entity(label).get("collectionName")
            if (
                name
                and not database.has_collection(name)
                and name not in ("person", "post", "comment", "tag")
            ):
                database.create_collection(name)
        for edge in resolver.all_edge_collections():
            if not database.has_collection(edge) and edge != "has_tag":
                database.create_collection(edge, edge=True)
        # 2012-06-01T00:00:00Z = 1338508800000; 100 days later is 2012-09-09.
        person = database.create_collection("person")
        person.import_bulk(
            [
                {"_key": "1", "id": 1, "creationDate": 1338508800000},  # 2012-06-01
                {"_key": "2", "id": 2, "creationDate": 1347148800000},  # 2012-09-09T00:00Z, the bound
                {"_key": "3", "id": 3, "creationDate": 1347148799999},  # one ms before it
            ]
        )
        person.add_index(
            {"type": "persistent", "fields": ["creationDate"], "name": "idx_person_creation_date"}
        )
        database.create_collection("post").import_bulk(
            [
                {"_key": "10", "id": 10, "creationDate": "2012-06-01T00:00:00.000+00:00"},
                {"_key": "11", "id": 11, "creationDate": "2012-09-08T23:59:59.999+00:00"},
                {"_key": "12", "id": 12, "creationDate": "2012-09-09T00:00:00.000+00:00"},
                {"_key": "13", "id": 13, "creationDate": "2012-12-18T00:00:00.000+00:00"},
            ]
        )
        database.create_collection("comment").import_bulk(
            [{"_key": "20", "id": 20, "creationDate": "2012-06-01T05:00:00.000+00:00"}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(
    db: Any, cypher: str, params: dict[str, Any] | None = None, mapping: MappingBundle | None = None
) -> list[Any]:
    q = translate(cypher, mapping=mapping or _mapping(), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


_JUNE = {"date": "2012-06-01T00:00:00.000+00:00"}


def test_an_iso_property_in_a_window_after_a_parameter(db: Any) -> None:
    # LDBC BI2: messages of the 100 days from $date, then of the 100 after.
    first = (
        "MATCH (m:Message) WHERE $date <= m.creationDate AND m.creationDate < $date + duration({days: 100}) "
        "RETURN m.id AS id ORDER BY id"
    )
    second = (
        "MATCH (m:Message) WHERE $date + duration({days: 100}) <= m.creationDate "
        "AND m.creationDate < $date + duration({days: 200}) RETURN m.id AS id ORDER BY id"
    )
    assert _run(db, first, _JUNE) == [{"id": 10}, {"id": 11}, {"id": 20}]
    assert _run(db, second, _JUNE) == [{"id": 12}]


def test_an_epoch_property_compared_with_a_computed_datetime_uses_its_index(db: Any) -> None:
    cypher = (
        "MATCH (p:Person) WHERE p.creationDate < $date + duration({days: 100}) RETURN p.id AS id ORDER BY id"
    )
    assert _run(db, cypher, _JUNE, _epoch_mapping()) == [{"id": 1}, {"id": 3}]
    # With no encoding declared, both sides are converted: same rows, no index.
    assert _run(db, cypher, _JUNE) == [{"id": 1}, {"id": 3}]
    q = translate(cypher, mapping=_epoch_mapping(), params=_JUNE)
    assert "p.creationDate < DATE_TIMESTAMP(" in q.aql
    plan = db.aql.explain(q.aql, bind_vars=q.bind_vars)
    assert any(n["type"] == "IndexNode" for n in plan["nodes"]), plan["nodes"]


def test_a_property_plus_a_duration_from_a_parameter(db: Any) -> None:
    # LDBC BI17: message2.creationDate > message1.creationDate + duration({hours: $delta})
    rows = _run(
        db,
        "MATCH (a:Post), (b:Message) WHERE b.creationDate > a.creationDate + duration({hours: $delta}) "
        "AND a.id = 10 RETURN b.id AS id ORDER BY id",
        {"delta": 4},
    )
    assert rows == [{"id": 11}, {"id": 12}, {"id": 13}, {"id": 20}]


_CLAMP = (
    "RETURN datetime('2012-01-31T10:00:00Z') + duration({months: 1}) AS feb, "
    "datetime('2013-03-31T10:00:00Z') - duration({months: 1, hours: 2}) AS back"
)


@pytest.mark.usefixtures("without_constant_folding")
def test_months_clamp_to_the_end_of_the_month(db: Any) -> None:
    # The run-time lowering (data-dependent operands take it); literals here.
    rows = _run(db, _CLAMP)
    assert rows == [{"feb": "2012-02-29T10:00:00.000Z", "back": "2013-02-28T08:00:00.000Z"}]


def test_months_clamp_when_folded(db: Any) -> None:
    # Constant operands fold at translate time and print as Neo4j does.
    rows = _run(db, _CLAMP)
    assert rows == [{"feb": "2012-02-29T10:00Z", "back": "2013-02-28T08:00Z"}]


def test_a_constant_in_a_named_zone_filters_stored_values(db: Any) -> None:
    # Folded, the bound prints as '2012-09-09T02:00+02:00[Europe/Berlin]';
    # AQL's DATE_TIMESTAMP reads the [zone] suffix as invalid (null), which
    # would silently match nothing. 02:00 in Berlin's summer time is 00:00Z.
    rows = _run(
        db,
        "MATCH (p:Post) WHERE p.creationDate >= "
        "datetime({year: 2012, month: 9, day: 9, hour: 2, timezone: 'Europe/Berlin'}) "
        "RETURN p.id AS id ORDER BY id",
    )
    assert rows == [{"id": 12}, {"id": 13}]


def test_a_null_duration_field_makes_the_sum_null(db: Any) -> None:
    rows = _run(db, "RETURN datetime('2012-01-01T00:00:00Z') + duration({hours: $h}) AS t", {"h": None})
    assert rows == [{"t": None}]


def test_an_optional_match_is_read_from_its_bound_end(db: Any) -> None:
    db.create_collection("tag").import_bulk(
        [{"_key": "t", "id": 1, "name": "T"}, {"_key": "u", "id": 2, "name": "U"}]
    )
    db.create_collection("has_tag", edge=True).import_bulk(
        [
            {"_from": "post/10", "_to": "tag/t"},
            {"_from": "post/11", "_to": "tag/t"},
            {"_from": "comment/20", "_to": "tag/u"},
        ]
    )
    cypher = (
        "MATCH (tag:Tag) OPTIONAL MATCH (m:Message)-[:HAS_TAG]->(tag) "
        "RETURN tag.name AS tag, count(m) AS n ORDER BY tag"
    )
    assert _run(db, cypher) == [{"tag": "T", "n": 2}, {"tag": "U", "n": 1}]
    aql = translate(cypher, mapping=_mapping()).aql
    assert "INBOUND tag" in aql, aql
