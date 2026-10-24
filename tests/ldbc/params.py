"""Substitution parameters for the LDBC read operations (WP-34).

LDBC's ``paramgen`` needs factor tables that the SF0.003 samples do not ship,
so for SF ≤ 0.1 the bindings are picked here, against the oracle:

1. Each parameter draws candidates from the loaded graph in a deterministic
   order — the most connected persons, the most tagged tags, the busiest
   countries, quantiles of message creation dates.
2. Each operation walks the product of its candidates (bounded) and runs the
   **verbatim reference query on Neo4j** for each binding, keeping the first
   :data:`WANTED` bindings that return rows and the first one that returns
   none (the empty result is an edge case worth comparing too).

The output, ``params/<scale>.yml``, is committed and reviewed like any fixture;
the execution suites read it and never need Neo4j to choose values. Regenerate
(needs the Neo4j of ``docker-compose.neo4j.yml``)::

    RUN_CROSS=1 .venv/bin/python -m tests.ldbc.params --write

Datetime values (BI) are stored in LDBC's own text format,
``2011-12-01T00:00:00.000+00:00``: ArangoDB receives that string, which orders
like the stored ``creationDate`` strings, and Neo4j a native ``datetime``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tests.ldbc.corpus import LDBC_DIR, Operation, load

logger = logging.getLogger(__name__)

PARAMS_DIR = LDBC_DIR / "params"
WANTED = 3
#: Bindings tried per operation before giving up on finding WANTED non-empty ones.
MAX_TRIES = 400
#: Candidates kept per parameter.
PER_PARAM = 6

_WORKLOAD_DATASET = {"interactive_v2": "sf0.003-epoch-ms", "bi": "sf0.003-iso"}

#: Operations proven to return no rows for any binding at a scale, with how it was proven.
#: Their recorded binding is still compared: the translation must return nothing too.
EMPTY_AT_SCALE: dict[str, dict[str, str]] = {
    "sf0.003": {
        "BI17": "no tag yields a row: all 422 tags carried by a Comment tried with delta=0 on 2026-10-24",
    },
}

# ---------------------------------------------------------------------------
# Candidate sources (Cypher over the oracle graph; column ``v``)
# ---------------------------------------------------------------------------

_PERSON = "MATCH (p:Person)-[:KNOWS]-() WITH p, count(*) AS d RETURN p.id AS v ORDER BY d DESC, v LIMIT $n"
_MESSAGE = (
    "MATCH (m:Message) OPTIONAL MATCH (m)<-[r:REPLY_OF]-() "
    "WITH m, count(r) AS d RETURN m.id AS v ORDER BY d DESC, v LIMIT $n"
)
_COUNTRY = "MATCH (c:Country)<-[:IS_LOCATED_IN]-(:Message) WITH c, count(*) AS n RETURN c.name AS v ORDER BY n DESC, v LIMIT $n"
_TAG = "MATCH (t:Tag)<-[:HAS_TAG]-() WITH t, count(*) AS n RETURN t.name AS v ORDER BY n DESC, v LIMIT $n"
_TAGCLASS = (
    "MATCH (tc:TagClass)<-[:HAS_TYPE]-(:Tag)<-[:HAS_TAG]-() "
    "WITH tc, count(*) AS n RETURN tc.name AS v ORDER BY n DESC, v LIMIT $n"
)
_FIRST_NAME = "MATCH (p:Person) WITH p.firstName AS v, count(*) AS n RETURN v ORDER BY n DESC, v LIMIT $n"
_WORK_FROM = (
    "MATCH ()-[w:WORK_AT]->() WITH w.workFrom AS v, count(*) AS n RETURN v ORDER BY n DESC, v LIMIT $n"
)
_LANGUAGES = "MATCH (p:Post) WHERE p.language IS NOT NULL WITH p.language AS v, count(*) AS n RETURN v ORDER BY n DESC, v LIMIT 4"
_PERSON_PAIR = (
    "MATCH (a:Person)-[:KNOWS*1..3]-(b:Person) WHERE a.id < b.id "
    "WITH DISTINCT a.id AS a, b.id AS b RETURN [a, b] AS v ORDER BY a, b LIMIT $n"
)
_DATES = "MATCH (m:Message) RETURN m.creationDate AS v ORDER BY v"
#: IC3 asks for a friend within two hops, living outside countries X and Y,
#: who posted from both; such triples are rare at SF0.003.
_FRIEND_TWO_COUNTRIES = (
    "MATCH (person:Person)-[:KNOWS*1..2]-(friend:Person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(home:Country) "
    "WHERE person <> friend "
    "MATCH (friend)<-[:HAS_CREATOR]-(:Message)-[:IS_LOCATED_IN]->(cx:Country) "
    "MATCH (friend)<-[:HAS_CREATOR]-(:Message)-[:IS_LOCATED_IN]->(cy:Country) "
    "WHERE cx.name < cy.name AND cx <> home AND cy <> home "
    "WITH person.id AS p, cx.name AS x, cy.name AS y, count(*) AS n "
    "RETURN [p, x, y] AS v ORDER BY n DESC, p, x, y LIMIT $n"
)
#: BI16 asks for one person who posted with tag A on day A and tag B on day B.
_TAG_DAY_PAIRS = (
    "MATCH (p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag) "
    "WITH p, t.name AS tag, date(m.creationDate) AS d ORDER BY tag, d "
    "WITH p, collect(DISTINCT [tag, toString(d)]) AS pairs WHERE size(pairs) >= 2 "
    "RETURN [pairs[0][0], pairs[0][1], pairs[1][0], pairs[1][1]] AS v ORDER BY p.id LIMIT $n"
)

SOURCES: dict[str, str] = {
    "personId": _PERSON,
    "messageId": _MESSAGE,
    "firstName": _FIRST_NAME,
    "countryName": _COUNTRY,
    "countryXName": _COUNTRY,
    "countryYName": _COUNTRY,
    "country": _COUNTRY,
    "country1": _COUNTRY,
    "country2": _COUNTRY,
    "tagName": _TAG,
    "tag": _TAG,
    "tagA": _TAG,
    "tagB": _TAG,
    "tagClassName": _TAGCLASS,
    "tagClass": _TAGCLASS,
    "workFromYear": _WORK_FROM,
}

#: Values copied from the reference queries' ``:params`` examples.
CONSTANTS: dict[str, list[Any]] = {
    "month": [5, 1, 11, 7],
    "lengthThreshold": [20, 40],
    "delta": [4, 12],
    "maxKnowsLimit": [4, 2],
}

#: Parameters that must differ from each other within one binding.
_DISTINCT = (("countryXName", "countryYName"), ("country1", "country2"), ("tagA", "tagB"))

_SINGLE_DATES = ("maxDate", "minDate", "date", "datetime", "dateA", "dateB")
_QUANTILES = (0.5, 0.75, 0.25, 0.9)
_WINDOWS = ((0.4, 0.6), (0.25, 0.75), (0.1, 0.9), (0.6, 0.8))


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DateTimeParam:
    """A BI datetime parameter, kept in LDBC's text format."""

    text: str

    def to_neo4j(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.text)


