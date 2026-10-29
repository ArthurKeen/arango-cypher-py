"""A known string sorts and compares by code point, as Cypher does (PRD §8.2).

The behaviour tests run on ArangoDB in ``tests/integration/test_string_order_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.arithmetic import INT, STRING
from arango_cypher._translate_v0.core import _code_point_operands, _code_point_sort_key
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    ("static_type", "key"), [(STRING, "TO_HEX(n.name)"), (INT, "n.name"), (None, "n.name")]
)
def test_the_sort_key_follows_the_static_type(static_type: str | None, key: str) -> None:
    assert _code_point_sort_key("n.name", static_type) == key


def test_comparison_operands() -> None:
    assert _code_point_operands("a", STRING, "b", STRING) == ("TO_HEX(a)", "TO_HEX(b)")
    assert _code_point_operands("a", STRING, "b", None) == ("TO_HEX(a)", "(IS_STRING(b) ? TO_HEX(b) : b)")
    assert _code_point_operands("a", None, "b", None) == ("a", "b")
    assert _code_point_operands("a", INT, "b", INT) == ("a", "b")


def _aql(cypher: str) -> str:
    return translate(cypher, mapping=mapping_bundle_for("pg")).aql


def test_a_string_function_sorts_by_its_bytes() -> None:
    assert "TO_HEX(LOWER(n.name)) DESC" in _aql("MATCH (n:User) RETURN n.id ORDER BY toLower(n.name) DESC")


def test_a_number_sorts_as_it_is() -> None:
    assert "TO_HEX" not in _aql("MATCH (n:User) RETURN n.id, size(n.name) AS len ORDER BY len")


def test_a_value_of_unknown_type_keeps_the_aql_order() -> None:
    # As its ``<`` does: sorting and comparing must agree (TCK WithOrderBy1 [45]).
    assert "TO_HEX" not in _aql("UNWIND ['b', 'B'] AS x RETURN x ORDER BY x")


def test_a_range_comparison_with_a_string_literal_compares_code_points() -> None:
    aql = _aql("MATCH (n:User) WHERE n.name < 'Z' RETURN n.id")
    assert "< TO_HEX('Z')" in aql
    # The null guard reads the value itself: TO_HEX(null) is "".
    assert "n.name != null" in aql


def test_equality_is_unchanged() -> None:
    assert "TO_HEX" not in _aql("MATCH (n:User) WHERE n.name = 'Z' RETURN n.id")
