# TCK Conformance Plan — road to 100%

**Status:** Proposed · **Date:** 2026-09-10 · **Owner:** transpiler
**Baseline measured:** `tests/tck/analyze_execution.py` against ArangoDB 3.11, 2026-09-10
**Scorecard:** `docs/cypher_support_scorecard.md` · **Contract:** `docs/cypher_capability_matrix.md`

Not to be confused with `docs/cypher_coverage_plan.md` (2026-06-23), which targets the
22-query FinReflectKG benchmark and is largely complete. This plan targets the openCypher
TCK corpus.

## Baseline ledger

Every scenario accounted for, from the live run:

| Outcome | Count |
| --- | ---: |
| TCK assertion passed | 1,299 |
| Correctly rejected (negative test) | 267 |
| **Correct outcomes** | **1,566 (40.6%)** |
| Failures to close | 2,244 |
| Harness-skipped (procedure step) | 51 |
| **Total** | **3,861** |

## Definition of done

"100% syntax" **must** mean *passability* — the scenario translates, or is rejected exactly
when the TCK expects an error. It must never mean "translates": **690 of 3,862 scenarios
(17.9%) are negative tests**, and translating one of those is a defect, not coverage.

"100% round trip" should mean *differential equivalence against a reference engine*, not
*matching the TCK's declared assertion*. See [Oracle upgrade](#oracle-upgrade).

## Where the 2,244 failures are

Aggregated from 435 distinct failure reasons. **Only half the gap is missing syntax.**

| # | Family | Count | Size |
| --- | --- | ---: | --- |
| A | Missing translation (syntax gap) | 1,133 | XL |
| B | False accepts — we translate queries that must error | 413 | S–M |
| C | Semantic mismatches — runs, wrong answer | 480 | L |
| D | Generated AQL fails to run | 206 | M |
| E | Parse rejections + harness skips | 130 | M |

Family A splits as temporal **857**, `CALL` **50**, all other categories **276**.

## Work packages

| WP | Phase | Description | Unlocks | Size |
| --- | --- | --- | ---: | --- |
| WP-0 | 0 | Per-scenario outcome ledger + CI no-regress ratchet | 0 | S |
| WP-1.1 | 1 | Scope tracking → `UndefinedVariable` | 53 | S |
| WP-1.2 | 1 | Re-binding detection → `VariableAlreadyBound` | 77 | S |
| WP-1.3 | 1 | Node/relationship type conflict | 72 | S |
| WP-1.4 | 1 | Function signature + argument type validation | 164 | M |
| WP-1.5 | 1 | Long tail: 20 further error classes | 47 | M |
| WP-2.1 | 2 | `ERR 1203` — unbound pattern var emitted as collection | 53 | S |
| WP-2.2 | 2 | `ERR 1511/1512` — alpha-rename AQL variables | 46 | S |
| WP-2.3 | 2 | `ERR 1501` — invalid AQL emission (CASE lowering) | 31 | S |
| WP-2.4 | 2 | `ERR 1551/1552` — bind-parameter plumbing | 25 | S |
| WP-2.5 | 2 | `ERR 1202/1563/1504` — runtime value errors | 24 | S |
| WP-3 | 3 | Null / three-valued logic model | 218 | M |
| WP-4 | 4 | Remaining value + cardinality mismatches (triage first) | 262 | L |
| WP-5.1 | 5 | Non-temporal category gap (quantifier 60, with-orderBy 38, merge 31, …) | 276 | M–L |
| WP-5.2 | 5 | Grammar gaps (`Cypher.g4` + ANTLR regen) | 79 | M |
| WP-6 | 6 | Multi-statement / transaction execution model — **API-breaking** | 73 | L |
| WP-7 | 7 | Temporal type system (6 types, tz, truncation, duration arithmetic) | 857 | XL |
| WP-8 | 8 | `CALL` procedure catalog — **scope decision required** | 101 | XL |

## Sequencing

Ordered by scenarios unlocked per unit of effort, dependencies respected.

| Milestone | Phases | Unlocks | Cumulative | Full | Core |
| --- | --- | ---: | ---: | ---: | ---: |
| Baseline | — | — | 1,566 | 40.6% | 55.8% |
| M1 Correctness first | 0–2 | 592 | 2,158 | 55.9% | ~77% |
| M2 Semantics | 3–4 | 480 | 2,638 | 68.3% | ~94% |
| **M3 Core complete** | 5 | 355 | 2,993 | 77.5% | **~100%** |
| M4 Multi-write | 6 | 73 | 3,066 | 79.4% | 100% |
| M5 Temporal | 7 | 857 | — | ~99% | 100% |
| M6 CALL | 8 | 101 | 3,861 | 100% | 100% |

Buckets overlap — a scenario can fail for two reasons — so cumulative sums are optimistic
near the end. **Re-measure after every phase.** Phase 3 is expected to close part of
Phase 5 for free (quantifier null handling).

Phases 1–2 are pure defect and validation work with no new Cypher surface, and together
unlock more than any grammar work would. They are worth doing regardless of this plan:
every `ERR 1203`/`1501` case produces AQL a reviewer would call wrong on sight.

## Oracle upgrade

Schedule **between Phase 2 and Phase 3**.

Matching the TCK's declared assertion is a specification proxy. Direct reference-engine
comparison currently covers **34 queries** (`pytest -m cross`). Extend that harness to run
the whole TCK corpus against both Neo4j 5 and ArangoDB 3.11 and compare row by row,
converting 3,861 assertion checks into 3,861 equivalence checks.

Doing this before the semantic phases means Phase 3 and 4 are measured against the strong
oracle from the start, instead of being re-validated afterwards.

## Feasibility — the honest read

| Target | Verdict |
| --- | --- |
| **Core 100%** (2,805 scenarios) | Realistic. No architectural blocker; Phases 1–5. |
| **Full 100%** | Possible but gated on temporal (857 scenarios, 38% of the gap) — a funded project in its own right. |
| `CALL` | Requires emulating a Neo4j procedure catalog on an engine with no procedures. Some scenarios assert Neo4j-specific procedure-signature errors. **Recommend declaring out of scope** unless Neo4j procedure compatibility is a product goal. |
| Multi-write | Requires `translate()` to return a statement sequence + transaction envelope. Version deliberately; do not let coverage work force an unplanned breaking change. |

**Recommendation:** target **100% of Core** with temporal and `CALL` documented as explicit
exclusions. That is a defensible compatibility claim, reachable through Phases 0–5, and it
avoids letting 1.3% of the corpus distort the roadmap.

## Risks

- **Temporal could consume the entire budget** — 38% of the gap in one bucket. A partial
  temporal implementation is worse than none: inconsistent semantics produce silently wrong
  answers instead of clean rejections.
- **The 187-scenario "other value mismatch" bucket is unanalysed** — the largest unstructured
  item and the most likely source of schedule surprise. Triage early even though it sits in
  Phase 4.
- **Bucket overlap makes the milestone arithmetic optimistic.** The Phase 0 ledger is the
  only reliable progress signal.

## Reproduce the baseline

```bash
docker compose up -d
RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/analyze_execution.py > tck-execution.json
./.venv/bin/python tests/tck/render_coverage_report.py --execution-json tck-execution.json --write
```
