"""Golden AQL for supertypes on both LDBC layouts (PRD §8.4).

PG lowers a supertype to a UNION over its member collections and tests
membership on a traversal target; LPG filters the labels array. The behaviour
tests are ``test_translate_supertypes_pg.py`` / ``_lpg.py``; these pin the text.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(813, 821)])
def test_translate_supertypes_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    assert out.warnings == []
