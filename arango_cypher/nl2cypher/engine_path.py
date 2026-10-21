"""NL → Cypher on the shared NL engine (issue #24).

``nl_to_cypher`` historically ran its own generate → validate → repair loop
(:func:`arango_cypher.nl2cypher._core._call_llm_with_retry`). The other
English→query paths in the portfolio — arango-sparql-py, contextual-data-fabric —
run :class:`arango_query_core.nl.NLQueryEngine`, so a fix to that engine had
to be made twice. This module runs the same translation on the engine with
:class:`~arango_cypher.nl2cypher.adapter.CypherAdapter`, behind a switch:

* ``ARANGO_CYPHER_NL_ENGINE=legacy`` (the default until the NL evaluation
  shows the engine is not worse) keeps the old loop;
* ``ARANGO_CYPHER_NL_ENGINE=shared`` (or ``nl_to_cypher(engine="shared")``)
  runs the engine.

Everything the old loop did is kept, through the engine's own seams where it
has one: the tenant guardrail runs as the first postcondition (so a violation
is retried with its hint, as before), resolved entities render through the
grounding seam (after the examples, as before), and the tenant and
graph-intent sections extend the grammar section. Four things the engine
cannot do yet are done in the provider bridge and marked for removal: the
caller's hint on the first user message, finding the query in a response that
does not start with a fence, Cypher's label repair, and retrying a provider
call that raised.

The result keeps :class:`~arango_cypher.nl2cypher._core.NL2CypherResult`'s
contract: ``method`` ``llm`` / ``validation_failed`` / ``tenant_guardrail_blocked``,
the same explanation wording ("Last attempted Cypher was:" is what the UI's
report masking looks for), and ``None`` when no candidate was ever produced
(the caller then falls back to the rule-based translator).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arango_query_core.nl import NLQueryEngine
from arango_query_core.nl.grounding import GroundedEntity, LabelIndex
from arango_query_core.nl.postconditions import PostconditionContext, PostconditionViolation

from ._core import (
    _RETRY_USER_SUFFIX,
    NL2CypherResult,
    PromptBuilder,
    _extract_cypher_from_response,
    _fix_labels,
    _get_default_fewshot_index,
    _SchemaCtx,
)
from .adapter import CypherAdapter
from .tenant_guardrail import check_tenant_scope, multitenancy_physical_enforcement

if TYPE_CHECKING:
    from arango_query_core.mapping import MappingBundle
    from arango_query_core.nl.fewshot import FewShotIndex
    from arango_query_core.nl.providers import LLMProvider

    from .postconditions import Postcondition
    from .tenant_guardrail import TenantContext
    from .tenant_scope import TenantScopeManifest

logger = logging.getLogger(__name__)

ENGINE_ENV = "ARANGO_CYPHER_NL_ENGINE"
LEGACY = "legacy"
SHARED = "shared"
DEFAULT_ENGINE = LEGACY
#: ``code`` of the tenant-scope postcondition, distinguishing it from callers'.
TENANT_SCOPE_CODE = "tenant_scope"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")


def nl_engine_choice(explicit: str | None = None) -> str:
    """Which loop ``nl_to_cypher`` runs: *explicit*, else ``ARANGO_CYPHER_NL_ENGINE``,
    else :data:`DEFAULT_ENGINE`. An unknown value is logged and ignored."""
    raw = explicit if explicit is not None else os.environ.get(ENGINE_ENV, "")
    value = (raw or DEFAULT_ENGINE).strip().lower()
    if value not in (LEGACY, SHARED):
        logger.warning("%s=%r is not %r or %r; using %r", ENGINE_ENV, raw, LEGACY, SHARED, DEFAULT_ENGINE)
        return DEFAULT_ENGINE
    return value


@dataclass
class _ProviderBridge:
    """The caller's provider as the engine sees it.

    Records every raw response (the successful one becomes ``explanation``)
    and every candidate (the last one is quoted when generation fails), and
    accumulates usage across all calls, retried ones included.
    """

    provider: LLMProvider
    ctx: _SchemaCtx | None
    max_retries: int
    first_user_hint: str = ""
    raw: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_USAGE_KEYS, 0))

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        # upstream: arango-solutions-org/arango-query-core#10 — remove when NLQueryEngine.generate takes a first-message hint
        if self.first_user_hint and not self.raw and not self.errors:
            # The caller's hint ("Regenerate with error hint") rides on the first
            # user message, as in the old loop, keeping the system prefix cacheable.
            user += _RETRY_USER_SUFFIX.format(error=self.first_user_hint)
        # upstream: arango-solutions-org/arango-query-core#9 — remove when the engine retries a provider call that raised within its budget
        for call in range(self.max_retries + 1):
            try:
                result = self.provider.generate(system, user)
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
                logger.warning("LLM call failed (call %d/%d): %s", call + 1, self.max_retries + 1, exc)
                if call == self.max_retries:
                    raise
                continue
            content, usage = result if isinstance(result, tuple) else (result, {})
            for key in _USAGE_KEYS:
                self.usage[key] += int((usage or {}).get(key, 0) or 0)
            self.raw.append(content)
            # upstream: arango-solutions-org/arango-query-core#7 — remove when the engine finds a fenced query anywhere in the response
            cypher = _extract_cypher_from_response(content)
            # upstream: arango-solutions-org/arango-query-core#8 — remove when the adapter can normalize a candidate before validation
            if self.ctx is not None:
                cypher = _fix_labels(cypher, self.ctx)
            self.candidates.append(cypher)
            return cypher, dict(usage or {})
        raise RuntimeError("unreachable: the loop returns or raises")


@dataclass(frozen=True)
class _TenantScopePostcondition:
    """The tenant guardrail as the engine's first postcondition, so a
    cross-tenant candidate is retried with the guardrail's hint (sharing the
    retry budget, as in the old loop) instead of refused outright."""

    tenant_context: TenantContext
    tenant_manifest: TenantScopeManifest | None
    physical_enforcement: Any
    code: str = TENANT_SCOPE_CODE

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        violation = check_tenant_scope(
            query,
            tenant_context=self.tenant_context,
            manifest=self.tenant_manifest,
            physical_enforcement=self.physical_enforcement,
        )
        if violation is None:
            return None
        return PostconditionViolation(
            reason=violation.reason, suggested_hint=violation.suggested_hint or "", code=TENANT_SCOPE_CODE
        )

    def prompt_section(self) -> str:
        return ""  # the tenant rules are in the grammar section (see _EngineAdapter)


@dataclass
class _EngineAdapter(CypherAdapter):
    """:class:`CypherAdapter` with the per-call prompt pieces of the old loop.

    Built per translation, so per-question content is legitimate here.
    """

    tenant_context: TenantContext | None = None
    tenant_manifest: TenantScopeManifest | None = None
    graph_intent: bool = False
    use_fewshot: bool = True
    fewshot_override: FewShotIndex | None = None
    resolved_entities: list[str] = field(default_factory=list)

    def grammar_prompt_section(self, schema_context: str) -> str:
        """The old system prompt's head: base + schema, tenant rules, graph
        intent. Examples, resolved entities and postcondition rules are added
        by the engine (seams 2 and 6, and ``postconditions``)."""
        builder = PromptBuilder(
            schema_summary=schema_context,
            tenant_context=self.tenant_context,
            tenant_manifest=self.tenant_manifest,
            graph_intent=self.graph_intent,
        )
        return builder.render_system()

    def few_shot_index(self) -> FewShotIndex | None:
        if not self.use_fewshot:
            return None
        return self.fewshot_override if self.fewshot_override is not None else _get_default_fewshot_index()

    def grounding_index(self) -> LabelIndex | None:
        """Seam 6 carries the pre-flight entity resolution (WP-25.2): the
        resolver already matched this question, so the index holds exactly its
        resolutions and :meth:`grounding_prompt_section` renders them as the old
        prompt did."""
        if not self.resolved_entities:
            return None
        return LabelIndex([GroundedEntity(id=line, labels=(line,)) for line in self.resolved_entities])

    def grounding_prompt_section(self, question: str, index: LabelIndex, k: int = 20) -> str:
        if not self.resolved_entities:
            return ""
        return PromptBuilder(
            schema_summary="", resolved_entities=self.resolved_entities
        )._render_resolved_entities_section()


def translate_with_engine(
    question: str,
    *,
    provider: LLMProvider,
    schema_summary: str,
    ctx: _SchemaCtx | None,
    mapping: MappingBundle,
    db: Any | None,
    max_retries: int,
    use_fewshot: bool,
    fewshot_index: FewShotIndex | None,
    resolved_entities: Sequence[str],
    tenant_context: TenantContext | None,
    tenant_manifest: TenantScopeManifest | None,
    retry_context: str | None,
    graph_intent: bool,
    postconditions: Sequence[Postcondition] | None,
) -> NL2CypherResult | None:
    """One NL→Cypher translation on :class:`NLQueryEngine`; the counterpart of
    :func:`~arango_cypher.nl2cypher._core._call_llm_with_retry`."""
    physical_enforcement = multitenancy_physical_enforcement(mapping) if mapping is not None else None
    adapter = _EngineAdapter(
        mapping=mapping,
        db=db,
        tenant_context=tenant_context,
        tenant_manifest=tenant_manifest,
        graph_intent=graph_intent,
        use_fewshot=use_fewshot,
        fewshot_override=fewshot_index,
        resolved_entities=list(resolved_entities),
    )
    checks: list[Any] = []
    if tenant_context is not None:
        checks.append(_TenantScopePostcondition(tenant_context, tenant_manifest, physical_enforcement))
    checks.extend(postconditions or ())
    bridge = _ProviderBridge(
        provider=provider, ctx=ctx, max_retries=max_retries, first_user_hint=retry_context or ""
    )
    engine = NLQueryEngine(
        provider=bridge,
        adapter=adapter,
        few_shot_k=3,
        max_retries=max_retries,
        guardrail_context={"tenant_context": tenant_context, "tenant_manifest": tenant_manifest},
        postconditions=tuple(checks),
        postcondition_mapping=mapping,
    )

    error = ""
    try:
        outcome = engine.generate(question, schema_context=schema_summary)
    except Exception as exc:  # noqa: BLE001 — the old loop's contract: degrade, never raise
        if not bridge.candidates:
            logger.warning("NL2Cypher (engine): no candidate produced: %s", exc)
            return None
        error = bridge.errors[-1] if bridge.errors else f"{type(exc).__name__}: {exc}"
    else:
        if outcome.ok:
            return NL2CypherResult(
                cypher=outcome.query,
                explanation=bridge.raw[-1] if bridge.raw else "",
                confidence=0.8,
                method="llm",
                schema_context=schema_summary,
                prompt_tokens=bridge.usage["prompt_tokens"],
                completion_tokens=bridge.usage["completion_tokens"],
                total_tokens=bridge.usage["total_tokens"],
                retries=outcome.retries,
                cached_tokens=bridge.usage["cached_tokens"],
            )
        error = outcome.error
        if outcome.postcondition is not None and outcome.postcondition.suggested_hint:
            error = f"{outcome.postcondition.reason} {outcome.postcondition.suggested_hint}"

    last = bridge.candidates[-1] if bridge.candidates else ""

    def failed(method: str, explanation: str) -> NL2CypherResult:
        return NL2CypherResult(
            cypher="",
            explanation=explanation,
            confidence=0.0,
            method=method,
            schema_context=schema_summary,
            prompt_tokens=bridge.usage["prompt_tokens"],
            completion_tokens=bridge.usage["completion_tokens"],
            total_tokens=bridge.usage["total_tokens"],
            retries=max_retries,
            cached_tokens=bridge.usage["cached_tokens"],
        )

    # Fail closed on tenant scoping exactly as the old loop: the last candidate
    # escapes the tenant, whatever the last failure was.
    if (
        tenant_context is not None
        and last
        and check_tenant_scope(
            last,
            tenant_context=tenant_context,
            manifest=tenant_manifest,
            physical_enforcement=physical_enforcement,
        )
        is not None
    ):
        return failed(
            "tenant_guardrail_blocked",
            (
                "Tenant-scoping guardrail: could not produce a Cypher "
                f"query scoped to tenant {tenant_context.display_name!r} "
                f"after {1 + max_retries} attempts. Last attempt was:\n\n"
                f"{last}\n\n"
                "Refusing to emit an unscoped query. Try rephrasing the "
                "question to reference the entity more concretely, or "
                "add a few-shot correction that demonstrates the "
                "tenant traversal path for this schema."
            ),
        )
    if last:
        logger.warning(
            "NL2Cypher (engine) validation_failed after %d attempts; last error: %s", 1 + max_retries, error
        )
        return failed(
            "validation_failed",
            (
                f"NL → Cypher validation failed after {1 + max_retries} "
                f"attempts. Last error: {error}.\n\n"
                f"Last attempted Cypher was:\n\n{last}"
            ),
        )
    return None
