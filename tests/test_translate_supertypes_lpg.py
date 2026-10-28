"""Supertypes on LPG: MULTI_LABEL entities (WP-35 stage 1; docs/ldbc_benchmark_plan.md D8).

LDBC's ``Message`` is every ``Post`` and every ``Comment``. On the LPG layout
each node's ``labels`` array lists its supertype, so ``(m:Message)`` becomes
``@labelValue IN m[@labelsField]``. Creating a member writes that array, and a
replacement keeps it, so supertype queries see new nodes too.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _t(cypher: str, **params):
    return translate(cypher, mapping=mapping_bundle_for("ldbc_lpg"), params=params or None)


def _multi_label(q, var: str) -> tuple[str, str]:
    m = re.search(rf"@(\w+) IN {var}\[@(\w+)\]", q.aql)
    assert m, q.aql
    return q.bind_vars[m.group(1)], q.bind_vars[m.group(2)]


def test_scan_start_filters_on_the_labels_array():
    q = _t("MATCH (m:Message {id: 1}) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_traversal_target_filters_on_the_labels_array():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m:Message) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_label_predicate_on_a_supertype():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m) RETURN m:Message AS isMessage")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_a_concrete_label_is_preferred_as_primary():
    q = _t("MATCH (m:Comment:Message {id: 1}) RETURN m.id")
    assert re.search(r"m\[@\w+\] == @\w+", q.aql), q.aql  # Comment's type-field filter
    assert "Message" not in q.aql and "Message" not in q.bind_vars.values()  # every Comment is a Message


def test_a_supertype_the_primary_does_not_imply_filters_on_its_labels_field():
    q = _t("MATCH (m:Person:Message {id: 1}) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_place_and_organisation_are_supertypes_too():
    assert _multi_label(_t("MATCH (x:Place {id: 1}) RETURN x.name"), "x") == ("Place", "labels")
    assert _multi_label(_t("MATCH (x:Organisation {id: 1}) RETURN x.name"), "x") == ("Organisation", "labels")


def test_create_writes_the_supertype_labels():
    q = _t("CREATE (p:Post {id: 9})")
    labels = [v for v in q.bind_vars.values() if isinstance(v, list)]
    assert ["Message", "Post"] in labels, q.bind_vars
    assert re.search(r"labels: @\w+", q.aql), q.aql


def test_create_by_a_supertype_is_refused():
    with pytest.raises(CoreError) as exc:
        _t("CREATE (m:Message {id: 9})")
    assert exc.value.code == "UNSUPPORTED"
    assert "supertype" in str(exc.value)


def test_merge_on_a_member_label_is_refused():
    with pytest.raises(CoreError) as exc:
        _t("MERGE (p:Post {id: 9})")
    assert exc.value.code == "UNSUPPORTED"


def test_a_label_without_supertypes_creates_as_before():
    q = _t("CREATE (t:Tag {id: 9})")
    assert not [v for v in q.bind_vars.values() if isinstance(v, list)], q.bind_vars


def test_delete_through_a_supertype_filters_on_the_labels_array():
    q = _t("MATCH (p:Person {id: 1})<-[r:HAS_CREATOR]-(m:Message {id: 2}) DELETE r")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_replace_keeps_the_labels_array():
    q = _t("MATCH (p:Post {id: 1}) SET p = {id: 1, content: 'x'}")
    assert '"labels"' in q.aql, q.aql
