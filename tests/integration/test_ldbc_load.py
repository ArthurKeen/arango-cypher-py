"""Load the LDBC SNB SF0.003 sample into ArangoDB as PG and LPG (WP-31).

Each data set × layout gets its own database (``ldbc_sf0_003_<encoding>_<layout>``),
which later LDBC suites reuse. The loader already proves the database holds
exactly what the CSV files contain; these tests additionally pin the SF0.003
totals (so both encodings must yield the same graph), check that a re-load is
idempotent, and spot-check the value shapes the reference queries depend on.

Activation: ``RUN_INTEGRATION=1``. The first run downloads two 2.4 MB archives
into ``$LDBC_CACHE_DIR`` (default ``~/.cache/arango-cypher/ldbc``).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from arango import ArangoClient

from tests.ldbc.datasets import DATASETS, fetch
from tests.ldbc.loader import LoadReport, load, lpg_indexes, pg_indexes

pytestmark = pytest.mark.integration

#: SF0.003 node counts per concrete label, counted from the CSV files on 2026-10-23.
SF0003_NODES = {
    "Person": 50,
    "Forum": 381,
    "Post": 3189,
    "Comment": 471,
    "Tag": 16080,
    "TagClass": 71,
    "City": 1343,
    "Country": 111,
    "Continent": 6,
    "University": 6380,
    "Company": 1575,
}

#: SF0.003 edge counts per relationship type (summed over the polymorphic files).
SF0003_EDGES = {
    "KNOWS": 83,
    "HAS_CREATOR": 3660,
    "REPLY_OF": 471,
    "CONTAINER_OF": 3189,
    "HAS_MEMBER": 1253,
    "HAS_MODERATOR": 381,
    "HAS_TAG": 2424,
    "HAS_INTEREST": 1256,
    "HAS_TYPE": 16080,
    "IS_SUBCLASS_OF": 70,
    "IS_LOCATED_IN": 11665,
    "IS_PART_OF": 1454,
    "STUDY_AT": 42,
    "WORK_AT": 103,
    "LIKES": 492,
}

_CASES = [(name, layout) for name in sorted(DATASETS) for layout in ("pg", "lpg")]


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def _db(name: str) -> Any:
    client = ArangoClient(hosts=_env("ARANGO_URL", "http://localhost:8529"))
    user, pw = _env("ARANGO_USER", "root"), _env("ARANGO_PASS", "openSesame")
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(name):
        sys_db.create_database(name)
    return client.db(name, username=user, password=pw)


def _db_name(dataset: str, layout: str) -> str:
    return f"ldbc_{dataset.replace('.', '_').replace('-', '_')}_{layout}"


@pytest.fixture(scope="module")
def loaded() -> dict[tuple[str, str], tuple[Any, LoadReport]]:
    out = {}
    for name, layout in _CASES:
        db = _db(_db_name(name, layout))
        out[(name, layout)] = (db, load(db, fetch(name), layout=layout, encoding=DATASETS[name].encoding))
    return out


@pytest.mark.parametrize("dataset,layout", _CASES)
def test_counts_match_the_sf0003_sample(loaded, dataset, layout):
    _, report = loaded[(dataset, layout)]
    assert dict(report.nodes_by_label) == SF0003_NODES
    assert dict(report.edges_by_type) == SF0003_EDGES


@pytest.mark.parametrize("dataset,layout", _CASES)
def test_reload_is_idempotent(loaded, dataset, layout):
    db, first = loaded[(dataset, layout)]
    again = load(db, fetch(dataset), layout=layout, encoding=DATASETS[dataset].encoding)
    assert again.nodes_by_label == first.nodes_by_label
    assert again.edges_by_type == first.edges_by_type


@pytest.mark.parametrize("dataset,layout", _CASES)
def test_declared_indexes_exist(loaded, dataset, layout):
    db, _ = loaded[(dataset, layout)]
    for spec in pg_indexes() if layout == "pg" else lpg_indexes():
        fields = [idx["fields"] for idx in db.collection(spec.collection).indexes()]
        assert list(spec.fields) in fields, f"{spec.collection} lacks {spec.fields}"


def test_pg_polymorphic_edges_point_into_member_collections(loaded):
    db, _ = loaded[("sf0.003-iso", "pg")]
    q = "FOR e IN is_located_in COLLECT c = PARSE_IDENTIFIER(e._to).collection WITH COUNT INTO n RETURN [c, n]"
    targets = dict(db.aql.execute(q))
    # Person→City 50; Post/Comment→Country 3660; Organisation→Place (city or country).
    assert set(targets) <= {"city", "country"}
    assert sum(targets.values()) == SF0003_EDGES["IS_LOCATED_IN"]


def test_lpg_nodes_carry_supertype_labels(loaded):
    db, _ = loaded[("sf0.003-iso", "lpg")]
    q = "FOR n IN nodes FILTER 'Message' IN n.labels COLLECT t = n.type WITH COUNT INTO c RETURN [t, c]"
    assert dict(db.aql.execute(q)) == {"Post": 3189, "Comment": 471}


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_empty_fields_are_absent_not_empty_strings(loaded, layout):
    db, _ = loaded[("sf0.003-iso", layout)]
    coll, extra = ("comment", "") if layout == "pg" else ("nodes", "FILTER c.type == 'Comment'")
    q = f"FOR c IN {coll} {extra} FILTER HAS(c, 'imageFile') OR c.content == '' COLLECT WITH COUNT INTO n RETURN n"
    assert list(db.aql.execute(q)) == [0]


@pytest.mark.parametrize(
    "dataset,expected_type",
    [("sf0.003-epoch-ms", "number"), ("sf0.003-iso", "string")],
)
def test_timestamps_keep_their_reference_encoding(loaded, dataset, expected_type):
    db, _ = loaded[(dataset, "pg")]
    q = "FOR p IN person LIMIT 1 RETURN [TYPENAME(p.creationDate), TYPENAME(p.birthday), TYPENAME(p.speaks)]"
    assert list(db.aql.execute(q)) == [[expected_type, expected_type, "array"]]
