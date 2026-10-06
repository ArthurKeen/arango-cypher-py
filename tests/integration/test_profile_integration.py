from __future__ import annotations

import json

import pytest
from arango import ArangoClient
from arango_query_core.exec import AqlExecutor

from arango_cypher import get_cypher_profile, translate, validate_cypher_profile
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.seed import seed_social_dataset


def _connect(url: str, db_name: str):
    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username="root", password="openSesame")
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)
    return client.db(db_name, username="root", password="openSesame")


@pytest.mark.integration
def test_get_cypher_profile_serializable():
    p = get_cypher_profile()
    json.dumps(p)
    assert p["profile_schema_version"] == "1"


@pytest.mark.integration
def test_validate_and_execute_roundtrip(arango_pytest_url: str):
    """
    Uses isolated Docker Arango on port 28530 (docker-compose.pytest.yml via session fixture).
    """
    db_name = "cypher_profile_it"
    db = _connect(arango_pytest_url, db_name)
    seed_social_dataset(db, mode="pg")
    mapping = mapping_bundle_for("pg")

    cypher = "MATCH (n:User) WHERE n.id = $id RETURN n.name AS name"
    v = validate_cypher_profile(cypher, mapping=mapping, params={"id": "u1"})
    assert v.ok, v.errors

    tq = translate(cypher, mapping=mapping, params={"id": "u1"})
    cur = AqlExecutor(db).execute(tq.to_aql_query())
    rows = list(cur)
    assert rows == [{"name": "Alice"}]


@pytest.mark.integration
def test_union_all_executes(arango_pytest_url: str):
    """UNION ALL should return combined rows from both branches."""
    db_name = "cypher_union_it"
    db = _connect(arango_pytest_url, db_name)
    seed_social_dataset(db, mode="pg")
    mapping = mapping_bundle_for("pg")

    cypher = (
        "MATCH (n:User) WHERE n.id = 'u1' RETURN n.name AS name "
        "UNION ALL "
        "MATCH (n:User) WHERE n.id = 'u2' RETURN n.name AS name"
    )
    tq = translate(cypher, mapping=mapping)
    cur = AqlExecutor(db).execute(tq.to_aql_query())
    rows = sorted(list(cur), key=lambda r: r["name"])
    assert rows == [{"name": "Alice"}, {"name": "Bob"}]


@pytest.mark.integration
def test_union_distinct_deduplicates(arango_pytest_url: str):
    """UNION (without ALL) should deduplicate identical rows."""
    db_name = "cypher_union_it"
    db = _connect(arango_pytest_url, db_name)
    seed_social_dataset(db, mode="pg")
    mapping = mapping_bundle_for("pg")

    cypher = (
        "MATCH (n:User) WHERE n.id = 'u1' RETURN n.name AS name "
        "UNION "
        "MATCH (n:User) WHERE n.id = 'u1' RETURN n.name AS name"
    )
    tq = translate(cypher, mapping=mapping)
    cur = AqlExecutor(db).execute(tq.to_aql_query())
    rows = list(cur)
    assert rows == [{"name": "Alice"}]


@pytest.mark.integration
def test_union_across_collections_executes(arango_pytest_url: str):
    """Branches over different collections reuse ``@@collection`` with different
    values; the second branch's copy is renamed. Each branch must still read
    its own collection."""
    db = _connect(arango_pytest_url, "cypher_union_it")
    seed_social_dataset(db, mode="pg")
    db.collection("persons").insert({"_key": "p1", "name": "Pat"}, overwrite=True)

    cypher = (
        "MATCH (n:User) WHERE n.id = $id RETURN n.name AS name UNION MATCH (n:Person) RETURN n.name AS name"
    )
    tq = translate(cypher, mapping=mapping_bundle_for("pg"), params={"id": "u1"})
    assert "@@collection_u1" in tq.aql
    rows = sorted(AqlExecutor(db).execute(tq.to_aql_query()), key=lambda r: r["name"])
    assert rows == [{"name": "Alice"}, {"name": "Pat"}]


@pytest.mark.integration
def test_union_across_labels_in_one_collection_executes(arango_pytest_url: str):
    """LPG: both branches scan ``vertices``, so ``@@collection`` merges, while the
    type value differs and is renamed. Each branch must filter on its own label."""
    db = _connect(arango_pytest_url, "cypher_union_lpg_it")
    seed_social_dataset(db, mode="lpg")
    db.collection("vertices").insert({"_key": "d1", "type": "Doc", "name": "Spec"}, overwrite=True)

    cypher = (
        "MATCH (n:User) WHERE n.id = 'u2' RETURN n.name AS name UNION ALL MATCH (n:Doc) RETURN n.name AS name"
    )
    tq = translate(cypher, mapping=mapping_bundle_for("lpg"))
    assert "@typeValue_u1" in tq.aql
    rows = sorted(AqlExecutor(db).execute(tq.to_aql_query()), key=lambda r: r["name"])
    assert rows == [{"name": "Bob"}, {"name": "Spec"}]


@pytest.mark.integration
def test_optional_match_sole_clause_hit(arango_pytest_url: str):
    """OPTIONAL MATCH as sole clause should return matching rows normally."""
    db_name = "cypher_optional_it"
    db = _connect(arango_pytest_url, db_name)
    seed_social_dataset(db, mode="pg")
    mapping = mapping_bundle_for("pg")

    cypher = "OPTIONAL MATCH (n:User) WHERE n.id = 'u1' RETURN n.name AS name"
    tq = translate(cypher, mapping=mapping)
    cur = AqlExecutor(db).execute(tq.to_aql_query())
    rows = list(cur)
    assert rows == [{"name": "Alice"}]


@pytest.mark.integration
def test_optional_match_sole_clause_miss(arango_pytest_url: str):
    """OPTIONAL MATCH as sole clause with no match returns one null row."""
    db_name = "cypher_optional_it"
    db = _connect(arango_pytest_url, db_name)
    seed_social_dataset(db, mode="pg")
    mapping = mapping_bundle_for("pg")

    cypher = "OPTIONAL MATCH (n:User) WHERE n.id = 'nonexistent' RETURN n.name AS name"
    tq = translate(cypher, mapping=mapping)
    cur = AqlExecutor(db).execute(tq.to_aql_query())
    rows = list(cur)
    assert rows == [{"name": None}]
