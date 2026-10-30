"""``parameters are:`` tables in the TCK harness (``tests/tck/runner.py``)."""

from __future__ import annotations

import pytest

from tests.tck.runner import _parse_params


def test_every_row_is_a_parameter() -> None:
    # No header row: reading one dropped the first parameter of every scenario.
    assert _parse_params([["elt", "null"], ["coll", "[1, 2, 3, null]"]]) == {
        "elt": None,
        "coll": [1, 2, 3, None],
    }


def test_single_parameter() -> None:
    assert _parse_params([["idx", "1"]]) == {"idx": 1}


def test_malformed_row_is_an_error() -> None:
    with pytest.raises(ValueError, match="name | value"):
        _parse_params([["a", "1", "2"]])
