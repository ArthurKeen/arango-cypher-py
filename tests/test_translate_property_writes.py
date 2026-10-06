"""SET and REMOVE as one write per variable, in the variable's own collection.

AQL refuses a second write to a collection in one query (ERR 1579), so a
statement's SET/REMOVE items are merged per variable; relationship and
end-node variables write their own collections; CREATE folds them into the
insert. The live counterpart is ``tests/integration/test_write_clauses_live.py``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.property_writes import PropertyOp, PropertyWrites, keep_fields_for
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, mapping: str = "pg", params: dict | None = None) -> str:
    return translate(cypher, mapping=mapping_bundle_for(mapping), params=params).aql


def _set(name: str, value: str) -> PropertyOp:
    return PropertyOp("set", name=name, key=name, value=value)


def _remove(name: str) -> PropertyOp:
    return PropertyOp("remove", name=name, key=f'"{name}"')


class TestStoredWrite:
    def test_property_changes_are_one_literal_and_later_wins(self) -> None:
        w = PropertyWrites()
        for op in (_set("a", "1"), _set("b", "2"), _set("a", "3")):
            w.add("n", op)
        out = w.stored_write("n", [])
        assert (out.operation, out.document, out.options) == ("UPDATE", "{b: 2, a: 3}", "")

    def test_a_removal_is_a_null_under_keep_null_false(self) -> None:
        w = PropertyWrites()
        w.add("n", _set("a", "1"))
        w.add("n", _remove("a"))
        out = w.stored_write("n", [])
        assert out.document == '{"a": null}' and out.options == "OPTIONS {keepNull: false}"

    def test_a_merge_map_keeps_its_place_in_the_order(self) -> None:
        w = PropertyWrites()
        w.add("n", _set("a", "1"))
        w.add("n", PropertyOp("merge", value="@props"))
        w.add("n", _set("b", "2"))
        assert w.stored_write("n", []).document == "MERGE(n, {a: 1}, @props, {b: 2})"

    def test_a_replacement_keeps_the_mapping_fields_and_applies_later_items(self) -> None:
        w = PropertyWrites()
        w.add("n", _set("dropped", "1"))
        w.add("n", PropertyOp("replace", value="{name: 'x'}"))
        w.add("n", _set("a", "2"))
        w.add("n", _remove("name"))
        out = w.stored_write("n", ['"type"'])
        assert out.operation == "REPLACE"
        assert out.document == 'MERGE(UNSET(MERGE({name: \'x\'}, {a: 2}), "name"), KEEP(n, "type"))'


class TestFoldedIntoInsert:
    def test_items_apply_to_the_inserted_document(self) -> None:
        w = PropertyWrites()
        w.add("n", _set("b", "2"))
        w.add("n", _remove("tmp"))
        assert w.folded_into("n", "{a: 1, tmp: 0}", []) == 'UNSET(MERGE({a: 1, tmp: 0}, {b: 2}), "tmp")'

    def test_a_value_reading_the_created_variable_sees_the_document_so_far(self) -> None:
        w = PropertyWrites()
        w.add("n", _set("a", "2"))
        w.add("n", _set("b", "n.a + 1"))
        assert (
            w.folded_into("n", "{}", [])
            == "FIRST(FOR _d0 IN [MERGE({}, {a: 2})] RETURN MERGE(_d0, {b: _d0.a + 1}))"
        )

    def test_a_replacement_keeps_the_server_fields(self) -> None:
        w = PropertyWrites()
        w.add("n", PropertyOp("replace", value="{x: 1}"))
        assert w.folded_into("n", "{type: @typeValue, a: 1}", ["type: @typeValue"]) == (
            "MERGE({x: 1}, {type: @typeValue})"
        )

    def test_other_variables_are_untouched(self) -> None:
        assert PropertyWrites().folded_into("n", "{a: 1}", []) == "{a: 1}"


class TestKeepFields:
    def test_a_vertex_collection_without_labels_keeps_nothing(self) -> None:
        assert keep_fields_for("users", mapping_bundle_for("pg").physical_mapping) == []

    def test_an_edge_collection_keeps_its_endpoints(self) -> None:
        assert keep_fields_for("follows", mapping_bundle_for("pg").physical_mapping) == ['"_from"', '"_to"']

    def test_label_style_collections_keep_the_type_field(self) -> None:
        physical = mapping_bundle_for("lpg").physical_mapping
        assert keep_fields_for("vertices", physical) == ['"type"']
        assert keep_fields_for("edges", physical) == ['"_from"', '"_to"', '"type"']


class TestTranslation:
    def test_several_removals_are_one_update(self) -> None:
        aql = _aql("MATCH (n:User) REMOVE n.x, n.y")
        assert aql.count("UPDATE") == 1
        assert 'UPDATE n WITH {"x": null, "y": null} IN @@collection OPTIONS {keepNull: false}' in aql

    def test_separate_set_clauses_are_one_update(self) -> None:
        aql = _aql("MATCH (n:User) SET n.a = 1 SET n.b = 2")
        assert aql.count("UPDATE") == 1 and "{a: 1, b: 2}" in aql

    def test_query_order_decides_between_remove_and_set(self) -> None:
        assert "{x: 7}" in _aql("MATCH (n:User) REMOVE n.x SET n.x = 7")
        assert '{"x": null}' in _aql("MATCH (n:User) SET n.x = 7 REMOVE n.x")

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (a:User)-[r:FOLLOWS]->(b:User) SET r.w = 1",
            "MATCH (a:User)-[r:FOLLOWS]->(b:User) REMOVE r.w",
            "MATCH (a:User)-[r:FOLLOWS]->(b:User) DELETE r",
        ],
    )
    def test_a_relationship_writes_its_edge_collection(self, cypher: str) -> None:
        t = translate(cypher, mapping=mapping_bundle_for("pg"))
        assert " r " in t.aql and "IN @@edgeCollection" in t.aql.splitlines()[-1]
        assert t.bind_vars["@edgeCollection"] == "follows"

    def test_an_end_node_writes_its_own_collection(self) -> None:
        t = translate(
            "MATCH (u:User)-[:FOLLOWS]->(p:Person) SET p.seen = true", mapping=mapping_bundle_for("pg")
        )
        last = t.aql.splitlines()[-1]
        key = last.rsplit("IN @", 1)[1].strip()
        assert t.bind_vars[key] == "persons"

    def test_two_variables_in_one_collection_are_refused(self) -> None:
        with pytest.raises(CoreError, match="both write collection 'users'") as exc:
            _aql("MATCH (a:User)-[:FOLLOWS]->(b:User) SET a.x = 1, b.x = 2")
        assert exc.value.code == "UNSUPPORTED"

    @pytest.mark.parametrize(
        ("cypher", "message"),
        [
            ("MATCH (n:User) SET n.a.b = 1", "nested property"),
            ("MATCH (n:User) REMOVE n.a.b", "nested property"),
            ("MATCH (n:User) SET n:Admin", "label"),
            ("MATCH (a:User)-[r:FOLLOWS*1..2]->(b) SET r.w = 1", "variable-length relationship"),
            ("MATCH (n:User) OPTIONAL MATCH (n)-[r:FOLLOWS]->() DELETE r", "not bound by the first MATCH"),
        ],
    )
    def test_forms_that_used_to_write_the_wrong_thing_are_refused(self, cypher: str, message: str) -> None:
        with pytest.raises(CoreError, match=message) as exc:
            _aql(cypher)
        assert exc.value.code == "UNSUPPORTED"

    def test_removing_a_label_is_skipped_as_before(self) -> None:
        assert "UPDATE" not in _aql("MATCH (n:User) REMOVE n:Admin")

    def test_a_parenthesised_variable_is_a_variable(self) -> None:
        assert "UPDATE n WITH {z: 4}" in _aql("MATCH (n:User) SET (n).z = 4")

    def test_replacing_an_edge_keeps_its_endpoints(self) -> None:
        aql = _aql("MATCH (a:User)-[r:FOLLOWS]->(b:User) SET r = {w: 8}")
        assert 'REPLACE r WITH MERGE({w: 8}, KEEP(r, "_from", "_to")) IN @@edgeCollection' in aql

    def test_after_with_the_same_merging_applies(self) -> None:
        aql = _aql("MATCH (n:User) WITH n SET n.z = 1 REMOVE n.x")
        assert aql.count("UPDATE") == 1 and '{z: 1, "x": null}' in aql

    def test_create_folds_and_needs_no_trailing_let(self) -> None:
        aql = _aql("CREATE (n:User {name: 'c'}) SET n.y = 2")
        assert aql.strip() == "INSERT MERGE({name: 'c'}, {y: 2}) INTO @@collection"

    def test_set_after_create_on_a_matched_variable_is_still_refused(self) -> None:
        with pytest.raises(CoreError, match="not created in this query"):
            _aql("MATCH (m:User) CREATE (n:User {name: 'c'}) SET m.y = 2")
