# Physical layout, shard topology, and multi-tenant characterization

**Labels:** `tracking`, `multi-tenant`, `cluster-awareness`

> **Status (2026-04-28):** **Closed out.** Upstream §6.2 bullets 3 / 4 / 5
> have all shipped (`arango-schema-mapper` v0.5.0 + v0.6.0, PRs #15 / #16 /
> #17) and local adoption is complete in `arango-cypher-py` for all three
> blocks — `metadata.shardingProfile`, `metadata.multitenancy`, and
> `physicalMapping.shardFamilies`. Downstream consumers landed in:
>
> - **`feat/adopt-sharding-profile`** (arango-cypher-py PR #6, merged 2026-04-23) —
>   the main adoption PR; wired all three metadata blocks into
>   `nl2cypher._core` (`_deployment_style_hint`, `_shard_families_block`),
>   `tenant_scope.analyze_tenant_scope`, `tenant_guardrail.multitenancy_physical_enforcement`,
>   and `schema_acquire.acquire_mapping_bundle` observability.
> - **Docs refresh** (arango-cypher-py PR #9, merged 2026-04-27) —
>   `multitenant_prd.md` §3.1/§3.2 now document the `metadata.multitenancy`
>   block end-to-end; `schema_analyzer_issues/06` got a downstream-adoption
>   note; `python_prd.md` multi-tenant Layer-0 row flipped to
>   "Partially adopted".
>
> VCI (upstream PRD §6.2 bullet 2) is the only item still outstanding
> upstream. It is orthogonal to multi-tenancy, non-blocking for Wave 6b,
> and tracked as a follow-up — not re-filed as a new local issue.
> **Do not file as a separate GitHub issue** — the upstream PRD is the
> spec of record; this file is now retained for provenance (what was
> proposed, what shipped, what each block retired) and should be read as
> a historical artifact rather than a live proposal. §2 below preserves
> the original `shardFamilies` proposal text as filed; the corresponding
> production code path is documented in `python_prd.md` §5.7 and in the
> `_shard_families_block` docstring, which are the sources of truth
> going forward.

---

## 1) Alignment with upstream PRD §6.2

The upstream spec breaks the work into three deliverables, each emitted
as a `metadata.*` block on the `AnalysisResult`:

| Upstream feature | Emits | Retires locally | Status |
|---|---|---|---|
| **Sharding-pattern detection** (PRD §6.2 bullet 3) | `metadata.shardingProfile` with style ∈ {`OneShard`, `SmartGraph`, `DisjointSmartGraph`, `SatelliteGraph`, `Sharded`} plus per-collection evidence (`shardKeys`, `numberOfShards`, `smartGraphAttribute`, `isDisjoint`, `replicationFactor`). | Multi-tenant PRD §3 MT-0 (would have added `physicalLayout` locally in `schema_acquire.py`). | **Shipped** in `arangodb-schema-analyzer` v0.5.0 (mapper PR #15). Adopted locally in `nl2cypher._core._deployment_style_hint` + `schema_acquire.acquire_mapping_bundle` observability. |
| **Multitenancy detection** (PRD §6.2 bullet 4) | `metadata.multitenancy` with style ∈ {`none`, `disjoint_smartgraph`, `shard_key`, `discriminator_field`, `collection_per_tenant`, `database_per_tenant`, `unknown_single_db`}, `tenantKey`, `tenantKeyCollections`, `physicalEnforcement`, `evidence`. | `nl2cypher/tenant_scope.py` local heuristic becomes pure fallback (same pattern as existing `tenantScope` from issue #06). | **Shipped** in `arangodb-schema-analyzer` v0.6.0 (mapper PR #17). Adopted locally: `tenant_scope.analyze_tenant_scope` extends its discovery regex with the upstream `tenantKey[]`; `tenant_guardrail` surfaces `physicalEnforcement` on every violation; `schema_acquire` logs the classification. |
| **Shard-family detection** (PRD §6.2 bullet 5 — see §2 below) | `physicalMapping.shardFamilies` with `name`, `suffix`, `discriminator`, `sharedProperties`, `members[]`. | Nothing was local; D7 (parallel-shard detection in `docs/schema_inference_bugfix_prd.md`) is now retired by upstream. | **Shipped** in `arangodb-schema-analyzer` v0.6.0 (mapper PR #16). Adopted locally in `nl2cypher._core._shard_families_block`, which renders members + UNION-or-pick-one directive into the LLM prompt. Closes downstream defect D7. |
| **VCI detection** (PRD §6.2 bullet 2) | First-class `VCI` mapping style + schema-level duplication detection. | Nothing local today (we already consume per-index `vci=true`). | Orthogonal — not blocking multi-tenant. Follow-up. |

### 1.1 What local code adopts each block

**`metadata.shardingProfile`:**

- `multitenant_prd.md` §3 work-package MT-0 (`physicalLayout.kind` per
  collection) is fully satisfied by
  `shardingProfile.members[*].{kind, smartGraphAttribute, isDisjoint, graphName, shardKeys, numberOfShards, replicationFactor}`.
- `multitenant_prd.md` §7 (EXPLAIN-plan validator, Layer 5) reads
  `shardingProfile.style` once at session start to decide whether to
  enforce per-plan shard-key checks (OneShard ⇒ no-op, Sharded ⇒
  required).
- No new code in `schema_acquire.py`; the interim "compute locally"
  alternative from `multitenant_prd.md` §3 is skipped.

**`metadata.multitenancy`:**

- `arango_cypher/nl2cypher/tenant_scope.py::analyze_tenant_scope`
  consumes `multitenancy.tenantKey` directly instead of re-running its
  denorm-field regex. Local regex heuristic retained as fallback for
  analyzer bundles older than this release, same contract as #06.
- `arango_cypher/nl2cypher/tenant_guardrail.py` reads
  `multitenancy.physicalEnforcement` to decide whether to escalate
  guardrail failures to refusals (enforced ⇒ hard refuse; convention
  ⇒ warn + retry).

---

## 2) `shardFamilies` — landed upstream as PRD §6.2 bullet 5

**Now part of PRD §6.2 bullet 5** (was originally proposed here as a
small addition outside §6.2; folded into the upstream PRD before
implementation). Shipped in `arangodb-schema-analyzer` v0.6.0
(mapper PR #16) and adopted downstream in this repo's
`feat/adopt-sharding-profile` branch — see the table above.

### 2.1 Motivation

Hybrid schemas commonly duplicate structurally-identical collections
keyed on a per-repo / per-stream / per-upstream-source dimension:

```
IBEX_Documents     → IBEXDocument
MAROCCHINO_Documents → MAROCCHINODocument
MOR1KX_Documents   → MOR1KXDocument
OR1200_Documents   → OR1200Document
```

All four share the same property set, differ only in an implicit
discriminator (here, `repo`), and collectively form one logical entity
from the LLM / NL→Cypher perspective. The mapper today lists them as
four independent entities; downstream consumers that want to reason
about the family (NL prompt builder; UI mapping panel; future
visualizers) have to re-derive it with their own heuristic.

This is **not multi-tenancy** (it's a per-source data-organization
pattern, not a per-customer isolation boundary). It deserves its own
block so `multitenancy.style == "none"` remains correct while
`shardFamilies` captures the structural duplication.

### 2.2 Proposed emission — `physicalMapping.shardFamilies[]`

```jsonc
"physicalMapping": {
  "entities": { ... },
  "shardFamilies": [
    {
      "name": "Document",
      "suffix": "Document",
      "discriminator": { "source": "collection_prefix", "field": "repo" },
      "sharedProperties": ["doc_version", "label", "path", "source_commit"],
      "members": [
        { "entity": "IBEXDocument",       "collectionName": "IBEX_Documents",       "discriminatorValue": "IBEX" },
        { "entity": "MAROCCHINODocument", "collectionName": "MAROCCHINO_Documents", "discriminatorValue": "MAROCCHINO" },
        { "entity": "MOR1KXDocument",     "collectionName": "MOR1KX_Documents",     "discriminatorValue": "MOR1KX" },
        { "entity": "OR1200Document",     "collectionName": "OR1200_Documents",     "discriminatorValue": "OR1200" }
      ]
    }
  ]
}
```

### 2.3 Detection rules (deterministic, no LLM)

1. Bucket entities by `sha256(sorted(property_names))`. Skip buckets of
   size < 2.
2. Within each bucket, find the longest common suffix of the conceptual
   entity names that is ≥ 4 characters and ends on a capital-letter
   boundary. Skip buckets with no qualifying suffix.
3. Extract the prefix as the discriminator candidate (`IBEX`,
   `MAROCCHINO`, …). Optionally probe for a matching field on the
   collection (default `repo`, configurable via
   `SCHEMA_ANALYZER_SHARD_DISCRIMINATOR_FIELDS`). When found, record
   `discriminator.source = "field"` + `discriminator.field`. When not
   found but prefix is consistent, record
   `discriminator.source = "collection_prefix"`.
4. Emit one family entry per confirmed bucket. Families of 1 are never
   emitted.

### 2.4 Downstream impact — landed 2026-04-23

- `nl2cypher/_core.py::_shard_families_block` renders families as
  grouped sections in the LLM prompt, with an explicit hint that a
  repo-agnostic question must UNION across members. This directly
  retired the class-of-error that produced the
  "`no entity mapping for AppVersion`" / wrong-shard picks reported
  in `docs/schema_inference_bugfix_prd.md` candidate D7.
- UI mapping panel renders each family as a collapsible summary row
  with a member-count badge and member details. This was out of scope
  for the `feat/adopt-sharding-profile` adoption PR and later landed
  as a downstream UI paper-cut.

### 2.5 How this got filed — historical

Filed as a small addition to `arango-schema-mapper/docs/PRD.md` §6.2
— a fourth bullet alongside VCI / sharding / multitenancy, following
the same style. Accepted upstream, shipped in
`arango-schema-mapper` v0.6.0 (mapper PR #16, merged into the upstream
PRD as §6.2 bullet 5). Retained here as the as-filed proposal snapshot
for provenance; no further action.

---

## 3) Cross-reference: what the downstream-PRD trail says

- `multitenant_prd.md` §3 (Schema mapper requirements) — now
  satisfied by upstream `shardingProfile` + `multitenancy`.
  MT-0 work-package is marked **superseded** once Phase 1 ships.
- `schema_inference_bugfix_prd.md` candidate D7 (parallel-shard
  detection) — routed to §2 of this document.
- `schema_inference_bugfix_prd.md` candidate D8 (property existence
  check in resolver) — stays local in
  `arango_cypher/nl2cypher/entity_resolution.py`; not a mapper
  concern.

---

## 4) Close-out summary (2026-04-28)

| Upstream block | Shipped in | Adopted in `arango-cypher-py` | Retired locally |
|---|---|---|---|
| `metadata.shardingProfile` (§6.2 bullet 3) | analyzer v0.5.0 | PR #6 | `_deployment_style_hint` now reads `shardingProfile.style` directly (heuristic retained as fallback for pre-0.5 bundles). |
| `metadata.multitenancy` (§6.2 bullet 4) | analyzer v0.6.0 | PR #6 + PR #9 | `tenant_scope.analyze_tenant_scope` consumes `multitenancy.tenantKey`; `tenant_guardrail` reads `physicalEnforcement` for log-triage labelling (PR #9 §3.1/§3.2 docs). |
| `physicalMapping.shardFamilies` (§6.2 bullet 5) | analyzer v0.6.0 | PR #6 | `_shard_families_block` renders families in the NL prompt; D7 (parallel-shard detection) retired. |

**Still open upstream (not blocking):**

- **VCI detection** (§6.2 bullet 2) — first-class `VCI` mapping style +
  schema-level duplication detection. Per-index `vci=true` flag already
  consumed locally (issue #01, shipped in analyzer v0.2.0). First-class
  emission is an orthogonal capability upgrade; tracked by the analyzer
  roadmap, not re-filed here.

**Downstream paper-cut closed:**

- UI mapping panel renders shard-family groups as collapsible rows with
  member-count badges. Cosmetic; no functional gap.

[upstream-prd-commit]: https://github.com/ArthurKeen/arango-schema-mapper/commit/b3d4744
