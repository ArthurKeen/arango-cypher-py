from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .aql import AqlQuery


@dataclass
class AqlExecutor:
    db: Any  # python-arango Database

    def execute(self, query: AqlQuery, *, batch_size: int | None = None, **kwargs: Any) -> Any:
        aql = self.db.aql
        return aql.execute(query.text, bind_vars=query.bind_vars, batch_size=batch_size, **kwargs)


def safe_execute(
    *,
    db: Any,
    aql: str,
    client_bind_vars: dict[str, Any] | None,
    session: Any,
    validator: Callable[..., None],
    manifest: Any,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None = None,
    execute_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Layer 6 — execute AQL only after Layer 5 has certified the plan.

    Implements the contract from ``docs/multitenant_prd.md`` §9 / Wave 7
    part 4. The bind-var spread order is load-bearing:

    .. code-block:: python

        bind_vars = {
            **client_bind_vars,
            "tenantId": session.tenant_id,
            "tenantKey": session.tenant_key,
        }

    The session value silently overrides any caller-supplied
    ``tenantId`` / ``tenantKey``. Layer 5 (``validator``) then
    verifies the bind-var matches the session — closing T7 (bind-var
    override) by construction.

    Returns ``(cursor, bind_vars)`` so the caller can echo the final
    bind vars back to the UI for transparency (§9.2). The validator is
    injected rather than imported here so ``arango_query_core`` stays
    free of an ``arango_cypher`` reverse dependency.

    Raises whatever the validator raises — typically
    ``arango_cypher.tenant_plan_validator.TenantScopeViolation`` — on
    refusal. The execute never runs in that case.
    """
    if session is None:
        raise PermissionError("safe_execute: no authenticated session; cannot bind tenant context")
    bind_vars = dict(client_bind_vars or {})
    bind_vars["tenantId"] = getattr(session, "tenant_id", None)
    bind_vars["tenantKey"] = getattr(session, "tenant_key", None)

    validator(
        db=db,
        aql=aql,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile,
        collection_to_entity=collection_to_entity,
        session=session,
    )

    cursor = db.aql.execute(aql, bind_vars=bind_vars, **(execute_kwargs or {}))
    return cursor, bind_vars


def explain_aql(
    db: Any,
    aql: str,
    bind_vars: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Plan the query via ``POST /_api/explain`` without executing it.

    Returns ``(ok, error_message)``. On success, *error_message* is the
    empty string. On failure, it's a short, LLM-friendly description of
    the planner error — short enough to paste back into a retry prompt,
    and stripped of stack traces, HTTP payloads, and sensitive detail.

    This is the hook WP-25.3 uses to catch semantic errors
    (non-existent collections/properties, invalid traversal directions)
    that ANTLR's grammar-only check happily waves through.  We never
    execute the query — the ``explain`` endpoint only plans it, so there
    is no cost to row materialization.

    Safe to call against any read-only or read-write database: the AQL
    is only planned, never run.  Caller is responsible for ensuring
    *db* is a valid python-arango database handle.
    """
    bv = dict(bind_vars or {})
    try:
        result = db.aql.explain(aql, bind_vars=bv)
    except Exception as exc:
        return False, _summarize_explain_error(exc)

    if isinstance(result, dict) and result.get("error"):
        msg = str(result.get("errorMessage") or result.get("error") or "EXPLAIN failed")
        return False, msg[:500]
    return True, ""


def _summarize_explain_error(exc: BaseException) -> str:
    """Collapse a python-arango / server error into a single short line.

    python-arango raises ``AQLQueryExplainError`` or ``ArangoServerError``
    whose ``str()`` can include multi-line HTTP payloads and stack frames.
    We strip to the most informative line for LLM feedback.
    """
    msg = str(exc) or exc.__class__.__name__
    msg = msg.splitlines()[0] if "\n" in msg else msg
    msg = msg.strip()
    if len(msg) > 500:
        msg = msg[:497] + "..."
    return msg
