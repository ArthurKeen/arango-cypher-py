"""Tests for the Wave 8-pre shared tenant-rewrite vocabulary.

Pins the contract that the three Wave 8a sub-agents (MT-2, MT-3, MT-4)
will consume:

* :class:`TenantPredicateShape` validates its own arguments at
  construction time so a rewriter cannot accidentally produce a
  malformed predicate.
* :func:`predicate_for_entity` maps a conceptual label through the
  decision tree in PRD §6.2: GLOBAL → None, TENANT_ROOT →
  property_map/_key/tenantKey, TENANT_SCOPED with denorm →
  property_map/<denorm>/tenantId, TENANT_SCOPED with only a scoping
  path → traversal_path/.../tenantKey, unmapped / unscopable →
  UnknownEntityScope.
* :func:`predicate_for_collection` short-circuits to ``None`` for
  satellite / system collections, otherwise resolves via
  ``collectionToEntity`` map and delegates to
  :func:`predicate_for_entity`.
* :func:`is_bindvar_reference` matches the ArangoDB EXPLAIN-plan
  ``parameter`` node shape.
* :func:`is_literal_tenant_value` reads ``manifest.known_tenant_keys``
  and **fails closed** when the key set is ``None``.
"""

from __future__ import annotations

import pytest

from arango_cypher.nl2cypher.tenant_ast_common import (
    TENANT_ID_BIND,
    TENANT_KEY_BIND,
    TenantPredicateShape,
    UnknownEntityScope,
    is_bindvar_reference,
    is_literal_tenant_value,
    predicate_for_collection,
    predicate_for_entity,
)
from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)


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


# ---------------------------------------------------------------------------
# TenantPredicateShape construction-time validation
# ---------------------------------------------------------------------------


class TestTenantPredicateShape:
    def test_property_map_shape_round_trips(self) -> None:
        shape = TenantPredicateShape(
            style="property_map",
            field="TENANT_HEX_ID",
            bind_name=TENANT_ID_BIND,
        )
        assert shape.field == "TENANT_HEX_ID"
        assert shape.bind_name == TENANT_ID_BIND
        assert shape.scoping_path is None

    def test_traversal_path_shape_requires_path_and_no_field(self) -> None:
        shape = TenantPredicateShape(
            style="traversal_path",
            field=None,
            bind_name=TENANT_KEY_BIND,
            scoping_path=("TENANTUSERTENANT", "GSUITEUSERTENANTUSER"),
        )
        assert shape.scoping_path == ("TENANTUSERTENANT", "GSUITEUSERTENANTUSER")
        assert shape.field is None

    def test_invalid_style_rejected(self) -> None:
        with pytest.raises(ValueError, match="style"):
            TenantPredicateShape(
                style="not_a_real_style",
                field="x",
                bind_name=TENANT_ID_BIND,
            )

    def test_literal_bind_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="bind_name"):
            TenantPredicateShape(
                style="property_map",
                field="x",
                bind_name="literal-value",
            )

    def test_property_map_without_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="field"):
            TenantPredicateShape(
                style="property_map",
                field=None,
                bind_name=TENANT_ID_BIND,
            )

    def test_traversal_path_with_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="field=None"):
            TenantPredicateShape(
                style="traversal_path",
                field="x",
                bind_name=TENANT_KEY_BIND,
                scoping_path=("R1",),
            )

    def test_traversal_path_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="scoping_path"):
            TenantPredicateShape(
                style="traversal_path",
                field=None,
                bind_name=TENANT_KEY_BIND,
                scoping_path=(),
            )

    def test_property_map_with_scoping_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="scoping_path"):
            TenantPredicateShape(
                style="property_map",
                field="x",
                bind_name=TENANT_ID_BIND,
                scoping_path=("R1",),
            )


# ---------------------------------------------------------------------------
# predicate_for_entity decision tree
# ---------------------------------------------------------------------------


