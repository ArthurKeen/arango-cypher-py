"""AQL that ArangoDB rejects is not a tenant-scope refusal.

Layer 5 refuses any query whose EXPLAIN fails (fail-closed), and that stays.
But when EXPLAIN failed because ArangoDB rejected the AQL itself (HTTP 400,
a syntax or semantic error), the response said ``403 tenant_scope_violation``
— which on BYOC made a translator bug (unquoted hyphenated collection names,
ERR 1501) look like a permissions problem. The live counterpart is
``tests/integration/test_aql_rejected_live.py``.
"""

from __future__ import annotations

from arango.exceptions import AQLQueryExecuteError, AQLQueryExplainError

from arango_cypher.service.routes.cypher import _tenant_violation_response
from arango_cypher.tenant_plan_validator import TenantScopeViolation
from tests.helpers.fake_arango import server_error

SYNTAX = "syntax error, unexpected - operator near '-AWS-DOCS-DEMO_Chunks' at position 1:477"


def _violation(cause: Exception | None, code: str = "EXPLAIN_FAILED") -> TenantScopeViolation:
    violation = TenantScopeViolation(
        code=code, message=f"EXPLAIN failed: {cause}", aql_digest="a" * 64, plan_digest=""
    )
    violation.__cause__ = cause
    return violation


def test_generated_aql_the_database_rejected_is_our_bug() -> None:
    v = _violation(server_error(AQLQueryExplainError, 400, 1501, SYNTAX))
    resp = _tenant_violation_response(v, generated_aql=True)
    assert resp.status_code == 500
    assert resp.detail["error"] == "generated_aql_rejected"
    assert resp.detail["code"] == "GENERATED_AQL_REJECTED"
    assert "bug in Arango Cypher, not in your query" in resp.detail["message"]
    assert SYNTAX in resp.detail["message"] and "ArangoDB error 1501" in resp.detail["message"]
    assert "tenant" not in resp.detail["message"]


def test_supplied_aql_the_database_rejected_is_a_400() -> None:
    v = _violation(server_error(AQLQueryExplainError, 400, 1501, SYNTAX))
    resp = _tenant_violation_response(v, generated_aql=False)
    assert resp.status_code == 400
    assert resp.detail["code"] == "AQL_REJECTED"
    assert resp.detail["message"].startswith("ArangoDB rejected this AQL: syntax error")
    assert "bug in Arango Cypher" not in resp.detail["message"]


def test_other_explain_failures_stay_tenant_refusals() -> None:
    # A 403 from EXPLAIN (no access) is not "invalid AQL".
    forbidden = _violation(server_error(AQLQueryExplainError, 403, 11, "not authorized"))
    assert _tenant_violation_response(forbidden, generated_aql=True).status_code == 403
    # No cause at all (a malformed plan, say).
    bare = _violation(None)
    assert _tenant_violation_response(bare, generated_aql=True).detail["error"] == "tenant_scope_violation"


def test_genuine_tenant_refusals_are_unchanged() -> None:
    cause = server_error(AQLQueryExecuteError, 400, 1501, SYNTAX)  # even with a 400 cause
    v = _violation(cause, code="UNCONSTRAINED_COLLECTION_ACCESS")
    resp = _tenant_violation_response(v, generated_aql=True)
    assert resp.status_code == 403
    assert resp.detail == {
        "error": "tenant_scope_violation",
        "code": "UNCONSTRAINED_COLLECTION_ACCESS",
        "message": v.message,
        "aql_digest": "a" * 16,
        "plan_digest": "",
    }


# A collection that does not exist (ERR 1203, HTTP 404) — the schema mapping is
# out of date. Seen live on BYOC as "403 tenant_scope_violation".
NOT_FOUND = "collection or view not found: name: IAM-AWS-DOCS-DEMO_Chunks (while parsing)"


def test_a_missing_collection_in_generated_aql_means_the_schema_is_out_of_date() -> None:
    v = _violation(server_error(AQLQueryExplainError, 404, 1203, NOT_FOUND))
    resp = _tenant_violation_response(v, generated_aql=True)
    assert resp.status_code == 409
    assert resp.detail["code"] == "SCHEMA_OUT_OF_DATE"
    assert resp.detail["collection"] == "IAM-AWS-DOCS-DEMO_Chunks"
    assert "Click Refresh schema" in resp.detail["message"]
    assert "tenant" not in resp.detail["message"]
    assert not hasattr(resp, "inbox_report")  # not our bug: never filed


def test_a_missing_collection_in_supplied_aql_is_a_400() -> None:
    v = _violation(server_error(AQLQueryExplainError, 404, 1203, NOT_FOUND))
    resp = _tenant_violation_response(v, generated_aql=False)
    assert resp.status_code == 400 and resp.detail["code"] == "AQL_UNKNOWN_COLLECTION"
    assert "'IAM-AWS-DOCS-DEMO_Chunks'" in resp.detail["message"]


def test_the_execution_time_message_shape_names_the_collection_too() -> None:
    from arango_cypher.service.routes.cypher import _aql_rejected_response

    cause = server_error(
        AQLQueryExecuteError, 404, 1203, "AQL: collection or view not found: nosuch (while parsing)"
    )
    resp = _aql_rejected_response(cause, generated_aql=True)
    assert resp.status_code == 409 and resp.detail["collection"] == "nosuch"
