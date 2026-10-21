"""NL → Cypher on the shared NL engine (issue #24).

``nl_to_cypher`` generates through :class:`arango_query_core.nl.NLQueryEngine`
with :class:`~arango_cypher.nl2cypher.adapter.CypherAdapter` — the engine the
other English→query paths in the portfolio (arango-sparql-py,
contextual-data-fabric) run, so a fix to the generate → validate → repair loop
is made once. Until issue #24 this package ran its own copy of that loop.

What that loop did is kept, each piece through the engine's own seam:

* the tenant guardrail runs as the first postcondition, so a violation is
  retried with its hint;
* resolved entities render through the grounding seam, after the examples;
* the tenant and graph-intent sections extend the grammar section;
* the caller's hint rides on the first user message (``user_hint``);
* a provider call that raises spends one attempt of the retry budget
  (``retry_provider_errors``);
* the query is found in a fenced block anywhere in the response, and
  :meth:`CypherAdapter.normalize` strips prose before an unfenced one and
  repairs hallucinated labels.

The result keeps :class:`~arango_cypher.nl2cypher._core.NL2CypherResult`'s
contract: ``method`` ``llm`` / ``validation_failed`` / ``tenant_guardrail_blocked``,
the same explanation wording ("Last attempted Cypher was:" is what the UI's
report masking looks for), and ``None`` when no candidate was ever produced
(the caller then falls back to the rule-based translator).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arango_query_core.nl import NLQueryEngine
from arango_query_core.nl.fewshot import FewShotIndex
from arango_query_core.nl.grounding import GroundedEntity, LabelIndex
from arango_query_core.nl.postconditions import PostconditionContext, PostconditionViolation

from ._core import (
    _RETRY_USER_SUFFIX,
    NL2CypherResult,
    PromptBuilder,
    _get_default_fewshot_index,
    _SchemaCtx,
)
from .adapter import CypherAdapter
from .tenant_guardrail import check_tenant_scope, multitenancy_physical_enforcement

if TYPE_CHECKING:
    from arango_query_core.mapping import MappingBundle
    from arango_query_core.nl.providers import LLMProvider

    from .postconditions import Postcondition
    from .tenant_guardrail import TenantContext
    from .tenant_scope import TenantScopeManifest

logger = logging.getLogger(__name__)

#: ``code`` of the tenant-scope postcondition, distinguishing it from callers'.
TENANT_SCOPE_CODE = "tenant_scope"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")


@dataclass
class _RecordingProvider:
    """The caller's provider as the engine sees it, recording what it answered.

    Keeps every raw response (the successful one becomes ``explanation``,
    which the engine does not return) and accumulates usage across every
    call that answered, so a translation that ends in an exception still
    reports its tokens.
    """

    provider: LLMProvider
    raw: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_USAGE_KEYS, 0))

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        result = self.provider.generate(system, user)
        # LLMProvider returns (text, usage); a bare string is tolerated, as
        # nl_to_cypher always has, for providers that report no usage.
        content, usage = result if isinstance(result, tuple) else (result, {})
        for key in _USAGE_KEYS:
            self.usage[key] += int((usage or {}).get(key, 0) or 0)
        self.raw.append(content)
        return content, dict(usage or {})


@dataclass(frozen=True)
class _ExamplesOrNone:
    """A few-shot retriever over *index* that answers no examples when the
    index raises (a dense model that fails to load or embed, say): the
    question is then asked zero-shot, as ``nl_to_cypher`` always has, instead
    of the whole translation falling to the rule-based translator."""

    index: FewShotIndex

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        try:
            return self.index.retrieve(question, k=k)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning(
                "Few-shot retrieval failed; asking without examples: %s: %s", type(exc).__name__, exc
            )
            return []


@dataclass(frozen=True)
class _TenantScopePostcondition:
    """The tenant guardrail as the engine's first postcondition, so a
    cross-tenant candidate is retried with the guardrail's hint (sharing the
    retry budget with parse and EXPLAIN failures) instead of refused outright."""

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
    """:class:`CypherAdapter` with the per-call prompt pieces of
    ``nl_to_cypher``: tenant rules, graph intent, the caller's few-shot
    choice and the resolved entities.

    Built per translation, so per-question content is legitimate here.
    """

    tenant_context: TenantContext | None = None
    tenant_manifest: TenantScopeManifest | None = None
    graph_intent: bool = False
    use_fewshot: bool = True
    fewshot_override: FewShotIndex | None = None
    resolved_entities: list[str] = field(default_factory=list)
    #: Every candidate the engine validated, normalized; the last is quoted
    #: when generation fails (the engine returns no query then).
    candidates: list[str] = field(default_factory=list)

    def normalize(self, query: str) -> str:
        candidate = query
        try:
            candidate = super().normalize(query)
        finally:
            # Recorded even when the repair raises: the engine then logs it
            # and validates the candidate as it came.
            self.candidates.append(candidate)
        return candidate

    def grammar_prompt_section(self, schema_context: str) -> str:
        """The system prompt's head, from :class:`PromptBuilder`: base +
        schema, tenant rules, graph intent. Examples, resolved entities and
        postcondition rules are added by the engine (seams 2 and 6, and
        ``postconditions``)."""
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
        index = self.fewshot_override if self.fewshot_override is not None else _get_default_fewshot_index()
        return FewShotIndex(_ExamplesOrNone(index)) if index is not None else None

    def grounding_index(self) -> LabelIndex | None:
        """Seam 6 carries the pre-flight entity resolution (WP-25.2): the
        resolver already matched this question, so the index holds exactly its
        resolutions and :meth:`grounding_prompt_section` renders them in
        :class:`PromptBuilder`'s section."""
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
    """One NL→Cypher translation on :class:`NLQueryEngine`.

    Returns the :class:`NL2CypherResult` contract ``nl_to_cypher`` has always
    had: ``method`` ``llm`` / ``validation_failed`` /
    ``tenant_guardrail_blocked``, the same explanation wording ("Last
    attempted Cypher was:" is what the UI's report masking looks for), and
    ``None`` when no candidate was ever produced (the caller then falls back
    to the rule-based translator).
    """
    physical_enforcement = multitenancy_physical_enforcement(mapping) if mapping is not None else None
    adapter = _EngineAdapter(
        mapping=mapping,
        db=db,
        schema_ctx=ctx,
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
    bridge = _RecordingProvider(provider=provider)
    engine = NLQueryEngine(
        provider=bridge,
        adapter=adapter,
        few_shot_k=3,
        max_retries=max_retries,
        guardrail_context={"tenant_context": tenant_context, "tenant_manifest": tenant_manifest},
        postconditions=tuple(checks),
        postcondition_mapping=mapping,
        retry_provider_errors=True,
    )
    # The caller's hint ("Regenerate with error hint") in the wording the
    # prompt has always used; the engine puts it after the question.
    user_hint = _RETRY_USER_SUFFIX.format(error=retry_context).lstrip("\n") if retry_context else ""

    error = ""
    try:
        outcome = engine.generate(question, schema_context=schema_summary, user_hint=user_hint)
    except Exception as exc:  # noqa: BLE001 — nl_to_cypher's contract: degrade, never raise
        # Every provider call raised (the engine re-raises the last), or a
        # seam did: with no candidate the rule-based translator takes over.
        if not adapter.candidates:
            logger.warning("NL2Cypher: no candidate produced: %s: %s", type(exc).__name__, exc)
            return None
        error = f"{type(exc).__name__}: {exc}"
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

    last = adapter.candidates[-1] if adapter.candidates else ""

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

    # Fail closed on tenant scoping: the last candidate escapes the tenant,
    # whatever the last failure was.
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
            "NL2Cypher validation_failed after %d attempts; last error: %s", 1 + max_retries, error
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