class TestPredicateForEntity:
    def test_global_returns_none(self) -> None:
        manifest = _manifest({"Country": _scope(EntityTenantRole.GLOBAL)})
        assert predicate_for_entity("Country", manifest) is None

    def test_tenant_root_returns_key_property_map(self) -> None:
        manifest = _manifest({"Tenant": _scope(EntityTenantRole.TENANT_ROOT)})
        shape = predicate_for_entity("Tenant", manifest)
        assert shape is not None
        assert shape.style == "property_map"
        assert shape.field == "_key"
        assert shape.bind_name == TENANT_KEY_BIND

    def test_tenant_scoped_with_denorm_returns_denorm_property_map(self) -> None:
        manifest = _manifest(
            {
                "Employee": _scope(
                    EntityTenantRole.TENANT_SCOPED,
                    denorm="TENANT_HEX_ID",
                ),
            }
        )
        shape = predicate_for_entity("Employee", manifest)
        assert shape is not None
        assert shape.style == "property_map"
        assert shape.field == "TENANT_HEX_ID"
        assert shape.bind_name == TENANT_ID_BIND

    def test_tenant_scoped_traversal_only_returns_path(self) -> None:
        manifest = _manifest(
            {
                "Device": _scope(
                    EntityTenantRole.TENANT_SCOPED,
                    path=("TENANTUSERTENANT", "DEVICETENANTUSER"),
                ),
            }
        )
        shape = predicate_for_entity("Device", manifest)
        assert shape is not None
        assert shape.style == "traversal_path"
        assert shape.field is None
        assert shape.bind_name == TENANT_KEY_BIND
        assert shape.scoping_path == ("TENANTUSERTENANT", "DEVICETENANTUSER")

    def test_unknown_label_raises(self) -> None:
        manifest = _manifest({"Tenant": _scope(EntityTenantRole.TENANT_ROOT)})
        with pytest.raises(UnknownEntityScope) as ei:
            predicate_for_entity("MysteryBox", manifest)
        assert ei.value.name == "MysteryBox"

    def test_tenant_scoped_without_denorm_or_path_raises(self) -> None:
        # Manifest says "Employee is tenant-scoped" but provides neither
        # a denormalised field nor a scoping path — there is no safe
        # way to scope a query to it, so we refuse rather than silently
        # producing a no-op rewrite.
        manifest = _manifest({"Employee": _scope(EntityTenantRole.TENANT_SCOPED)})
        with pytest.raises(UnknownEntityScope):
            predicate_for_entity("Employee", manifest)

    def test_empty_label_raises(self) -> None:
        manifest = _manifest({})
        with pytest.raises(UnknownEntityScope):
            predicate_for_entity("", manifest)


# ---------------------------------------------------------------------------
# predicate_for_collection short-circuits + delegation
# ---------------------------------------------------------------------------


class TestPredicateForCollection:
    def test_satellite_collection_short_circuits_to_none(self) -> None:
        manifest = _manifest({"Country": _scope(EntityTenantRole.GLOBAL)})
        profile = {"collections": [{"name": "Country", "kind": "satellite"}]}
        assert predicate_for_collection("Country", manifest, profile) is None

    def test_system_collection_short_circuits_to_none(self) -> None:
        manifest = _manifest({})
        profile = {"collectionKinds": {"_users": "system"}}
        assert predicate_for_collection("_users", manifest, profile) is None

    def test_collection_resolves_via_collection_to_entity_map(self) -> None:
        manifest = _manifest(
            {
                "Employee": _scope(
                    EntityTenantRole.TENANT_SCOPED,
                    denorm="TENANT_HEX_ID",
                ),
            }
        )
        profile = {
            "collections": [{"name": "EmployeeColl", "kind": "smartgraph"}],
            "collectionToEntity": {"EmployeeColl": "Employee"},
        }
        shape = predicate_for_collection("EmployeeColl", manifest, profile)
        assert shape is not None
        assert shape.field == "TENANT_HEX_ID"
        assert shape.bind_name == TENANT_ID_BIND

    def test_collection_falls_back_to_name_equals_label(self) -> None:
        manifest = _manifest(
            {
                "Employee": _scope(
                    EntityTenantRole.TENANT_SCOPED,
                    denorm="TENANT_HEX_ID",
                ),
            }
        )
        shape = predicate_for_collection("Employee", manifest, sharding_profile=None)
        assert shape is not None
        assert shape.style == "property_map"

    def test_unknown_collection_raises(self) -> None:
        manifest = _manifest({})
        with pytest.raises(UnknownEntityScope):
            predicate_for_collection("Mystery", manifest, sharding_profile=None)


