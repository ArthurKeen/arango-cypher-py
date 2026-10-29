"""Unit tests for ``_translate_v0.distinct_order``."""

from __future__ import annotations

from arango_cypher._translate_v0.distinct_order import live_sort_terms, order_keeping_collect


def test_a_sort_followed_by_order_keeping_clauses_is_live() -> None:
    lines = [
        "FOR n IN @@c",
        "  SORT (n.a == null) DESC, n.a DESC, f(n.b, 'x, y') ASC, n.c",
        "  LET x = 1",
        "  LIMIT 3",
    ]
    assert live_sort_terms(lines) == [
        ("(n.a == null)", "DESC"),
        ("n.a", "DESC"),
        ("f(n.b, 'x, y')", "ASC"),
        ("n.c", "ASC"),
    ]


def test_a_loop_or_collect_after_the_sort_ends_it() -> None:
    assert live_sort_terms(["FOR n IN @@c", "  SORT n.a", "  FOR m IN @@d"]) is None
    assert live_sort_terms(["FOR n IN @@c", "  SORT n.a", "  COLLECT a = n.a"]) is None
    assert (
        live_sort_terms(["FOR n IN @@c", "  SORT n.a", "  LET s = (\n  FOR x IN [1]\n  RETURN x\n)"]) is None
    )
    assert live_sort_terms(["FOR n IN @@c"]) is None


def test_the_collect_keeps_each_groups_first_row_and_sorts_by_it() -> None:
    forbidden = {"_d"}
    collect, restore = order_keeping_collect(
        "k = n.k", [("n.a", "DESC"), ("n.b", "ASC")], forbidden_vars=forbidden
    )
    assert collect == [
        "  COLLECT k = n.k INTO _distinct_rows = [n.a, n.b]",
        "  LET _distinct_first = FIRST(FOR _d_1 IN _distinct_rows SORT _d_1[0] DESC, _d_1[1] ASC RETURN _d_1)",
    ]
    assert restore == ["  SORT _distinct_first[0] DESC, _distinct_first[1] ASC"]
