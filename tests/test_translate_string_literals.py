"""openCypher string literals reach AQL with the escapes AQL reads differently
rewritten. The live check is in ``tests/integration/test_write_clauses_live.py``."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher._translate_v0.literals import _aql_string_from_cypher


@pytest.mark.parametrize(
    ("cypher", "aql"),
    [
        ("'plain'", "'plain'"),
        ("'it\\'s'", "'it\\'s'"),
        ("'\\N\\T\\B\\F\\R'", "'\\n\\t\\b\\f\\r'"),
        ("'\\u00e9'", "'\\u00e9'"),
        ("'\\U000000e9'", "'\\u00e9'"),
        ("'\\U0001F600'", "'\\ud83d\\ude00'"),
        ("'\\U00000022'", "'\\u0022'"),
        ("'\\\\U0001F600'", "'\\\\U0001F600'"),
        ('"a\\\\"', '"a\\\\"'),
    ],
)
def test_escapes_aql_reads_differently_are_rewritten(cypher: str, aql: str) -> None:
    assert _aql_string_from_cypher(cypher) == aql


def test_a_code_point_past_unicode_is_refused() -> None:
    with pytest.raises(CoreError, match="not a Unicode code point"):
        _aql_string_from_cypher("'\\U00110000'")
