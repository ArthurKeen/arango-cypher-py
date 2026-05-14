"""Tests for the Wave 8a MT-4a AQL AST rewriter core.

Pins the contract that the MT-4a slice of
``arango_cypher.tenant_ast_aql.inject_tenant_scope`` ships:

* Satellite / system / GLOBAL collections are unchanged.
* Tenant-scoped collections receive a ``FILTER <var>.<field> ==
  @<bind>`` immediately after the matching ``FOR <var> IN <coll>``.
* A collection lacking both a denormalised field and a scoping path
  is **rejected** (manifest is incomplete; cannot safely constrain).
* ``SubqueryNode`` bodies are walked recursively.
* ``TraversalNode`` and collection-arg function calls raise
  :class:`TenantScopeRewriteIncomplete` — MT-4b owns those.
* The rewrite is idempotent: ``inject(inject(aql)) == inject(aql)``
  byte-for-byte.
* The returned ``augmented_bind_vars`` contains no literal tenant
  value (T2 defence; the only legitimate carrier of a tenant id /
  key is the canonical ``@tenantId`` / ``@tenantKey`` bind).
* The ``changes`` list is human-readable, one entry per splice,
  shaped like the spec's example.

All tests use the ``plan_override`` test hook to avoid the live
``db.aql.explain`` round-trip. The plan-node shapes mirror what
ArangoDB EXPLAIN actually emits — see
``tenant_plan_validator._PlanWalker`` for the canonical
interpretation — so the same fixtures could be reused to pin
Layer 5's contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.tenant_ast_aql import (
    TenantScopeRewriteIncomplete,
    TenantScopeRewriteRejection,
    inject_tenant_scope,
)

# ---------------------------------------------------------------------------
# Manifest + plan-node helpers
# ---------------------------------------------------------------------------


def _scope(
    role: EntityTenantRole,
    *,
    denorm: str | None = None,
    path: tuple[str, ...] | None = None,
) -> EntityScope:
    return EntityScope(
        role=role,
        denorm_field=denorm,
        reachable_from_tenant=role in (EntityTenantRole.TENANT_ROOT, EntityTenantRole.TENANT_SCOPED),
        scoping_path=path,
    )


def _manifest(
    entities: dict[str, EntityScope],
    *,
    tenant_entity: str | None = "Tenant",
    known_tenant_keys: frozenset[str] | None = None,
) -> TenantScopeManifest:
    return TenantScopeManifest(
        tenant_entity=tenant_entity,
        entities=entities,
        known_tenant_keys=known_tenant_keys,
    )


def _enumerate_node(*, collection: str, out_var: str) -> dict[str, Any]:
    return {
        "type": "EnumerateCollectionNode",
        "collection": collection,
        "outVariable": {"name": out_var, "id": 0},
    }


def _calc_eq_bindvar(
    *,
    var_name: str,
    attr: str,
    bind: str,
    out_name: str = "calc0",
) -> dict[str, Any]:
    """Build a CalculationNode whose expression is
    ``<var_name>.<attr> == @<bind>``.

    Used by the idempotency tests to seed the plan with an existing
    FILTER (which ArangoDB lifts into a CalculationNode + FilterNode
    pair during EXPLAIN).
    """
    return {
        "type": "CalculationNode",
        "outVariable": {"name": out_name, "id": 99},
        "expression": {
            "type": "compare ==",
            "subNodes": [
                {
                    "type": "attribute access",
                    "name": attr,
                    "subNodes": [{"type": "reference", "name": var_name}],
                },
                {"type": "parameter", "name": bind},
            ],
        },
    }


def _calc_function_call_over_collection(
    *,
    function: str,
    collection: str,
    out_name: str = "calcFC",
) -> dict[str, Any]:
    """CalculationNode whose expression is ``LENGTH(<coll>)`` or similar.

    Models how ArangoDB EXPLAIN serialises ``RETURN LENGTH(Employee)``:
    a CalculationNode containing a ``function call`` subtree whose
    argument is a ``collection`` reference.
    """
    return {
        "type": "CalculationNode",
        "outVariable": {"name": out_name, "id": 99},
        "expression": {
            "type": "function call",
            "name": function,
            "subNodes": [{"type": "collection", "name": collection}],
        },
    }


# Manifests reused across cases ---------------------------------------------


def _satellite_country_manifest() -> TenantScopeManifest:
    return _manifest({"Country": _scope(EntityTenantRole.GLOBAL)})


def _global_only_manifest() -> TenantScopeManifest:
    # Identical to the satellite manifest from the manifest's point of
    # view — the satellite/system short-circuit happens in
    # `predicate_for_collection` via the sharding profile, not in the
    # manifest. We pair this with a profile that classifies Country as
    # `satellite`.
    return _manifest({"Country": _scope(EntityTenantRole.GLOBAL)})


def _employee_manifest() -> TenantScopeManifest:
    return _manifest(
        {
            "Employee": _scope(
                EntityTenantRole.TENANT_SCOPED,
                denorm="TENANT_HEX_ID",
            ),
        }
    )


def _employee_unscopable_manifest() -> TenantScopeManifest:
    # TENANT_SCOPED with neither denorm field nor scoping path.
    return _manifest({"Employee": _scope(EntityTenantRole.TENANT_SCOPED)})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnumerateCollection:
    def test_satellite_only_no_change(self) -> None:
        aql = "FOR c IN Country RETURN c"
        plan = {"nodes": [_enumerate_node(collection="Country", out_var="c")]}
        profile = {"collections": [{"name": "Country", "kind": "satellite"}]}
        rewritten, binds, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={},
            manifest=_satellite_country_manifest(),
            sharding_profile=profile,
            tenant_id="tenant-A",
            tenant_key="tenant-A-key",
            plan_override=plan,
        )
        assert rewritten == aql
        assert binds == {}
        assert changes == []

    def test_global_collection_no_change(self) -> None:
        # Same shape as the satellite case but no sharding profile —
        # the collection name maps to a GLOBAL entity and
        # `predicate_for_entity` returns None directly.
        aql = "FOR c IN Country RETURN c"
        plan = {"nodes": [_enumerate_node(collection="Country", out_var="c")]}
        rewritten, binds, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={},
            manifest=_global_only_manifest(),
            sharding_profile=None,
            tenant_id="tenant-A",
            tenant_key="tenant-A-key",
            plan_override=plan,
        )
        assert rewritten == aql
        assert changes == []

    def test_tenant_scoped_collection_filter_injected(self) -> None:
        aql = "FOR e IN Employee RETURN e"
        plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        rewritten, binds, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "tenant-A"},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="tenant-A",
            tenant_key="tenant-A-key",
            plan_override=plan,
        )
        # The FILTER is spliced immediately after the FOR line.
        assert "FOR e IN Employee\nFILTER e.TENANT_HEX_ID == @tenantId" in rewritten
        assert rewritten.endswith("RETURN e")
        # bind_vars not augmented in MT-4a.
        assert binds == {"tenantId": "tenant-A"}
        assert len(changes) == 1
        assert changes[0] == "Added FILTER e.TENANT_HEX_ID == @tenantId after FOR e IN Employee"

    def test_filter_preserves_indentation(self) -> None:
        # The transpiler emits indented AQL; the spliced FILTER must
        # carry the same leading whitespace as the host FOR.
        aql = "LET subq = (\n    FOR e IN Employee\n        RETURN e\n)"
        plan = {
            "nodes": [
                {
                    "type": "SubqueryNode",
                    "subquery": {
                        "nodes": [_enumerate_node(collection="Employee", out_var="e")],
                    },
                },
            ],
        }
        rewritten, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="tenant-A",
            tenant_key="tenant-A-key",
            plan_override=plan,
        )
        assert "    FOR e IN Employee\n    FILTER e.TENANT_HEX_ID == @tenantId" in rewritten
        assert len(changes) == 1


class TestIdempotency:
    def test_existing_filter_kept_no_dup(self) -> None:
        # The plan shows both an EnumerateCollectionNode for Employee
        # AND a downstream CalculationNode with `e.TENANT_HEX_ID ==
        # @tenantId`. The rewriter must detect the existing predicate
        # and produce a byte-identical AQL.
        aql = "FOR e IN Employee\nFILTER e.TENANT_HEX_ID == @tenantId\nRETURN e"
        plan = {
            "nodes": [
                _enumerate_node(collection="Employee", out_var="e"),
                _calc_eq_bindvar(var_name="e", attr="TENANT_HEX_ID", bind="tenantId"),
            ],
        }
        rewritten, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "tenant-A"},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="tenant-A",
            tenant_key="tenant-A-key",
            plan_override=plan,
        )
        assert rewritten == aql
        assert changes == []

    def test_idempotent_double_pass(self) -> None:
        # Run inject twice with progressively-updated plans. After the
        # first pass the AQL has a FILTER; the "second pass" supplies a
        # plan that mirrors that state (EnumerateCollectionNode +
        # CalculationNode). Output must be byte-identical to the
        # first pass.
        aql = "FOR e IN Employee\nRETURN e"
        first_plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        manifest = _employee_manifest()
        first_rewrite, _, _ = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "t"},
            manifest=manifest,
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=first_plan,
        )
        second_plan = {
            "nodes": [
                _enumerate_node(collection="Employee", out_var="e"),
                _calc_eq_bindvar(var_name="e", attr="TENANT_HEX_ID", bind="tenantId"),
            ],
        }
        second_rewrite, _, second_changes = inject_tenant_scope(
            db=None,
            aql=first_rewrite,
            bind_vars={"tenantId": "t"},
            manifest=manifest,
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=second_plan,
        )
        assert second_rewrite == first_rewrite
        assert second_changes == []


class TestRejection:
    def test_collection_lacking_both_attrs_rejected(self) -> None:
        aql = "FOR e IN Employee RETURN e"
        plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        with pytest.raises(TenantScopeRewriteRejection) as ei:
            inject_tenant_scope(
                db=None,
                aql=aql,
                bind_vars={},
                manifest=_employee_unscopable_manifest(),
                sharding_profile=None,
                tenant_id="t",
                tenant_key="tk",
                plan_override=plan,
            )
        assert ei.value.code == "UNCONSTRAINED_COLLECTION_ACCESS"
        assert "Employee" in ei.value.message
        assert "Employee" in ei.value.where


class TestSubquery:
    def test_subquery_recursion(self) -> None:
        aql = "LET xs = (\nFOR e IN Employee\nRETURN e\n)\nRETURN xs"
        plan = {
            "nodes": [
                {
                    "type": "SubqueryNode",
                    "subquery": {
                        "nodes": [_enumerate_node(collection="Employee", out_var="e")],
                    },
                },
            ],
        }
        rewritten, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "t"},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=plan,
        )
        assert "FOR e IN Employee\nFILTER e.TENANT_HEX_ID == @tenantId" in rewritten
        assert len(changes) == 1

    def test_subquery_idempotency_isolated_per_scope(self) -> None:
        # A calc node in the OUTER scope must NOT satisfy the
        # idempotency check for an EnumerateCollectionNode in the
        # INNER subquery — that would be a tenant-scope leak vector.
        # We pin the contract by giving the outer scope a calc that
        # references `e.TENANT_HEX_ID == @tenantId` (note: `e` is the
        # subquery's variable; the outer scope's calc cannot legitimately
        # reference it, but if the walker leaked scopes it would
        # falsely think the inner Enumerate was already filtered).
        aql = "LET xs = (\nFOR e IN Employee\nRETURN e\n)\nRETURN xs"
        plan = {
            "nodes": [
                # Outer-scope calc — must not be visible to the
                # subquery's idempotency check.
                _calc_eq_bindvar(var_name="e", attr="TENANT_HEX_ID", bind="tenantId"),
                {
                    "type": "SubqueryNode",
                    "subquery": {
                        "nodes": [_enumerate_node(collection="Employee", out_var="e")],
                    },
                },
            ],
        }
        rewritten, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "t"},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=plan,
        )
        # Filter should still be injected — outer calc cannot count.
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in rewritten
        assert len(changes) == 1


class TestIncomplete:
    def test_traversal_raises_incomplete(self) -> None:
        aql = "FOR v, e, p IN 1..3 OUTBOUND 'Employee/1' GRAPH 'g' RETURN v"
        plan = {
            "nodes": [
                {
                    "type": "TraversalNode",
                    "graphName": "g",
                    "graph": {"vertexCollections": ["Employee"]},
                    "options": {},
                },
            ],
        }
        with pytest.raises(TenantScopeRewriteIncomplete) as ei:
            inject_tenant_scope(
                db=None,
                aql=aql,
                bind_vars={},
                manifest=_employee_manifest(),
                sharding_profile=None,
                tenant_id="t",
                tenant_key="tk",
                plan_override=plan,
            )
        assert "TraversalNode" in str(ei.value)

    def test_function_call_collection_raises_incomplete(self) -> None:
        aql = "RETURN LENGTH(Employee)"
        plan = {
            "nodes": [
                _calc_function_call_over_collection(function="LENGTH", collection="Employee"),
            ],
        }
        with pytest.raises(TenantScopeRewriteIncomplete) as ei:
            inject_tenant_scope(
                db=None,
                aql=aql,
                bind_vars={},
                manifest=_employee_manifest(),
                sharding_profile=None,
                tenant_id="t",
                tenant_key="tk",
                plan_override=plan,
            )
        assert "function" in str(ei.value).lower()
        assert "Employee" in str(ei.value)

    def test_function_call_nested_in_expression_raises(self) -> None:
        # The function-call subtree may be buried inside a larger
        # expression (e.g. `LENGTH(Employee) > 0`). The walker must
        # recurse and still raise.
        aql = "FILTER LENGTH(Employee) > 0 RETURN 1"
        plan = {
            "nodes": [
                {
                    "type": "CalculationNode",
                    "outVariable": {"name": "calc", "id": 1},
                    "expression": {
                        "type": "compare >",
                        "subNodes": [
                            {
                                "type": "function call",
                                "name": "LENGTH",
                                "subNodes": [{"type": "collection", "name": "Employee"}],
                            },
                            {"type": "value", "value": 0},
                        ],
                    },
                },
            ],
        }
        with pytest.raises(TenantScopeRewriteIncomplete):
            inject_tenant_scope(
                db=None,
                aql=aql,
                bind_vars={},
                manifest=_employee_manifest(),
                sharding_profile=None,
                tenant_id="t",
                tenant_key="tk",
                plan_override=plan,
            )


class TestBindVarHygiene:
    def test_no_literal_tenant_in_bind_vars(self) -> None:
        # MT-4a does not add bind vars; the property check is on the
        # *input*. We pass a known-tenant-keys manifest plus an input
        # binding that is NOT the canonical tenantId — if such a bind
        # carries a known tenant key, the rewriter refuses it.
        manifest = _manifest(
            {"Employee": _scope(EntityTenantRole.TENANT_SCOPED, denorm="TENANT_HEX_ID")},
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        # Canonical tenant binds carrying tenant values: accepted.
        _, binds, _ = inject_tenant_scope(
            db=None,
            aql="FOR e IN Employee RETURN e",
            bind_vars={"tenantId": "tenant-A-uuid", "tenantKey": "tk"},
            manifest=manifest,
            sharding_profile=None,
            tenant_id="tenant-A-uuid",
            tenant_key="tk",
            plan_override=plan,
        )
        # Sanity: the returned dict contains exactly the canonical
        # binds (which legitimately carry the tenant value).
        assert binds == {"tenantId": "tenant-A-uuid", "tenantKey": "tk"}
        # No non-canonical bind in the output carries a known tenant key.
        canonical = {"tenantId", "tenantKey"}
        for name, value in binds.items():
            if name in canonical:
                continue
            assert value not in manifest.known_tenant_keys  # type: ignore[operator]

    def test_non_canonical_bind_with_tenant_value_rejected(self) -> None:
        # A bind var named something OTHER than @tenantId / @tenantKey
        # but carrying a known tenant key is a T2 injection attempt.
        manifest = _manifest(
            {"Employee": _scope(EntityTenantRole.TENANT_SCOPED, denorm="TENANT_HEX_ID")},
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        with pytest.raises(TenantScopeRewriteRejection) as ei:
            inject_tenant_scope(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={"tenantId": "tenant-A-uuid", "evilBind": "tenant-A-uuid"},
                manifest=manifest,
                sharding_profile=None,
                tenant_id="tenant-A-uuid",
                tenant_key="tk",
                plan_override=plan,
            )
        assert ei.value.code == "LITERAL_TENANT_PREDICATE"
        assert "evilBind" in ei.value.message


class TestChangesList:
    def test_changes_list_human_readable(self) -> None:
        # Spec sample: "Added FILTER e.TENANT_HEX_ID == @tenantId
        # after FOR e IN Employee".
        aql = "FOR e IN Employee RETURN e"
        plan = {"nodes": [_enumerate_node(collection="Employee", out_var="e")]}
        _, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=plan,
        )
        assert changes == ["Added FILTER e.TENANT_HEX_ID == @tenantId after FOR e IN Employee"]

    def test_changes_one_per_splice(self) -> None:
        # Multiple FORs over the same tenant-scoped collection (e.g.
        # the transpiler emits a join-like pattern) → one change per
        # splice. The transpiler emits unique variable names so the
        # textual splice anchors are unambiguous.
        aql = "FOR e1 IN Employee\nFOR e2 IN Employee\nRETURN [e1, e2]"
        plan = {
            "nodes": [
                _enumerate_node(collection="Employee", out_var="e1"),
                _enumerate_node(collection="Employee", out_var="e2"),
            ],
        }
        rewritten, _, changes = inject_tenant_scope(
            db=None,
            aql=aql,
            bind_vars={"tenantId": "t"},
            manifest=_employee_manifest(),
            sharding_profile=None,
            tenant_id="t",
            tenant_key="tk",
            plan_override=plan,
        )
        assert len(changes) == 2
        assert "FOR e1 IN Employee\nFILTER e1.TENANT_HEX_ID == @tenantId" in rewritten
        assert "FOR e2 IN Employee\nFILTER e2.TENANT_HEX_ID == @tenantId" in rewritten
