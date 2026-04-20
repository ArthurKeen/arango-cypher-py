# Detect multi-type edge collections and emit per-type `GENERIC_WITH_TYPE` entries

**Labels:** `enhancement`, `inference-accuracy`

## Background

`baseline.py` already distinguishes `DEDICATED_COLLECTION` (one relationship type per edge collection) from `GENERIC_WITH_TYPE` (a single edge collection carrying multiple relationship types, discriminated by a field). The mechanism is `_choose_type_field(col, is_edge=True)`: when it returns a field name, the edge collection becomes `GENERIC_WITH_TYPE`; when it returns `None`, the collection becomes `DEDICATED_COLLECTION`.

The problem is that `_choose_type_field` picks *one* best candidate, and the candidate is drawn from `candidate_type_fields` (populated by `_detect_candidate_type_fields` in `snapshot.py`, which filters keys against a hard-coded `CANDIDATE_TYPE_KEYS` set). Two observed failure modes:

1. **Missed detection** — the discriminator field exists but is not in `CANDIDATE_TYPE_KEYS`. The collection gets mis-classified as `DEDICATED_COLLECTION` even though it contains multiple semantic relationship types.
2. **Single-edge emission** — even when detection succeeds for `GENERIC_WITH_TYPE`, `baseline.py` emits *one* relationship per *collection* keyed by `typeField`, not *one relationship per distinct type value*. Downstream consumers that need per-type `domain`/`range` / `properties` have to re-query the database to split the collection.

`arango-cypher-py` works around both with `_fixup_dedicated_edges()` (80 LOC) which re-probes every `DEDICATED_COLLECTION` edge for a discriminator field and, when found, splits the single mapping entry into N `GENERIC_WITH_TYPE` entries — one per distinct `typeField` value.

## Current behavior

```python
# baseline.py (summary)
type_field = _choose_type_field(col, is_edge=True)
if type_field:
    # Emits ONE rel entry keyed by type_field, not per distinct value
    rel_mapping = {"style": "GENERIC_WITH_TYPE", "collectionName": ..., "typeField": type_field}
else:
    # Emits DEDICATED_COLLECTION even if a discriminator exists but wasn't in CANDIDATE_TYPE_KEYS
    rel_mapping = {"style": "DEDICATED_COLLECTION", "edgeCollectionName": ...}
```

Result: a single edge collection `edges` carrying `{relation: "ACTED_IN"}` and `{relation: "DIRECTED"}` either:
- becomes a single `GENERIC_WITH_TYPE` entry (if `relation` is in `CANDIDATE_TYPE_KEYS`) — but without per-type breakdown, or
- becomes a single `DEDICATED_COLLECTION` entry (if `relation` is not in `CANDIDATE_TYPE_KEYS`) — silently collapsing two distinct relationship types into one.

## Desired behavior

Two changes:

### Change A: broaden discriminator detection

When `_pick_best_type_field` returns `None` for an edge collection, run a secondary check before settling on `DEDICATED_COLLECTION`:

1. Take the snapshot's `sample_field_value_counts` (already captured in Phase 2 of `snapshot.py`).
2. For any field whose value distribution shows **≥ 2 distinct values** covering **≥ 80 % of sampled edges**, and whose values are all strings with **≤ 64 chars each** and **alphanumeric / underscore only**, treat that field as a discriminator candidate.
3. This is deliberately conservative — it doesn't need to be in `CANDIDATE_TYPE_KEYS`. The distribution shape is the signal. An ID field with 500 distinct values across 500 edges is not a discriminator; a field with 3 distinct values across 1000 edges is.

Alternatively, expose the threshold as a configurable in `defaults.py` (`MIN_TYPE_FIELD_DISTINCT_VALUES` already exists there; add a matching `MAX_TYPE_FIELD_DISTINCT_VALUES` and `MIN_TYPE_FIELD_COVERAGE_FRACTION`).

### Change B: emit one `GENERIC_WITH_TYPE` entry per distinct `typeValue`

When `GENERIC_WITH_TYPE` is emitted, split it into per-type entries in the physical mapping:

```jsonc
{
  "physicalMapping": {
    "relationships": {
      "ACTED_IN": {
        "style": "GENERIC_WITH_TYPE",
        "edgeCollectionName": "edges",
        "typeField": "relation",
        "typeValue": "ACTED_IN",
        "domain": "Person",
        "range": "Movie",
        "properties": { ... }
      },
      "DIRECTED": {
        "style": "GENERIC_WITH_TYPE",
        "edgeCollectionName": "edges",
        "typeField": "relation",
        "typeValue": "DIRECTED",
        "domain": "Person",
        "range": "Movie",
        "properties": { ... }
      }
    }
  }
}
```

The conceptual schema already models these as separate relationship types — the physical mapping should too. Per-type `domain` / `range` inference needs a small sample of edges matching each type value; see `arango-cypher-py/arango_cypher/schema_acquire.py::_infer_lpg_edge_endpoints` (line 495) for a correct, fast implementation.

Also — and this is the critical piece — rename the key from `collectionName` to `edgeCollectionName` for relationships. See [Issue 05](./05-align-property-key-naming.md).

## Acceptance criteria

1. Fixture `edges` collection with `{relation: "ACTED_IN"}` and `{relation: "DIRECTED"}` (100 rows each) produces two relationship entries in `physicalMapping.relationships`, each with correct `typeValue`, `domain`, `range`.
2. Fixture `edges` collection with `{kind: "A"}` × 50 and `{kind: "B"}` × 50 — where `kind` is *not* in `CANDIDATE_TYPE_KEYS` — is still detected as `GENERIC_WITH_TYPE` (Change A), producing two entries.
3. Fixture dedicated edge collection `acted_in` with no discriminator field is still emitted as `DEDICATED_COLLECTION` (no false positives).
4. Fixture edge collection with a high-cardinality ID-like field (e.g. `comment_id` with 1000 distinct values across 1000 edges) is correctly *not* classified as `GENERIC_WITH_TYPE`.
5. Regression coverage in `tests/test_baseline.py` (or a dedicated `tests/test_edge_splitting.py`).

## Non-goals

- No change to entity-style classification (`COLLECTION` vs `LABEL`) — document collection multi-type detection is out of scope here. (Issue 03 is scoped to edges only; document-side can be a follow-up.)
- No LLM-assisted detection. Pure statistical thresholds; deterministic output.

## Impact on downstream consumers

Removes `_fixup_dedicated_edges` (80 LOC) from `arango-cypher-py/arango_cypher/schema_acquire.py`. Also eliminates a class of silent correctness bugs: today, a graph whose discriminator field is spelled something other than `type`/`relation`/`relType`/`_type`/`label` gets silently collapsed to one relationship type, which is invisible to the downstream transpiler until a user notices missing results.

## Implementation notes

`snapshot.py` already captures `sample_field_value_counts` (Phase 2, line 509). Change A only needs to consult that data in `_pick_best_type_field`, no new snapshot work. Change B is structural — splitting the single-entry-per-collection emission in `baseline.py:320-334` into a loop over `_type_values_for_field(entry, best_field)`. Porting `_infer_lpg_edge_endpoints` from `arango-cypher-py` covers the per-type `domain`/`range` inference.
