# LDBC SNB Benchmark — Research & Implementation Plan

**Status:** Proposed — awaiting maintainer review
**Date:** 2026-10-23
**Implements:** [`PRD.md`](./PRD.md) §12.1 (LDBC SNB benchmark)
**Work packages:** WP-31 … WP-44 (continues the numbering in [`implementation_plan.md`](./implementation_plan.md))

> These are not LDBC Benchmark Results. This plan uses the LDBC Social Network
> Benchmark (SNB) schema, data generator output, and reference queries as a
> validation and performance corpus. It is not an implementation of an LDBC
> Benchmark, and no number it produces is an audited result. (See §2.6.)

---

## 1. Why

Today the transpiler has three kinds of evidence. Golden fixtures pin exact AQL.
The openCypher TCK measures language coverage over tiny, synthetic graphs. Neo4j
cross-validation compares results on Movies (~170 nodes) and Northwind. None of the
three exercises **realistic multi-hop analytical Cypher written by someone else**
against a **non-trivial graph**, in **both physical layouts**, at **sizes where
performance is measurable**.

LDBC SNB fills that gap. It has:

- An industry-standard schema with label hierarchies (`Message` ⊇ `Post`,
  `Comment`), polymorphic edges, and multi-valued properties.
- 57 reference Cypher queries and update operations written for Neo4j by the LDBC
  task force, Apache-2.0 licensed.
- A deterministic data generator with scale factors from 0.003 (2.4 MB) to 30,000.
- Reference implementations in Cypher *and* SQL, which give us an independent
  oracle for correctness.

The benchmark therefore serves three goals the user stated: **validate** the
transpiler, **detect and fix** defects (especially silent ones), and **measure
performance** across PG and LPG.

---

## 2. Research summary

### 2.1 The LDBC benchmark family

| Benchmark | What it is | Query language of the reference | Relevance here |
| --- | --- | --- | --- |
| **SNB Interactive v2** (2023/24) | Transactional workload: 14 complex reads (IC1–IC14), 7 short reads (IS1–IS7), 8 inserts (INS1–INS8), 8 *deep* deletes (DEL1–DEL8). SF up to 1,000. | Cypher (Neo4j 5.19), SQL (PostgreSQL, Umbra, MSSQL) | **Primary.** Multi-hop navigational reads plus writes. |
| **SNB Business Intelligence (BI)** | Analytical workload: BI1–BI20, plus daily insert/delete micro-batches. | Cypher (Neo4j 5.20), SQL (Umbra), GSQL (TigerGraph) | **Secondary.** Heavy aggregation; needs temporal functions and `CALL {}`. |
| SNB Interactive v1 | Predecessor of v2 (no deletes, SF ≤ 1,000). Audits still use it. | Cypher, SQL, … | Superseded for our purpose. The IC queries are near-identical to v2. |
| FinBench | Financial-transaction workload (2023+), different schema. | Cypher (Neo4j, Galaxybase, TuGraph) | Future candidate (§9). |
| Graphalytics, SPB | Graph algorithms; RDF/SPARQL publishing workload. | — / SPARQL | Out of scope (SPB may interest `arango-sparql-py`). |

Reference repositories examined (cloned 2026-10-23):

| Repo | Commit | Cypher files |
| --- | --- | --- |
| `ldbc/ldbc_snb_interactive_v2_impls` | `d05cc76` (2026-04-06) | `cypher/queries/*.cypher` (37) |
| `ldbc/ldbc_snb_bi` | `47dd38b` (2026-04-06) | `neo4j/queries/*.cypher` (20 + 6 GDS helpers) |
| `ldbc/ldbc_snb_interactive_v1_impls` | `11db98c` (2026-03-26) | `cypher/queries/*.cypher` (29) |
| `ldbc/ldbc_finbench_transaction_impls` | `5a4f9d7` (2025-09-23) | `neo4j/queries/*.cypher` (36) |

### 2.2 Schema

- **Concrete node labels (11):** `Person`, `Forum`, `Post`, `Comment`, `Tag`,
  `TagClass`, `City`, `Country`, `Continent`, `University`, `Company`.
- **Supertype labels (3):** `Message` (`Post` | `Comment`); `Place` (`City` |
  `Country` | `Continent`); `Organisation` (`University` | `Company`). The Neo4j
  reference loads a comment as `:Message:Comment`, and the queries match on
  either label.
- **Relationship types (15):** `KNOWS`, `HAS_CREATOR`, `REPLY_OF`,
  `CONTAINER_OF`, `HAS_MEMBER`, `HAS_MODERATOR`, `HAS_TAG`, `HAS_INTEREST`,
  `HAS_TYPE`, `IS_SUBCLASS_OF`, `IS_LOCATED_IN`, `IS_PART_OF`, `STUDY_AT`,
  `WORK_AT`, `LIKES`. Several are **polymorphic**:
  - `IS_LOCATED_IN` connects Person→City, Message→Country and Organisation→Place.
  - `HAS_TAG` connects Message and Forum to Tag.
  - `REPLY_OF` connects Comment to Message.
