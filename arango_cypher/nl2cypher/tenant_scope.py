"""Per-entity tenant-scope classification for multi-tenant graphs.

Why this exists
---------------
The first cut of the tenant guardrail (``tenant_guardrail.py``) hardcoded
two assumptions that turned out to be wrong as soon as we tried it on a
real schema:

1. *Every* tenant-scoped collection has a denormalised ``TENANT_ID``
   field. — False. Some collections only carry the tenant scope through
   a graph edge (``Tenant -[edge]-> X``), no inline column.
2. *Every* collection in the database is tenant-scoped. — Also false.
   Reference / metadata collections (``Cve``, ``AppVersion``,
   ``Library``, …) are intentionally global so that all tenants share a
   single canonical source. Forcing a ``:Tenant`` binding on a query
   that touches only those is a translation bug, not a security
   measure — it would refuse legitimate queries like "list all CVEs".

As of ``arangodb-schema-analyzer`` v0.4 (upstream issue #13, this
project's PR #14), the analyzer emits a first-class ``tenantScope``
annotation on every ``physicalMapping.entities[*]`` entry, plus a
``metadata.tenantScopeReport`` summary. **Those annotations always
win** — :func:`analyze_tenant_scope` reads them first via
:func:`_explicit_scope_from_mapping` and only falls back to the
local heuristic when the annotation is absent (e.g. hand-crafted
mappings, older analyzer versions, pre-tenant-feature snapshots).

The local heuristic is kept indefinitely for two reasons:

1. **Resilience.** The cypher-py service may receive mapping bundles
   from non-analyzer sources (test fixtures, manual operator
   exports, third-party tools) that don't carry the upstream
   annotation. Refusing to classify them would cripple the guardrail
   in those paths.
2. **Override surface.** The ``NL2CYPHER_TENANT_FIELD_REGEX`` env var
   exists so deployments can teach the heuristic about
   organisation-specific naming conventions (e.g. ``customerId``)
   without requiring a re-analyze. When the upstream annotation is
   present, this env var is irrelevant — the annotation already
   carries the field name as observed at analysis time.

Discovery rules
---------------
For each entity declared in the conceptual schema we assign exactly
one :class:`EntityTenantRole`:

* ``TENANT_ROOT`` — the entity literally named ``Tenant`` (the entry
  point of the tenant hierarchy). At most one per mapping.
* ``TENANT_SCOPED`` — either:

    * carries a denormalised tenant-reference field whose name matches
      the configured regex (default: ``^tenant[_-]?(id|key)$``,
      case-insensitive), in which case ``denorm_field`` is set to the
      *exact* name as it appears in the schema, OR
    * is reachable from the Tenant entity within ``max_traversal_hops``
      steps in the conceptual relationship graph (BFS), in which case
      ``denorm_field`` is ``None`` and the only way to scope a query
      to it is via traversal.

* ``GLOBAL`` — neither of the above. Treated as tenant-independent
  reference data; queries touching only global entities are exempt
  from the tenant guardrail.

Configuration precedence
------------------------
1. **Explicit annotation in the mapping** (highest priority). If a
   physical-mapping entry carries a ``tenantScope`` block (see
   :func:`_explicit_scope_from_mapping`), it wins outright. This is
   the contract the upstream analyzer PR will use.
2. **Discovery from declared properties + relationships.**
3. **Override regex via env var** ``NL2CYPHER_TENANT_FIELD_REGEX``
   (must compile under :mod:`re`). Used when operators have an
   organisation-wide naming convention that the default regex misses
   (e.g. ``customerId`` for an ISV that calls tenants "customers").

The classification is pure (no DB I/O), so we re-derive it on every
call. It's fast — a 50-entity schema classifies in well under a
millisecond — and the alternative (caching on a mutable mapping) was
the source of three earlier "stale tenant scope" bugs.
"""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class EntityTenantRole(StrEnum):
    """Role an entity plays in the tenant hierarchy."""

    TENANT_ROOT = "tenant_root"
    TENANT_SCOPED = "tenant_scoped"
    GLOBAL = "global"


