"""Mine one saved query, or a whole database, into verified examples.

Per saved query: run the source as the reference (:mod:`.binding`), draft a
question + Cypher (:mod:`.generate`), transpile and execute the Cypher, and
keep the draft only if its results match the source's (:mod:`.signature`).
A failed attempt — the draft does not parse, does not translate, does not run,
or returns different documents — is fed back to the model, up to
:data:`MAX_ATTEMPTS` times.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from arango.exceptions import ArangoError
from arango_query_core import CoreError
from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.providers import LLMProvider

from .._arango_sync import sync
from ..api import translate
from .binding import SourceRejected, SourceRun, run_read_only, run_source
from .generate import Draft, GenerationError, ProviderError, generate_draft
from .harvest import SavedQuery, harvest_saved_queries
from .signature import ResultSignature, Verdict, compare, signature_of

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
#: A batch stops after this many queries in a row fail at the provider: a bad
#: key or exhausted quota would fail every remaining query the same way.
MAX_CONSECUTIVE_PROVIDER_FAILURES = 2
#: Properties that identify a selected start node to the model, in preference
#: order; the first few present are shown.
IDENTIFYING_PROPERTIES = ("name", "title", "external_id", "arn", "id", "email", "label")
MAX_IDENTIFYING = 3


@dataclass(frozen=True)
class MinedExample:
    question: str
    cypher: str
    params: dict[str, Any]
    aql: str  # the transpiled AQL that was verified
    bind_vars: dict[str, Any]
    verdict: Verdict
    source: SavedQuery
    source_signature: ResultSignature
    attempts: int


@dataclass(frozen=True)
class Attempt:
    """One failed draft and why it failed — the retry feedback, and the report."""

    failure: str
    question: str = ""
    cypher: str = ""


@dataclass
class Outcome:
    query: SavedQuery
    example: MinedExample | None = None
    reason: str = ""
    failed_attempts: list[Attempt] = field(default_factory=list)
    #: The LLM call failed outright; the query itself was never judged.
    provider_failed: bool = False

    @property
    def verified(self) -> bool:
        return self.example is not None


def start_properties(db: Any, start: str | None) -> dict[str, Any] | None:
    """The identifying properties of a canvas action's sampled start node."""
    if not start:
        return None
    try:
        doc = next(iter(sync(db.aql.execute("RETURN DOCUMENT(@id)", bind_vars={"id": start}))), None)
    except ArangoError as exc:
        logger.warning("could not read start node %s: %s", start, exc)
        return None
    if not isinstance(doc, dict):
        return None
    shown = {
        k: doc[k]
        for k in IDENTIFYING_PROPERTIES
        if k in doc and isinstance(doc[k], (str, int, float)) and str(doc[k]).strip()
    }
    picked = dict(list(shown.items())[:MAX_IDENTIFYING])
    return picked or {"_key": doc.get("_key")}


#: Node patterns with no label: ``(x)``, ``()``, ``(x {k: v})``. Used to make
#: the transpiler's "A single label is required" feedback actionable — the
#: error itself does not say which pattern lacks one.
_UNLABELED_NODE = re.compile(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)?\s*(?:\{[^}]*\})?\s*\)")
_PROCEDURE_CALL = re.compile(r"\bCALL\s+[A-Za-z_][\w.]*\s*\(", re.IGNORECASE)


def _unlabeled_nodes(cypher: str) -> list[str]:
    names = [m.group(1) or "()" for m in _UNLABELED_NODE.finditer(cypher)]
    return list(dict.fromkeys(names))


def _translation_feedback(error: str, cypher: str) -> str:
    feedback = f"the Cypher does not translate: {error}"
    if "single label is required" in error:
        nodes = _unlabeled_nodes(cypher)
        if nodes:
            feedback += (
                f". These node patterns have no label: {', '.join(nodes)}. Give each exactly one label; "
                "if a node may have several, write one query per label joined with UNION"
            )
    return feedback


@dataclass(frozen=True)
class _Checked:
    """A draft that translated, ran and matched the reference."""

    aql: str
    bind_vars: dict[str, Any]
    verdict: Verdict