- **Properties:** `id` (int64, unique per label, and unique across a supertype's
  members) and timestamps (`creationDate`, `birthday`).
  - `KNOWS.creationDate`, `STUDY_AT.classYear` and `WORK_AT.workFrom` are edge
    properties.
  - `speaks` and `email` are string arrays.

### 2.3 Data

- **Generator:** `ldbc_snb_datagen_spark`. The Neo4j references consume the
  `composite-projected-fk` CSV layout: pipe-delimited, gzip, one directory per
  entity or edge type, with `initial_snapshot/`, `inserts/` and `deletes/`.
- **Timestamp encoding differs per workload, and it matters:**
  - **Interactive v2 Neo4j:** timestamps are **epoch-milliseconds `LONG`**
    (`creationDate:LONG`). The queries rely on this. IC7 subtracts timestamps
    arithmetically, and IC10 calls `datetime({epochMillis: …}).month`.
  - **BI Neo4j:** timestamps are native **`DATETIME` / `DATE`**. The queries use
    `datetime('…')` parameters, `.year`/`.month` accessors and `duration()`.
- **Sample data sets (published by LDBC, verified reachable 2026-10-23):**
  - `social-network-sf0.003-bi-composite-projected-fk-neo4j-compressed-epoch-millis.zip`
    (2.4 MB, used by the Interactive reference CI)
  - `social-network-sf0.003-bi-composite-projected-fk-neo4j-compressed.zip`
    (2.4 MB, ISO-8601 timestamps, used by the BI reference CI)
- **SF0.003 contents (counted):**
  - Nodes: 50 Person, 381 Forum, 3,189 Post, 471 Comment, 16,080 Tag,
    71 TagClass, 1,460 Place, 7,955 Organisation.
  - Edges: about 37 k in total, 83 of them `KNOWS`.
  - The sample includes `inserts/` and `deletes/` streams.
- **Larger SFs:** SF1 is 318 MB compressed (`bi-sf1-composite-projected-fk.tar.zst`
  on `datasets.ldbcouncil.org`). SF10 to SF10,000 are published as pre-generated
  sets. Substitution parameters for SF ≥ 1 come from LDBC's `paramgen`, run over
  the published factor tables.

### 2.4 Correctness oracles LDBC provides

- **Interactive driver (Java):** has `create-validation-parameters` and
  `validate` modes, which produce and check a `validation_params.json` of
  expected results. LDBC does not publish these files. They are generated by
  running a reference implementation.
- **BI repository:** cross-validates two systems by running both in `--validate`
  mode and diffing their outputs (`scripts/cross-validate.sh`).

Either way, **the oracle is a reference system we run ourselves.** We already
operate Neo4j for cross-validation (`docker-compose.neo4j.yml`), so Neo4j
running the *verbatim* reference Cypher is the natural oracle (§4, D4).

### 2.5 Cypher features the reference queries use

Pattern-matched across the 57 operations (37 Interactive v2 + 20 BI):

| Feature | Queries |
| --- | --- |
| Supertype labels `:Message` / `:Place` / `:Organisation` | IC2 IC7 IC8 IC9 IC14 IS4–IS7 INS6 INS7 DEL1 DEL4 DEL6 BI1–BI10 BI12–BI17 (30) |
| `shortestPath` / `allShortestPaths` | IC1 IC13 IC14 BI15 BI19 BI20 |
| Temporal (`datetime()`, `date()`, `duration()`, `.year`, `.month`) | IC10 BI1 BI2 BI13 BI15 BI16 BI17 |
| `CALL { … }` subqueries (with `UNION` inside) | BI4 BI15 BI16 |
| Neo4j GDS / APOC procedures | IC14 BI10 BI15 BI19 BI20 |
| Multi-type hop `[:A\|B*0..]` | IC12 |
| Aggregate nested in an expression (`head(collect(…))`) | IC7 BI8 |
| Variable-length `*0..`, `*1..2`, unbounded `*` | IS2 IS6 IC3 IC5 IC6 IC9 IC11 IC12 DEL1 DEL4 DEL6 DEL7 BI3 BI4 BI9 BI12 BI15 BI17 … |
| Write pipelines (`MATCH … CREATE … WITH … UNWIND … MATCH … CREATE`) | INS1 INS4 INS6 INS7 |
| `OPTIONAL MATCH`-bound `DETACH DELETE` | DEL1 DEL4 |

### 2.6 Fair-use constraints (LDBC policy)

- "LDBC BENCHMARK" is a registered trademark. Only audited results inside an
  official results set may be called **"LDBC Benchmark Results"**.
