"""Cypher names reach AQL text verbatim; ones AQL would read differently are refused.

Inside AQL backticks ``\\`` starts an escape, so ``n.`a\\``` used to close its
quoting a character later than Cypher's and splice the rest of the query into
the AQL as code. ``MATCH (n:User) WHERE n.`a\\` = n.`) OR true //` RETURN n``
matched every user. The live counterpart is
``tests/integration/test_name_safety_live.py``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_SPLICE = "MATCH (n:User) WHERE n.`a\\` = n.`) OR true //` RETURN n.name AS x"


def _aql(cypher: str, *, mapping: str = "pg", params: dict | None = None) -> str:
    return translate(cypher, mapping=mapping_bundle_for(mapping), params=params).aql


@pytest.mark.parametrize("mapping", ["pg", "lpg"])
@pytest.mark.parametrize(
    "cypher",
    [
        pytest.param(_SPLICE, id="property-in-where"),
        pytest.param("MATCH (n:User {`a\\`: 1}) RETURN n", id="inline-property"),
        pytest.param("MATCH (n:User) RETURN {`a\\`: 1} AS x", id="map-literal-key"),
        pytest.param("MATCH (`n\\`:User) RETURN `n\\`.name AS x", id="variable"),
        pytest.param("MATCH (n:User) RETURN n.name AS `x\\`", id="alias"),
        pytest.param("MATCH (n:User) SET n.`a\\` = 1", id="set"),
        pytest.param("MATCH (n:User) RETURN n ORDER BY n.`a\\`", id="order-by"),
        pytest.param("MATCH (n:User) RETURN n.`a``b` AS x", id="doubled-backtick"),
        pytest.param("MATCH (n:`User\\`) RETURN n", id="label"),
    ],
)
def test_a_name_aql_would_read_differently_is_refused(cypher: str, mapping: str) -> None:
    with pytest.raises(CoreError, match="backslash or a backtick") as exc:
        _aql(cypher, mapping=mapping)
    assert exc.value.code == "UNSUPPORTED"


def test_a_long_name_is_shortened_in_the_error() -> None:
    name = "a" * 200 + "\\"
    with pytest.raises(CoreError) as exc:
        _aql(f"MATCH (n:User) RETURN n.`{name}` AS x")
    assert len(str(exc.value)) < 200
    assert "…" in str(exc.value)


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (n:User) WHERE n.`first name` = 1 RETURN n", "n.`first name` == 1"),
        ("MATCH (n:User) WHERE n.`prénom` = 1 RETURN n", "n.`prénom` == 1"),
        ('MATCH (n:User) WHERE n.`a"b` = 1 RETURN n', 'n.`a"b` == 1'),
        ("MATCH (`my n`:User) RETURN `my n`.name AS x", "FOR `my n` IN"),
    ],
)
def test_names_without_backslash_or_backtick_still_translate(cypher: str, expected: str) -> None:
    assert expected in _aql(cypher)


class TestRemove:
    """``REMOVE n.prop`` names the attribute inside a string, not after a dot."""

    def test_a_quoted_key_loses_its_backticks(self) -> None:
        assert 'UNSET(n, "first name")' in _aql("MATCH (n:User) REMOVE n.`first name`")

    def test_a_double_quote_in_the_key_cannot_end_the_string(self) -> None:
        aql = _aql('MATCH (n:User) REMOVE n.`a") OR true //`')
        assert 'UNSET(n, "a\\") OR true //")' in aql

    def test_a_plain_key_is_unchanged(self) -> None:
        assert 'UNSET(n, "age")' in _aql("MATCH (n:User) REMOVE n.age")


class TestParameters:
    def test_a_plain_parameter_binds_by_name(self) -> None:
        assert "@who" in _aql("MATCH (n:User) WHERE n.name = $who RETURN n", params={"who": "a"})

    def test_a_backtick_quoted_parameter_binds_by_its_bare_name(self) -> None:
        aql = _aql("MATCH (n:User) WHERE n.name = $`who` RETURN n", params={"who": "a"})
        assert "@who" in aql and "`" not in aql

    @pytest.mark.parametrize("name", ["`a) OR true //`", "`a b`", "`é`"])
    def test_a_name_aql_cannot_bind_is_refused(self, name: str) -> None:
        with pytest.raises(CoreError, match="not a valid AQL bind parameter name") as exc:
            _aql(f"MATCH (n:User) WHERE n.name = ${name} RETURN n")
        assert exc.value.code == "UNSUPPORTED"
