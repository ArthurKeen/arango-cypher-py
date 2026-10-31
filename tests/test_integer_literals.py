"""Integer literals: decimal, hex and octal (``0o`` and legacy ``0``) reach AQL as decimal.

AQL has no octal literal and its hex literals stop short of 64 bits
(``0x7FFFFFFFFFFFFFFF`` is ERR 1501), so the translator writes the value.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.literals import parse_integer_literal
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("42", 42),
        ("0", 0),
        ("0x1F", 31),
        ("0X1f", 31),
        ("0o17", 15),
        ("017", 15),
        ("0o0", 0),
        ("0x7FFFFFFFFFFFFFFF", 2**63 - 1),
        ("0o777777777777777777777", 2**63 - 1),
    ],
)
def test_parse_integer_literal(text: str, value: int) -> None:
    assert parse_integer_literal(text) == value


@pytest.mark.parametrize(
    "query", ["RETURN 0o17 AS a", "RETURN -0o1 AS a", "RETURN [0o7] AS a", "RETURN {k: 0o10} AS a"]
)
def test_octal_literals_parse(query: str) -> None:
    parse_cypher(query)


@pytest.mark.usefixtures("without_constant_folding")
@pytest.mark.parametrize(
    ("query", "aql"),
    [
        ("RETURN 0o17 AS a", "RETURN {a: 15}"),
        ("RETURN 0x7FFFFFFFFFFFFFFF AS a", "RETURN {a: 9223372036854775807}"),
        ("RETURN [0o7, 0x1F, 42] AS a", "RETURN {a: [7,31,42]}"),
        ("RETURN 1.5e3 AS a", "RETURN {a: 1.5e3}"),
    ],
)
def test_literals_reach_aql_in_decimal(query: str, aql: str) -> None:
    assert translate(query, mapping=mapping_bundle_for("lpg")).aql.strip() == aql


def test_hex_literal_in_a_filter() -> None:
    out = translate("MATCH (n:User) WHERE n.age = 0x1F RETURN n.id", mapping=mapping_bundle_for("lpg"))
    assert "n.age == 31" in out.aql
