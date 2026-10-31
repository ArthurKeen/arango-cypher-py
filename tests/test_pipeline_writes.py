"""SET / REMOVE / DELETE before a WITH (``_translate_v0.pipeline_writes``).

The live behaviour is pinned by the TCK (Set6, Remove3, Delete6) through the
execution ratchet; these tests pin the AQL shape and the refusals.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, fixture: str = "pg") -> str:
    return translate(cypher, mapping=mapping_bundle_for(fixture)).aql


def test_clauses_after_a_set_read_the_written_document() -> None:
    aql = _aql("MATCH (n:Person) SET n.age = n.age + 1 WITH n WHERE n.age > 30 RETURN n.name AS name")
    lines = [line.strip() for line in aql.splitlines()]
    update = next(i for i, line in enumerate(lines) if line.startswith("UPDATE n WITH"))
    assert lines[update + 1] == "LET n_new = NEW"
    assert "n_new.age > 30" in aql and "RETURN {name: n_new.name}" in aql


def test_a_renamed_variable_is_rebound_after_the_write() -> None:
    aql = _aql("MATCH (p:Person) WITH p AS x SET x.seen = true WITH x RETURN x.seen AS seen")
    assert "_new = NEW" in aql
    assert aql.rstrip().endswith("_new.seen}")


def test_aggregating_after_a_remove() -> None:
    aql = _aql("MATCH (n:Person) REMOVE n.tmp WITH count(*) AS c RETURN c")
    assert aql.index("UPDATE n WITH") < aql.index("COLLECT AGGREGATE")


def test_a_delete_before_a_with_removes_inside_the_loop() -> None:
    aql = _aql("MATCH (n:Person) WITH n, n.age AS age DELETE n WITH age WHERE age > 1 RETURN age")
    assert aql.index("LET age = n.age") < aql.index("REMOVE n IN @@collection") < aql.index("FILTER")


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        # A MATCH after the write would read a written collection (ERR 1579).
        ("MATCH (n:Person) SET n.x = 1 WITH n MATCH (m:Person) RETURN m", "after SET/REMOVE/DELETE"),
        ("MATCH (n:Person) SET n.x = 1 WITH n OPTIONAL MATCH (n)-[:KNOWS]->(m) RETURN m", "ERR 1579"),
        # Two write groups would write one collection twice.
        ("MATCH (n:Person) SET n.x = 1 WITH n SET n.y = 2 WITH n RETURN n", "A second write"),
        ("MATCH (n:Person) SET n.x = 1 WITH n SET n.y = 2 RETURN n", "A write, MATCH or CALL"),
        # Only SET / REMOVE / DELETE may write mid-pipeline.
        ("MATCH (n:Person) MERGE (m:Person {id: 1}) WITH n RETURN n", "MERGE before WITH"),
    ],
)
def test_reading_or_writing_again_after_a_write_is_refused(cypher: str, message: str) -> None:
    with pytest.raises(CoreError, match=message) as err:
        _aql(cypher)
    assert err.value.code == "UNSUPPORTED"


def test_unwind_after_a_write_is_allowed() -> None:
    aql = _aql("MATCH (n:Person) SET n.x = 1 WITH n UNWIND [1, 2] AS i RETURN n.x + i AS v")
    assert "LET n_new = NEW" in aql and "n_new.x" in aql