def _check(db: Any, bundle: MappingBundle, draft: Draft, reference: ResultSignature) -> _Checked | str:
    """The verified translation of *draft*, or why it failed (retry feedback)."""
    if _PROCEDURE_CALL.search(draft.cypher):
        # Refused here, before translation: the transpiler does not support
        # procedures, and a CALL it half-translates fails later with an
        # unrelated-looking AQL error the model cannot act on.
        return "the Cypher calls a procedure; procedures (CALL ..., apoc.*) are not supported — use MATCH patterns"
    try:
        transpiled = translate(draft.cypher, mapping=bundle, params=draft.params)
    except CoreError as exc:
        return _translation_feedback(str(exc), draft.cypher)
    except Exception as exc:  # noqa: BLE001 - a parser failure is feedback for the model, not a crash
        return f"the Cypher does not parse: {type(exc).__name__}: {exc}"
    bind = dict(transpiled.bind_vars)
    try:
        rows = run_read_only(db, transpiled.aql, bind)
    except SourceRejected as exc:
        return f"the translated query {exc}"
    sig = signature_of(rows)
    verdict = compare(reference, sig)
    if not verdict.passed:
        return (
            f"its results differ from the saved query's ({verdict.detail}; saved query: "
            f"{reference.describe()}; yours: {sig.describe()})"
        )
    return _Checked(transpiled.aql, bind, verdict)


def mine_query(
    db: Any,
    bundle: MappingBundle,
    query: SavedQuery,
    provider: LLMProvider,
    *,
    schema_summary: str,
    edge_definitions: list[dict[str, Any]] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Outcome:
    outcome = Outcome(query)
    try:
        run: SourceRun = run_source(db, query, edge_definitions=edge_definitions)
    except SourceRejected as exc:
        outcome.reason = f"source: {exc}"
        return outcome

    props = start_properties(db, run.start)
    feedback: str | None = None
    previous: Draft | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            draft = generate_draft(
                provider,
                bundle,
                query,
                run.bind_vars,
                schema_summary=schema_summary,
                start_properties=props,
                feedback=feedback,
                previous=previous,
            )
        except GenerationError as exc:
            feedback = str(exc)
            outcome.failed_attempts.append(Attempt(feedback))
            continue
        except ProviderError as exc:
            outcome.reason = f"LLM provider failed: {exc}"
            outcome.provider_failed = True
            return outcome
        checked = _check(db, bundle, draft, run.signature)
        if isinstance(checked, _Checked):
            outcome.example = MinedExample(
                question=draft.question,
                cypher=draft.cypher,
                params=draft.params,
                aql=checked.aql,
                bind_vars=checked.bind_vars,
                verdict=checked.verdict,
                source=query,
                source_signature=run.signature,
                attempts=attempt,
            )
            return outcome
        feedback, previous = checked, draft
        outcome.failed_attempts.append(Attempt(checked, draft.question, draft.cypher))
    outcome.reason = (
        f"no verified Cypher after {max_attempts} attempts: {outcome.failed_attempts[-1].failure}"
    )
    return outcome


def mine_database(
    db: Any,
    bundle: MappingBundle,
    provider: LLMProvider,
    *,
    schema_summary: str,
    graph: str | None = None,
    include_builtins: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[list[Outcome], list[tuple[str, str]]]:
    """Outcomes for every saved query in *db* (scoped to *graph* when given),
    plus the harvest's skips. Visualizer built-ins are left out unless asked:
    "fetch edges of type X" teaches nothing about the domain."""
    report = harvest_saved_queries(db, graph=graph)
    edge_definitions: list[dict[str, Any]] = []
    if graph:
        edge_definitions = list(sync(db.graph(graph).properties()).get("edge_definitions") or [])
    outcomes: list[Outcome] = []
    skipped = list(report.skipped)
    provider_failures = 0
    for query in report.queries:
        where = f"{query.source}/{query.key}"
        if query.builtin and not include_builtins:
            skipped.append((where, "visualizer built-in"))
            continue
        if provider_failures >= MAX_CONSECUTIVE_PROVIDER_FAILURES:
            skipped.append((where, "not attempted: the LLM provider kept failing"))
            continue
        logger.info("mining %s %r", where, query.name)
        outcome = mine_query(
            db,
            bundle,
            query,
            provider,
            schema_summary=schema_summary,
            edge_definitions=edge_definitions,
            max_attempts=max_attempts,
        )
        provider_failures = provider_failures + 1 if outcome.provider_failed else 0
        outcomes.append(outcome)
    return outcomes, skipped