# ---------------------------------------------------------------------------
# is_bindvar_reference plan-node matcher
# ---------------------------------------------------------------------------


class TestIsBindvarReference:
    def test_matches_parameter_with_named_value(self) -> None:
        assert is_bindvar_reference(
            {"type": "parameter", "name": "tenantId"},
            name="tenantId",
        )

    def test_does_not_match_other_bindvar_name(self) -> None:
        assert not is_bindvar_reference(
            {"type": "parameter", "name": "otherBind"},
            name="tenantId",
        )

    def test_does_not_match_non_parameter_node(self) -> None:
        assert not is_bindvar_reference(
            {"type": "reference", "name": "tenantId"},
            name="tenantId",
        )

    def test_does_not_match_non_dict_input(self) -> None:
        assert not is_bindvar_reference("tenantId", name="tenantId")
        assert not is_bindvar_reference(None, name="tenantId")
        assert not is_bindvar_reference(["tenantId"], name="tenantId")


# ---------------------------------------------------------------------------
# is_literal_tenant_value — T2 defence with fail-closed default
# ---------------------------------------------------------------------------


class TestIsLiteralTenantValue:
    def test_known_key_returns_true(self) -> None:
        manifest = _manifest(
            {},
            known_tenant_keys=frozenset({"tenant-A-uuid", "tenant-B-uuid"}),
        )
        assert is_literal_tenant_value("tenant-A-uuid", manifest)
        assert is_literal_tenant_value({"type": "value", "value": "tenant-B-uuid"}, manifest)

    def test_unknown_string_returns_false_when_keyset_populated(self) -> None:
        manifest = _manifest(
            {},
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        assert not is_literal_tenant_value("banana", manifest)
        assert not is_literal_tenant_value({"type": "value", "value": "banana"}, manifest)

    def test_fails_closed_when_keyset_is_none(self) -> None:
        # The fail-closed contract: when the manifest has not sampled
        # the Tenant collection, we refuse to classify any string
        # literal as non-tenant. The rewriter then refuses the
        # comparison rather than silently allowing a tenant literal
        # to slip through (original T2 bug).
        manifest = _manifest({}, known_tenant_keys=None)
        assert is_literal_tenant_value("any-string-at-all", manifest)
        assert is_literal_tenant_value({"type": "value", "value": "x"}, manifest)

    def test_empty_keyset_is_not_fail_closed(self) -> None:
        # frozenset() is a positive statement: "I sampled, and the
        # collection had zero rows". Distinct from None.
        manifest = _manifest({}, known_tenant_keys=frozenset())
        assert not is_literal_tenant_value("anything", manifest)

    def test_non_string_literals_always_false(self) -> None:
        manifest = _manifest({}, known_tenant_keys=None)
        assert not is_literal_tenant_value(42, manifest)
        assert not is_literal_tenant_value(3.14, manifest)
        assert not is_literal_tenant_value(True, manifest)
        assert not is_literal_tenant_value({"type": "value", "value": 42}, manifest)
        assert not is_literal_tenant_value({"type": "value", "value": None}, manifest)

    def test_non_literal_nodes_return_false(self) -> None:
        manifest = _manifest({}, known_tenant_keys=None)
        assert not is_literal_tenant_value({"type": "reference", "name": "x"}, manifest)
        assert not is_literal_tenant_value({"type": "attribute access", "name": "tenant"}, manifest)
        assert not is_literal_tenant_value(None, manifest)


# ---------------------------------------------------------------------------
# Manifest contract: known_tenant_keys field exists and defaults to None
# ---------------------------------------------------------------------------


class TestManifestExtension:
    def test_default_known_tenant_keys_is_none(self) -> None:
        manifest = TenantScopeManifest(tenant_entity=None)
        assert manifest.known_tenant_keys is None

    def test_known_tenant_keys_round_trip(self) -> None:
        keys = frozenset({"tenant-A", "tenant-B"})
        manifest = TenantScopeManifest(
            tenant_entity="Tenant",
            entities={},
            known_tenant_keys=keys,
        )
        assert manifest.known_tenant_keys == keys