- Any number we publish MUST carry a disclaimer, such as "These are not LDBC
  Benchmark Results", and attribute the source (CC-BY 4.0 style).
- We MUST NOT describe the harness as "an implementation of the LDBC SNB
  benchmark". It does not run the LDBC driver's scheduled, time-compressed
  workload, and it does not satisfy the audit rules.
- The reference queries are Apache-2.0. If we vendor them, they keep their
  license header, and our `NOTICE` names the upstream repository and the pinned
  commit.

---

## 3. Baseline probe (2026-10-23, no code changes)

Every reference query was translated with `arango_cypher.translate()` against two
provisional mappings built from the schema above:

- **PG:** one collection per concrete label, plus one `DEDICATED_COLLECTION` per
  relationship type.
- **LPG:** `nodes` with `type`, plus `edges` with `type`.

The probe script lives outside the repo. WP-32 makes it a committed, ratcheted
test.

### 3.1 Translation outcome (57 benchmark operations)

| Outcome | PG | LPG |
| --- | --- | --- |
| Translates | 28 | 30 |
| `UNSUPPORTED` (fail-closed) | 21 | 19 |
| `CYPHER_SYNTAX_ERROR` (grammar) | 8 | 8 |

### 3.2 "Translates" is not "correct": silent-wrong findings

These are the most valuable findings, and they are why execution against an
oracle is mandatory (§4, D4):

1. **Supertype labels translate to wrong AQL in both layouts.**
   - **PG:** `MATCH (m:Message)` scans a `Message` collection, which a
     per-concrete-type layout does not have. On a correct LDBC PG load the
     query returns zero rows, or fails.
   - **LPG:** it emits `FILTER m.type == "Message"`, but a comment's `type` is
     `Comment`, so it matches nothing. `m:Comment` then compares the same
     `type` field, so `MATCH (m:Message) RETURN m:Comment` cannot be true for
     any row.
   - **Scale:** of the queries that "translate", **15 (PG) and 16 (LPG)
     reference a supertype label.** At most **13 (PG) and 14 (LPG)** can be
     correct before execution evidence.
2. **Temporal accessors on stored timestamps silently return `null`.**
   `m.creationDate.year` becomes `m.creationDate.year`, which is attribute
   access on a number or string. BI1 and BI13 "translate" this way.

Both violate the PRD's determinism and safety intent (§1.1): translation should
fail closed rather than emit plausible-looking wrong AQL. WP-35 and WP-41 fix
them. Until those land, the ledger records them as `translates_wrong`, not
`translatable`.

### 3.3 Root causes of the explicit failures

| Gap | Error today | Queries blocked (first failure) | Raise site |
| --- | --- | --- | --- |
| Comma-separated pattern parts in a `MATCH` after `WITH` | `Only a single pattern part is supported in v0` | IS2 IC3 IC4 IC6 IC10 BI10 BI11 BI14 | `_translate_v0/core.py:2314`, `:2529` |
| `shortestPath()` / `allShortestPaths()` syntax | `CYPHER_SYNTAX_ERROR` | IC1 IC13 IC14 | grammar |
| `CALL { … }` subquery syntax | `CYPHER_SYNTAX_ERROR` | BI4 BI16 (BI15) | grammar |
| GDS procedures (`CALL gds.…` with dotted `YIELD`) | `CYPHER_SYNTAX_ERROR` / `UNSUPPORTED` | IC14 BI15 BI19 BI20 | grammar / procedure registry |
| Multi-type hop `[:A\|B]` | `Exactly one relationship type is required` | IC12 | `core.py:4016` |
| Aggregate inside an expression | `Unsupported function in v0: collect` | IC7 BI8 | `core.py:5316` |
| Updating clause after `WITH … UNWIND … MATCH` | `Updating clauses are not supported in v0` | INS1 INS4 INS6 INS7 | `core.py:1378`, `:1421` |
| `DETACH DELETE` of `OPTIONAL MATCH` variables | `DELETE targets 'x', which is not bound by the first MATCH pattern` | DEL1 DEL4 | `writes.py` |
| Unlabeled start node with an ambiguous PG domain | `A single label is required` | IC5 IC9 (PG only) BI2 | `core.py:3796` |
| `datetime()` / `duration()` / `date()` | `Unsupported function in v0: datetime` | IC10 BI2 BI17 … | `core.py:5316` |

Constructs that **already translate** and need only execution evidence:

- unbounded and zero-hop variable length (`*`, `*0..`)
- pattern predicates in `WHERE NOT (…)` and as values in `RETURN`
- label-predicate expressions (`m:Comment AS isComment`)
- list comprehension over `nodes(path)`
- disconnected comma patterns in the *first* `MATCH`

### 3.4 Upstream reference quirks (to report to LDBC, not to fix here)

