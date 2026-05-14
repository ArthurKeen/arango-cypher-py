"""Layer 4 — AQL AST tenant injection pass (MT-4a phase: core rewriter).

This module implements the *first* slice of the MT-4 PRD §7
rewriter. It walks an ArangoDB EXPLAIN plan and splices tenant-scope
``FILTER`` clauses into the textual AQL such that every read from a
tenant-touching collection is constrained against ``@tenantId`` (the
session-bound bind variable). Together with Layer 3 (Cypher AST, MT-3)
and Layer 5 (EXPLAIN-plan validator, ``tenant_plan_validator.py``), it
turns the safety contract in PRD §1.2 from a "best-effort prompt" into
a structural guarantee.

Phase boundaries
----------------
* **MT-4a (this PR)** — ``EnumerateCollectionNode`` and
  ``SubqueryNode`` only. ``TraversalNode`` and collection-arg function
  calls (``LENGTH(Employee)`` and friends) raise
  :class:`TenantScopeRewriteIncomplete`; the caller falls through to
  Layer 5 (`tenant_plan_validator.validate_plan`) which independently
  refuses any plan that ends up reading a tenant-touching collection
  without the right predicate, so an MT-4a-only deployment is still
  *safe* (it may refuse some queries Layer 4 would otherwise be able
  to repair).
* **MT-4b (next)** — ``TraversalNode`` (``OPTIONS {prune: …}``
  injection) and the ``LENGTH(<coll>)`` → tenant-filtered subquery
  rewrites. Adds the ``_tenant_subq_<n>`` bind hygiene contract this
  module already documents.
* **MT-4c (last)** — wires ``inject_tenant_scope`` into the
  ``/translate`` / ``/execute`` / ``/execute-aql`` routes and the UI's
  rewritten-query preview.

Approach: EXPLAIN-plan-guided textual splicing
-----------------------------------------------
We deliberately avoid building a local AQL parser. ArangoDB's parser
is the source of truth and it's already willing to hand us a fully
resolved plan via ``db.aql.explain``. We walk that plan, identify
``EnumerateCollectionNode`` reads of tenant-scoped collections, look
up the matching ``FOR <var> IN <coll>`` site in the AQL text, and
splice in a ``FILTER <var>.<field> == @<bind>`` immediately after.

Why textual splicing rather than re-emit the AQL from the plan? Two
reasons:

1. **Round-trippable for the user.** A plan-to-AQL emitter would
   produce normalised AQL the user wouldn't recognise. Textual
   splicing preserves the transpiler's formatting, comments, and
   structure — important for the rewritten-query preview MT-4c will
   render in the UI.
2. **Idempotent and small.** The splice is a one-line insertion; the
   diff is exactly what an auditor expects to see.

Plan-node fields read
---------------------
* ``EnumerateCollectionNode.collection`` — physical collection name.
* ``EnumerateCollectionNode.outVariable.name`` — the variable bound
  by the ``FOR``. Used as the splice anchor.
* ``SubqueryNode.subquery.nodes`` — nested plan-node list, walked
  recursively.
* ``CalculationNode.expression`` — used for the idempotency check
  (does a downstream calc already compare ``outvar.<field> ==
  @tenantId``?). Field shape mirrors what
  ``tenant_plan_validator._calc_matches_tenant_eq_bindvar`` reads, so
  Layer 5's contract and Layer 4's idempotency stay in sync.
* ``CalculationNode.expression.subNodes[*]`` — recursed when
  detecting function-call references to collection names
  (``LENGTH(Employee)``), which signal MT-4b territory.
* ``TraversalNode`` — type tag only; MT-4b will read ``options.prune``
  and ``graph.vertexCollections``.

See ``tenant_plan_validator.py``'s ``_PlanWalker`` for the canonical
interpretation of these shapes — MT-4a deliberately reads only the
subset of fields Layer 5 already validates, so the two layers refuse
the same plans for the same reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .nl2cypher.tenant_ast_common import (
    TENANT_ID_BIND,
    TENANT_KEY_BIND,
    UnknownEntityScope,
    is_bindvar_reference,
    predicate_for_collection,
)
from .nl2cypher.tenant_scope import TenantScopeManifest

__all__ = [
    "TenantScopeRewriteIncomplete",
    "TenantScopeRewriteRejection",
    "inject_tenant_scope",
]


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class TenantScopeRewriteRejection(Exception):
    """Raised when the AQL rewriter refuses the query.

    Code values intentionally mirror :class:`tenant_plan_validator
    .TenantScopeViolation`'s codes (``UNCONSTRAINED_COLLECTION_ACCESS``,
    ``LITERAL_TENANT_PREDICATE``, etc.) so that audit pipelines can
    aggregate Layer-4 refusals alongside Layer-5 ones without
    code-translation glue.

    Attributes
    ----------
    code:
        Short reason code. Currently MT-4a emits only
        ``UNCONSTRAINED_COLLECTION_ACCESS`` (a tenant-scoped collection
        we cannot safely constrain because its manifest entry lacks
        both a denormalised field and a scoping path / smartgraph
        attribute). MT-4b will add the literal-tenant codes once it
        starts rewriting function-call arguments.
    message:
        Human-readable description; included in the exception's str
        form alongside the code.
    where:
        Optional anchor (e.g. ``"FOR e IN Employee"``) so the caller
        can render a useful 4xx error. May be the empty string when no
        single textual anchor applies.
    """

    def __init__(self, code: str, message: str, *, where: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({where})" if where else ""))
        self.code = code
        self.message = message
        self.where = where


class TenantScopeRewriteIncomplete(Exception):
    """Raised when a plan node kind requires MT-4b (``TraversalNode``,
    collection-arg function call).

    The caller is expected to *catch* this, log it as a non-fatal
    rewriter limitation, and fall through to Layer 5 — which will
    independently refuse the query if it ends up reading a
    tenant-touching collection without a tenant predicate. The
    distinction from :class:`TenantScopeRewriteRejection` is
    deliberate: a rejection says "this query is unsafe, never
    execute it"; an incomplete says "I, the rewriter, cannot fix this
    in this phase — defer to Layer 5".
    """


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inject_tenant_scope(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any],
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    tenant_id: str,
    tenant_key: str,
    plan_override: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], list[str]]:
    """Rewrite ``aql`` to enforce tenant scope on every read.

    Algorithm (PRD §7.2, MT-4a subset):

    1. Fetch the EXPLAIN plan once (``db.aql.explain(aql, bind_vars)``
       or ``plan_override`` when supplied by tests). The override path
       is the only way to skip the round-trip; production code always
       hits the database so the rewriter sees the plan ArangoDB
       actually produced.
    2. Walk plan nodes. For each ``EnumerateCollectionNode`` over a
       tenant-scoped collection (``predicate_for_collection`` returns
       a ``property_map`` shape), splice ``FILTER <var>.<field> ==
       @<bind>`` immediately after the matching ``FOR <var> IN <coll>``
       line in the AQL text.
    3. ``SubqueryNode`` bodies are walked recursively with a fresh
       scope so calculation indices don't leak across subqueries —
       same posture as Layer 5's ``_PlanWalker`` (see
       ``tenant_plan_validator._check_node``).
    4. ``TraversalNode`` and ``CalculationNode``s whose expression
       contains a ``function call`` over a collection name raise
       :class:`TenantScopeRewriteIncomplete`. MT-4b owns those.

    Parameters
    ----------
    db:
        ``StandardDatabase``-like object exposing ``aql.explain``.
        Not used when ``plan_override`` is supplied (tests).
    aql:
        The AQL produced by the upstream transpiler (Cypher→AQL) or
        the user's direct ``/execute-aql`` submission.
    bind_vars:
        Bind-var dict accompanying the AQL. **Not mutated.** A new
        dict is returned in the tuple's second slot. MT-4a does not
        add bind vars; the returned dict is a shallow copy of the
        input so callers can safely mutate it.
    manifest:
        Built by ``tenant_scope.analyze_tenant_scope``. Used only for
        per-collection role lookups via ``predicate_for_collection``.
    sharding_profile:
        ``metadata.shardingProfile`` from the schema bundle. ``None``
        is accepted (heuristic-mode mappings, older bundles); in that
        case the collection-name-equals-entity-name fallback in
        ``predicate_for_collection`` is the only resolver.
    tenant_id, tenant_key:
        Carried for symmetry with Layer 3's signature and forward
        compatibility with MT-4b's bind-introducing rewrites. MT-4a
        does not embed these values anywhere — the FILTER expression
        references the bind vars by name (``@tenantId`` /
        ``@tenantKey``), never the literal value. T2-defence: literal
        tenant values must never appear in the output, only bind-var
        references.
    plan_override:
        Test hook. When non-None, used in place of
        ``db.aql.explain(...)``. Mirrors Layer 5's pattern so the
        same plan fixtures power both modules' tests.

    Returns
    -------
    (rewritten_aql, augmented_bind_vars, changes)
        * ``rewritten_aql`` — the spliced AQL. Byte-identical to the
          input when no rewrites apply (e.g. all collections are
          satellite/global).
        * ``augmented_bind_vars`` — a shallow copy of ``bind_vars``.
          For MT-4a this is *value-identical* to the input — the
          phase introduces no new binds. MT-4b will populate
          ``@_tenant_subq_<n>`` entries here; the contract that
          values are bind-var references, not literal tenant values,
          is enforced even in this phase: the returned dict is
          guaranteed not to contain any element of
          ``manifest.known_tenant_keys``.
        * ``changes`` — list of human-readable strings, one per
          splice, suitable for the audit log and the rewritten-query
          preview UI. Empty when the AQL was already constrained.

    Raises
    ------
    TenantScopeRewriteRejection
        When a tenant-scoped collection cannot be safely constrained
        (no denorm field, no scoping path, no smartgraph attribute) —
        the manifest is incomplete and we must refuse rather than
        silently produce a no-op rewrite.
    TenantScopeRewriteIncomplete
        When a plan node kind requires MT-4b. The caller catches and
        relies on Layer 5 to refuse any unsafe execution.
    """
    plan = _resolve_plan(db=db, aql=aql, bind_vars=bind_vars, plan_override=plan_override)

    walker = _RewriteWalker(
        manifest=manifest,
        sharding_profile=sharding_profile or {},
    )
    ops = walker.collect_ops(plan)

    rewritten = aql
    changes: list[str] = []
    for op in ops:
        new_aql, change = op.apply(rewritten)
        if change:
            rewritten = new_aql
            changes.append(change)

    # T2-defence: confirm we did not somehow inline a tenant literal
    # into the bind-var dict. MT-4a doesn't add bind vars at all so
    # this is a property check more than a sanitiser, but the loop is
    # cheap and pins the invariant for MT-4b's eyes.
    augmented = _strip_literal_tenant_safeguard(bind_vars, manifest)
    return rewritten, augmented, changes


# ---------------------------------------------------------------------------
# Walker — collects ops and recurses into subqueries
# ---------------------------------------------------------------------------


@dataclass
class _FilterInjection:
    """A single ``FILTER <var>.<field> == @<bind>`` splice op.

    Each instance corresponds to exactly one
    ``EnumerateCollectionNode`` in the plan. Op application is
    text-based: we find the matching ``FOR <var> IN <coll>`` line and
    insert a new FILTER line immediately after, preserving the host
    line's leading whitespace. Failure to find the line is treated as
    a no-op (we return ``(aql, "")``) rather than an error — the plan
    can legitimately reference a synthetic ``FOR`` (e.g. for a graph-
    traversal expansion) that has no textual ``FOR <var> IN <coll>``
    anchor in the source AQL. MT-4b will handle those.
    """

    out_var: str
    collection: str
    field: str
    bind_name: str

    def apply(self, aql: str) -> tuple[str, str]:
        """Return ``(new_aql, change_description)`` or ``(aql, "")``.

        The splice anchor is the end of the ``FOR <var> IN <coll>``
        token sequence — *not* the end of the host line. AQL is
        whitespace-insensitive at clause boundaries, so a single-line
        query like ``FOR e IN Employee RETURN e`` becomes ``FOR e IN
        Employee\\nFILTER ... RETURN e`` (FILTER + RETURN may share a
        line; ArangoDB's parser accepts that). Multi-line AQL like
        the transpiler emits gets the FILTER on its own indented line,
        matching the host FOR's leading whitespace.

        AMBIGUITY: regex anchors on ``FOR <out_var> IN <coll>`` —
        AQL allows variable-name shadowing across nested scopes, so
        in pathological cases the regex could match the wrong site.
        The Cypher→AQL transpiler this module consumes emits unique
        variable names per scope, and hand-written AQL routed via
        ``/execute-aql`` is validated by Layer 5 regardless. MT-4b
        will tighten this by tracking the splice anchor at plan-node
        granularity.
        """
        pattern = re.compile(
            r"\bFOR\s+" + re.escape(self.out_var) + r"\s+IN\s+" + re.escape(self.collection) + r"\b"
        )
        match = pattern.search(aql)
        if match is None:
            return aql, ""
        # Indent is the whitespace at the start of the host line — the
        # FILTER we splice should align with the FOR, not with whatever
        # came before it on a folded single-line query.
        line_start = aql.rfind("\n", 0, match.start()) + 1
        prefix = aql[line_start : match.start()]
        indent_match = re.match(r"^[ \t]*", prefix)
        indent = indent_match.group(0) if indent_match else ""
        filter_clause = f"\n{indent}FILTER {self.out_var}.{self.field} == @{self.bind_name}"
        new_aql = aql[: match.end()] + filter_clause + aql[match.end() :]
        change = (
            f"Added FILTER {self.out_var}.{self.field} == @{self.bind_name} "
            f"after FOR {self.out_var} IN {self.collection}"
        )
        return new_aql, change


@dataclass
class _RewriteWalker:
    """Plan walker. One instance per inject_tenant_scope call.

    Internal state (``_calc_by_outvar``) is rebuilt per scope:
    :meth:`collect_ops` builds a fresh walker for each ``SubqueryNode``
    it recurses into, mirroring Layer 5's ``_PlanWalker.walk``
    semantics. This is load-bearing for security: accepting an outer-
    scope FILTER as evidence that an inner-scope EnumerateCollection
    is constrained would be a tenant-scope leak vector.
    """

    manifest: TenantScopeManifest
    sharding_profile: dict[str, Any]
    _calc_by_outvar: dict[str, dict[str, Any]] = field(default_factory=dict)

    def collect_ops(self, plan: dict[str, Any]) -> list[_FilterInjection]:
        nodes = plan.get("nodes") if isinstance(plan, dict) else None
        if not isinstance(nodes, list):
            return []
        self._index_calcs(nodes)
        return self._walk(nodes)

    def _index_calcs(self, nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") != "CalculationNode":
                continue
            outvar = _outvar_name(node)
            if outvar:
                self._calc_by_outvar[outvar] = node

    def _walk(self, nodes: list[Any]) -> list[_FilterInjection]:
        ops: list[_FilterInjection] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("type")
            if t == "EnumerateCollectionNode":
                op = self._handle_enumerate(node)
                if op is not None:
                    ops.append(op)
            elif t == "SubqueryNode":
                sub_plan = node.get("subquery") or {}
                if isinstance(sub_plan, dict):
                    inner = _RewriteWalker(
                        manifest=self.manifest,
                        sharding_profile=self.sharding_profile,
                    )
                    ops.extend(inner.collect_ops(sub_plan))
            elif t == "TraversalNode":
                raise TenantScopeRewriteIncomplete(
                    "MT-4a does not rewrite TraversalNode; defer to MT-4b. "
                    f"graphName={node.get('graphName')!r}"
                )
            elif t == "CalculationNode":
                self._guard_against_collection_function_call(node)
            elif t == "FunctionCallNode":
                raise TenantScopeRewriteIncomplete("MT-4a does not rewrite FunctionCallNode; defer to MT-4b.")
        return ops

    def _handle_enumerate(self, node: dict[str, Any]) -> _FilterInjection | None:
        collection = node.get("collection")
        if not isinstance(collection, str) or not collection:
            return None
        try:
            shape = predicate_for_collection(collection, self.manifest, self.sharding_profile)
        except UnknownEntityScope as exc:
            raise TenantScopeRewriteRejection(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"collection {collection!r} is not classified as satellite/system and "
                    "has no manifest entry with a denormalised tenant field or scoping path; "
                    "cannot inject a safe tenant predicate"
                ),
                where=f"FOR ... IN {collection}",
            ) from exc

        if shape is None:
            return None
        if shape.style != "property_map":
            # MT-4a handles only the property_map shape — the only one
            # an EnumerateCollectionNode can satisfy with a single
            # FILTER splice. traversal_path implies the entity needs a
            # graph-traversal rewrite (Layer 3 / MT-3 territory in
            # Cypher; MT-4b in AQL); prune is for TraversalNode only.
            raise TenantScopeRewriteIncomplete(
                "MT-4a only handles property_map predicates on EnumerateCollectionNode; "
                f"shape.style={shape.style!r} for {collection!r}"
            )
        field_name = shape.field
        if not field_name:
            # Defensive: TenantPredicateShape's __post_init__ already
            # forbids empty fields on property_map shapes, but if a
            # caller bypassed validation we still refuse rather than
            # emit a malformed FILTER.
            raise TenantScopeRewriteRejection(
                code="UNCONSTRAINED_COLLECTION_ACCESS",
                message=(
                    f"property_map shape for collection {collection!r} has empty field; "
                    "cannot emit a safe FILTER"
                ),
                where=f"FOR ... IN {collection}",
            )
        out_var = _outvar_name(node)
        if not out_var:
            return None
        if self._already_filtered(out_var, field_name, shape.bind_name):
            return None
        return _FilterInjection(
            out_var=out_var,
            collection=collection,
            field=field_name,
            bind_name=shape.bind_name,
        )

    def _already_filtered(self, out_var: str, field_name: str, bind_name: str) -> bool:
        """Idempotency check: does a downstream calc already compare
        ``<out_var>.<field_name>`` to ``@<bind_name>``?

        The match logic mirrors ``tenant_plan_validator
        ._calc_matches_tenant_eq_bindvar`` — same field-name shape,
        same accepted operand orders — but is intentionally inlined
        here so the two modules don't depend on each other's
        internals. (Layer 5 is the security boundary; Layer 4 is a
        usability layer that defers to it.)
        """
        for calc in self._calc_by_outvar.values():
            expr = calc.get("expression")
            if not isinstance(expr, dict):
                continue
            if expr.get("type") not in {"compare ==", "n-ary compare"}:
                continue
            subs = expr.get("subNodes") or []
            if len(subs) != 2 or not all(isinstance(s, dict) for s in subs):
                continue
            lhs, rhs = subs
            if (
                _is_attribute_access_on(lhs, var_name=out_var, attr=field_name)
                and is_bindvar_reference(rhs, name=bind_name)
            ) or (
                _is_attribute_access_on(rhs, var_name=out_var, attr=field_name)
                and is_bindvar_reference(lhs, name=bind_name)
            ):
                return True
        return False

    def _guard_against_collection_function_call(self, calc_node: dict[str, Any]) -> None:
        """Walk a CalculationNode's expression looking for a
        ``function call`` subnode that references a collection name.

        PRD §7.2's "function calls (LENGTH, COUNT, AVERAGE) referencing
        a collection name" item maps onto ArangoDB EXPLAIN's
        representation of expressions: function calls appear as
        ``{"type": "function call", "name": "LENGTH", "subNodes":
        [{"type": "collection", "name": "Employee"}]}`` subtrees inside
        a CalculationNode's expression. We refuse to rewrite them in
        MT-4a — MT-4b will turn them into tenant-filtered subqueries.

        Note: we treat any function call with a ``collection``
        subnode as "MT-4b territory" regardless of the function name,
        because the *transpiler* uses a small, known set of functions
        and any newcomer is more safely deferred than guessed at.
        """
        expr = calc_node.get("expression")
        if not isinstance(expr, dict):
            return
        offending = _find_function_call_over_collection(expr)
        if offending is not None:
            raise TenantScopeRewriteIncomplete(
                "MT-4a does not rewrite function calls over collection names; "
                f"defer to MT-4b. function={offending[0]!r} collection={offending[1]!r}"
            )


# ---------------------------------------------------------------------------
# Helpers — small, pure, plan-shape-only
# ---------------------------------------------------------------------------


def _resolve_plan(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any],
    plan_override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the plan dict.

    Mirrors ``tenant_plan_validator._coerce_plan``: ``db.aql.explain``
    returns ``{"plan": ..., "warnings": ...}`` in most python-arango
    versions; older versions return the plan directly. The override
    path is for tests only.
    """
    if plan_override is not None:
        plan = plan_override
    else:
        result = db.aql.explain(aql, bind_vars=bind_vars)
        plan = result.get("plan") if isinstance(result, dict) and "plan" in result else result
    if not isinstance(plan, dict):
        raise TenantScopeRewriteRejection(
            code="EXPLAIN_MALFORMED",
            message=f"EXPLAIN returned non-dict plan: {type(plan).__name__}",
        )
    return plan


def _outvar_name(node: dict[str, Any]) -> str | None:
    """Extract ``node.outVariable.name`` or ``None``."""
    out = node.get("outVariable")
    if isinstance(out, dict):
        n = out.get("name")
        if isinstance(n, str) and n:
            return n
    return None


def _is_attribute_access_on(
    expr: object,
    *,
    var_name: str,
    attr: str,
) -> bool:
    """Match ``<var_name>.<attr>`` — an ``attribute access`` node whose
    subnode is a ``reference`` to *var_name* and whose ``name`` is *attr*.

    Same shape ``tenant_plan_validator._is_attribute_access_on`` reads.
    """
    if not isinstance(expr, dict):
        return False
    if expr.get("type") != "attribute access":
        return False
    if expr.get("name") != attr:
        return False
    subs = expr.get("subNodes") or []
    if not isinstance(subs, list) or not subs:
        return False
    inner = subs[0]
    if not isinstance(inner, dict):
        return False
    if inner.get("type") != "reference":
        return False
    return inner.get("name") == var_name


def _find_function_call_over_collection(
    expr: dict[str, Any],
) -> tuple[str, str] | None:
    """Return ``(function_name, collection_name)`` if *expr* contains a
    ``function call`` subtree with a ``collection`` subnode.

    Walks recursively into ``subNodes`` so the check fires whether the
    function call is at the top of the expression or nested inside an
    arithmetic / boolean tree. Returns ``None`` when no such subtree
    exists.
    """
    if not isinstance(expr, dict):
        return None
    if expr.get("type") == "function call":
        fname = expr.get("name") if isinstance(expr.get("name"), str) else "<unknown>"
        for arg in expr.get("subNodes") or []:
            if not isinstance(arg, dict):
                continue
            if arg.get("type") == "collection" and isinstance(arg.get("name"), str):
                return fname, arg["name"]
            # Recurse through wrapper subNodes (an `array` arg type
            # wrapping a collection ref).
            nested = _find_function_call_over_collection(arg)
            if nested is not None:
                return nested
    for sub in expr.get("subNodes") or []:
        if isinstance(sub, dict):
            hit = _find_function_call_over_collection(sub)
            if hit is not None:
                return hit
    return None


def _strip_literal_tenant_safeguard(
    bind_vars: dict[str, Any],
    manifest: TenantScopeManifest,
) -> dict[str, Any]:
    """Return a shallow copy of ``bind_vars`` with the invariant that
    no value matches a known tenant key.

    MT-4a does not *add* any bind vars; this helper is a property
    check on the *input* so MT-4b can extend it without changing the
    public contract. We currently:

    * Copy the dict (so callers can mutate without affecting ours).
    * Assert (in the form of a runtime check) that no string value
      matches ``manifest.known_tenant_keys`` *except* for the
      canonical tenant-id/key binds — those are expected to carry
      tenant values, that's the whole point of them.

    The assertion is a defence-in-depth: Layer 1 is the one
    responsible for populating ``@tenantId`` / ``@tenantKey``, and
    if a *different* bind var happens to carry a tenant value, that's
    almost certainly a T2 injection attempt smuggling a tenant key
    through a non-canonical bind name. We refuse it.

    When ``manifest.known_tenant_keys`` is ``None`` (not sampled), we
    fail open here on purpose: the equivalent fail-closed behaviour
    is implemented in :func:`is_literal_tenant_value` for the
    literal-value path. Bind-var values are session-bound by Layer 1
    so the fail-closed treatment would be a usability regression with
    no security upside in the bind-var path.
    """
    out = dict(bind_vars)
    known = manifest.known_tenant_keys
    if known is None:
        return out
    canonical = {TENANT_ID_BIND, TENANT_KEY_BIND}
    for name, value in out.items():
        if name in canonical:
            continue
        if isinstance(value, str) and value in known:
            raise TenantScopeRewriteRejection(
                code="LITERAL_TENANT_PREDICATE",
                message=(
                    f"bind var @{name} carries a known tenant key "
                    f"({value!r}); only @{TENANT_ID_BIND} / @{TENANT_KEY_BIND} "
                    "may carry tenant values"
                ),
                where=f"@{name}",
            )
    return out
