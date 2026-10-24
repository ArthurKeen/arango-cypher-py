"""LDBC inserts and deletes, compared by post-state (WP-33 part 3; PRD §12.1 LDBC-4).

A write is checked by what it does to the graph, not by what it returns. Each
write runs **inside a transaction that is rolled back** — a stream transaction
on ArangoDB, an explicit one on Neo4j — and, inside it, the whole graph is
dumped canonically and diffed against the dump taken before. The **delta**
(nodes and edges added and removed) is what Neo4j's delta is recorded as and
ArangoDB's must equal. Rolling back means no reload per write, and a delta is
small enough to review.

Canonical rows are layout-independent: a node is ``[label, props]`` and an edge
``[type, src label, src id, dst label, dst id, props]``, with storage
bookkeeping (``_key``, ``type``, ``labels`` …) and null-valued properties
dropped — Neo4j cannot store a null property, so absent and null are the same.

Write bindings are not taken from LDBC's update streams: at SF0.003 several
deletes target entities only the insert stream creates, so they would be
no-ops on the initial snapshot. Instead each write gets one binding over the
initial snapshot (existing endpoints, fresh ids for new entities), chosen on
the oracle so that it changes the graph.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from tests.ldbc.loader import (
    LABELS_FIELD,
    LPG_EDGES,
    LPG_NODES,
    TYPE_FIELD,
    pg_collection,
    pg_edge_collection,
)
from tests.ldbc.schema import CONCRETE_LABELS, REL_TYPES, SUPERTYPES

#: Fresh ids for created entities: far above any SF0.003 id (max ~4e13).
NEW_ID = 90_000_000_000_000

_SUPERTYPE_NAMES = sorted(SUPERTYPES)
_NODE_BOOKKEEPING = {"_key", "_id", "_rev", TYPE_FIELD, LABELS_FIELD}
_EDGE_BOOKKEEPING = {"_key", "_id", "_rev", "_from", "_to", TYPE_FIELD}

Delta = dict[str, list[str]]


def _canon(row: list[Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def _props(doc: dict[str, Any], drop: set[str]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in drop and v is not None}


# ---------------------------------------------------------------------------
# Dumps
# ---------------------------------------------------------------------------


def dump_neo4j(tx: Any) -> tuple[Counter[str], Counter[str]]:
    """Canonical node and edge rows of the Neo4j graph, as seen by *tx*."""
    from tests.ldbc.oracle import jsonable

    concrete = "[l IN labels(n) WHERE NOT l IN $supers][0]"
    nodes: Counter[str] = Counter()
    for r in tx.run(f"MATCH (n) RETURN {concrete} AS label, properties(n) AS props", supers=_SUPERTYPE_NAMES):
        nodes[_canon([r["label"], _props(jsonable(dict(r["props"])), set())])] += 1
    edges: Counter[str] = Counter()
    q = (
        "MATCH (a)-[e]->(b) RETURN type(e) AS t, "
        "[l IN labels(a) WHERE NOT l IN $supers][0] AS sl, a.id AS si, "
        "[l IN labels(b) WHERE NOT l IN $supers][0] AS dl, b.id AS di, properties(e) AS props"
    )
    for r in tx.run(q, supers=_SUPERTYPE_NAMES):
        props = _props(jsonable(dict(r["props"])), set())
        edges[_canon([r["t"], r["sl"], r["si"], r["dl"], r["di"], props])] += 1
    return nodes, edges


def dump_arango(db: Any, layout: str) -> tuple[Counter[str], Counter[str]]:
    """Canonical node and edge rows of a loaded layout, as seen by *db* (a database or a transaction)."""
    nodes: Counter[str] = Counter()
    edges: Counter[str] = Counter()
    if layout == "pg":
        label_of = {pg_collection(lbl): lbl for lbl in CONCRETE_LABELS}
        for coll, label in label_of.items():
            for doc in db.aql.execute("FOR d IN @@c RETURN d", bind_vars={"@c": coll}):
                nodes[_canon([label, _props(doc, _NODE_BOOKKEEPING - {TYPE_FIELD, LABELS_FIELD})])] += 1
        q = (
            "FOR e IN @@c LET s = DOCUMENT(e._from) LET d = DOCUMENT(e._to) "
            "RETURN [PARSE_IDENTIFIER(e._from).collection, s.id, PARSE_IDENTIFIER(e._to).collection, d.id, e]"
        )
        for rel_type in REL_TYPES:
            for sc, si, dc, di, e in db.aql.execute(q, bind_vars={"@c": pg_edge_collection(rel_type)}):
                row = [
                    rel_type,
                    label_of.get(sc, sc),
                    si,
                    label_of.get(dc, dc),
                    di,
                    _props(e, _EDGE_BOOKKEEPING),
                ]
                edges[_canon(row)] += 1
    else:
        for doc in db.aql.execute("FOR d IN @@c RETURN d", bind_vars={"@c": LPG_NODES}):
            nodes[_canon([doc.get(TYPE_FIELD), _props(doc, _NODE_BOOKKEEPING)])] += 1
        q = (
            "FOR e IN @@c LET s = DOCUMENT(e._from) LET d = DOCUMENT(e._to) "
            "RETURN [e[@t], s[@t], s.id, d[@t], d.id, e]"
        )
        for t, sl, si, dl, di, e in db.aql.execute(q, bind_vars={"@c": LPG_EDGES, "t": TYPE_FIELD}):
            edges[_canon([t, sl, si, dl, di, _props(e, _EDGE_BOOKKEEPING)])] += 1
    return nodes, edges


def delta(before: tuple[Counter[str], Counter[str]], after: tuple[Counter[str], Counter[str]]) -> Delta:
    """Sorted canonical rows added and removed, for nodes and edges."""
    (nb, eb), (na, ea) = before, after
    return {
        "nodes_added": sorted((na - nb).elements()),
        "nodes_removed": sorted((nb - na).elements()),
        "edges_added": sorted((ea - eb).elements()),
        "edges_removed": sorted((eb - ea).elements()),
    }


def delta_difference(expected: Delta, actual: Delta) -> str | None:
    """The first way *actual* differs from *expected*, or ``None``."""
    for key in ("nodes_added", "nodes_removed", "edges_added", "edges_removed"):
        exp, act = Counter(expected.get(key, [])), Counter(actual.get(key, []))
        missing, extra = sorted((exp - act).elements()), sorted((act - exp).elements())
        if missing or extra:
            sample = f"missing {missing[:1]}" if missing else f"unexpected {extra[:1]}"
            return f"{key}: {len(missing)} missing, {len(extra)} unexpected; {sample}"
    return None


# ---------------------------------------------------------------------------
# Running a write without keeping it
# ---------------------------------------------------------------------------


def neo4j_write_delta(driver: Any, cypher: str, params: dict[str, Any], before: Any) -> Delta:
    with driver.session() as session:
        tx = session.begin_transaction()
        try:
            tx.run(cypher, params).consume()
            return delta(before, dump_neo4j(tx))
        finally:
            tx.rollback()


def arango_write_delta(db: Any, layout: str, aql: str, bind_vars: dict[str, Any], before: Any) -> Delta:
    from tests.ldbc.loader import _collections

    vertex, edge = _collections(layout)  # type: ignore[arg-type]
    tx = db.begin_transaction(read=vertex + edge, write=vertex + edge)
    try:
        list(tx.aql.execute(aql, bind_vars=bind_vars))
        return delta(before, dump_arango(tx, layout))
    finally:
        tx.abort_transaction()


# ---------------------------------------------------------------------------
# Choosing write bindings on the oracle
# ---------------------------------------------------------------------------


def _one(session: Any, cypher: str, **params: Any) -> list[Any]:
    rec = session.run(cypher, **params).single()
    if rec is None:
        raise RuntimeError(f"no candidate for a write binding: {cypher}")
    return list(rec.values())


def write_bindings(session: Any) -> dict[str, dict[str, Any]]:
    """One binding per insert/delete operation over the initial snapshot."""
    (t,) = _one(session, "MATCH (m:Message) RETURN max(m.creationDate) + 1000 AS t")
    (person,) = _one(
        session,
        "MATCH (p:Person)-[:KNOWS]-() WITH p, count(*) AS d RETURN p.id ORDER BY d DESC, p.id LIMIT 1",
    )
    tag_ids = [
        r["id"]
        for r in session.run(
            "MATCH (t:Tag)<-[:HAS_TAG]-() WITH t, count(*) AS n RETURN t.id AS id ORDER BY n DESC, id LIMIT 2"
        )
    ]
    (city,) = _one(session, "MATCH (c:City)<-[:IS_LOCATED_IN]-(:Person) RETURN c.id ORDER BY c.id LIMIT 1")
    (country,) = _one(
        session, "MATCH (c:Country)<-[:IS_LOCATED_IN]-(:Message) RETURN c.id ORDER BY c.id LIMIT 1"
    )
    (uni,) = _one(session, "MATCH (u:University) RETURN u.id ORDER BY u.id LIMIT 1")
    (company,) = _one(session, "MATCH (c:Company) RETURN c.id ORDER BY c.id LIMIT 1")
    (forum,) = _one(session, "MATCH (f:Forum)-[:CONTAINER_OF]->() RETURN f.id ORDER BY f.id LIMIT 1")
    (post,) = _one(session, "MATCH (p:Post) RETURN p.id ORDER BY p.id LIMIT 1")
    ins1 = {
        "personId": NEW_ID + 1,
        "personFirstName": "Ada",
        "personLastName": "Lovelace",
        "gender": "female",
        "birthday": 315532800000,
        "creationDate": t,
        "locationIP": "10.0.0.1",
        "browserUsed": "Firefox",
        "languages": ["en", "fr"],
        "emails": ["ada@example.org"],
        "cityId": city,
        "tagIds": tag_ids,
        "studyAt": [[uni, 2000]],
        "workAt": [[company, 2005]],
    }
    not_liked_post = _one(
        session,
        "MATCH (p:Person), (m:Post) WHERE NOT (p)-[:LIKES]->(m) RETURN p.id, m.id ORDER BY p.id, m.id LIMIT 1",
    )
    not_liked_comment = _one(
        session,
        "MATCH (p:Person), (m:Comment) WHERE NOT (p)-[:LIKES]->(m) RETURN p.id, m.id ORDER BY p.id, m.id LIMIT 1",
    )
    non_member = _one(
        session,
        "MATCH (f:Forum), (p:Person) WHERE NOT (f)-[:HAS_MEMBER]->(p) RETURN f.id, p.id ORDER BY f.id, p.id LIMIT 1",
    )
    strangers = _one(
        session,
        "MATCH (a:Person), (b:Person) WHERE a.id < b.id AND NOT (a)-[:KNOWS]-(b) "
        "RETURN a.id, b.id ORDER BY a.id, b.id LIMIT 1",
    )
    liked_post = _one(
        session, "MATCH (p:Person)-[:LIKES]->(m:Post) RETURN p.id, m.id ORDER BY p.id, m.id LIMIT 1"
    )
    liked_comment = _one(
        session, "MATCH (p:Person)-[:LIKES]->(m:Comment) RETURN p.id, m.id ORDER BY p.id, m.id LIMIT 1"
    )
    member = _one(
        session, "MATCH (f:Forum)-[:HAS_MEMBER]->(p:Person) RETURN f.id, p.id ORDER BY f.id, p.id LIMIT 1"
    )
    knows = _one(
        session, "MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN a.id, b.id ORDER BY a.id, b.id LIMIT 1"
    )
    # Deletes cascade over replies; the smallest non-trivial target keeps the delta reviewable.
    (small_person,) = _one(
        session,
        "MATCH (p:Person)<-[:HAS_CREATOR]-(m:Message) WITH p, count(m) AS n RETURN p.id ORDER BY n, p.id LIMIT 1",
    )
    (small_forum,) = _one(
        session,
        "MATCH (f:Forum)-[:CONTAINER_OF]->(:Post) WITH f, count(*) AS n RETURN f.id ORDER BY n, f.id LIMIT 1",
    )
    (replied_post,) = _one(
        session,
        "MATCH (p:Post)<-[:REPLY_OF]-(:Comment) WITH p, count(*) AS n RETURN p.id ORDER BY n, p.id LIMIT 1",
    )
    (comment,) = _one(session, "MATCH (c:Comment) RETURN c.id ORDER BY c.id LIMIT 1")

    return {
        "INS1": ins1,
        "INS1~schema-fix": ins1,
        "INS2": {"personId": not_liked_post[0], "postId": not_liked_post[1], "creationDate": t},
        "INS3": {"personId": not_liked_comment[0], "commentId": not_liked_comment[1], "creationDate": t},
        "INS4": {
            "forumId": NEW_ID + 4,
            "forumTitle": "Group for LDBC",
            "creationDate": t,
            "moderatorPersonId": person,
            "tagIds": tag_ids,
        },
        "INS5": {"forumId": non_member[0], "personId": non_member[1], "creationDate": t},
        "INS6": {
            "postId": NEW_ID + 6,
            "imageFile": "",
            "creationDate": t,
            "locationIP": "10.0.0.6",
            "browserUsed": "Chrome",
            "language": "en",
            "content": "An LDBC post",
            "length": 12,
            "authorPersonId": person,
            "forumId": forum,
            "countryId": country,
            "tagIds": tag_ids,
        },
        "INS7": {
            "commentId": NEW_ID + 7,
            "creationDate": t,
            "locationIP": "10.0.0.7",
            "browserUsed": "Safari",
            "content": "A reply",
            "length": 7,
            "authorPersonId": person,
            "countryId": country,
            "replyToPostId": post,
            "replyToCommentId": 0,
            "tagIds": tag_ids,
        },
        "INS8": {"person1Id": strangers[0], "person2Id": strangers[1], "creationDate": t},
        "DEL1": {"personId": small_person},
        "DEL2": {"personId": liked_post[0], "postId": liked_post[1]},
        "DEL3": {"personId": liked_comment[0], "commentId": liked_comment[1]},
        "DEL4": {"forumId": small_forum},
        "DEL5": {"forumId": member[0], "personId": member[1]},
        "DEL6": {"postId": replied_post},
        "DEL7": {"commentId": comment},
        "DEL8": {"person1Id": knows[0], "person2Id": knows[1]},
    }
