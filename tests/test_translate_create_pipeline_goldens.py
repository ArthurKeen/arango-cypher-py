"""Golden AQL for CREATE before a WITH (LDBC INS4), on PG and LPG (PRD §8.3).

The behaviour tests are ``tests/integration/test_create_pipeline_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(858, 860)])
def test_translate_create_pipeline_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    assert out.warnings == []
