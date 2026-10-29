"""A pattern whose last node is bound is read from that node (``pattern_reversal``).

The behaviour tests run on ArangoDB in ``tests/integration/test_unlabeled_nodes_pg_live.py``
and ``tests/integration/test_temporal_arithmetic_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, mapping=None) -> str:
    return translate(cypher, mapping=mapping or mapping_bundle_for("ldbc_pg")).aql


@pytest.mark.parametrize("layout", ["ldbc_pg", "ldbc_lpg"])
def test_an_optional_match_is_read_from_its_bound_end(layout: str) -> None:
    aql = _aql(
        "MATCH (tag:Tag) OPTIONAL MATCH (m:Message)-[:HAS_TAG]->(tag) RETURN tag.name, count(m) AS n",
        mapping_bundle_for(layout),
    )
    assert "INBOUND tag" in aql
    assert "_id == tag._id" not in aql


def test_a_pattern_bound_at_its_start_is_unchanged() -> None:
    aql = _aql("MATCH (tag:Tag) OPTIONAL MATCH (tag)<-[:HAS_TAG]-(m:Message) RETURN tag.name, count(m) AS n")
    assert "INBOUND tag" in aql


def test_a_pattern_bound_at_neither_end_is_unchanged() -> None:
    aql = _aql("MATCH (tag:Tag) WITH tag MATCH (m:Message)-[:HAS_TAG]->(t:Tag) RETURN count(m) AS n")
    assert "INBOUND" not in aql
