"""Quantifiers return Cypher's three-valued answers on ArangoDB.

The lowering is ``local_scopes.quantifier_aql``; folding is disabled so these
literal lists exercise the AQL a data-dependent list takes.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core.exec import AqlExecutor

from arango_cypher import translate
from arango_cypher._translate_v0.const_eval import constant_folding
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def arango_db() -> Any:
    """The ``_system`` database: these queries read no collections."""
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    return client.db(
        "_system",
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASS", "openSesame"),
    )


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("any(x IN [1, null] WHERE x > 0)", True),
        ("any(x IN [0, null] WHERE x > 0)", None),
        ("any(x IN [0] WHERE x > 0)", False),
        ("all(x IN [1, null] WHERE x > 0)", None),
        ("all(x IN [0, null] WHERE x > 0)", False),
        ("all(x IN [] WHERE x > 0)", True),
        ("none(x IN [0, null] WHERE x > 0)", None),
        ("none(x IN [1, null] WHERE x > 0)", False),
        ("single(x IN [1, null] WHERE x > 0)", None),
        ("single(x IN [1, 2, null] WHERE x > 0)", False),
        ("single(x IN [1, 0] WHERE x > 0)", True),
        ("any(x IN null WHERE x > 0)", None),
    ],
)
def test_three_valued_quantifiers(arango_db, expr: str, expected) -> None:
    with constant_folding(False):
        out = translate(f"RETURN {expr} AS r", mapping=mapping_bundle_for("lpg"))
    rows = list(AqlExecutor(arango_db).execute(out.to_aql_query()))
    assert rows == [{"r": expected}], out.aql
