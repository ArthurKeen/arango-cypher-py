"""List quantifier predicates: ``any`` / ``all`` / ``none`` / ``single``.

``<kind>(x IN list WHERE pred)`` lowers to a subquery that evaluates the
predicate per element as a value, then decides from the counts of true, false
and null results — Cypher's three-valued answer (``local_scopes.quantifier_aql``).
``tests/integration/test_quantifiers_live.py`` checks the answers on ArangoDB.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

# These pin how constructs lower to AQL using literal inputs, which compile-time
# folding would evaluate first (tests/test_const_eval.py covers folding).
pytestmark = pytest.mark.usefixtures("without_constant_folding")


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


def _verdict(aql: str) -> str:
    return aql[aql.index("RETURN (q") :]


class TestQuantifiers:
    def test_any_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE any(x IN p.roles WHERE x = "lead") RETURN p.name',
            mapping=pg,
        )
        assert "FOR q" in out.aql and "IN [p.roles]" in out.aql
        assert '(x == "lead")' in out.aql
        assert "FILTER CURRENT == true]) > 0 ? true" in _verdict(out.aql)

    def test_all_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE all(x IN p.roles WHERE x <> "") RETURN p.name',
            mapping=pg,
        )
        assert "FILTER CURRENT == false]) > 0 ? false" in _verdict(out.aql)

    def test_none_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE none(x IN p.roles WHERE x = "bad") RETURN p.name',
            mapping=pg,
        )
        assert "FILTER CURRENT == true]) > 0 ? false" in _verdict(out.aql)

    def test_single_in_where(self, pg):
        out = translate(
            'MATCH (p:Person) WHERE single(x IN p.roles WHERE x = "lead") RETURN p.name',
            mapping=pg,
        )
        verdict = _verdict(out.aql)
        assert "FILTER CURRENT == true]) > 1 ? false" in verdict
        assert "FILTER CURRENT == true]) == 1" in verdict

    def test_null_list_is_null(self, pg):
        out = translate("MATCH (p:Person) RETURN any(x IN p.roles WHERE x = 'a') AS r", mapping=pg)
        assert "_list == null ? null :" in out.aql

    def test_null_predicate_values_are_counted(self, pg):
        out = translate("MATCH (p:Person) RETURN all(x IN p.roles WHERE x > 1) AS r", mapping=pg)
        assert "FILTER CURRENT == null]) > 0 ? null" in out.aql

    def test_all_without_where(self, pg):
        out = translate("RETURN all(x IN [1, 2, 3]) AS r", mapping=pg)
        assert "RETURN (true))" in out.aql

    def test_quantifier_combined_with_other_predicate(self, pg):
        out = translate(
            "MATCH (p:Person) WHERE any(x IN [1, 2] WHERE x > 0) AND p.born > 1970 RETURN p.name",
            mapping=pg,
        )
        assert "IN [[1,2]]" in out.aql
        assert "p.born" in out.aql

    def test_loop_variable_bound_elsewhere_is_renamed(self, pg):
        # AQL keeps UNWIND's x in scope after WITH drops it; an inner FOR x
        # would redeclare it (ERR 1511).
        out = translate(
            "UNWIND [1, 2] AS x WITH collect(x) AS l RETURN any(x IN l WHERE x > 1) AS r",
            mapping=pg,
        )
        assert "FOR x_l" in out.aql
        assert out.aql.count("FOR x IN") == 1
