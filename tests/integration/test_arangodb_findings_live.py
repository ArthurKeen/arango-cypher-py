"""ArangoDB behaviour the translation works around, checked against the server.

When a server release changes it, these tests say so, and the workaround can
be reconsidered:

- arangodb/arangodb#23553: an uncorrelated subquery after a ``COLLECT`` with
  no group keys that counts evaluates to null (3.11.14, 3.12.12). ``count(*)``
  compiles to ``SUM(1)`` and ``count(x)`` to ``SUM(x == null ? 0 : 1)``, so
  the translation does not meet it.
- ArangoDB 3.11 carries a hoisted subquery's value on every row of the loop it
  was hoisted above (a ``UNION`` supertype scan after a ``COLLECT`` peaked past
  1 GB on 3.11.14 and at about 2 MB on 3.12.12), which
  ``supertypes.union_scan_source(after=…)`` prevents.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_arangodb_findings_it"


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("a").import_bulk([{"n": i} for i in range(30)])
        database.create_collection("b").import_bulk([{"n": i} for i in range(5)])
        database.create_collection("users").import_bulk([{"_key": k, "id": k} for k in "abcd"])
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _one(db: Any, aql: str) -> Any:
    return list(db.aql.execute(aql))[0]


@pytest.mark.xfail(strict=True, reason="arangodb/arangodb#23553: the subquery evaluates to null")
@pytest.mark.parametrize("collect", ["COLLECT WITH COUNT INTO total", "COLLECT AGGREGATE total = COUNT(m)"])
def test_a_subquery_after_a_counting_collect(db: Any, collect: str) -> None:
    aql = f"FOR m IN a {collect} LET k = (FOR y IN b RETURN 1) RETURN [total, LENGTH(k)]"
    assert _one(db, aql) == [30, 5]


def test_the_form_count_compiles_to_is_unaffected(db: Any) -> None:
    aql = "FOR m IN a COLLECT AGGREGATE total = SUM((m) == null ? 0 : 1) LET k = (FOR y IN b RETURN 1) RETURN [total, LENGTH(k)]"
    assert _one(db, aql) == [30, 5]


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_a_list_comprehension_after_count_star(db: Any) -> None:
    # The list was "(non-representable type none)" when count(*) was COUNT(1).
    rows = _rows(db, "MATCH (n:User) WITH count(*) AS c RETURN c, [y IN range(1, 3) | y] AS l")
    assert rows == [{"c": 4, "l": [1, 2, 3]}]


def test_a_match_after_count_star_keeps_its_rows(db: Any) -> None:
    rows = _rows(
        db,
        "MATCH (n:User) WITH count(*) AS c MATCH (u:User) WHERE u.id IN [x IN range(1, 2) | 'a'] "
        "RETURN c, count(u) AS k",
    )
    assert rows == [{"c": 4, "k": 1}]


def test_count_star_over_no_rows_is_zero(db: Any) -> None:
    # A single aggregate column comes back bare.
    assert _rows(db, "MATCH (n:User) WHERE n.id = 'z' RETURN count(*) AS c") == [0]
