"""MERGE forms the UPSERT translation cannot express are refused, not mis-compiled.

Each of these produced AQL that read a Cypher variable as a collection
(ERR 1203) or never bound the MERGE's path.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        (
            "CREATE (a:User), (b:User) MERGE (a)-[:FOLLOWS]->(b) RETURN count(a)",
            "a CREATE in the same query part declares",
        ),
        ("CREATE (a:User {num: 1}) MERGE (:User {v: a.num})", "a CREATE in the same query part declares"),
        (
            "CREATE (a:User), (b:User) MERGE (a)-[:FOLLOWS]->(b) CREATE (b)-[:LIKES]->(c:Doc) RETURN count(*)",
            "a, b",
        ),
        ("MERGE p = (a:User {num: 1}) RETURN p", "named path"),
    ],
)
def test_refused(cypher: str, message: str) -> None:
    with pytest.raises(CoreError, match=message) as err:
        translate(cypher, mapping=mapping_bundle_for("lpg"))
    assert err.value.code == "UNSUPPORTED"


def test_a_name_an_earlier_merge_binds_is_not_counted_as_created() -> None:
    # The CREATE reuses the MERGE's t: t is the MERGE's variable, not one the
    # CREATE declares, so this refusal does not apply (TCK Create3 [13]).
    try:
        translate(
            "MERGE (t:User {id: 42}) CREATE (f:Doc) CREATE (t)-[:LIKES]->(f)",
            mapping=mapping_bundle_for("lpg"),
        )
    except CoreError as err:
        assert "a CREATE in the same query part declares" not in str(err)
