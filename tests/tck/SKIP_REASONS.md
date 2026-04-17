# TCK Match Scenario Results

## Summary

| Metric | Count | Percentage |
|--------|-------|------------|
| Total scenarios | 426 | 100% |
| **Passed** | **282** | **66.2% of total, 90.7% of non-skipped** |
| Skipped | 115 | 27.0% |
| Failed | 29 | 6.8% |

**Pass rate of non-skipped scenarios: 90.7%** (target was ≥40%)

## Skipped Scenarios (115)

| Reason | Count | Notes |
|--------|-------|-------|
| Unsupported Cypher construct | 107 | CASE/WHEN, list comprehension, pattern expressions in WHERE, UNWIND, etc. |
| Not-implemented Cypher construct | 7 | Shared relationship variables across pattern parts, parameterized maps |
| Unsupported step format | 1 | "ignoring element order for lists" comparison mode |

### Details on unsupported constructs (107 skipped)
These scenarios use Cypher features that are outside the v0 translator's grammar:
- **CASE/WHEN expressions** — conditional expressions
- **List comprehension** — `[x IN list WHERE ...]`
- **Pattern expressions in WHERE** — `WHERE (n)-->(m)` 
- **UNWIND** — list expansion
- **Existential subqueries** — `EXISTS { MATCH ... }`
- **Named paths** (some) — `p = (a)-[r]->(b)`
- **Multiple MATCH + OPTIONAL MATCH combinations** (some complex forms)

## Failed Scenarios (29)

| Category | Count | Notes |
|----------|-------|-------|
| Row count mismatch | 14 | Wrong number of results returned |
| Named path as collection | 7 | Named path variable `p` treated as AQL collection name |
| Value mismatch | 4 | Results returned but values differ from expected |
| AQL variable conflict | 3 | Same variable used in multiple MATCH clauses |
| Invalid traversal depth | 1 | `*0..0` range rejected by ArangoDB |

### Row count mismatches (14)
Mainly caused by:
- **Multi-label matching** (`MATCH (a:A:B)`) — LPG mode stores a single `type` field, so multi-label intersection queries return 0 results (2 scenarios)
- **Zero-length variable length paths** (`*0`, `*0..N`) — ArangoDB traversal doesn't include the start node for 0-hop paths (10 scenarios)
- **Complex setup CREATE failures** — Some multi-step CREATE setups with computed properties fail to insert all data (2 scenarios)

### Named path failures (7)
All in Match6/Match7: the translator doesn't properly handle named path assignments (`p = (a)-[r]->(b)`), generating AQL that references `p` as a collection.

### Value mismatches (4)
- OPTIONAL MATCH result handling edge cases
- Relationship list serialization differences

### AQL variable conflicts (3)
ArangoDB rejects queries where the same variable name is used in multiple MATCH/traversal clauses. The translator doesn't deduplicate or rename conflicting variables.

## Translator Fixes Made

1. **`arango_cypher/translate_v0.py`**:
   - Fixed multi-CREATE statement handling: `force_let=True` for non-last CREATE clauses so variables are properly captured with `LET x = FIRST(INSERT ... RETURN NEW)` for cross-clause references
   - Added support for unlabeled node CREATE: nodes without labels now use `_infer_unlabeled_collection()` instead of raising an error
   - Fixed anonymous variable naming: anonymous nodes in CREATE patterns now get unique var names (`_anon0`, `_anon1`, ...) instead of all sharing `_anon`

2. **`tests/tck/runner.py`**:
   - Added "Given any graph" step handling (resets graph like "Given an empty graph")
   - Built dynamic LPG mapping system: scans scenario Cypher for labels/types and auto-generates mapping entries for the `vertices`/`edges` collections
   - Added direct CREATE execution fallback: when AQL fails with "access after data-modification" (ArangoDB limitation on multiple INSERTs to same collection), falls back to python-arango document API
   - Ensures `vertices` and `edges` collections exist before every scenario

3. **`tests/tck/normalize.py`**:
   - Added LPG `type` → `_labels` conversion for node comparison
   - Added `_labels: []` for unlabeled nodes
   - Added edge normalization: strips `_from`/`_to` and converts `type` → `_type` for relationship comparison
   - Fixed single-column result handling: wraps raw document results in expected column name
   - Fixed scalar result wrapping: wraps scalar AQL results in dict with expected column key

## Feature Files Tested

- `Match1.feature` — Match nodes (11 scenarios)
- `Match2.feature` — Match relationships (10 scenarios)
- `Match3.feature` — Match patterns (26 scenarios)
- `Match4.feature` — Variable length patterns (10 scenarios)
- `Match5.feature` — Variable length bounds (29 scenarios)
- `Match6.feature` — Named paths (20 scenarios)
- `Match7.feature` — OPTIONAL MATCH (31 scenarios)
- `Match8.feature` — Cross product (2 scenarios)
- `Match9.feature` — Relationship collections (9 scenarios)
- `MatchWhere1.feature` — WHERE with single predicates (15 scenarios)
- `MatchWhere2.feature` — WHERE with conjunctive predicates (2 scenarios)
- `MatchWhere3.feature` — WHERE with joins (3 scenarios)
- `MatchWhere4.feature` — WHERE with inequality (2 scenarios)
- `MatchWhere5.feature` — WHERE with null handling (4 scenarios)
- `MatchWhere6.feature` — WHERE after OPTIONAL MATCH (8 scenarios)
- `CountingSubgraphMatches1.feature` — Counting subgraph matches (11 scenarios)

Total: 16 feature files, 426 scenarios (after Scenario Outline expansion)
