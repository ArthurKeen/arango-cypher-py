"""Golden AQL for duration arithmetic, temporal comparisons (WP-41) and patterns read from their bound end.

The behaviour tests are ``tests/integration/test_temporal_arithmetic_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

# These pin how temporal constructs lower to AQL using literal inputs, which
# compile-time folding would evaluate first (tests/test_const_temporal.py
# covers folding).
pytestmark = pytest.mark.usefixtures("without_constant_folding")


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(877, 881)])
def test_translate_temporal_arithmetic_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