def _ldbc_datetime(value: dt.datetime) -> str:
    utc = value.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}+00:00"


def to_neo4j(value: Any) -> Any:
    if isinstance(value, DateTimeParam):
        return value.to_neo4j()
    if isinstance(value, list):
        return [to_neo4j(v) for v in value]
    return value


def to_arango(value: Any) -> Any:
    if isinstance(value, DateTimeParam):
        return value.text
    if isinstance(value, list):
        return [to_arango(v) for v in value]
    return value


def _dump_value(value: Any) -> Any:
    if isinstance(value, DateTimeParam):
        return {"datetime": value.text}
    if isinstance(value, list):
        return [_dump_value(v) for v in value]
    return value


def _load_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"datetime"}:
        return DateTimeParam(value["datetime"])
    if isinstance(value, list):
        return [_load_value(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def query_parameters(op: Operation) -> list[str]:
    """``$name`` parameters of *op*, comments excluded, sorted."""
    text = re.sub(r"/\*.*?\*/", "", op.text(), flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", text)))


def _values(session: Any, cypher: str, n: int = PER_PARAM) -> list[Any]:
    return [r["v"] for r in session.run(cypher, n=n)]


def _date_param(raw: Any, workload: str, *, midnight: bool) -> Any:
    if workload == "bi":
        native = raw.to_native() if hasattr(raw, "to_native") else raw
        if midnight:
            native = native.replace(hour=0, minute=0, second=0, microsecond=0)
        return DateTimeParam(_ldbc_datetime(native))
    return int(raw)


def candidates(session: Any, op: Operation) -> list[dict[str, Any]]:
    """The ordered candidate bindings for *op*, before oracle filtering."""
    params = query_parameters(op)
    needs_dates = any(p.endswith("Date") or p in _SINGLE_DATES for p in params)
    dates = [r["v"] for r in session.run(_DATES)] if needs_dates else []

    def quantile(q: float) -> Any:
        return dates[min(len(dates) - 1, int(q * len(dates)))]

    axes: list[tuple[tuple[str, ...], list[tuple[Any, ...]]]] = []
    remaining = list(params)
    if "startDate" in remaining and "endDate" in remaining:
        remaining = [p for p in remaining if p not in ("startDate", "endDate")]
        windows = [
            (
                _date_param(quantile(a), op.workload, midnight=True),
                _date_param(quantile(b), op.workload, midnight=True),
            )
            for a, b in _WINDOWS
        ]
        axes.append((("startDate", "endDate"), windows))
    if {"personId", "countryXName", "countryYName"} <= set(remaining):
        remaining = [p for p in remaining if p not in ("personId", "countryXName", "countryYName")]
        axes.append(
            (
                ("personId", "countryXName", "countryYName"),
                [tuple(v) for v in _values(session, _FRIEND_TWO_COUNTRIES)],
            )
        )
    if {"tagA", "dateA", "tagB", "dateB"} <= set(remaining):
        remaining = [p for p in remaining if p not in ("tagA", "dateA", "tagB", "dateB")]
        quads = [
            (ta, DateTimeParam(f"{da}T00:00:00.000+00:00"), tb, DateTimeParam(f"{db}T00:00:00.000+00:00"))
            for ta, da, tb, db in _values(session, _TAG_DAY_PAIRS)
        ]
        axes.append((("tagA", "dateA", "tagB", "dateB"), quads))
    if "person1Id" in remaining and "person2Id" in remaining:
        remaining = [p for p in remaining if p not in ("person1Id", "person2Id")]
        axes.append((("person1Id", "person2Id"), [tuple(v) for v in _values(session, _PERSON_PAIR)]))
    for p in remaining:
        if p in SOURCES:
            vals = _values(session, SOURCES[p])
        elif p in CONSTANTS:
            vals = CONSTANTS[p]
        elif p in _SINGLE_DATES or p.endswith("Date"):
            vals = [_date_param(quantile(q), op.workload, midnight=op.workload == "bi") for q in _QUANTILES]
        elif p == "languages":
            langs = _values(session, _LANGUAGES)
            vals = [langs[:2], langs[1:3], langs[:1]]
        else:
            raise ValueError(f"{op.id}: no candidate source for parameter ${p}")
        axes.append(((p,), [(v,) for v in vals]))

    out = []
    for combo in itertools.product(*(values for _, values in axes)):
        binding: dict[str, Any] = {}
        for (names, _), values in zip(axes, combo, strict=True):
            binding.update(zip(names, values, strict=True))
        if any(a in binding and b in binding and binding[a] == binding[b] for a, b in _DISTINCT):
            continue
        out.append(binding)
    return out


def pick(session: Any, op: Operation) -> list[dict[str, Any]]:
    """Up to WANTED non-empty bindings, then one empty one, each with its row count."""
    hits: list[dict[str, Any]] = []
    empty: dict[str, Any] | None = None
    for tries, binding in enumerate(candidates(session, op)):
        if tries >= MAX_TRIES or (len(hits) >= WANTED and empty is not None):
            break
        rows = len(list(session.run(op.text(), {k: to_neo4j(v) for k, v in binding.items()})))
        if rows and len(hits) < WANTED:
            hits.append({"binding": binding, "rows": rows})
        elif not rows and empty is None:
            empty = {"binding": binding, "rows": 0}
    if len(hits) < WANTED:
        logger.warning("%s: only %d non-empty binding(s) within %d tries", op.id, len(hits), MAX_TRIES)
    return hits + ([empty] if empty else [])


def read_operations() -> list[Operation]:
    """Reads the oracle can run: not out of subset, not a variant."""
    return [op for op in load() if op.kind == "read" and not op.out_of_subset and not op.variant_of]


def generate(driver: Any, scale: str = "sf0.003") -> dict[str, list[dict[str, Any]]]:
    from tests.ldbc.datasets import DATASETS, fetch
    from tests.ldbc.neo4j_loader import load_neo4j

    out: dict[str, list[dict[str, Any]]] = {}
    for workload in ("interactive_v2", "bi"):
        name = _WORKLOAD_DATASET[workload]
        load_neo4j(driver, fetch(name), encoding=DATASETS[name].encoding)
        with driver.session() as session:
            for op in (o for o in read_operations() if o.workload == workload):
                out[op.id] = pick(session, op)
                logger.info("%s: %s", op.id, [b["rows"] for b in out[op.id]])
    return out


# ---------------------------------------------------------------------------
# File format
# ---------------------------------------------------------------------------


def params_path(scale: str = "sf0.003") -> Path:
    return PARAMS_DIR / f"{scale}.yml"


def write(bindings: dict[str, list[dict[str, Any]]], scale: str = "sf0.003", *, neo4j_version: str) -> None:
    doc = {
        "scale": scale,
        "datasets": dict(_WORKLOAD_DATASET),
        "oracle": neo4j_version,
        "operations": {
            op_id: [
                {"binding": {k: _dump_value(v) for k, v in b["binding"].items()}, "rows": b["rows"]}
                for b in bs
            ]
            for op_id, bs in bindings.items()
        },
    }
    header = (
        "# LDBC read bindings, chosen against the Neo4j oracle — generated by\n"
        "#   RUN_CROSS=1 .venv/bin/python -m tests.ldbc.params --write\n"
        "# `rows` is the oracle's row count for that binding. Do not edit by hand.\n"
    )
    PARAMS_DIR.mkdir(exist_ok=True)
    params_path(scale).write_text(
        header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8"
    )


def read(scale: str = "sf0.003") -> dict[str, list[dict[str, Any]]]:
    doc = yaml.safe_load(params_path(scale).read_text(encoding="utf-8"))
    return {
        op_id: [
            {"binding": {k: _load_value(v) for k, v in b["binding"].items()}, "rows": b["rows"]} for b in bs
        ]
        for op_id, bs in (doc.get("operations") or {}).items()
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate LDBC read bindings against the Neo4j oracle.")
    ap.add_argument(
        "--write", action="store_true", help=f"write {params_path().relative_to(LDBC_DIR.parents[1])}"
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("neo4j").setLevel(logging.WARNING)  # planner notifications are per query
    from tests.integration.neo4j_reference import get_driver

    driver = get_driver()
    try:
        version = driver.get_server_info().agent
        bindings = generate(driver)
    finally:
        driver.close()
    short = {
        op: len([b for b in bs if b["rows"]])
        for op, bs in bindings.items()
        if len([b for b in bs if b["rows"]]) < WANTED
    }
    print(f"{len(bindings)} operations; short of {WANTED} non-empty bindings: {short or 'none'}")
    if args.write:
        write(bindings, neo4j_version=version)
        print(f"wrote {params_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
