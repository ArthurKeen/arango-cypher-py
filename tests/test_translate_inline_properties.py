"""Inline pattern properties compare as Cypher's ``=`` does.

``(n {name: 'Alice'})`` matches exactly (an index can serve it); a null value
matches nothing. ``case_insensitive_inline_strings=True`` opts string
literals into a case-insensitive match.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_a_string_literal_matches_exactly(layout: str) -> None:
    aql = translate("MATCH (n:User {name: 'Alice'}) RETURN n.id", mapping=mapping_bundle_for(layout)).aql
    assert "(n.name == 'Alice')" in aql
    assert "LOWER" not in aql


def test_a_parameter_is_guarded_against_null() -> None:
    aql = translate("MATCH (n:User {id: $id}) RETURN n.name", mapping=mapping_bundle_for("pg")).aql
    assert "FILTER (@id != null AND n.id == @id)" in aql


def test_a_relationship_property_matches_exactly() -> None:
    aql = translate(
        "MATCH (a:User)-[r:FOLLOWS {note: 'Hi'}]->(b:User) RETURN b.id", mapping=mapping_bundle_for("pg")
    ).aql
    assert "LOWER" not in aql
    assert ".note == 'Hi')" in aql


def test_case_insensitive_matching_is_an_opt_in() -> None:
    aql = translate(
        "MATCH (n:User {name: 'Alice', id: $id}) RETURN n.id",
        mapping=mapping_bundle_for("pg"),
        case_insensitive_inline_strings=True,
    ).aql
    assert "(LOWER(n.name) == LOWER('Alice'))" in aql
    # Only string literals: a parameter still compares exactly.
    assert "(@id != null AND n.id == @id)" in aql


def test_the_option_is_part_of_the_cache_key() -> None:
    cypher = "MATCH (n:User {name: 'Alice'}) RETURN n.id"
    mapping = mapping_bundle_for("pg")
    exact = translate(cypher, mapping=mapping).aql
    folded = translate(cypher, mapping=mapping, case_insensitive_inline_strings=True).aql
    assert exact != folded
    assert translate(cypher, mapping=mapping).aql == exact