@dataclass(frozen=True)
class EntityScope:
    """Per-entity tenant-scope classification."""

    role: EntityTenantRole
    # Name of the denormalised tenant-reference field on this entity,
    # exactly as it appears in the conceptual properties (e.g.
    # ``"TENANT_ID"`` or ``"tenant_key"``). ``None`` means the only
    # way to scope queries to this entity is via graph traversal from
    # the Tenant entity. Always ``None`` for ``TENANT_ROOT`` and
    # ``GLOBAL`` roles.
    denorm_field: str | None = None
    # True when the entity is reachable from the Tenant entity through
    # the conceptual relationship graph. Used to distinguish "scoped
    # by traversal" (no denorm field, but reachable) from "global"
    # (no denorm field, not reachable).
    reachable_from_tenant: bool = False
    # MT-0 residual (PRD docs/multitenant_prd.md §3, last paragraph /
    # Wave 7 part 2): the BFS-derived edge path from the Tenant entity
    # to this entity, expressed as the ordered list of relationship-
    # type names. ``None`` when the entity is the Tenant root, has a
    # ``denorm_field`` (no traversal needed), is reachable but the
    # path could not be derived deterministically, or is unreachable.
    # Consumed by Wave 8a's Cypher AST rewrite (Layer 3) to inject a
    # constrained traversal pattern for TENANT_SCOPED entities that
    # lack an inline tenant column, and read by Layer 5 to recognise
    # a constrained traversal as safe.
    scoping_path: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TenantScopeManifest:
    """Per-entity tenant-scope manifest derived from a mapping bundle.

    ``tenant_entity`` is the conceptual entity name that plays the
    tenant-root role (typically ``"Tenant"``); ``None`` when the
    schema is single-tenant.

    ``known_tenant_keys`` is the snapshot of tenant-collection keys
    that were known to exist at manifest-build time, populated by
    ``acquire_mapping_bundle`` via a one-shot ``FOR t IN Tenant
    RETURN t._key`` (and analogous denorm-field harvest). Consumed
    by ``tenant_ast_common.is_literal_tenant_value`` (Wave 8-pre)
    so the Layer 2/3/4 rewriters can decide whether a literal in
    user-supplied Cypher/AQL is a tenant key being smuggled in
    (T2 defense; PRD §5.2 / §6.2 / §7.2). ``None`` means "not
    sampled" — callers fail-closed and refuse any literal-tenant
    comparison rather than silently accepting them, because the
    alternative ("can't tell, allow") is the original T2 bug.
    """

    tenant_entity: str | None
    entities: dict[str, EntityScope] = field(default_factory=dict)
    known_tenant_keys: frozenset[str] | None = None

    def role_of(self, entity_name: str) -> EntityTenantRole:
        """Return the role for ``entity_name``, defaulting to GLOBAL.

        Defaulting to GLOBAL is deliberate: an entity referenced in a
        Cypher query that the analyzer doesn't know about cannot be
        retroactively classified as tenant-scoped — refusing the query
        for "missing classification" would surface as a hard-to-debug
        translator failure. GLOBAL is the safe default because if the
        entity *is* in fact tenant-scoped, the schema analyzer
        discovery is the bug to fix.
        """
        e = self.entities.get(entity_name)
        return e.role if e else EntityTenantRole.GLOBAL

    def denorm_field_of(self, entity_name: str) -> str | None:
        e = self.entities.get(entity_name)
        return e.denorm_field if e else None

    def scoped_entities(self) -> list[str]:
        """Names of all entities classified as ``TENANT_SCOPED``."""
        return [name for name, scope in self.entities.items() if scope.role is EntityTenantRole.TENANT_SCOPED]

    def global_entities(self) -> list[str]:
        return [name for name, scope in self.entities.items() if scope.role is EntityTenantRole.GLOBAL]

    def scoping_path_of(self, entity_name: str) -> tuple[str, ...] | None:
        """Return the relationship-type path from ``Tenant`` to
        ``entity_name``, or ``None`` if no path was derived.

        See :class:`EntityScope.scoping_path` for the contract.
        """
        e = self.entities.get(entity_name)
        return e.scoping_path if e else None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

