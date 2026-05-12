"""MT-0 residual / Wave 7 part 2 — ``compute_scoping_path`` BFS regressions.

Pins the contract from ``docs/multitenant_prd.md`` §3 (last paragraph)
and ``docs/agent_prompts_multitenant.md`` Wave 7 part 2:

* Direct edge from ``Tenant`` to the target → 1-hop path.
* Two hops via an intermediate entity → 2-hop path; the BFS picks the
  shortest available chain.
* Tied path lengths → deterministic lexicographic tiebreak on the
  relationship-type-name sequence.
* Unreachable target → ``None``.
* Cyclic graph → BFS terminates and still returns the shortest path.
* ``analyze_tenant_scope`` populates ``EntityScope.scoping_path`` for
  ``TENANT_SCOPED`` entities that lack a denorm field; entities with
  a denorm field, tenant root, and ``GLOBAL`` entities have ``None``.
"""

from __future__ import annotations

from arango_cypher.nl2cypher.tenant_scope import (
    EntityTenantRole,
    analyze_tenant_scope,
    compute_scoping_path,
)


def _conceptual(entities: list[str], relationships: list[dict]) -> dict:
    """Build a minimal conceptual-schema dict with named entities and
    typed relationships in the canonical ``fromEntity`` / ``toEntity``
    / ``type`` shape (the post-analyzer-0.4 contract used by the
    existing relationship-graph builder)."""
    return {
        "entities": [{"name": name} for name in entities],
        "relationships": relationships,
    }


class TestComputeScopingPathBfs:
    """BFS correctness over hand-crafted conceptual schemas."""

    def test_direct_edge_returns_single_relationship(self) -> None:
        cs = _conceptual(
            ["Tenant", "Device"],
            [{"fromEntity": "Tenant", "toEntity": "Device", "type": "TENANTDEVICE"}],
        )
        assert compute_scoping_path(cs, to_label="Device") == ("TENANTDEVICE",)

    def test_two_hop_path_via_intermediate(self) -> None:
        cs = _conceptual(
            ["Tenant", "TenantUser", "GSuiteUser"],
            [
                {"fromEntity": "Tenant", "toEntity": "TenantUser", "type": "TENANTUSERTENANT"},
                {"fromEntity": "TenantUser", "toEntity": "GSuiteUser", "type": "GSUITEUSERTENANTUSER"},
            ],
        )
        assert compute_scoping_path(cs, to_label="GSuiteUser") == (
            "TENANTUSERTENANT",
            "GSUITEUSERTENANTUSER",
        )

    def test_three_hop_path(self) -> None:
        cs = _conceptual(
            ["Tenant", "TenantUser", "GSuiteUser", "Device"],
            [
                {"fromEntity": "Tenant", "toEntity": "TenantUser", "type": "TUT"},
                {"fromEntity": "TenantUser", "toEntity": "GSuiteUser", "type": "GST"},
                {"fromEntity": "GSuiteUser", "toEntity": "Device", "type": "DGS"},
            ],
        )
        assert compute_scoping_path(cs, to_label="Device") == ("TUT", "GST", "DGS")

    def test_bfs_prefers_shorter_path(self) -> None:
        """Multiple paths exist; BFS picks the 2-hop over the 3-hop."""
        cs = _conceptual(
            ["Tenant", "T1", "T2", "Asset"],
            [
                {"fromEntity": "Tenant", "toEntity": "T1", "type": "AAA"},
                {"fromEntity": "T1", "toEntity": "T2", "type": "BBB"},
                {"fromEntity": "T2", "toEntity": "Asset", "type": "CCC"},
                {"fromEntity": "T1", "toEntity": "Asset", "type": "ZZZ"},
            ],
        )
        assert compute_scoping_path(cs, to_label="Asset") == ("AAA", "ZZZ")

    def test_bfs_lexicographic_tiebreak_on_equal_lengths(self) -> None:
        """Two paths of equal length → lexicographically smallest wins
        (sequence-wise; ``ALPHA`` < ``BRAVO`` etc.)."""
        cs = _conceptual(
            ["Tenant", "A", "B", "Asset"],
            [
                {"fromEntity": "Tenant", "toEntity": "A", "type": "BRAVO"},
                {"fromEntity": "A", "toEntity": "Asset", "type": "RIGHT"},
                {"fromEntity": "Tenant", "toEntity": "B", "type": "ALPHA"},
                {"fromEntity": "B", "toEntity": "Asset", "type": "RIGHT"},
            ],
        )
        assert compute_scoping_path(cs, to_label="Asset") == ("ALPHA", "RIGHT")

    def test_disconnected_target_returns_none(self) -> None:
        cs = _conceptual(
            ["Tenant", "Connected", "Floater"],
            [{"fromEntity": "Tenant", "toEntity": "Connected", "type": "T_C"}],
        )
        assert compute_scoping_path(cs, to_label="Floater") is None

    def test_unknown_target_returns_none(self) -> None:
        cs = _conceptual(
            ["Tenant", "Device"],
            [{"fromEntity": "Tenant", "toEntity": "Device", "type": "TENANTDEVICE"}],
        )
        assert compute_scoping_path(cs, to_label="NotPresent") is None

    def test_self_path_returns_empty_tuple(self) -> None:
        cs = _conceptual(
            ["Tenant", "Device"],
            [{"fromEntity": "Tenant", "toEntity": "Device", "type": "TENANTDEVICE"}],
        )
        assert compute_scoping_path(cs, to_label="Tenant") == ()

    def test_cycle_does_not_loop(self) -> None:
        cs = _conceptual(
            ["Tenant", "A", "B"],
            [
                {"fromEntity": "Tenant", "toEntity": "A", "type": "T_A"},
                {"fromEntity": "A", "toEntity": "B", "type": "A_B"},
                {"fromEntity": "B", "toEntity": "A", "type": "B_A_BACK"},
            ],
        )
        assert compute_scoping_path(cs, to_label="B") == ("T_A", "A_B")

    def test_max_hops_truncates(self) -> None:
        cs = _conceptual(
            ["Tenant", "X1", "X2", "X3", "X4", "Far"],
            [
                {"fromEntity": "Tenant", "toEntity": "X1", "type": "R1"},
                {"fromEntity": "X1", "toEntity": "X2", "type": "R2"},
                {"fromEntity": "X2", "toEntity": "X3", "type": "R3"},
                {"fromEntity": "X3", "toEntity": "X4", "type": "R4"},
                {"fromEntity": "X4", "toEntity": "Far", "type": "R5"},
            ],
        )
        assert compute_scoping_path(cs, to_label="Far", max_hops=3) is None
        assert compute_scoping_path(cs, to_label="Far", max_hops=10) == (
            "R1",
            "R2",
            "R3",
            "R4",
            "R5",
        )

    def test_traversal_walks_undirected_edges(self) -> None:
        """The conceptual graph is undirected for tenant-scope BFS — an
        edge declared ``Asset -> Tenant`` is still walkable in reverse
        because the analyzer occasionally swaps endpoint order.
        """
        cs = _conceptual(
            ["Tenant", "Asset"],
            [{"fromEntity": "Asset", "toEntity": "Tenant", "type": "ASSETOFTENANT"}],
        )
        assert compute_scoping_path(cs, to_label="Asset") == ("ASSETOFTENANT",)


