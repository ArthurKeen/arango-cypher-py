"""Ask an LLM for an NL question and conceptual Cypher equivalent to a saved AQL.

The NL → Cypher prompts show the model only the conceptual schema (PRD §1.2).
Mining cannot: its input *is* physical AQL. So this prompt — offline, build-time,
never on the user path — also carries the physical → conceptual name table
(collection ``aws_iam_role`` is label ``AwsIamRole``; edge collection
``CAN_ASSUME`` is relationship ``CAN_ASSUME``), and the model must answer in
conceptual names only; the transpiler, not the model, maps them back.

The reply is one JSON object::

    {"question": "...", "cypher": "...", "params": {...}}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.providers import LLMProvider

from .harvest import SavedQuery

#: Bind values longer than this are elided from the prompt.
_MAX_PROMPT_VALUE = 300

SYSTEM_PROMPT = """You translate an ArangoDB AQL query into an equivalent openCypher query over a
conceptual graph schema, and write the plain-English question an analyst would ask to get its answer.

Conceptual schema (the ONLY labels, relationship types and properties you may use):
{schema}

Physical names in the AQL and what they are in the schema:
{names}

Rules:
- The Cypher must return the same documents as the AQL: the same start nodes, the same filters,
  the same traversal direction and depth, the same LIMIT. Return the nodes and relationships the AQL
  returns (a path, its edges or its vertices).
- Write labels and relationship types exactly as in the schema; never a collection name.
- AQL bind values become Cypher parameters ($name) with the same values in "params". A start node
  given as a document id is matched by its properties shown in the request, not by its id.
- Cypher subset this engine translates: every node pattern has exactly one label — when a node may
  be one of several labels, write one query per label joined with UNION (same RETURN columns in each);
  no CALL / procedures (no apoc.*); name a path variable `path` (or path1, path2) and never reuse it as
  a node or relationship variable.
- The question is how a domain analyst would ask for this answer: lead with the intent (the saved
  query's description says what it is for), in one plain sentence. Name concrete values a user would
  type (a role or bucket name, a tag) when the query filters on them. Leave housekeeping filters
  implicit — tenant or org ids, soft-delete flags, status codes — although the Cypher keeps them.
  Never mention AQL, Cypher, collections, bind parameters, or how to return the result.
- Reply with one JSON object and nothing else:
  {{"question": "...", "cypher": "...", "params": {{...}}}}"""


class GenerationError(Exception):
    """The model's reply was not a usable draft; the message is retry feedback."""


class ProviderError(Exception):
    """The LLM call itself failed (auth, quota, network) — not something a retry
    with feedback can fix. Never carries the request or any credential."""


@dataclass(frozen=True)
class Draft:
    question: str
    cypher: str
    params: dict[str, Any] = field(default_factory=dict)


def physical_name_table(bundle: MappingBundle) -> str:
    """``collection -> Label`` / ``edge collection -> TYPE`` lines for the prompt."""
    pm = bundle.physical_mapping or {}
    lines: list[str] = []
    for label, spec in sorted((pm.get("entities") or {}).items()):
        spec = spec or {}
        coll = spec.get("collectionName")
        if not coll:
            continue
        if spec.get("style") == "LABEL" and spec.get("typeField"):
            lines.append(
                f"- documents in {coll} with {spec['typeField']} = {spec.get('typeValue')!r} -> :{label}"
            )
        else:
            lines.append(f"- collection {coll} -> :{label}")
    for rtype, spec in sorted((pm.get("relationships") or {}).items()):
        spec = spec or {}
        coll = spec.get("edgeCollectionName")
        if not coll:
            continue
        if spec.get("style") == "GENERIC_WITH_TYPE" and spec.get("typeField"):
            lines.append(
                f"- edges in {coll} with {spec['typeField']} = {spec.get('typeValue')!r} -> [:{rtype}]"
            )
        else:
            lines.append(f"- edge collection {coll} -> [:{rtype}]")
    return "\n".join(lines) or "(none)"


def _short(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= _MAX_PROMPT_VALUE else text[:_MAX_PROMPT_VALUE] + "…"


def build_user_prompt(
    query: SavedQuery,
    bind_vars: dict[str, Any],
    *,
    start_properties: dict[str, Any] | None = None,
    feedback: str | None = None,
    previous: Draft | None = None,
) -> str:
    parts = [f"Saved query name: {query.name or '(none)'}", f"Description: {query.description or '(none)'}"]
    parts.append(f"AQL:\n{query.aql}")
    if bind_vars:
        parts.append("Bind values:\n" + "\n".join(f"- @{k} = {_short(v)}" for k, v in bind_vars.items()))
    if start_properties:
        shown = ", ".join(f"{k} = {_short(v)}" for k, v in start_properties.items())
        parts.append(f"@nodes[0] is the node selected in the graph; match it by: {shown}")
    if feedback:
        # A reply that did not parse leaves no draft to show, but its
        # rejection is still the feedback the next attempt needs.
        if previous is not None:
            parts.append(f"Your previous answer:\n{json.dumps(previous.__dict__)}")
        parts.append(
            f"Your previous answer was rejected: {feedback}\nFix it and reply with the corrected JSON object."
        )
    return "\n\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_draft(content: str) -> Draft:
    """The JSON object in the model's reply, fences and prose around it tolerated."""
    text = _FENCE.sub("", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise GenerationError("the reply contained no JSON object")
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise GenerationError(f"the reply's JSON did not parse ({exc})") from exc
    question = obj.get("question")
    cypher = obj.get("cypher")
    params = obj.get("params") or {}
    if not isinstance(question, str) or not question.strip():
        raise GenerationError('"question" is missing or empty')
    if not isinstance(cypher, str) or not cypher.strip():
        raise GenerationError('"cypher" is missing or empty')
    if not isinstance(params, dict):
        raise GenerationError('"params" must be an object')
    return Draft(question.strip(), cypher.strip(), dict(params))


def generate_draft(
    provider: LLMProvider,
    bundle: MappingBundle,
    query: SavedQuery,
    bind_vars: dict[str, Any],
    *,
    schema_summary: str,
    start_properties: dict[str, Any] | None = None,
    feedback: str | None = None,
    previous: Draft | None = None,
) -> Draft:
    system = SYSTEM_PROMPT.format(schema=schema_summary, names=physical_name_table(bundle))
    user = build_user_prompt(
        query, bind_vars, start_properties=start_properties, feedback=feedback, previous=previous
    )
    try:
        result = provider.generate(system, user)
    except Exception as exc:  # noqa: BLE001 - providers raise transport-specific errors
        raise ProviderError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    content = result[0] if isinstance(result, tuple) else result
    return parse_draft(str(content))