# Default regex matches TENANT_ID, tenant_id, tenantId, tenant_key,
# TENANT-ID, etc. The character class allows underscore or hyphen
# between "tenant" and "id"/"key", or no separator at all (to catch
# camelCase). Case-insensitive.
_DEFAULT_TENANT_FIELD_REGEX = re.compile(r"^tenant[_-]?(id|key)$", re.IGNORECASE)

_TENANT_ENTITY_NAME = "Tenant"


def _resolve_tenant_field_regex() -> re.Pattern[str]:
    """Honor the NL2CYPHER_TENANT_FIELD_REGEX env var when present.

    Falls back silently to the default on a bad pattern — this is a
    convenience knob, not a security boundary, so we don't want a
    deployment misconfiguration to crash translation.
    """
    raw = os.environ.get("NL2CYPHER_TENANT_FIELD_REGEX")
    if not raw:
        return _DEFAULT_TENANT_FIELD_REGEX
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return _DEFAULT_TENANT_FIELD_REGEX


def _multitenancy_tenant_keys(mapping: Any) -> list[str]:
    """Return ``metadata.multitenancy.tenantKey`` as a list of strings.

    Consumes the database-wide tenant-key hint emitted by
    ``arangodb-schema-analyzer>=0.6`` (upstream PRD §6.2 bullet 4,
    arango-schema-mapper PR #17). Returns ``[]`` when:

    * the bundle carries no metadata block,
    * ``metadata.multitenancy`` is missing or has ``style == "none"``,
    * ``tenantKey`` is missing / empty / not a list of strings.

    Empty list means "no upstream hint" — discovery falls back to the
    env-var regex / built-in default exactly as before. A non-empty
    list is treated as an *additive* hint: the local regex still
    fires (so denormalised fields the analyzer didn't surface still
    get classified), but property names matching one of the upstream
    keys are preferred when both fire on the same entity.
    """
    if hasattr(mapping, "metadata"):
        meta = mapping.metadata or {}
    elif isinstance(mapping, dict):
        meta = mapping.get("metadata") or {}
    else:
        meta = {}
    if not isinstance(meta, dict):
        return []
    block = meta.get("multitenancy")
    if not isinstance(block, dict):
        return []
    style = block.get("style")
    if not isinstance(style, str) or style == "none":
        return []
    keys = block.get("tenantKey")
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, str) and k]


def _build_tenant_field_matcher(
    explicit_regex: re.Pattern[str] | None,
    upstream_keys: list[str],
) -> re.Pattern[str]:
    """Return a regex that matches the local default OR the upstream keys.

    The combined regex is ``^(<key1>|<key2>|...|<default>)$``,
    case-insensitive. Upstream keys are matched as exact (escaped)
    strings to avoid regex-injection through analyzer output. When
    ``explicit_regex`` is supplied (operator override via env var),
    we still OR-in the upstream keys — the operator's intent is
    "at least these names should match", not "*only* these".
    """
    base_regex = explicit_regex or _resolve_tenant_field_regex()
    if not upstream_keys:
        return base_regex
    escaped = "|".join(re.escape(k) for k in upstream_keys)
    base_pattern = base_regex.pattern.lstrip("^").rstrip("$")
    return re.compile(rf"^({escaped}|{base_pattern})$", re.IGNORECASE)


