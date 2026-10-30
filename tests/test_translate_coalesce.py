"""coalesce() returns the first non-null argument (docs/ldbc_benchmark_plan.md §3.5).

It was emitted as ``FIRST(REMOVE_VALUES([a, b], null))``. REMOVE_VALUES takes
an *array* of values to remove, so a bare ``null`` removed nothing and the
expression always returned ``a`` — null or not. LDBC IS2's
``coalesce(message.imageFile, message.content)`` returned null for every text
post. AQL's NOT_NULL() has exactly coalesce's semantics.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

# These pin how constructs lower to AQL using literal inputs, which compile-time
# folding would evaluate first (tests/test_const_eval.py covers folding).
pytestmark = pytest.mark.usefixtures("without_constant_folding")


def test_coalesce_is_not_null():
    aql = translate(
        "MATCH (m:Post) RETURN coalesce(m.imageFile, m.content, 'none') AS c",
        mapping=mapping_bundle_for("ldbc_pg"),
    ).aql
    assert (
        "NOT_NULL(m.imageFile, m.content, 'none')" in aql or 'NOT_NULL(m.imageFile, m.content, "none")' in aql
    )
    assert "REMOVE_VALUES" not in aql


def test_coalesce_without_arguments_is_refused():
    with pytest.raises(CoreError):
        translate("MATCH (m:Post) RETURN coalesce() AS c", mapping=mapping_bundle_for("ldbc_pg"))
