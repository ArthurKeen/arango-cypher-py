# Align property key naming on the mapping export

**Labels:** `enhancement`, `export-contract`, `breaking-change-candidate`

## Background

The physical mapping export in `schema_analyzer/tool_contract/v1/response.schema.json` uses two key names that force every downstream consumer to rewrite them before the mapping is usable:

1. **Properties use `physicalFieldName`** (schema line 127, 129) where the transpiler contract expects `field`. Every consumer that reads property-level physical mapping information has to rename the key.
2. **Relationships accept both `edgeCollectionName` and `collectionName`** (schema lines 145–146). Neither is marked `required`. The baseline emits `edgeCollectionName` (`baseline.py:354`), but the LLM prompt (`analyzer.py:97`) trains on `edgeCollectionName` while the response-schema's presence of `collectionName` creates ambiguity that the LLM occasionally takes.

Both mismatches are papered over by `arango-cypher-py/arango_cypher/schema_acquire.py::_normalize_analyzer_pm` (line 1082) and `_normalize_props` (line 1099), which exist purely to rename `physicalFieldName` → `field` and `collectionName` → `edgeCollectionName`. That is ~30 LOC of adapter code replicated by every consumer of the analyzer's output.

## Current behavior

```jsonc
// Exported physical mapping today
{
  "entities": {
    "Person": {
      "style": "COLLECTION",
      "collectionName": "persons",
      "properties": {
        "name": {
          "physicalFieldName": "name",   // ← consumer has to rename to "field"
          "indexed": true
        }
      }
    }
  },
  "relationships": {
    "ACTED_IN": {
      "style": "DEDICATED_COLLECTION",
      "edgeCollectionName": "acted_in"   // ← but schema also allows "collectionName" here, ambiguous
    }
  }
}
```

## Desired behavior

### Part A: rename `physicalFieldName` → `field` on property entries

Matches the shape the transpiler already assumes. `physicalFieldName` is verbose for no benefit — the containing object already declares it as a "physical mapping" property, so `field` is unambiguous.

### Part B: require `edgeCollectionName` on relationships, deprecate `collectionName`

On relationship entries the key should be unambiguously `edgeCollectionName`. Entities continue to use `collectionName` (they aren't edges). The dual-accepted shape in v1 is a soft bug — make it crisp in v2.

### Proposed target shape

```jsonc
{
  "entities": {
    "Person": {
      "style": "COLLECTION",
      "collectionName": "persons",
      "properties": {
        "name": {
          "field": "name",
          "indexed": true
        }
      }
    }
  },
  "relationships": {
    "ACTED_IN": {
      "style": "DEDICATED_COLLECTION",
      "edgeCollectionName": "acted_in"
    }
  }
}
```

## Migration strategy

The tool contract is in `tool_contract/v1/` and [`validation.py`](../../schema_analyzer/validation.py) calls it out as a stable API. Two viable paths:

### Option A (preferred): add `v2` alongside `v1`

- Create `schema_analyzer/tool_contract/v2/response.schema.json` with the renamed keys.
- Keep `v1` emitters untouched for backwards compatibility.
- Add a `contract_version` (or `--contract v2`) selector to `export_mapping` and to the CLI / tool entrypoint.
- Default the tool to `v2` after one release cycle; leave `v1` available for one more release cycle after that.
- This is the path ArangoDB itself follows for API versioning and the one the existing `tool_contract/v1/` directory layout already anticipates.

### Option B: minor-version additive emit (less clean)

- Emit **both** key names in v1 (`field` alongside `physicalFieldName`; require `edgeCollectionName`, still accept `collectionName` on read).
- Update the schema to `required: ["field"]` with `physicalFieldName` as an optional alias.
- Every downstream consumer transparently starts seeing the new names without a version bump.
- Keep this path around indefinitely; remove alias keys only when a v2 actually lands.

Option A is cleaner; Option B ships faster if the v2 effort is otherwise blocked.

## Acceptance criteria

For whichever option is chosen:

1. A property-mapping entry in the export has `field` as its primary key. (Option A: only.)  (Option B: both `field` and `physicalFieldName`, identical values.)
2. A relationship-mapping entry has `edgeCollectionName` and does **not** emit `collectionName` on relationships.
3. JSON schema validation (`validation.py`) rejects a relationship that sets `collectionName` on its physical mapping in the new contract version.
4. The LLM prompt in `analyzer.py` is updated to reference the new key names; the validate-repair loop must accept LLM output that uses the new keys.
5. Baseline emission (`baseline.py`) is updated to emit the new keys.
6. Regression coverage in `tests/test_exports.py` confirming the emitted shape.

## Non-goals

- No change to the conceptual-schema layer. Conceptual entity / relationship representation is fine as-is.
- No other key renames. `style`, `typeField`, `typeValue`, `indexes`, `unique`, `sparse`, `fields` all stay.
- No OWL export changes (separate concern; `owl_export.py` is a different emitter).

## Impact on downstream consumers

Removes `_normalize_analyzer_pm` and `_normalize_props` from `arango-cypher-py/arango_cypher/schema_acquire.py`. Same adapter presumably lives in other consumers (the MCP server uses the same export) — this benefits every one of them.

More broadly: reduces the "integration burden" of the analyzer. A consumer today has to either (a) copy the normalizer, (b) discover the mismatch through a failure and write their own adapter, or (c) pick a different library. Fixing this at the source makes the analyzer a drop-in dependency.

## Implementation notes

- The JSON schema file `response.schema.json` is the source of truth; change it first, add a v2 directory copy, then update emitters to match.
- `analyzer.py:94-98` (the LLM prompt's example payload) is a prompt-engineering surface — update the example to the new keys so the LLM produces v2-shaped output.
- `validation.py` has an inline JSON schema duplicating `response.schema.json`'s structure (lines 93–170). Keep them in sync — or, better, make `validation.py` load from `response.schema.json` instead of re-declaring.
- `eval/scoring.py` compares LLM output against expected fixtures; if the evaluation fixtures use the old keys, regenerate them for v2.
