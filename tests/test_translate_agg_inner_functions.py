"""Cypher scalar functions inside an aggregate argument.

``max(size(m.tagline))`` reached ``_compile_agg_expr`` as raw text and bypassed
the parse-tree function compiler, so ``size`` was left un-lowered and executed as
an unknown AQL function. The argument now goes through the expression compiler
like any other expression; ``count(a) + count(b)`` is two aggregates, not one.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestAggInnerScalarFns:
    def test_size_in_avg(self, pg):
        out = translate("MATCH (m:Movie) RETURN avg(size(m.tagline)) AS a", mapping=pg)
        assert "AVG(LENGTH(m.tagline))" in out.aql
        assert "size(" not in out.aql

    def test_size_in_max_with(self, pg):
        out = translate("MATCH (m:Movie) WITH max(size(m.tagline)) AS x RETURN x", mapping=pg)
        assert "MAX(LENGTH(m.tagline))" in out.aql

    def test_tointeger_in_sum(self, pg):
        out = translate("MATCH (m:Movie) RETURN sum(toInteger(m.released)) AS s", mapping=pg)
        # toInteger() truncates; AQL's TO_NUMBER does not.
        assert (
            "SUM(((m.released == null) ? null : ((m.released >= 0) ? FLOOR(m.released) : CEIL(m.released))))"
            in (out.aql)
        )

    def test_lower_in_count_distinct(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN count(DISTINCT lower(m.title)) AS c",
            mapping=pg,
        )
        assert "COUNT_DISTINCT(LOWER(m.title))" in out.aql

    def test_plain_count_unaffected(self, pg):
        out = translate(
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, count(m) AS c",
            mapping=pg,
        )
        # count() skips nulls: AQL COUNT() would count them.
        assert "SUM((m) == null ? 0 : 1)" in out.aql


class TestAggregateCallText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("count(*)", ("count", "*", False)),
            ("count(DISTINCT n.x)", ("count", "n.x", True)),
            ("COUNT ( n )", ("count", "n", False)),
            ("sum(toInteger(n.x))", ("sum", "toInteger(n.x)", False)),
            ("count(distinctive)", ("count", "distinctive", False)),
            ("count(a) + count(b)", None),
            ("max(a)[0]", None),
            ("collect(a)", None),
            ("counter(a)", None),
        ],
    )
    def test_aggregate_call(self, text, expected):
        from arango_cypher._translate_v0.core import _aggregate_call

        assert _aggregate_call(text) == expected