def _conceptual_schema(mapping: Any) -> dict[str, Any]:
    """Extract the conceptual schema dict from a bundle/dict, or {} if missing."""
    if hasattr(mapping, "conceptual_schema"):
        cs = mapping.conceptual_schema or {}
    elif isinstance(mapping, dict):
        cs = mapping.get("conceptual_schema") or mapping.get("conceptualSchema") or {}
    else:
        cs = {}
    return cs if isinstance(cs, dict) else {}


def _physical_mapping(mapping: Any) -> dict[str, Any]:
    """Extract the physical mapping dict from a bundle/dict, or {} if missing."""
    if hasattr(mapping, "physical_mapping"):
        pm = mapping.physical_mapping or {}
    elif isinstance(mapping, dict):
        pm = mapping.get("physical_mapping") or mapping.get("physicalMapping") or {}
    else:
        pm = {}
    return pm if isinstance(pm, dict) else {}


def _entity_property_names(entity: dict[str, Any]) -> list[str]:
    """Return the property names declared on a conceptual entity.

    Tolerates two shapes commonly seen in the wild:

    * ``properties`` is a list of strings ``["TENANT_ID", "NAME", …]``.
    * ``properties`` is a list of dicts ``[{"name": "TENANT_ID", …}, …]``.
    """
    props = entity.get("properties") or []
    if not isinstance(props, list):
        return []
    out: list[str] = []
    for p in props:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            n = p.get("name")
            if isinstance(n, str):
                out.append(n)
    return out


def _explicit_scope_from_mapping(
    physical_entry: dict[str, Any] | None,
) -> EntityScope | None:
    """Return an :class:`EntityScope` if the mapping carries a
    ``tenantScope`` annotation; otherwise ``None``.

    Recognises the upstream-PR contract:

    .. code-block:: jsonc

       "Device": {
         "style": "COLLECTION",
         "collectionName": "Device",
         "tenantScope": {
           "role": "tenant_scoped" | "tenant_root" | "global",
           "tenantField": "TENANT_ID"   // optional; ignored unless role=tenant_scoped
         }
       }
    """
    if not isinstance(physical_entry, dict):
        return None
    ts = physical_entry.get("tenantScope")
    if not isinstance(ts, dict):
        return None
    raw_role = ts.get("role")
    try:
        role = EntityTenantRole(raw_role) if raw_role else None
    except ValueError:
        role = None
    if role is None:
        return None
    field_name = ts.get("tenantField")
    if not isinstance(field_name, str) or not field_name:
        field_name = None
    if role is not EntityTenantRole.TENANT_SCOPED:
        field_name = None  # ignored for ROOT / GLOBAL
    return EntityScope(
        role=role,
        denorm_field=field_name,
        reachable_from_tenant=role is EntityTenantRole.TENANT_SCOPED,
    )


def _relationship_type_name(rel: dict[str, Any]) -> str | None:
    """Extract the relationship-type name from a conceptual-schema
    relationship dict, tolerating every shape the analyzer has emitted.

    Looks for ``type`` → ``name`` → ``label`` → ``relationshipType`` in
    that order. Returns ``None`` when none are present or non-string.
    """
    for key in ("type", "name", "label", "relationshipType"):
        v = rel.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _build_relationship_path_graph(
    cs: dict[str, Any],
) -> dict[str, list[tuple[str, str]]]:
    """Build an undirected adjacency map of entity → [(neighbour, edge_type)].

    Parallel to :func:`_build_relationship_graph` but retains the
    relationship-type name of each edge so a BFS can return the
    sequence of edge types — not just the sequence of nodes — between
    two entities. Tolerates the same endpoint-shape zoo as
    :func:`_build_relationship_graph` and silently drops edges whose
    type cannot be recovered (a path through a nameless edge is
    indistinguishable from the no-edge case downstream).
    """
    rels = cs.get("relationships") or []
    adj: dict[str, list[tuple[str, str]]] = {}
    if not isinstance(rels, list):
        return adj
    for r in rels:
        if not isinstance(r, dict):
            continue
        src = _endpoint_label(
            r.get("from") or r.get("fromEntity") or r.get("source") or r.get("sourceEntity")
        )
        dst = _endpoint_label(r.get("to") or r.get("toEntity") or r.get("target") or r.get("targetEntity"))
        if not src or not dst:
            continue
        rtype = _relationship_type_name(r)
        if not rtype:
            continue
        adj.setdefault(src, []).append((dst, rtype))
        adj.setdefault(dst, []).append((src, rtype))
    return adj