class TestAnalyzeTenantScopePopulatesScopingPath:
    """``analyze_tenant_scope`` writes the BFS-derived path onto every
    ``TENANT_SCOPED`` entity that lacks a denormalised tenant column.
    """

    def test_traversal_only_entity_gets_scoping_path(self) -> None:
        """Wave 7 part 2 acceptance: a `TENANT_SCOPED` entity reachable
        from Tenant via a 2-hop chain (no denorm field) carries the
        relationship-type sequence on its ``EntityScope``.
        """
        mapping = {
            "conceptual_schema": _conceptual(
                ["Tenant", "TenantUser", "GSuiteUser"],
                [
                    {"fromEntity": "Tenant", "toEntity": "TenantUser", "type": "TUT"},
                    {"fromEntity": "TenantUser", "toEntity": "GSuiteUser", "type": "GST"},
                ],
            ),
            "physical_mapping": {"entities": {}},
        }
        manifest = analyze_tenant_scope(mapping)
        gsu = manifest.entities["GSuiteUser"]
        assert gsu.role is EntityTenantRole.TENANT_SCOPED
        assert gsu.denorm_field is None
        assert gsu.scoping_path == ("TUT", "GST")
        assert manifest.scoping_path_of("GSuiteUser") == ("TUT", "GST")

    def test_denorm_field_entity_has_no_scoping_path(self) -> None:
        """When the entity carries a denorm column the path is left
        ``None`` — Layer 3 will prefer the column-filter rewrite over
        a traversal.
        """
        mapping = {
            "conceptual_schema": {
                "entities": [
                    {"name": "Tenant"},
                    {
                        "name": "Device",
                        "properties": ["TENANT_ID", "MAC"],
                    },
                ],
                "relationships": [
                    {"fromEntity": "Tenant", "toEntity": "Device", "type": "TENANTDEVICE"},
                ],
            },
            "physical_mapping": {"entities": {}},
        }
        manifest = analyze_tenant_scope(mapping)
        dev = manifest.entities["Device"]
        assert dev.role is EntityTenantRole.TENANT_SCOPED
        assert dev.denorm_field == "TENANT_ID"
        assert dev.scoping_path is None

    def test_tenant_root_and_global_entities_have_none(self) -> None:
        mapping = {
            "conceptual_schema": {
                "entities": [
                    {"name": "Tenant"},
                    {"name": "Cve", "properties": ["CVE_ID"]},
                ],
                "relationships": [],
            },
            "physical_mapping": {"entities": {}},
        }
        manifest = analyze_tenant_scope(mapping)
        assert manifest.entities["Tenant"].role is EntityTenantRole.TENANT_ROOT
        assert manifest.entities["Tenant"].scoping_path is None
        assert manifest.entities["Cve"].role is EntityTenantRole.GLOBAL
        assert manifest.entities["Cve"].scoping_path is None

    def test_explicit_tenant_scoped_without_denorm_gets_path(self) -> None:
        """Upstream-annotated ``tenant_scoped`` entry without
        ``tenantField`` → BFS-derived path is computed and attached.
        """
        mapping = {
            "conceptual_schema": _conceptual(
                ["Tenant", "Asset"],
                [{"fromEntity": "Tenant", "toEntity": "Asset", "type": "OWNS_ASSET"}],
            ),
            "physical_mapping": {
                "entities": {
                    "Asset": {
                        "style": "COLLECTION",
                        "collectionName": "Asset",
                        "tenantScope": {"role": "tenant_scoped"},
                    },
                },
            },
        }
        manifest = analyze_tenant_scope(mapping)
        asset = manifest.entities["Asset"]
        assert asset.role is EntityTenantRole.TENANT_SCOPED
        assert asset.denorm_field is None
        assert asset.scoping_path == ("OWNS_ASSET",)
