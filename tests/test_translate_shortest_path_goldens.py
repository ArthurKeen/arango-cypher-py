"""Golden AQL for shortestPath() between bound nodes (WP-39), PG and LPG.

The behaviour tests are ``tests/integration/test_shortest_path_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(860, 862)])
def test_translate_shortest_path_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    # An unbounded * is capped, and the translation says so.
    assert all("limited to" in w["message"] for w in out.warnings)