def compute_scoping_path(
    cs_or_mapping: Any,
    *,
    from_label: str = _TENANT_ENTITY_NAME,
    to_label: str,
    max_hops: int = 6,
) -> tuple[str, ...] | None:
    """Return the ordered relationship-type names connecting
    ``from_label`` to ``to_label`` via the conceptual relationship
    graph, or ``None`` if no path exists.

    PRD ``docs/multitenant_prd.md`` §3 (last paragraph) / Wave 7
    part 2: the schema mapper supplies per-entity ``tenantScope.role``
    and ``tenantScope.tenantField``; it does **not** supply the BFS-
    derived edge path from ``Tenant`` to a ``TENANT_SCOPED`` entity
    that lacks a denormalised tenant column. We compute it locally
    so Layer 3 (Cypher AST rewrite, Wave 8a) can inject a constrained
    traversal pattern, and Layer 5 can recognise such a traversal as
    safe.

    BFS is shortest-path with a deterministic tiebreak: when multiple
    paths share the minimum length, the lexicographically smallest
    sequence of relationship-type names is preferred. Self-paths
    (``from_label == to_label``) return the empty tuple. Unreachable
    targets return ``None``.

    Parameters
    ----------
    cs_or_mapping:
        Either a conceptual-schema dict (the inner ``conceptual_schema``
        block) or the outer mapping bundle / dict — accepted symmetrically
        so callers can pass whichever they have at hand.
    from_label, to_label:
        Source and target entity names as they appear in
        ``conceptual_schema.entities[*].name``.
    max_hops:
        Cap on BFS depth. Default 6 — long enough to cover every
        chain we have observed in the wild without exploding on
        cyclic graphs.
    """
    if not isinstance(to_label, str) or not to_label:
        return None
    if from_label == to_label:
        return ()
    if isinstance(cs_or_mapping, dict) and "entities" in cs_or_mapping:
        cs = cs_or_mapping
    else:
        cs = _conceptual_schema(cs_or_mapping)
    adj = _build_relationship_path_graph(cs)
    if from_label not in adj or to_label not in adj:
        return None
    frontier: deque[tuple[str, tuple[str, ...]]] = deque([(from_label, ())])
    best_at_depth: dict[str, tuple[str, ...]] = {from_label: ()}
    while frontier:
        node, path = frontier.popleft()
        if len(path) >= max_hops:
            continue
        for neighbour, rtype in sorted(adj.get(node, []), key=lambda nr: (nr[1], nr[0])):
            new_path = path + (rtype,)
            existing = best_at_depth.get(neighbour)
            if existing is None or (
                len(new_path) < len(existing) or (len(new_path) == len(existing) and new_path < existing)
            ):
                best_at_depth[neighbour] = new_path
                frontier.append((neighbour, new_path))
    return best_at_depth.get(to_label)


