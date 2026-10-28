"""Golden AQL for DELETE / DETACH DELETE (PRD §8.3): read every matched row, then write.

The behaviour tests are ``tests/integration/test_delete_live.py``; these pin the text.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(849, 852)])
def test_translate_deletes_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    # An unbounded *0.. is capped at 10 hops, and the translation says so.
    assert all("limited to 10 hops" in w["message"] for w in out.warnings)
