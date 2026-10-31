"""percentileDisc / percentileCont, Cypher-ordered min / max, and aggregates in
ORDER BY. Live values: ``tests/integration/test_aggregates_live.py`` and the
TCK (Aggregation2, Aggregation6, ReturnOrderBy6, WithOrderBy4).
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.aggregates import aggregate_key
from arango_cypher._translate_v0.percentiles import _split_arguments, parse_percentile, percentile_value
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str) -> str:
    return translate(cypher, mapping=mapping_bundle_for("pg")).aql


@pytest.mark.parametrize("case_id", ["C1117", "C1118", "C1119", "C1120"])
def test_goldens(corpus_cases, case_id: str) -> None:
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params)
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("percentileDisc(n.price, $p)", ("disc", "n.price", "$p")),
        ("PERCENTILECONT ( n.x , 0.5 )", ("cont", "n.x", "0.5")),
        ("percentileCont(coalesce(n.x, [1, 2][0]), 0.5)", ("cont", "coalesce(n.x, [1, 2][0])", "0.5")),
    ],
)
def test_parse_percentile(text: str, expected: tuple[str, str, str]) -> None:
    spec = parse_percentile(text)
    assert spec is not None
    kind, inner, p = expected
    assert spec["inner"] == inner and spec["percentile"] == (kind, p)


@pytest.mark.parametrize(
    "text", ["percentileDisc(n.x)", "percentileDisc(n.x, 0.5) + 1", "collect(n.x)", "percentile(n.x, 1)"]
)
def test_not_a_single_percentile_call(text: str) -> None:
    assert parse_percentile(text) is None


def test_split_arguments_respects_brackets_and_strings() -> None:
    assert _split_arguments("a, 'x, y', [1, 2], f(b, c)") == ["a", "'x, y'", "[1, 2]", "f(b, c)"]
    assert _split_arguments("a)") is None


def test_percentile_value_fails_out_of_range_and_is_null_without_values() -> None:
    aql = percentile_value("vals", "disc", "@p")
    assert 'FAIL("NumberOutOfRange' in aql
    assert "_pc == 0 ? null" in aql and "SORTED(REMOVE_VALUE(vals, null))" in aql


def test_min_of_strings_orders_by_code_point_but_min_of_a_property_stays_an_aggregate() -> None:
    assert "TO_HEX(_ev)" in _aql("UNWIND ['a', 'B'] AS s RETURN min(s) AS m")
    assert "AGGREGATE m = MIN(u.age)" in _aql("MATCH (u:User) RETURN min(u.age) AS m")


def test_max_of_a_mixed_list_ranks_types() -> None:
    aql = _aql("UNWIND [1, 'a', [1, 2]] AS x RETURN max(x) AS m")
    assert "TRANSLATE(TYPENAME(_ev)" in aql and "DESC LIMIT 1" in aql


def test_order_by_an_aggregate_the_projection_computes() -> None:
    aql = _aql("MATCH (u:User) WITH u.city AS city, sum(u.age) AS s ORDER BY sum(u.age) DESC RETURN city")
    assert "SORT (s == null) DESC, s DESC" in aql


def test_order_by_an_aggregate_the_projection_does_not_compute_is_refused() -> None:
    with pytest.raises(CoreError, match="does not compute") as err:
        _aql("MATCH (u:User) RETURN u.city AS city, count(*) AS c ORDER BY max(u.age)")
    assert err.value.code == "UNSUPPORTED"


def test_aggregate_key_ignores_whitespace() -> None:
    assert aggregate_key("count( you.age )") == aggregate_key("count(you.age)")
