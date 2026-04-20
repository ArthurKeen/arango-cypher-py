# Emit a `statistics` block with per-relationship cardinality and selectivity

**Labels:** `enhancement`, `export-contract`, `new-capability`

## Background

The analyzer produces a conceptual schema (what entities and relationships exist) and a physical mapping (how they are realized in collections). It does not produce any information about **how many of each** exist, nor about the shape of each relationship (1:1 vs 1:N vs N:M). That statistical layer is what every cost-based downstream — query planner, NL-prompt enricher, index advisor — needs in order to make intelligent decisions.

Because the analyzer does not emit this, every consumer re-implements it. In `arango-cypher-py` there is a ~170-line local implementation (`compute_statistics`, `_classify_cardinality`, `enrich_bundle_with_statistics` in `arango_cypher/schema_acquire.py`) that runs `LENGTH()` and `COLLECT WITH COUNT` AQL queries on every `get_mapping()` call and computes avg-out / avg-in / selectivity / cardinality-pattern per relationship. This logic is not specific to Cypher-to-AQL translation — any consumer of the analyzer would need the same numbers — so it belongs upstream.

## Current behavior

`exports.py::export_mapping` returns only `conceptualSchema`, `physicalMapping`, and `metadata`. The `metadata` block does not include counts, degrees, or any other statistical summary. Downstream consumers must hit the live database themselves, duplicating work the analyzer already has a DB handle for.

## Desired behavior

Extend the analyzer's metadata (or add a new top-level `statistics` key to the export) with a deterministic statistics block. The shape below matches what `arango-cypher-py` has been computing in production, so downstream clients can swap to the analyzer's version without re-deriving the schema:

```jsonc
{
  "statistics": {
    "computed_at": "2026-04-20T14:32:11+00:00",
    "collections": {
      "<collectionName>": {
        "count": 1420,
        "is_edge": false
      }
    },
    "entities": {
      "<EntityLabel>": {
        "estimated_count": 1420
      }
    },
    "relationships": {
      "<RelationshipType>": {
        "edge_count": 3140,
        "source_count": 1420,
        "target_count": 998,
        "avg_out_degree": 2.21,
        "avg_in_degree": 3.15,
        "cardinality_pattern": "N:M",
        "selectivity": 0.002217
      }
    }
  }
}
```

### Computation rules (reference implementation)

Already implemented and tested in `arango-cypher-py/arango_cypher/schema_acquire.py`, lines 1113–1260. Porting-ready:

- **`collections[<col>].count`** — `RETURN LENGTH(<col>)` (constant time in ArangoDB).
- **`collections[<col>].is_edge`** — `collection.properties()["type"] == 3`.
- **`entities[<label>].estimated_count`** — for `COLLECTION`-style mappings, same as `collections[<col>].count`. For `LABEL` / `GENERIC_WITH_TYPE`, `FOR d IN <col> FILTER d.<typeField> == <typeValue> COLLECT WITH COUNT INTO c RETURN c`.
- **`relationships[<rel>].edge_count`** — analogous, using `edgeCollectionName` + optional `typeField`/`typeValue` filter for `GENERIC_WITH_TYPE`.
- **`relationships[<rel>].source_count` / `target_count`** — read from `entities[domainLabel].estimated_count` / `entities[rangeLabel].estimated_count`.
- **`avg_out_degree`** = `edge_count / source_count` (0 when `source_count` is 0), rounded to 2 dp.
- **`avg_in_degree`** = `edge_count / target_count`, rounded to 2 dp.
- **`selectivity`** = `edge_count / (source_count * target_count)` when both are positive, else `1.0`, rounded to 6 dp.
- **`cardinality_pattern`**: bucketing by 1.5 threshold (documented, tunable if needed):
  - `avg_out ≤ 1.5 AND avg_in ≤ 1.5` → `"1:1"`
  - `avg_out > 1.5 AND avg_in ≤ 1.5` → `"1:N"`
  - `avg_out ≤ 1.5 AND avg_in > 1.5` → `"N:1"`
  - `avg_out > 1.5 AND avg_in > 1.5` → `"N:M"`

## Acceptance criteria

1. `export_mapping(...)["metadata"]["statistics"]` is present for every analyzer invocation that has a live DB handle.
2. For a fixture with three relationships covering each of 1:1, 1:N, and N:M, the emitted `cardinality_pattern` matches the expected label.
3. `selectivity` is bounded in `[0, 1]` for any non-degenerate case; `0` is permitted (no edges) and `1.0` is emitted for degenerate cases (no source or target documents).
4. Statistics computation is gated — the analyzer still works without a live DB (e.g. analysis from a pre-captured snapshot); in that case the `statistics` block is absent and `metadata.statistics_status` is `"skipped_no_db"` (or similar documented sentinel).
5. Total AQL work for a 100-collection / 1000-relationship schema is bounded: one `LENGTH()` per collection and one `COLLECT WITH COUNT` per typed subset, no per-document scans. Benchmark target: < 2 s against a 100 k-row schema.
6. Coverage in `tests/test_exports.py` (or a new `tests/test_statistics.py`) using a `python-arango` mock or a small live fixture.

## Non-goals

- No histogramming, no p50/p95/p99 degree distributions. Averages + the 1.5 bucketing have been sufficient for `arango-cypher-py`'s index advisor and NL prompt layers; add distribution work only when a consumer has a concrete need.
- No query plan costing, no index-coverage analysis. Those are separate downstream concerns.
- No per-property value-distribution stats (min/max/ndv). `baseline.py` already samples per-property values for type detection; extending that into full NDV is a separate feature request.

## Impact on downstream consumers

Removes these from `arango-cypher-py/arango_cypher/schema_acquire.py` (~170 LOC):

- `compute_statistics()` (line 1113)
- `_classify_cardinality()` (line 1250)
- `enrich_bundle_with_statistics()` (line 1263)
- The unconditional `bundle = enrich_bundle_with_statistics(db, bundle)` call in `get_mapping()` (line 1340)

And consolidates the cardinality computation into a single well-tested location that any ArangoDB Python client can consume.

## Implementation notes

A new `statistics.py` module in `schema_analyzer/` keeps the concern separated from `baseline.py` (inference) and `exports.py` (contract projection). `workflow.py` (or wherever the `AgenticSchemaAnalyzer` orchestration lives) calls it after baseline inference completes and stashes the result in `AnalysisResult.metadata.statistics` so `export_mapping` picks it up transparently.

The one subtle piece is figuring out the right domain/range labels for `GENERIC_WITH_TYPE` relationships, which is already solved in `arango-cypher-py::_infer_lpg_edge_endpoints` (line 495) — worth porting along with the statistics logic so the `source_count`/`target_count` attribution is correct on LPG schemas.