**INS1** (`interactive-update-1.cypher`) creates
`[:WORKS_AT]` (the schema says `WORK_AT`) and a property `languages` (the schema
says `speaks`). Neo4j accepts both silently. A mapping-driven transpiler rejects
the first (`MAPPING_NOT_FOUND`), which is *correct* behaviour. We keep the
verbatim file, add a corrected variant, and file an issue upstream (WP-32).

---

## 4. Scope decisions (recommendations for review)

| # | Decision | Recommendation | Alternatives |
| --- | --- | --- | --- |
| D1 | Workloads | **Interactive v2 (all 37 ops) first, then BI (20).** Interactive v1 is skipped. | BI first (more aggregation coverage, but blocked on temporal and `CALL {}`). |
| D2 | Physical layouts | **PG and LPG, both mandatory** (as requested). Hybrid is optional, in Phase 3 (§9). | Add hybrid now. It mirrors LDBC's CSV split: `Place`/`Organisation` as typed collections, `Post`/`Comment` separate. |
| D3 | Query text | **Vendor the reference Cypher verbatim**, pinned to the upstream commit. Never edit it. Adaptations (GDS replacements, upstream-bug corrections) go in separate `variants/` files with a stated reason. The ledger reports verbatim and variant outcomes separately. | Rewrite into a portable subset. Rejected: it hides exactly the gaps we want to find. |
| D4 | Oracle | **Neo4j 5.x, pinned to the reference version (5.20)**, runs the verbatim queries on the same data and parameters. Results are **recorded as committed snapshots** for SF0.003, so the integration tier needs no Neo4j. A `RUN_CROSS` job re-verifies them against live Neo4j. | (a) LDBC driver validation mode: Java, heavy, and still needs a reference run. (b) PostgreSQL reference: an independent second oracle, worth adding for disputed cases (§9). |
| D5 | Timestamp encoding | **Load each workload in its reference encoding.** Interactive uses epoch-ms numbers; BI uses ISO-8601 strings, declared temporal in the mapping. Neo4j gets the same encoding the reference expects. | One encoding for both. Rejected: the Interactive queries do arithmetic on timestamps and the BI queries use temporal accessors. |
| D6 | Data in repo | **Download on demand, verified against pinned content digests** (SHA-256 over each entity's sorted, decompressed rows; LDBC republishes the archives with new part-file names, so archive hashes are not stable), cached under `~/.cache/arango-cypher/ldbc/` and `actions/cache` in CI. Only mappings, parameters and snapshots are committed. | Vendor the 2.4 MB sample. Smaller CI surface, but it is a generated artifact with its own provenance. |
| D7 | Scale factors | **SF0.003 in CI** (integration tier). **SF0.1 nightly and SF1 manual** for performance. SF10 runs ad hoc on dedicated hardware. | SF0.1 in CI (~30 MB; slower to load). |
| D8 | Supertype modeling (the biggest design choice) | Extend the **conceptual** schema with `supertypes` / `subtypeOf` **in arango-query-core** (fix upstream, don't fork). Physically: **LPG** uses a `labelsField` array (`"Message" IN n.labels`) with an array index. **PG** treats the supertype as the union of its members' collections: an `IS_SAME_COLLECTION` any-of filter on traversal targets, and a branch union for scan starts. The benchmark then *measures* what that union costs. | (a) Require a hybrid layout for supertypes (a `message` collection plus a type field), which dodges the PG question. (b) Expand supertype queries into `UNION ALL` at the Cypher level. |
| D9 | GDS / APOC queries (IC14 BI10 BI15 BI19 BI20) | **Report as out-of-subset verbatim.** Add `arango` dialect variants (weighted shortest path via `K_SHORTEST_PATHS`/`arango.shortest_path`) with *results* checked against a pure-Cypher or SQL reference where feasible. | Exclude entirely. |
| D10 | Performance comparison | Report per query × layout × SF: translation time, plus Arango execution p50/p95/p99. Neo4j on identical container limits is **indicative only** and carries the disclaimer. A hand-written AQL baseline for a subset measures the "transpilation tax" (WP-44). | Arango only. |

---

## 5. Architecture

### 5.1 Layout (mirrors the `tests/tck/` precedent)

```
tests/ldbc/
  UPSTREAM.md              # repo URLs, pinned commits, license, refresh procedure
  NOTICE                   # Apache-2.0 attribution for vendored queries
  queries/
    interactive_v2/        # verbatim *.cypher (37)
    bi/                    # verbatim *.cypher (20)
    variants/              # adapted copies: <op>.<reason>.cypher, each with a header comment
  manifest.yml             # op id → file, workload, writes?, oracle mode, ordering contract
  schema.py                # LDBC labels, supertypes, CSV files and column orders (single source)
  datasets.py              # fetch + content-digest verification + cache
  loader.py                # LDBC CSV → Arango PG | LPG (the Neo4j loader is WP-33)
  params/
    generate.py            # deterministic picker for SF ≤ 0.1
    sf0.003/<op>.yml       # committed bindings (several per op)
  expected/sf0.003/<layout-independent>/<op>/<binding>.json   # Neo4j snapshots
  ledger.py                # computes outcomes → conformance_ledger.tsv
  conformance_ledger.tsv   # op × layout × {parse, translate, execute, oracle}
  test_ldbc_*_unit.py      # default suite, no DB (loader, ratchet, …)
  bench.py                 # perf runner CLI: python -m tests.ldbc.bench --sf 0.1 --layout pg,lpg
  report.py                # results JSON → docs/ldbc_results.md (with disclaimer)
tests/integration/        # DB-backed suites live here, so they inherit the
  test_ldbc_load.py        #   local-database guard in tests/integration/conftest.py
  test_ldbc_execution.py   # RUN_INTEGRATION: execute + compare to snapshots
  test_ldbc_crossvalidate.py  # RUN_CROSS: live Neo4j re-verification
tests/fixtures/mappings/
  ldbc_pg.export.json
  ldbc_lpg.export.json
```

### 5.2 The outcome model

This extends the TCK's four-outcome contract (`cypher_capability_matrix.md`).
For each operation × layout:

1. `parses`
2. `translates`
3. `executes`
4. `matches_oracle`

There are three non-passing terminal states. Each must state its reason:

- `fails_closed` (an explicit `UNSUPPORTED`, the acceptable failure)
- `translates_wrong` (an oracle mismatch, which is a **defect**)
- `out_of_subset` (GDS or APOC)

As in the TCK, the ratchet fails CI if any cell **leaves** a passing state or an
improvement goes unrecorded. A `translates_wrong` cell can never be grandfathered
as passing.

### 5.3 Result comparison

- Reuse the normalisation in `tests/integration/test_movies_crossvalidate.py`:
  int/float folding, null-versus-missing, and recursive normalisation. Lift it
  into a shared helper rather than copying it.
- **Ordering contract.** Every LDBC read ends in `ORDER BY … LIMIT` with
  tie-breakers. Compare *ordered* when the manifest marks the ordering total.
  Otherwise compare as a multiset: the canonical-row SHA-256 multiset signature
  pattern, plus a diff.
- Nodes are never returned whole by LDBC reads (they project properties), so no
  `_id`/`_key` normalisation is needed. Write operations are compared by their
  **post-state**: a fixed set of read-back queries run on both systems after the
  operation.

### 5.4 Loader

- **PG.** One vertex collection per concrete label (`person`, `post`,
  `comment`, …) and one edge collection per relationship type.
  - `_key` is `str(id)`.
  - `_from`/`_to` resolve to the concrete member collection.
  - Polymorphic endpoints come from the CSV file name, e.g.
    `Comment_replyOf_Post`.
- **LPG.** All nodes go in `nodes`; all edges go in `edges`.
  - Each node gets `type` (its concrete label) and `labels` (all of its labels,
    e.g. `["Message", "Comment"]`).
  - `_key` is `"<Type>_<id>"`, because ids collide across labels.
  - Each edge gets `type`.
- **Indexes (both layouts)** mirror the reference DDL:
  - Unique `id` per label: for PG, a unique index per collection; for LPG, a
    persistent index on `[type, id]`.
  - `firstName`, `name` and `creationDate`.
  - LPG also gets edge VCIs on `[_from, type]` and `[_to, type]` (the
    translator's traversal hint picks the one led by the traversal's anchor),
    a `labels[*]` array index, and type-led `[type, …]` node indexes.
- **Arrays.** `speaks` and `email` are split on `;` into arrays.
- **Empty fields** are omitted, not stored as `""`, matching the reference
  import's `--ignore-empty-strings=true`, so `coalesce(m.imageFile, m.content)`
  behaves identically on both engines.
- **Endpoints.** A `Place` or `Organisation` endpoint is resolved to its
  concrete label per row. A dangling endpoint, in the CSV or in the loaded
  database, fails the load.
- **Integrity.** The loader is idempotent, reports row counts per
  entity, and asserts that **PG count == LPG count == CSV rows** for every
  entity. A mismatch fails loudly; nothing is dropped silently.
- **Bulk path.** `import_bulk` in batches. `arangoimport` is optional for
  SF ≥ 1.
- **Neo4j (WP-33).** Batched `UNWIND` for SF ≤ 0.1, mirroring the reference's labels
  (`:Message:Comment`). `neo4j-admin import`, with the reference's headers, for
  SF ≥ 1.

### 5.5 Mapping acquisition is also under test

PRD §12 requires live-database coverage for §7. After loading, the harness runs
`acquire_mapping` against both layouts and **diffs the acquired bundle against
the committed fixture.** The committed fixture is the contract. A difference is
either an analyzer defect (filed upstream against `arangodb-schema-analyzer`) or
a fixture error. The benchmark therefore also measures whether the analyzer can
detect LDBC's label hierarchy, which today it cannot.

---

## 6. Work packages

Sizes: **S** ≤ 2 days · **M** ≤ 1 week · **L** 1–2 weeks. ⚡ marks critical-path
packages. Each WP ships as one to three PRs of five files or fewer, each green
on its own (incremental-over-atomic).

### Phase 0 — Harness and baseline (no transpiler changes)

#### WP-31: Data sets and loader ⚡ (M)
- **Scope:**
  - `tests/ldbc/schema.py`: labels, supertypes, files and column orders.
  - `tests/ldbc/datasets.py`: SF0.003 sample URLs (both encodings),
    content-digest pins and cache.
  - `tests/ldbc/loader.py`: PG and LPG loaders. The Neo4j loader moved to
    WP-33, which owns the oracle.
- **Tests:**
  - Unit tests for CSV parsing: quoting, `;` arrays, both timestamp encodings,
    and polymorphic endpoint resolution.
  - An integration test that loads SF0.003 into both layouts and asserts the
    per-entity counts from §2.3.
  - An idempotence test (load twice, same counts).
- **Acceptance:** `RUN_INTEGRATION=1 pytest tests/integration/test_ldbc_load.py`
  is green. Load time for SF0.003 is recorded.
- **Status (2026-10-24): implemented.**
  - 48 unit tests in `tests/ldbc/test_ldbc_loader_unit.py`; 18 integration
    tests in `tests/integration/test_ldbc_load.py`.
  - SF0.003 loads in about 2–3 s per layout (local Arango 3.11, Apple
    silicon).
  - A mutation check (dropping one document) is caught by the count guard in
    both layouts.
  - CI caches the samples (`actions/cache`, keyed on the catalog).

#### WP-32: Corpus, mappings and translation ledger ⚡ (M)
- **Scope:**
  - Vendor the queries with `UPSTREAM.md` and `NOTICE`.
  - Write `manifest.yml`.
  - Write `ldbc_pg` and `ldbc_lpg` mapping fixtures.
    - **Pre-WP-35:** supertypes are declared but marked unsupported, so they fail
      closed instead of translating wrongly. This is the first transpiler-adjacent
      change. It comes in two parts:
      - in the fixtures: supertype entities have no physical mapping;
      - in the translator: a missing physical mapping for a known conceptual
        entity raises `MAPPING_NOT_FOUND` naming the supertype.
  - `ledger.py`, `conformance_ledger.tsv` and `test_ldbc_ratchet.py` (default
    suite, no DB).
  - The INS1 corrected variant, and the upstream issue filed.
- **Acceptance:**
  - The ratchet runs in under 5 s.
  - The ledger reproduces §3.1, minus the silent-wrong cells, which now show
    `fails_closed`.

#### WP-33: Oracle snapshots and execution tier ⚡ (M)
- **Scope:**
  - Pin Neo4j to `5.20-community` in `docker-compose.neo4j.yml`. It currently
    floats on `5-community`; the existing suites need re-verification after the
    pin.
  - A snapshot recorder (`python -m tests.ldbc.record --sf 0.003`).
  - `test_ldbc_execution.py`: Arango PG and LPG versus the snapshots.
  - `test_ldbc_crossvalidate.py`: live Neo4j re-verification.
  - Lift the shared result-equivalence helper.
  - Write operations: reset per test (a reload of SF0.003 takes seconds), then
    post-state comparison.
- **Acceptance:**
  - All four outcome columns are populated for every operation × layout.
  - A deliberately corrupted snapshot makes the test fail. This is a
    mutation check, so the gate is shown not to be vacuous.

#### WP-34: Substitution parameters (S)
- **Scope:**
  - `params/generate.py`: a seeded, deterministic picker for SF ≤ 0.1. It
    chooses bindings that yield non-empty results, plus at least one
    empty-result binding per operation, because the empty case is an edge case
    too.
  - Commit `params/sf0.003/*.yml`.
  - Document how to drive LDBC `paramgen` for SF ≥ 1 (consumed by WP-43).
- **Acceptance:** at least 3 bindings per read; regeneration is byte-identical.

**Milestone M1 (end of Phase 0).** We have a reproducible, ratcheted baseline for
57 operations × 2 layouts at SF0.003, with every failure filed as an issue tagged
`ldbc`. This alone yields a correctness report. It needs no transpiler work
beyond the fail-closed guard.

### Phase 1 — Correctness gaps, ordered by operations unlocked

#### WP-35: Label hierarchies (supertypes) ⚡ (L)
- **Upstream (`arango-query-core`):**
  - Conceptual `subtypeOf` / `supertypes` in `MappingBundle`.
  - A physical `labelsField` for LPG.
  - Resolver APIs to enumerate member collections and labels.
  - A pin bump in this repo.
- **Upstream (`arangodb-schema-analyzer`):** detect a labels array or
  `_from`-type co-occurrence and emit the hierarchy. Filed as an issue, not
  blocking, because the fixtures carry the contract.
- **Transpiler:**
  - LPG: `"Message" IN n.labels`.
  - PG: the union lowering described in D8.
  - The label-predicate expression `m:Comment` is lowered against the labels
    field rather than the type field.
  - `WITH` collection declarations cover all member collections.
- **Tests:**
  - Goldens for scan, traversal source and target, and label predicate, in both
    layouts.
  - Execution evidence from the ledger.
- **Unlocks:** the 30 operations in §2.5. This WP gates most of the rest.

#### WP-36: Multi-part `MATCH` after `WITH` (M)
- **Scope:** comma-separated pattern parts in a bound `MATCH`
  (`_compile_match_from_bound`, `core.py:2295`), with shared variables joined.
- **Unlocks:** IS2 IC3 IC4 IC6 IC10 BI10 BI11 BI14 (first-failure basis).

#### WP-37: Aggregates nested in expressions (S)
- **Scope:** `head(collect(…))`, `size(collect(…))` and similar. Hoist the
  aggregate into `COLLECT … AGGREGATE` / `INTO`, then apply the function.
- **Unlocks:** IC7 BI8.

#### WP-38: Multi-type relationship hops `[:A|B]` (M)
- **Scope:** closes the PRD §8.4 gap.
  - PG: multiple edge collections in one traversal.
  - LPG: `r.type IN [...]` with the VCI.
  - Also with variable length.
- **Unlocks:** IC12. Broadly useful beyond LDBC.

#### WP-39: `shortestPath()` / `allShortestPaths()` (M)
- **Scope:**
  - Add the grammar rule (regenerate ANTLR).
  - Lower to AQL `SHORTEST_PATH` / `ALL_SHORTEST_PATHS` / `K_SHORTEST_PATHS`.
  - Support `length(path)` and `nodes(path)`, and the "no path" case
    (`-1` / empty).
- **Unlocks:** IC1 IC13, and the IC14 existence check.

#### WP-40: Write pipelines (M)
- **Scope:**
  - `MATCH … CREATE … WITH … UNWIND … MATCH … CREATE` (INS1 INS4 INS6 INS7).
  - `DETACH DELETE` over `OPTIONAL MATCH` variables (DEL1 DEL4).
- **LPG warning:** LPG puts every node in one collection and every edge in
  another. Multi-statement writes therefore hit **ERR 1579 (same-collection
  multi-write)** far more often than PG does. This is a genuine layout-dependent
  limitation, and it feeds PRD §16's "multi-statement execution model" question
  with concrete data.

#### WP-41: Temporal subset (L)
- **Scope:**
  - The mapping declares a temporal property type and its encoding: `epoch_ms`
    or `iso8601`.
  - The translator lowers:
    - `datetime({epochMillis:})` and `datetime('…')` parameters;
    - `.year`, `.month`, `.day` to `DATE_YEAR`, `DATE_MONTH`, `DATE_DAY` (AQL's
      `DATE_*` functions accept both encodings);
    - `duration({days:})` arithmetic to `DATE_ADD`;
    - `date()` truncation.
  - Accessors on non-temporal properties **fail closed** (closing §3.2 item 2).
- **Unlocks:** IC10 BI1 BI2 BI13 BI16 BI17. It also moves the TCK temporal
  category (14.6% today).

#### WP-42: `CALL { … }` subqueries (M)
- **Scope:**
  - Grammar support.
  - Lower to an AQL subquery with an imported-variable `WITH`.
  - `UNION ALL` inside the subquery body.
- **Unlocks:** BI4 BI16 (BI15 also needs GDS).

**Milestone M2 (end of WP-35 to WP-40).** All 21 Interactive reads (except
IC10 and the GDS part of IC14) and all 16 updates reach `matches_oracle` on
**both** layouts.

**Milestone M3 (after WP-41 and WP-42).** All BI operations without GDS or APOC
reach `matches_oracle`.

### Phase 2 — Performance

#### WP-43: Benchmark runner and report (M). It can start right after M1, on whatever passes.
- **Measurements:**
  - **Translation:** `translate()` with the cache cleared and with it warm;
    p50 and p95 per operation. The PRD v0.1 criterion of P95 < 50 ms for a
    single hop is re-checked on real queries.
  - **Execution:** per operation × layout × SF. Discard K warm-up runs, then
    take N bindings × R repeats. Report p50/p95/p99, mean, row count, and
    server-side `executionTime` from cursor stats (which excludes the client),
    with a per-query timeout recorded as `TIMEOUT`.
  - **Plan digest:** from `EXPLAIN`. Full collection scans, index usage and
    traversal options are stored, so plan regressions are diffable.
- **Environment capture:**
  - Arango and Neo4j versions, image digests, CPU, RAM and container limits;
  - the SF and its data set checksum;
  - the git SHA and the mapping fixture hash.
- **Output:** `results/<date>-<sf>-<host>.json` (a CI artifact, not committed),
  plus `docs/ldbc_results.md`, generated by `report.py`. It carries the §2.6
  disclaimer and attribution. A conformance test checks that every number in
  the doc matches a committed summary JSON (the published-results conformance
  pattern), so the prose cannot drift.
- **CI:** a `ldbc-bench.yml` workflow:
  - `workflow_dispatch` for SF0.1 and SF1;
  - an optional nightly at SF0.1, non-blocking.

#### WP-44: Optimisation loop and baselines (L, open-ended)
- **Scope:**
  - Triage the slowest operations per layout from the plan digests: missing
    indexes, scans that should be traversals, and filter placement (§7.2
    statistics).
  - Fix each in the transpiler with a golden plus a plan-shape assertion.
  - Optional hand-written AQL for IS1–IS7, IC2, IC8, IC9 and IC11 quantifies
    the transpilation tax.
  - Optional Neo4j timings on identical limits (indicative).
- **Acceptance:** each optimisation lands with a before and after row in the
  report, and a regression bound.

---

## 7. Sequencing

```
WP-31 ─┬─ WP-32 ─┬─ WP-33 ──► M1 ──► WP-43 (perf on passing subset)
       │         │
       └─ WP-34 ─┘
M1 ─► WP-35 ⚡ ─┬─ WP-36 ─┬─ WP-40 ──► M2
               ├─ WP-37 ─┤
               ├─ WP-38 ─┤
               └─ WP-39 ─┘
M2 ─► WP-41 ─┬─ WP-42 ──► M3 ──► WP-44
```

WP-36 to WP-39 are independent of each other and can run in parallel. WP-35's
upstream half (arango-query-core) should start during Phase 0, because it
crosses repositories.

---

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| The PG union lowering for supertypes is slow, because a scan start becomes a multi-branch subquery | That is a finding, not a failure. Measure it (WP-43) and compare against LPG and the optional hybrid. D8 keeps the lowering behind the resolver, so it can change. |
| Neo4j results are non-deterministic under ties | The manifest states each query's ordering contract. Compare as a multiset when the order is not total. |
| Neo4j version drift changes snapshots | Pin the image (WP-33). Re-record only through the explicit `record` command, with the diff reviewed. |
| CI time | SF0.003 loads in seconds. The ratchet tier needs no DB. SF ≥ 0.1 stays out of PR CI. |
| The upstream mapping change stalls WP-35 | The fixtures and the fail-closed guard (WP-32) keep `main` safe. Only the unlock waits. Any interim workaround carries an `# upstream:` marker. |
| Unaudited numbers are read as LDBC results | Generated reports carry the disclaimer. The conformance test fails if it is missing. |

---

## 9. Out of scope / future

- **Hybrid layout.** A third mapping that mirrors LDBC's CSV split. It is cheap
  once the loader exists, and it gives a three-way performance comparison.
- **A second oracle.** The PostgreSQL SQL reference, for cases where Neo4j and
  we disagree and the Cypher semantics are disputed.
- **FinBench.** A different schema with heavier temporal and window
  predicates. Reuse the harness.
- **The LDBC Java driver** (scheduled workload, throughput, the 95% on-time
  rule). That is needed only to pursue an audit.
- **NL → Cypher.** LDBC queries come with English descriptions in the spec,
  which could become a harder NL eval set (`tests/nl2cypher/eval`).

---

## 10. Open questions for review

1. **D8, supertype modeling.** Should we pursue the union lowering for PG in
   arango-query-core? Or should we declare supertypes LPG- or hybrid-only and
   have PG fail closed? This is the largest design choice in the plan.
2. **D2, hybrid layout.** Add it now as a third layout, or keep it in Phase 3?
3. **Targets.** Should M2 and M3 become PRD success criteria (e.g. "v0.5: LDBC
   Interactive v2 reads match the oracle on PG and LPG")?
4. **D9, GDS queries.** Are Arango-dialect variants (`K_SHORTEST_PATHS`) for
   IC14, BI15, BI19 and BI20 worth the effort without a Neo4j oracle?
5. **Performance hardware.** Where do SF1 and SF10 runs happen: a dedicated
   runner, a developer machine, or Arango Cloud? Which Arango version is the
   baseline: 3.11 (CI) or 3.12?
6. **Publication.** Is `docs/ldbc_results.md` in this public repo acceptable,
   given it carries the fair-use disclaimer, or should numbers stay internal?
