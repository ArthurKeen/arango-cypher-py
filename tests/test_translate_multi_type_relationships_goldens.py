"""Golden AQL for relationships of several types and long MATCH-after-WITH patterns (WP-38).

The behaviour tests are ``tests/integration/test_multi_type_relationships_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(854, 858)])
def test_translate_multi_type_relationships_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    assert all("limited to" in w["message"] or "VCI" in w["message"] for w in out.warnings)
