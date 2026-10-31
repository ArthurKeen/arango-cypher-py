"""Cypher's cross-type sort order in ORDER BY (``_translate_v0.sort_keys``).

The order itself is checked on ArangoDB by the TCK (ReturnOrderBy1 [9]/[10],
WithOrderBy1 [9]/[10], WithOrderBy2 [22]-[24]) through the execution ratchet;
these tests pin when the ranked key is used, and when it is not.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.sort_keys import cypher_sort_key, read_grouping_keys
from tests.helpers.mapping_fixtures import mapping_bundle_for

_RANKED = "TRANSLATE(TYPENAME("


def _aql(cypher: str) -> str:
    return translate(cypher, mapping=mapping_bundle_for("pg")).aql


@pytest.mark.parametrize("case_id", ["C1114", "C1115", "C1116"])
def test_sort_order_goldens(corpus_cases, case_id: str) -> None:
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture))
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND [[], ['a'], [1, null]] AS l RETURN l ORDER BY l",
        "UNWIND [1, 'a', null, true] AS x WITH x ORDER BY x DESC LIMIT 2 RETURN x",
        "MATCH (n:User) WITH collect(n.name) AS names RETURN names ORDER BY names",
    ],
)
def test_a_list_or_a_mixed_list_element_sorts_by_the_ranked_key(cypher: str) -> None:
    aql = _aql(cypher)
    sort = next(line for line in aql.splitlines() if line.strip().startswith("SORT"))
    assert _RANKED in sort
    assert "== null" not in sort  # null has its own rank: no separate null key


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:User) RETURN n.name ORDER BY n.name",
        "MATCH (n:User) WITH n.city AS city, count(*) AS c ORDER BY c DESC RETURN city",
        "UNWIND [3, 1, 2] AS x RETURN x ORDER BY x",
        "UNWIND $values AS x RETURN x ORDER BY x",
    ],
)
def test_a_property_an_aggregate_or_one_typed_value_keeps_the_plain_key(cypher: str) -> None:
    assert _RANKED not in _aql(cypher)


def test_the_key_ranks_types_then_values() -> None:
    key = cypher_sort_key("x")
    assert key.startswith("[(IS_OBJECT((x))")
    assert '"string": 5, "array": 3' in key
    assert "(x)[* RETURN [" in key  # list elements keyed without a subquery
    assert "FOR " not in key


@pytest.mark.parametrize(
    ("expr", "grouped", "expected"),
    [
        ("a.name", {"a.name": "name"}, "name"),
        ("CONCAT(a.name, 'C')", {"a.name": "name"}, "CONCAT(name, 'C')"),
        ("a.name2", {"a.name": "name"}, "a.name2"),
        ("b.a.name", {"a.name": "name"}, "b.a.name"),
        ("a.name + a.age", {"a.name": "name", "a.age": "age"}, "name + age"),
    ],
)
def test_read_grouping_keys(expr: str, grouped: dict[str, str], expected: str) -> None:
    assert read_grouping_keys(expr, grouped) == expected
