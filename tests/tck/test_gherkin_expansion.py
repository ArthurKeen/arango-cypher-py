"""Scenario Outline expansion in the TCK harness (``tests/tck/gherkin.py``)."""

from __future__ import annotations

from pathlib import Path

from tests.tck.gherkin import _split_table_row, _substitute, parse_feature


def test_less_than_between_placeholders_is_not_a_placeholder() -> None:
    # A generic <[^>]+> pattern reads " < <rhs" as one placeholder.
    assert _substitute("RETURN <lhs> < <rhs> AS r", {"lhs": "1", "rhs": "2.0"}) == "RETURN 1 < 2.0 AS r"


def test_unknown_angle_brackets_are_left_alone() -> None:
    assert _substitute("RETURN 1 <> <x>", {"x": "2"}) == "RETURN 1 <> 2"
    assert _substitute("<notAHeader>", {"x": "2"}) == "<notAHeader>"


def test_longer_header_wins_over_its_prefix() -> None:
    assert _substitute("<a> <ab>", {"a": "1", "ab": "2"}) == "1 2"


def test_table_cells_honour_gherkin_escapes() -> None:
    assert _split_table_row(r"| 'a\|b' | x\\y | |") == ["'a|b'", "x\\y", ""]
    assert _split_table_row("| a | b |") == ["a", "b"]


def test_outline_with_comparison_expands(tmp_path: Path) -> None:
    feature = tmp_path / "t.feature"
    feature.write_text(
        "Feature: t\n"
        "  Scenario Outline: cmp\n"
        "    When executing query:\n"
        '      """\n'
        "      RETURN <lhs> < <rhs> AS result\n"
        '      """\n'
        "    Then the result should be, in any order:\n"
        "      | result   |\n"
        "      | <result> |\n"
        "    Examples:\n"
        "      | lhs | rhs | result |\n"
        "      | 1   | 2   | true   |\n",
        encoding="utf-8",
    )
    (sc,) = parse_feature(feature).scenarios
    assert sc.steps[0].doc_string.strip() == "RETURN 1 < 2 AS result"
    assert sc.steps[1].data_table == [["result"], ["true"]]
