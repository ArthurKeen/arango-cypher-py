# Emit `vci` and `deduplicate` flags on physical-mapping indexes

**Labels:** `enhancement`, `export-contract`

## Background

`baseline.py::_extract_indexes_for_mapping` projects each index into a fixed five-key dictionary (`type`, `fields`, `unique`, `sparse`, `name`) before attaching it to the physical mapping. The raw output of `python-arango`'s `Collection.indexes()` (preserved faithfully by `snapshot.py::_normalize_index` — "preserve remaining keys for forward compatibility") contains additional flags that are silently dropped at the export boundary.

Two of those dropped flags are needed by downstream transpilers:

- **`vci` / vertex-centric index marker** — ArangoDB 3.12 introduced vertex-centric indexes (edge-collection indexes keyed on `_from` or `_to` plus edge properties). These are the indexes the traversal planner can use for `OPTIONS.indexHint` in AQL `FOR … IN OUTBOUND/INBOUND`. Without knowing which indexes are VCI-capable, a downstream transpiler cannot emit a correct hint and must either omit the hint (slow traversal) or guess (correctness risk).
- **`deduplicate`** — governs array-index dedup semantics. Needed so the transpiler can distinguish an array index that will or won't cause duplicate rows in a `FOR … IN` scan, which changes whether a `DISTINCT` projection is necessary for correctness.

## Current behavior

```python
# baseline.py:178-189
entry: dict[str, Any] = {
    "type": idx_type,
    "fields": [str(f) for f in idx.get("fields", []) if isinstance(f, str)],
}
if idx.get("unique"):
    entry["unique"] = True
if idx.get("sparse"):
    entry["sparse"] = True
name = idx.get("name")
if isinstance(name, str) and name:
    entry["name"] = name
result.append(entry)
```

Any other keys on the source index dict (including `vci`, `deduplicate`, `storedValues`, `inBackground`, `cacheEnabled`, `estimates`, etc.) are discarded.

## Desired behavior

Extend `_extract_indexes_for_mapping` so the projected entry carries these additional fields when present on the source index:

| Export field | Source field on python-arango's index dict | Type | Conditional |
|---|---|---|---|
| `vci` | check both `idx.get("vci")` and `idx_type in {"vci", "vertex_centric_index"}` — ArangoDB has reported this field under different keys across minor versions, so probe both for forward-compat | `bool` | Emit only when `True` |
| `deduplicate` | `idx.get("deduplicate")` | `bool` | Emit only when explicitly `False` (the default is `True` for array indexes; explicit `False` is the signal) |
| `storedValues` | `idx.get("storedValues")` if a non-empty list | `list[str]` | Emit when present and non-empty — downstream projection planners can use it to read from the index without a document fetch |

The existing `type`, `fields`, `unique`, `sparse`, `name` fields are unchanged.

## Acceptance criteria

1. A persistent index with no VCI and default deduplicate semantics produces exactly the current output (no regression).
2. An edge-collection index where `col.indexes()` returns `{"type": "persistent", "fields": ["_from", "someField"], "vci": true, ...}` appears in the export's `physicalMapping[<rel>].indexes[i]` with `"vci": True`.
3. An array index created with `deduplicate=False` appears in the export with `"deduplicate": False`; an array index with default `deduplicate=True` does **not** carry the key (keeps the export slim).
4. A persistent index with `storedValues=["email", "status"]` round-trips those values to the export entry.
5. Unit test in `tests/test_exports.py` (or a new `tests/test_index_export.py`) covers each of the above using a mocked snapshot — no live DB required.

## Non-goals

- No change to the analyzer's capture of indexes in `snapshot.py` — it already preserves everything via `_normalize_index`. Fix is confined to the export boundary in `baseline.py`.
- No change to the conceptual-schema layer. Indexes remain a physical-mapping concern only.
- No index *creation* / advisory logic. This issue is strictly about not dropping information that python-arango already returns.

## Impact on downstream consumers

Closes [`arango-cypher-py/arango_query_core/mapping.py`]'s `IndexInfo.vci` and `IndexInfo.deduplicate` fields, which are defined and consumed (`MappingResolver.has_vci()`, `translate_v0.py::_emit_vci_indexhint_options`) but currently always read `False` because the analyzer strips them. Index-aware traversal emission (WP-18 in `arango-cypher-py`) starts working against analyzer-sourced mappings instead of only against hand-authored mappings.

## Implementation sketch

```python
def _extract_indexes_for_mapping(col: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for idx in col.get("indexes") or []:
        if not isinstance(idx, dict):
            continue
        idx_type = idx.get("type", "")
        if idx_type == "primary":
            continue
        entry: dict[str, Any] = {
            "type": idx_type,
            "fields": [str(f) for f in idx.get("fields", []) if isinstance(f, str)],
        }
        if idx.get("unique"):
            entry["unique"] = True
        if idx.get("sparse"):
            entry["sparse"] = True
        name = idx.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name

        if idx.get("vci") or idx_type in {"vci", "vertex_centric_index"}:
            entry["vci"] = True
        if idx.get("deduplicate") is False:
            entry["deduplicate"] = False
        sv = idx.get("storedValues")
        if isinstance(sv, list) and sv:
            entry["storedValues"] = [str(f) for f in sv if isinstance(f, str)]

        result.append(entry)
    return result
```

Same ~10 LOC extension is also needed in `baseline.py::_build_index_lookup` (line 96) if VCI-carrying indexes should also set the `indexed` flag on properties — but only for non-VCI indexes (VCI indexes are edge-centric, not property-scan indexes, so they should **not** flip `indexed=True` on a property). Worth a separate verification step during PR review.
