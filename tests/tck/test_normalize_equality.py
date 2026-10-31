"""Cell equality used by the TCK harness (``tests/tck/normalize.py``).

A lenient comparison inflates the conformance figure, so each rule here is a
case where Python's ``==`` would have passed a wrong result, or failed a right one.
"""

from __future__ import annotations

import math

from tests.tck.normalize import results_match, values_equal


def test_boolean_never_equals_number() -> None:
    assert not values_equal(True, 1)
    assert not values_equal(0, False)
    assert not values_equal([True], [1])
    assert values_equal(True, True)


def test_integer_and_float_compare_numerically() -> None:
    # ArangoDB returns a whole float as an integer.
    assert values_equal(1, 1.0)
    assert not values_equal(1, 1.5)


def test_nan_equals_nan() -> None:
    assert values_equal(math.nan, math.nan)
    assert not values_equal(math.nan, 1.0)


def test_string_never_equals_number() -> None:
    assert not values_equal("1", 1)


def test_lists_are_ordered_unless_told_otherwise() -> None:
    assert not values_equal([1, 2], [2, 1])
    assert values_equal([1, 2], [2, 1], ignore_list_order=True)
    assert values_equal({"a": [[1, 2], [3]]}, {"a": [[3], [2, 1]]}, ignore_list_order=True)
    assert not values_equal([1, 1, 2], [1, 2, 2], ignore_list_order=True)


def test_maps_need_the_same_keys() -> None:
    assert not values_equal({"a": 1}, {"a": 1, "b": None})
    assert values_equal({"a": 1, "b": None}, {"b": None, "a": 1})


def test_unordered_rows_match_as_a_multiset() -> None:
    table = [{"x": "1"}, {"x": "1.5"}]
    assert results_match([{"x": 1.5}, {"x": 1}], table)[0]
    assert not results_match([{"x": 1}, {"x": 1}], table)[0]


def test_ordered_rows_compare_by_position() -> None:
    table = [{"x": "1"}, {"x": "2"}]
    assert results_match([{"x": 1}, {"x": 2}], table, ordered=True)[0]
    assert not results_match([{"x": 2}, {"x": 1}], table, ordered=True)[0]


def test_ignoring_list_order_applies_to_cells() -> None:
    table = [{"l": "[1, 2]"}]
    assert not results_match([{"l": [2, 1]}], table)[0]
    assert results_match([{"l": [2, 1]}], table, ignore_list_order=True)[0]


def test_string_cells_are_cypher_literals() -> None:
    from tests.tck.normalize import normalize_expected_value

    assert normalize_expected_value("'\\''") == "'"
    assert normalize_expected_value("'a\\\\b'") == "a\\b"
    assert normalize_expected_value("'a\\nb'") == "a\nb"
    assert normalize_expected_value("'\\u0041'") == "A"
    assert normalize_expected_value("'plain'") == "plain"
