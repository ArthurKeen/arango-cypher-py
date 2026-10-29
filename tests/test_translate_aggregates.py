"""Cypher aggregate null semantics lowered to AQL (arango_cypher/_translate_v0/aggregates.py).

AQL's COUNT and COUNT_DISTINCT count nulls and SUM over no rows is null; Cypher
skips nulls and sums no rows to 0. The live check that these return the right
values is ``tests/integration/test_optional_match_live.py``.
"""

from __future__ import annotations

from arango_cypher import translate
from arango_cypher._translate_v0.aggregates import lower_aggregate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _reserve(name: str) -> str:
    return name


def test_count_star_sums_ones():
    # Not COUNT(1): a subquery after a counting COLLECT is null (arangodb/arangodb#23553).
    assert lower_aggregate("c", "COUNT(1)", _reserve) == (["_c_rows = SUM(1)"], "NOT_NULL(_c_rows, 0)")


def test_count_sums_non_null_indicators():
    assert lower_aggregate("c", "COUNT(f)", _reserve) == (
        ["_c_nonnull = SUM((f) == null ? 0 : 1)"],
        "NOT_NULL(_c_nonnull, 0)",
    )


def test_count_distinct_discounts_the_null_value():
    pieces, post = lower_aggregate("c", "COUNT_DISTINCT(f.name)", _reserve)
    assert pieces == ["_c_distinct = COUNT_DISTINCT(f.name)", "_c_has_null = MAX((f.name) == null ? 1 : 0)"]
    assert post == "_c_distinct - NOT_NULL(_c_has_null, 0)"


def test_sum_over_no_rows_is_zero():
    assert lower_aggregate("s", "SUM(x.n)", _reserve) == (["_s_sum = SUM(x.n)"], "NOT_NULL(_s_sum, 0)")


def test_null_ignoring_aggregates_are_unchanged():
    for expr in ("AVG(x.n)", "MIN(x.n)", "MAX(x.n)"):
        assert lower_aggregate("a", expr, _reserve) == ([f"a = {expr}"], None)


def test_reserve_supplies_the_helper_names():
    taken: list[str] = []

    def reserve(name: str) -> str:
        taken.append(f"{name}_1")
        return f"{name}_1"

    pieces, post = lower_aggregate("c", "COUNT(f)", reserve)
    assert taken == ["_c_nonnull_1"]
    assert pieces == ["_c_nonnull_1 = SUM((f) == null ? 0 : 1)"]
    assert post == "NOT_NULL(_c_nonnull_1, 0)"


def test_with_and_return_bind_the_aggregate_after_the_collect():
    pg = mapping_bundle_for("pg")
    for cypher in (
        "MATCH (u:User) WITH count(u.name) AS n RETURN n",
        "MATCH (u:User) RETURN count(u.name) AS n",
    ):
        aql = translate(cypher, mapping=pg).aql
        assert "AGGREGATE _n_nonnull = SUM((u.name) == null ? 0 : 1)" in aql
        assert "LET n = NOT_NULL(_n_nonnull, 0)" in aql


def test_collect_drops_nulls():
    aql = translate("MATCH (u:User) RETURN collect(u.nickname) AS ns", mapping=mapping_bundle_for("pg")).aql
    assert "REMOVE_VALUE(_collect_rows[*].ns, null)" in aql