def _build_relationship_graph(
    cs: dict[str, Any],
) -> dict[str, set[str]]:
    """Build an undirected adjacency map of entity → reachable entities
    via declared relationships.

    Tolerates every endpoint shape the analyzer has emitted in the wild:

    * ``"from": "Tenant", "to": "Device"`` — flat strings.
    * ``"from": {"label": "Tenant"}, "to": {"label": "Device"}`` — dicts.
    * ``"fromEntity": "Tenant", "toEntity": "Device"`` — current
      ``arangodb-schema-analyzer`` ``conceptual_schema.relationships[]``
      shape (the source of the reachable=False regression that made
      tenant-scoping misclassify every entity as unreachable even when
      ``Tenant -[edge]-> X`` edges existed).
    * ``"sourceEntity"`` / ``"targetEntity"`` keys — older mappings.
    """
    rels = cs.get("relationships") or []
    adj: dict[str, set[str]] = {}
    if not isinstance(rels, list):
        return adj
    for r in rels:
        if not isinstance(r, dict):
            continue
        src = _endpoint_label(
            r.get("from") or r.get("fromEntity") or r.get("source") or r.get("sourceEntity")
        )
        dst = _endpoint_label(r.get("to") or r.get("toEntity") or r.get("target") or r.get("targetEntity"))
        if not src or not dst:
            continue
        adj.setdefault(src, set()).add(dst)
        adj.setdefault(dst, set()).add(src)
    return adj


def _endpoint_label(endpoint: Any) -> str | None:
    if isinstance(endpoint, str):
        return endpoint or None
    if isinstance(endpoint, dict):
        for key in ("label", "name", "entity", "type"):
            v = endpoint.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _reachable_from(
    start: str,
    adj: dict[str, set[str]],
    *,
    max_hops: int,
) -> set[str]:
    """BFS from ``start`` over ``adj``, returning the set of entities
    reachable within ``max_hops`` (inclusive). ``start`` itself is
    always included."""
    seen: set[str] = {start}
    if max_hops <= 0:
        return seen
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_hops:
            continue
        for neighbour in adj.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append((neighbour, depth + 1))
    return seen


def analyze_tenant_scope(
    mapping: Any,
    *,
    max_traversal_hops: int = 5,
    tenant_field_regex: re.Pattern[str] | None = None,
) -> TenantScopeManifest:
    """Classify every conceptual entity in ``mapping`` by tenant role.

    Parameters
    ----------
    mapping:
        A schema-analyzer bundle (object with ``conceptual_schema`` /
        ``physical_mapping`` attrs) or a plain dict carrying the same
        keys (snake- or camel-case both accepted).
    max_traversal_hops:
        Cap on BFS depth when deciding whether a non-denorm entity is
        reachable from Tenant. Default 5 — enough for the
        ``Tenant -> TenantUser -> GSuiteUser -> Device -> ...`` chains
        we've seen in the wild without exploding on cyclic graphs.
    tenant_field_regex:
        Override the regex used to detect denormalised tenant-id
        fields. Defaults to the env-var resolution
        (:envvar:`NL2CYPHER_TENANT_FIELD_REGEX`) or the built-in
        ``^tenant[_-]?(id|key)$``.

    Returns
    -------
    TenantScopeManifest
        A frozen, fully-populated manifest. Always returns a manifest
        even if the schema has no Tenant entity at all (in which case
        ``tenant_entity`` is ``None`` and every classified entity is
        ``GLOBAL``).
    """
    cs = _conceptual_schema(mapping)
    pm_entities = _physical_mapping(mapping).get("entities") or {}
    if not isinstance(pm_entities, dict):
        pm_entities = {}

    entities = cs.get("entities") or []
    if not isinstance(entities, list):
        entities = []

    # Pull the upstream multitenancy hint (analyzer >=0.6) and OR it
    # into the regex used for denorm-field discovery. This lets the
    # heuristic auto-detect non-default tenant key names like
    # ``customerId`` or ``orgId`` without requiring an env-var
    # override on every deployment that uses them.
    upstream_keys = _multitenancy_tenant_keys(mapping)
    field_regex = _build_tenant_field_matcher(tenant_field_regex, upstream_keys)
    if upstream_keys:
        logger.debug(
            "tenant_scope: extending field-name matcher with %d upstream "
            "tenant key(s) from metadata.multitenancy: %s",
            len(upstream_keys),
            upstream_keys,
        )

    # Index entities by name and detect the tenant root.
    by_name: dict[str, dict[str, Any]] = {}
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if isinstance(name, str) and name:
            by_name[name] = e

    tenant_entity = _TENANT_ENTITY_NAME if _TENANT_ENTITY_NAME in by_name else None

    # Reachability from Tenant via the relationship graph.
    if tenant_entity is not None:
        adj = _build_relationship_graph(cs)
        reachable = _reachable_from(
            tenant_entity,
            adj,
            max_hops=max_traversal_hops,
        )
    else:
        reachable = set()

    out: dict[str, EntityScope] = {}
    explicit_count = 0

    def _scoping_path_for(target: str) -> tuple[str, ...] | None:
        """BFS edge-type path from Tenant to ``target``.

        Guarded by ``tenant_entity is not None`` at call sites because
        ``compute_scoping_path`` requires a known root.
        """
        return compute_scoping_path(
            cs,
            from_label=tenant_entity,  # type: ignore[arg-type]
            to_label=target,
            max_hops=max_traversal_hops,
        )

    for name, entity in by_name.items():
        # Step 1: explicit annotation wins.
        explicit = _explicit_scope_from_mapping(pm_entities.get(name))
        if explicit is not None:
            scoping_path: tuple[str, ...] | None = None
            if (
                explicit.role is EntityTenantRole.TENANT_SCOPED
                and explicit.denorm_field is None
                and tenant_entity is not None
            ):
                scoping_path = _scoping_path_for(name)
            out[name] = EntityScope(
                role=explicit.role,
                denorm_field=explicit.denorm_field,
                reachable_from_tenant=explicit.reachable_from_tenant,
                scoping_path=scoping_path,
            )
            explicit_count += 1
            continue

        # Step 2: tenant root.
        if tenant_entity is not None and name == tenant_entity:
            out[name] = EntityScope(
                role=EntityTenantRole.TENANT_ROOT,
                denorm_field=None,
                reachable_from_tenant=True,
                scoping_path=None,
            )
            continue

        # Step 3: declared denormalised tenant-reference field.
        denorm = _find_denorm_field(entity, field_regex)
        if denorm is not None:
            out[name] = EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field=denorm,
                reachable_from_tenant=name in reachable,
                scoping_path=None,
            )
            continue

        # Step 4: reachable from Tenant via traversal only.
        if tenant_entity is not None and name in reachable:
            out[name] = EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field=None,
                reachable_from_tenant=True,
                scoping_path=_scoping_path_for(name),
            )
            continue

        # Step 5: global metadata.
        out[name] = EntityScope(
            role=EntityTenantRole.GLOBAL,
            denorm_field=None,
            reachable_from_tenant=False,
            scoping_path=None,
        )

    if explicit_count and explicit_count == len(out):
        # Every entity carried an upstream tenantScope annotation; the
        # local heuristic was a no-op. Log at DEBUG (not INFO) so the
        # signal is available when investigating "why is the guardrail
        # making this call?" without flooding healthy production logs.
        logger.debug(
            "tenant_scope: classified %d/%d entities from upstream "
            "annotations (analyzer >=0.4.0); local heuristic unused.",
            explicit_count,
            len(out),
        )
    elif explicit_count:
        logger.debug(
            "tenant_scope: %d/%d entities classified from upstream "
            "annotations; remainder via local heuristic.",
            explicit_count,
            len(out),
        )

    return TenantScopeManifest(tenant_entity=tenant_entity, entities=out)


def _find_denorm_field(
    entity: dict[str, Any],
    regex: re.Pattern[str],
) -> str | None:
    """Return the first property name on ``entity`` that matches ``regex``."""
    for prop_name in _entity_property_names(entity):
        if regex.match(prop_name):
            return prop_name
    return None


__all__ = [
    "EntityScope",
    "EntityTenantRole",
    "TenantScopeManifest",
    "analyze_tenant_scope",
    "compute_scoping_path",
]
