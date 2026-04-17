"""Natural Language to Cypher translation pipeline.

Converts plain-English questions into Cypher queries using schema context
from the MappingBundle. Supports pluggable LLM backends via the
``LLMProvider`` protocol and includes a rule-based fallback for common
patterns when no LLM is configured.

Usage::

    from arango_cypher.nl2cypher import nl_to_cypher

    result = nl_to_cypher(
        "Find all people who acted in The Matrix",
        mapping=my_mapping_bundle,
    )
    print(result.cypher)

    # With a custom provider:
    from arango_cypher.nl2cypher import OpenAIProvider, nl_to_cypher
    provider = OpenAIProvider(model="gpt-4o", api_key="sk-...")
    result = nl_to_cypher("...", mapping=bundle, llm_provider=provider)
"""
from __future__ import annotations

import abc
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from arango_query_core.mapping import MappingBundle

logger = logging.getLogger(__name__)


@dataclass
class NL2CypherResult:
    """Result of a natural language to Cypher translation."""
    cypher: str
    explanation: str = ""
    confidence: float = 0.0
    method: str = "rule_based"
    schema_context: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    retries: int = 0


# ---------------------------------------------------------------------------
# Pluggable LLM provider interface
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM backends that generate text from a prompt.

    ``generate`` accepts a user question and schema context string and
    returns ``(response_text, usage_dict)`` where *usage_dict* contains
    ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``.
    """

    def generate(
        self, question: str, schema_summary: str,
    ) -> tuple[str, dict[str, int]]:
        """Return ``(content, usage_dict)`` for the given question and schema."""
        ...


class _BaseChatProvider:
    """Shared HTTP-based chat completion logic for OpenAI-compatible APIs."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        timeout: int = 30,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._extra_headers = extra_headers or {}

    def _chat(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int]]:
        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return content, {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }


class OpenAIProvider(_BaseChatProvider):
    """OpenAI-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENAI_API_KEY``
      - ``base_url`` / ``OPENAI_BASE_URL``  (default: OpenAI)
      - ``model``   / ``OPENAI_MODEL``      (default: gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, question: str, schema_summary: str) -> tuple[str, dict[str, int]]:
        system = _SYSTEM_PROMPT.format(schema=schema_summary)
        return self._chat(system, question)


class OpenRouterProvider(_BaseChatProvider):
    """OpenRouter-compatible chat completion provider.

    Reads configuration from constructor args or environment variables:
      - ``api_key`` / ``OPENROUTER_API_KEY``
      - ``model``   / ``OPENROUTER_MODEL``   (default: openai/gpt-4o-mini)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            model=model or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            temperature=temperature,
            timeout=timeout,
        )

    def generate(self, question: str, schema_summary: str) -> tuple[str, dict[str, int]]:
        system = _SYSTEM_PROMPT.format(schema=schema_summary)
        return self._chat(system, question)


def get_llm_provider() -> OpenAIProvider | OpenRouterProvider | None:
    """Create an LLM provider from environment configuration.

    Resolution order:
    1. ``LLM_PROVIDER=openai``      → :class:`OpenAIProvider`
    2. ``LLM_PROVIDER=openrouter``  → :class:`OpenRouterProvider`
    3. Auto-detect: if ``OPENROUTER_API_KEY`` is set and ``OPENAI_API_KEY``
       is not, use :class:`OpenRouterProvider`; otherwise
       :class:`OpenAIProvider`.

    Returns ``None`` when no API key is available.
    """
    explicit = os.environ.get("LLM_PROVIDER", "").lower().strip()
    if explicit == "openrouter":
        p = OpenRouterProvider()
        return p if p.api_key else None
    if explicit == "openai":
        p = OpenAIProvider()
        return p if p.api_key else None

    has_openai = bool(os.environ.get("OPENAI_API_KEY", ""))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY", ""))
    if has_openrouter and not has_openai:
        return OpenRouterProvider()
    if has_openai:
        return OpenAIProvider()
    return None


def _property_quality_hint(prop_meta: dict[str, Any] | None) -> str:
    """Render a compact data-quality hint suffix for a property.

    Returns a string like ``" [sentinels: 'NULL', 'N/A'; numeric-like]"`` when
    the physical mapping carries sentinel/numeric-like metadata, otherwise
    the empty string.
    """
    if not isinstance(prop_meta, dict):
        return ""
    parts: list[str] = []
    sentinels = prop_meta.get("sentinelValues") or prop_meta.get("sentinel_values")
    if isinstance(sentinels, list | tuple) and sentinels:
        quoted = ", ".join(f"'{s}'" for s in list(sentinels)[:3])
        parts.append(f"sentinels: {quoted}")
    if prop_meta.get("numericLike") or prop_meta.get("numeric_like"):
        parts.append("numeric-like string")
    if not parts:
        return ""
    return f" [{'; '.join(parts)}]"


def _pm_entity_props(label: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("entities"), dict):
        ent = pm["entities"].get(label, {})
        if isinstance(ent, dict):
            props = ent.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _pm_relationship_props(rtype: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("relationships"), dict):
        rel = pm["relationships"].get(rtype, {})
        if isinstance(rel, dict):
            props = rel.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _flagged_properties(
    labeled_props: dict[str, dict[str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Flatten (label, propName, meta) for properties carrying quality hints."""
    flagged: list[tuple[str, str, dict[str, Any]]] = []
    for label, props in labeled_props.items():
        for pname, meta in props.items():
            if not isinstance(meta, dict):
                continue
            has_sentinel = bool(meta.get("sentinelValues") or meta.get("sentinel_values"))
            has_numeric = bool(meta.get("numericLike") or meta.get("numeric_like"))
            if has_sentinel or has_numeric:
                flagged.append((label, pname, meta))
    return flagged


_DATA_QUALITY_BLOCK_CYPHER = (
    "\nData-quality hints:\n"
    "  - When a property is marked 'sentinels: ...', the column uses string "
    "placeholder(s) for missing values (e.g. the literal text 'NULL'). These "
    "are NOT real nulls — exclude them in WHERE clauses, for example "
    "`WHERE t.COMPANY_SIZE <> 'NULL' AND t.COMPANY_SIZE IS NOT NULL`.\n"
    "  - When a property is marked 'numeric-like string', cast to number "
    "before ordering or comparing numerically, e.g. "
    "`toInteger(t.COMPANY_SIZE)` or `toFloat(t.AMOUNT)`.\n"
    "  - For 'top-N by numeric field X', combine both: filter out the "
    "sentinels, then ORDER BY the cast numeric value."
)


def _build_schema_summary(bundle: MappingBundle) -> str:
    """Build a conceptual-only schema description for LLM context.

    Per §1.2, the LLM prompt contains only conceptual labels, relationship
    types, properties (by conceptual name), and domain/range — never
    physical collection names, mapping styles, typeField/typeValue, physical
    field names, or other physical mapping details.  The LLM generates
    pure Cypher against the ontology; the transpiler handles the physical
    mapping.

    Data-quality hints (string sentinels, numeric-like string columns) are
    included in the property listing so the LLM can emit the right filters
    and casts.
    """
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping
    lines: list[str] = ["Graph schema (Cypher labels and relationship types):"]

    entities_emitted: list[str] = []

    def _format_entity(label: str, prop_names: list[str]) -> str:
        pm_props = _pm_entity_props(label, pm)
        parts: list[str] = []
        for name in prop_names[:8]:
            hint = _property_quality_hint(pm_props.get(name))
            parts.append(f"{name}{hint}")
        prop_str = ", ".join(parts) if parts else "no properties"
        return f"  Node :{label} ({prop_str})"

    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        for e in cs_entities:
            name = e.get("name", "")
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if not props:
                props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)
    elif isinstance(cs_entity_types, list) and cs_entity_types:
        for name in cs_entity_types:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    if not entities_emitted and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rtype = r.get("type", "")
            from_e = r.get("fromEntity", "?")
            to_e = r.get("toEntity", "?")
            rprops = [
                p.get("name", "") for p in r.get("properties", [])
                if isinstance(p, dict) and p.get("name")
            ]
            pm_rprops = _pm_relationship_props(rtype, pm)
            formatted = [
                f"{n}{_property_quality_hint(pm_rprops.get(n))}" for n in rprops
            ]
            prop_str = f" [{', '.join(formatted)}]" if formatted else ""
            lines.append(f"  (:{from_e})-[:{rtype}{prop_str}]->(:{to_e})")
    elif isinstance(cs_rel_types, list) and cs_rel_types:
        for rtype in cs_rel_types:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{from_e})-[:{rtype}]->(:{to_e})")
    elif isinstance(pm.get("relationships"), dict):
        for rtype in pm["relationships"]:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{from_e})-[:{rtype}]->(:{to_e})")

    labeled_ent_props = {
        label: _pm_entity_props(label, pm) for label in entities_emitted
    }
    if _flagged_properties(labeled_ent_props):
        lines.append(_DATA_QUALITY_BLOCK_CYPHER)

    return "\n".join(lines)


def _conceptual_props_for(
    label: str, cs: dict[str, Any], pm: dict[str, Any],
) -> list[str]:
    """Return property names for a conceptual label (max 8).

    Prefers conceptual schema properties; falls back to physical mapping
    property *names* (which are conceptual property names, not field names).
    """
    for e in cs.get("entities", []):
        if isinstance(e, dict) and e.get("name") == label:
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if props:
                return props[:8]

    if isinstance(pm.get("entities"), dict):
        pme = pm["entities"].get(label, {})
        if isinstance(pme, dict):
            return list(pme.get("properties", {}).keys())[:8]
    return []


def _conceptual_domain_range(
    rtype: str, cs: dict[str, Any], pm: dict[str, Any],
) -> tuple[str, str]:
    """Return (domain, range) for a relationship using conceptual metadata."""
    for r in cs.get("relationships", []):
        if isinstance(r, dict) and r.get("type") == rtype:
            return r.get("fromEntity", "?"), r.get("toEntity", "?")
    if isinstance(pm.get("relationships"), dict):
        pmr = pm["relationships"].get(rtype, {})
        if isinstance(pmr, dict):
            return pmr.get("domain", "?"), pmr.get("range", "?")
    return "?", "?"


_SYSTEM_PROMPT = """You are a Cypher query expert. Given a natural language question and a graph schema, generate a valid Cypher query.

Rules:
- Use only node labels and relationship types from the schema
- Use property names from the schema
- Return a single Cypher query (no explanation)
- Use standard Cypher syntax (MATCH, WHERE, RETURN, ORDER BY, LIMIT, etc.)
- For counts, use count()
- For aggregations, use collect(), sum(), avg(), min(), max()
- Wrap the query in ```cypher``` code block

{schema}"""


def _extract_cypher_from_response(text: str) -> str:
    """Extract Cypher query from LLM response (handles code blocks)."""
    m = re.search(r"```(?:cypher)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    cypher_lines = []
    for line in lines:
        upper = line.upper()
        if any(kw in upper for kw in ("MATCH", "RETURN", "WHERE", "WITH", "OPTIONAL", "ORDER", "LIMIT", "UNWIND", "CREATE", "SET", "DELETE")):
            cypher_lines.append(line)
        elif cypher_lines:
            cypher_lines.append(line)
    return "\n".join(cypher_lines) if cypher_lines else text.strip()


def _validate_cypher(cypher: str) -> tuple[bool, str]:
    """Syntactic check using the ANTLR parser.

    Returns ``(ok, error_message)``.  On success *error_message* is empty.
    Falls back to a keyword heuristic when the parser is unavailable.
    """
    if not cypher or not cypher.strip():
        return False, "empty Cypher string"
    try:
        from arango_cypher.parser import parse_cypher
        parse_cypher(cypher)
        return True, ""
    except Exception as exc:
        return False, str(exc)
    upper = cypher.upper()
    ok = any(kw in upper for kw in ("MATCH", "RETURN", "CREATE", "MERGE", "CALL"))
    return ok, ("" if ok else "no recognizable Cypher clause found")


_DEFAULT_PROVIDER: _BaseChatProvider | None = None
_DEFAULT_PROVIDER_RESOLVED = False


def _get_default_provider() -> _BaseChatProvider | None:
    """Lazily create a default LLM provider via :func:`get_llm_provider`."""
    global _DEFAULT_PROVIDER, _DEFAULT_PROVIDER_RESOLVED
    if _DEFAULT_PROVIDER_RESOLVED:
        return _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = get_llm_provider()
    _DEFAULT_PROVIDER_RESOLVED = True
    return _DEFAULT_PROVIDER


def _fix_labels(cypher: str, ctx: "_SchemaCtx") -> str:
    """Rewrite Cypher labels that don't exist in the mapping to the closest match.

    Handles common LLM hallucinations like ``Actor`` → ``Person`` by using the
    same fuzzy matching the rule-based engine uses, plus role-synonym lookup.
    """
    def _replace_label(m: re.Match) -> str:
        prefix = m.group(1)  # ":" or ":" preceded by "("
        label = m.group(2)
        if label.lower() in ctx.entities:
            return prefix + ctx.entities[label.lower()]["name"]
        if label.lower() in {r["type"].lower() for r in ctx.relationships.values()}:
            return prefix + label
        # Try role-synonym → relationship → fromEntity
        role = label.lower().rstrip("s")
        rel = ctx.role_to_rel.get(role)
        if rel:
            from_e = rel.get("fromEntity", "")
            if from_e and from_e != "Any":
                return prefix + from_e
        # Try fuzzy entity match
        ent = _match_entity(label, ctx.entities)
        if ent:
            return prefix + ent["name"]
        return prefix + label

    return re.sub(r"((?:\(|\[)[a-zA-Z0-9_]*:)([A-Z]\w*)", _replace_label, cypher)


def _call_llm_with_retry(
    question: str,
    schema_summary: str,
    provider: LLMProvider,
    max_retries: int = 2,
    ctx: "_SchemaCtx | None" = None,
) -> NL2CypherResult | None:
    """Call the LLM provider with parse-based validation and retry.

    After each LLM call the generated Cypher is parsed via the ANTLR
    grammar.  If parsing fails, the specific parse error is fed back to
    the LLM for up to ``max_retries`` additional attempts.  The result
    includes a ``retries`` count so callers can observe how many rounds
    were needed.
    """
    last_error = ""
    best_cypher = ""
    best_content = ""
    total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for attempt in range(1 + max_retries):
        try:
            prompt_question = question
            if attempt > 0 and last_error:
                prompt_question = (
                    f"{question}\n\n"
                    f"Your previous Cypher was invalid: {last_error}. "
                    f"Please fix it."
                )

            result = provider.generate(prompt_question, schema_summary)
            if isinstance(result, tuple):
                content, usage = result
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
            else:
                content = result

            cypher = _extract_cypher_from_response(content)

            if ctx:
                cypher = _fix_labels(cypher, ctx)

            if not best_cypher:
                best_cypher = cypher
                best_content = content

            ok, err_msg = _validate_cypher(cypher)
            if ok:
                return NL2CypherResult(
                    cypher=cypher,
                    explanation=content,
                    confidence=0.8,
                    method="llm",
                    schema_context=schema_summary,
                    prompt_tokens=total_usage["prompt_tokens"],
                    completion_tokens=total_usage["completion_tokens"],
                    total_tokens=total_usage["total_tokens"],
                    retries=attempt,
                )

            best_cypher = cypher
            best_content = content
            last_error = err_msg or "generated text did not parse as Cypher"
            logger.info(
                "LLM attempt %d/%d: validation failed for: %s",
                attempt + 1, 1 + max_retries, cypher[:120],
            )
        except Exception as e:
            logger.warning("LLM call failed (attempt %d): %s", attempt + 1, e)
            last_error = str(e)

    if best_cypher:
        return NL2CypherResult(
            cypher=best_cypher,
            explanation=f"WARNING: Cypher failed validation after {1 + max_retries} attempts. "
                        f"Last error: {last_error}\n\n{best_content}",
            confidence=0.3,
            method="llm",
            schema_context=schema_summary,
            prompt_tokens=total_usage["prompt_tokens"],
            completion_tokens=total_usage["completion_tokens"],
            total_tokens=total_usage["total_tokens"],
            retries=max_retries,
        )
    return None


def _build_schema_context(bundle: MappingBundle) -> "_SchemaCtx":
    """Extract entities, relationships, and derived lookup tables from the mapping."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: dict[str, dict] = {}
    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        entities = {e["name"].lower(): e for e in cs_entities if "name" in e}
    elif isinstance(cs_entity_types, list):
        for name in cs_entity_types:
            entities[name.lower()] = {"name": name, "properties": []}
    if not entities and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            entities[name.lower()] = {"name": name, "properties": []}

    relationships: dict[str, dict] = {}
    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        relationships = {r["type"].lower(): r for r in cs_rels if "type" in r}
    elif isinstance(cs_rel_types, list):
        for rtype in cs_rel_types:
            pm_rel = pm.get("relationships", {}).get(rtype, {}) if isinstance(pm.get("relationships"), dict) else {}
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }
    if not relationships and isinstance(pm.get("relationships"), dict):
        for rtype, pm_rel in pm["relationships"].items():
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }

    # Build role-noun → relationship mapping (actor→ACTED_IN, director→DIRECTED, etc.)
    _ROLE_SYNONYMS: dict[str, list[str]] = {
        "acted_in": ["actor", "actress", "cast", "star", "performer"],
        "directed": ["director"],
        "produced": ["producer"],
        "wrote": ["writer", "author", "screenwriter"],
        "reviewed": ["reviewer", "critic"],
        "follows": ["follower"],
        "knows": ["friend", "acquaintance", "contact"],
    }
    role_to_rel: dict[str, dict] = {}
    for rkey, rdef in relationships.items():
        for synonyms in _ROLE_SYNONYMS.values():
            normalized_rkey = rkey.replace("_", "")
            for syn_key, syn_list in _ROLE_SYNONYMS.items():
                if syn_key.replace("_", "") == normalized_rkey or syn_key == rkey:
                    for s in syn_list:
                        role_to_rel[s] = rdef
                    break

    return _SchemaCtx(entities=entities, relationships=relationships,
                      role_to_rel=role_to_rel, pm=pm)


@dataclass
class _SchemaCtx:
    entities: dict[str, dict]
    relationships: dict[str, dict]
    role_to_rel: dict[str, dict]
    pm: dict[str, Any]


def _extract_filter_value(text: str) -> str:
    """Extract a meaningful filter value from text, stripping articles and entity nouns."""
    text = re.sub(r"^(?:the|a|an|some|all|any)\s+", "", text.strip())
    text = re.sub(r"\s+(?:movie|movies|film|films|person|persons|people)s?$", "", text, flags=re.I)
    return text.strip()


def _find_rel_for_verb(verb: str, relationships: dict[str, dict]) -> dict | None:
    """Map a verb from the question to a relationship using verb stems and synonyms."""
    verb = verb.lower().rstrip("s").rstrip("ed")
    _VERB_TO_REL: dict[str, str] = {
        "act": "acted_in", "star": "acted_in", "appear": "acted_in",
        "direct": "directed", "helm": "directed",
        "produc": "produced", "made": "produced",
        "writ": "wrote", "wrot": "wrote", "pen": "wrote",
        "review": "reviewed", "rat": "reviewed", "critiqu": "reviewed",
        "follow": "follows",
        "know": "knows",
    }
    for stem, rel_key in _VERB_TO_REL.items():
        if verb.startswith(stem) or stem.startswith(verb):
            if rel_key in relationships:
                return relationships[rel_key]
    rel = _match_relationship(verb, relationships)
    if rel:
        return rel
    return None


def _rule_based_translate(question: str, bundle: MappingBundle) -> NL2CypherResult:
    """Rule-based fallback: pattern-match common natural language queries."""
    ctx = _build_schema_context(bundle)
    entities = ctx.entities
    relationships = ctx.relationships
    role_to_rel = ctx.role_to_rel

    q = question.lower().strip().rstrip("?").rstrip(".")
    wants_single = bool(re.search(r"\b(?:a\s+(?:random|single|sample)|one)\b", q))

    # --- Pattern 1: "which/what ROLE verb prep FILTER" ---
    # "which actors were in the Star Wars Movies"
    _VERB_PHRASES = (
        r"(?:were|was|are|is)\s+in"
        r"|act(?:ed)?\s+in|star(?:red)?\s+in|appear(?:ed)?\s+in"
        r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e|ten)?|wrote"
        r"|review(?:ed)?|follow(?:ed|s)?|know[sn]?"
    )
    m = re.match(
        r"(?:which|what|find|list|show)\s+(\w+)\s+"
        r"(?:(?:that|who|which)\s+)?"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        role_word = m.group(1).rstrip("s")
        filter_text = _extract_filter_value(m.group(2)) if m.group(2).strip() else ""
        # Find the verb phrase to determine the relationship
        verb_match = re.search(
            r"((?:were|was|are|is)\s+in|acted?\s*in|starred?\s*in|appeared?\s*in"
            r"|directed?|produced?|wrote?|written|reviewed?|followed?|known?)", q,
        )
        rel = role_to_rel.get(role_word)
        if not rel and verb_match:
            verb_text = verb_match.group(1).split()[0]
            rel = _find_rel_for_verb(verb_text, relationships)
        if not rel:
            rel = _find_rel_for_verb(role_word, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {from_e} via {rel['type']}", confidence=0.6, method="rule_based",
                )

    # --- Pattern 2: "which/what ENTITY did NAME verb in" (reverse direction) ---
    # "what movies did Tom Hanks act in" / "which movies has Keanu Reeves directed"
    m = re.match(
        r"(?:which|what)\s+(\w+)\s+"
        r"(?:did|has|have|had|does|do)\s+"
        r"(.+?)\s+"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        entity_hint = m.group(1).rstrip("s")
        person_name = m.group(2).strip()
        entity = _match_entity(entity_hint, entities)
        verb_match = re.search(
            r"((?:were|was)\s+in|act(?:ed)?\s*in|star(?:red)?\s*in"
            r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e)?|review(?:ed)?|follow(?:ed)?)", q,
        )
        rel = _find_rel_for_verb(verb_match.group(1).split()[0], relationships) if verb_match else None
        if entity and rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                target_label = entity["name"]
                source_label = from_e if target_label != from_e else to_e
                cypher = (
                    f"MATCH (a:{source_label})-[:{rel['type']}]->(b:{target_label})\n"
                    f"WHERE toLower(a.name) CONTAINS '{person_name}'\n"
                    f"RETURN b"
                )
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {target_label} via {rel['type']}", confidence=0.6, method="rule_based",
                )

    # --- Pattern 3: "who VERB FILTER" ---
    # "who directed The Matrix" / "who acted in Forrest Gump"
    m = re.match(r"who\s+(\w+)\s+(?:in\s+)?(.+)", q)
    if m:
        verb = m.group(1)
        filter_text = _extract_filter_value(m.group(2))
        rel = _find_rel_for_verb(verb, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = (
                    f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})\n"
                    f"WHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'\n"
                    f"RETURN a"
                )
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find who {verb} {filter_text}", confidence=0.5, method="rule_based",
                )

    # --- Normalize for simpler patterns ---
    q = re.sub(
        r"^(?:(?:can\s+you\s+)?(?:please\s+)?(?:give\s+me|show\s+me|get\s+me|tell\s+me|i\s+(?:want|need))\s+)",
        "get ", q,
    )

    # Extract explicit numeric limit: "get 5 people" / "get 2 random people"
    explicit_limit: int | None = None
    limit_m = re.match(r"^(\w+\s+)(\d+)\s+", q)
    if limit_m:
        explicit_limit = int(limit_m.group(2))
        q = limit_m.group(1) + q[limit_m.end():]

    q = re.sub(r"^(\w+\s+)(?:(?:a|an)\s+(?:random|single|sample)\s+|the\s+|an?\s+)", r"\1", q)
    q = re.sub(r"^(\w+\s+)(?:some|any)\s+", r"\1all ", q)
    # Strip "random" / "sample" that may remain after number extraction
    q = re.sub(r"^(\w+\s+)(?:random|sample)\s+", r"\1", q)

    # --- Pattern 4: "find/get/list X" (simple entity lookup) ---
    m = re.match(r"(?:find|list|show|get|return|fetch|display|retrieve|select|which|what)\s+(?:all\s+)?(\w+)s?\b(.*)", q)
    if m:
        entity_hint = m.group(1)
        rest = m.group(2).strip()

        # Check if the entity hint is actually a role synonym
        role_word = entity_hint.rstrip("s")
        rel = role_to_rel.get(role_word)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                filter_text = _extract_filter_value(rest) if rest else ""
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher, explanation=f"Find {from_e} via {rel['type']}", confidence=0.5, method="rule_based",
                )

        entity = _match_entity(entity_hint, entities)
        if entity:
            name = entity["name"]
            props = [p["name"] for p in entity.get("properties", [])[:5]]
            ret = ", ".join(f"n.{p}" for p in props) if props else "n"
            if explicit_limit:
                limit = f"\nLIMIT {explicit_limit}"
            elif wants_single:
                limit = "\nLIMIT 1"
            else:
                limit = ""

            if rest:
                where = _parse_simple_filter(rest, "n")
                if where:
                    return NL2CypherResult(
                        cypher=f"MATCH (n:{name})\nWHERE {where}\nRETURN {ret}{limit}",
                        explanation=f"Find {name} nodes with filter",
                        confidence=0.5, method="rule_based",
                    )

            return NL2CypherResult(
                cypher=f"MATCH (n:{name})\nRETURN {ret}{limit}",
                explanation=f"{'Get one' if wants_single else 'List all'} {name} node{'s' if not wants_single else ''}",
                confidence=0.6, method="rule_based",
            )

    # --- Pattern 5: "how many X" / "count X" ---
    m = re.match(r"(?:how many|count)\s+(\w+)s?\b", q)
    if m:
        entity = _match_entity(m.group(1), entities)
        if entity:
            return NL2CypherResult(
                cypher=f"MATCH (n:{entity['name']})\nRETURN count(n)",
                explanation=f"Count {entity['name']} nodes",
                confidence=0.7, method="rule_based",
            )

    # --- Pattern 6: relationship type name appears in the question ---
    for rtype, rdef in relationships.items():
        if rtype.replace("_", " ") in q or rtype in q:
            from_e = rdef.get("fromEntity", "Any")
            to_e = rdef.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                return NL2CypherResult(
                    cypher=f"MATCH (a:{from_e})-[:{rdef['type']}]->(b:{to_e})\nRETURN a, b",
                    explanation=f"Pattern matched relationship type {rdef['type']}",
                    confidence=0.3, method="rule_based",
                )

    return NL2CypherResult(
        cypher="",
        explanation="Could not generate a query from the input. Try rephrasing or use an LLM backend.",
        confidence=0.0,
        method="rule_based",
    )


_IRREGULAR_PLURALS: dict[str, str] = {
    "people": "person", "persons": "person",
    "men": "man", "women": "woman",
    "children": "child", "mice": "mouse",
    "data": "datum", "indices": "index",
}


def _match_entity(hint: str, entities: dict[str, dict]) -> dict | None:
    """Fuzzy-match an entity name from user input."""
    hint = hint.lower()
    hint_singular = _IRREGULAR_PLURALS.get(hint, hint)
    stems = {hint, hint_singular, hint.rstrip("s"), hint.rstrip("es"), re.sub(r"ies$", "y", hint)}
    for key, val in entities.items():
        key_stems = {key, key.rstrip("s"), key.rstrip("es"), re.sub(r"ies$", "y", key)}
        if stems & key_stems:
            return val
    # Substring matching
    for key, val in entities.items():
        if hint in key or key in hint:
            return val
    # Levenshtein-lite: check if stems share a common prefix of ≥3 chars
    for key, val in entities.items():
        key_base = re.sub(r"[aeiouy]+$", "", key)
        for s in stems:
            s_base = re.sub(r"[aeiouy]+$", "", s)
            if len(s_base) >= 3 and (s_base.startswith(key_base) or key_base.startswith(s_base)):
                return val
    return None


def _match_relationship(hint: str, relationships: dict[str, dict]) -> dict | None:
    """Fuzzy-match a relationship type."""
    hint = hint.lower()
    for key, val in relationships.items():
        normalized = key.replace("_", "").lower()
        if hint in normalized or normalized in hint:
            return val
    verb_map = {
        "acted": "acted_in",
        "directed": "directed",
        "produced": "produced",
        "wrote": "wrote",
        "reviewed": "reviewed",
        "follows": "follows",
        "knows": "knows",
        "purchased": "purchased",
        "bought": "purchased",
        "ordered": "purchased",
        "sold": "sold_by",
        "reports": "reports_to",
        "supplies": "supplied_by",
    }
    mapped = verb_map.get(hint)
    if mapped:
        for key, val in relationships.items():
            if key == mapped:
                return val
    return None


def _parse_simple_filter(text: str, var: str) -> str | None:
    """Parse simple 'where/with/in X' filters."""
    m = re.match(r"(?:where|with|in|from)\s+(?:the\s+)?(?:name|title)\s+(?:is\s+)?['\"]?(.+?)['\"]?$", text)
    if m:
        val = m.group(1).strip("'\"")
        return f"{var}.name = '{val}'"

    m = re.match(r"(?:where|in|from)\s+(?:country\s+)?(?:is\s+)?['\"]?(\w+)['\"]?$", text)
    if m:
        return f"{var}.country = '{m.group(1)}'"

    return None


@dataclass
class NL2AqlResult:
    """Result of a natural language to AQL direct translation."""
    aql: str
    bind_vars: dict[str, Any]
    explanation: str = ""
    confidence: float = 0.0
    method: str = "llm"
    schema_context: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


_AQL_SYSTEM_PROMPT = """You are an ArangoDB AQL query expert. Given a natural language question and a database schema, generate a valid AQL query.

{schema}

## AQL Syntax Reference

### Basic query structure
```
FOR doc IN collection
  FILTER condition
  SORT doc.field ASC|DESC
  LIMIT [offset,] count
  RETURN doc | {{ field1: doc.field1, ... }}
```

### Graph traversal (1-hop)
```
FOR vertex, edge IN 1..1 OUTBOUND|INBOUND|ANY startVertex edgeCollection
  RETURN vertex
```
- OUTBOUND: follows _from -> _to direction (startVertex is _from)
- INBOUND: reverse direction (startVertex is _to)
- ANY: both directions
- The startVertex must be a document or document _id

### Graph traversal (multi-hop)
```
FOR vertex, edge, path IN min..max OUTBOUND|INBOUND|ANY startVertex edgeCollection
  RETURN vertex
```
- path.vertices is array of vertices along the path
- path.edges is array of edges along the path

### Chaining traversals (multi-step patterns)
```
FOR a IN Collection1
  FILTER a.prop == "value"
  FOR b IN OUTBOUND a edgeCollection1
    FOR c IN OUTBOUND b edgeCollection2
      RETURN {{ a: a, b: b, c: c }}
```

### Aggregation with COLLECT
```
// Count with grouping
FOR doc IN collection
  FOR related IN OUTBOUND doc edgeCol
    COLLECT key = doc.field WITH COUNT INTO count
    RETURN {{ key, count }}

// Aggregate functions
FOR doc IN collection
  COLLECT key = doc.field AGGREGATE total = SUM(doc.value), avg = AVG(doc.value), cnt = COUNT(doc)
  RETURN {{ key, total, avg, cnt }}

// Collect into array
FOR a IN col1
  FOR b IN OUTBOUND a edgeCol
    COLLECT groupKey = a.field INTO items = b
    RETURN {{ groupKey, items }}
```

### OPTIONAL MATCH equivalent (left join)
```
FOR d IN Collection
  LET related = FIRST(FOR r IN OUTBOUND d edgeCol RETURN r)
  RETURN {{ d, related }}  // related is null if no match
```

### Subqueries
```
FOR doc IN collection
  LET count = LENGTH(FOR x IN OUTBOUND doc edgeCol RETURN 1)
  RETURN {{ doc, connectionCount: count }}
```

### Type discriminator filtering
When entities share a collection with a type discriminator field:
```
FOR doc IN sharedCollection
  FILTER doc.typeField == "TypeValue"
  RETURN doc
```

## Critical Rules
1. Use EXACT collection names from the schema (case-sensitive)
2. Use EXACT field names from the schema (case-sensitive)
3. Do NOT use bind parameters (@@col or @param) — use literal names and values
4. Use LOWER() for case-insensitive string comparisons: FILTER LOWER(doc.name) == LOWER("value")
5. For CONTAINS matching: FILTER CONTAINS(LOWER(doc.field), LOWER("value"))
6. System properties: _key, _id, _rev, _from, _to (edges also have _from, _to)
7. _id format is "collectionName/key" (e.g., "Device/12345")
8. When filtering by a related entity's property, traverse to that entity first
9. Use DISTINCT to deduplicate: RETURN DISTINCT doc
10. AQL uses == for equality (not =), != for inequality
11. String concatenation: use CONCAT(), not +
12. When counting, use COLLECT WITH COUNT INTO or COLLECT AGGREGATE cnt = COUNT(1)
13. For "top N" queries: SORT field DESC LIMIT N
14. Wrap the query in ```aql``` code block
15. Return a single valid AQL query
"""


def _build_physical_schema_summary(bundle: MappingBundle) -> str:
    """Build a physical-schema description for direct NL→AQL generation.

    Unlike the conceptual-only summary used for NL→Cypher, this includes
    collection names, edge collection names, type fields, physical
    field names, and cardinality statistics so the LLM can generate
    efficient AQL directly.
    """
    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    stats = bundle.metadata.get("statistics", {})
    entity_stats = stats.get("entities", {})
    col_stats = stats.get("collections", {})
    rel_stats = stats.get("relationships", {})

    lines: list[str] = ["Database schema (ArangoDB collections and edges):"]

    flagged_entity_props: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(pm.get("entities"), dict):
        lines.append("\nDocument collections:")
        for label, ent in pm["entities"].items():
            if not isinstance(ent, dict):
                continue
            col = ent.get("collectionName", label)
            style = ent.get("style", "COLLECTION")
            props = ent.get("properties", {})
            if isinstance(props, dict):
                prop_entries = list(props.items())[:12]
                formatted: list[str] = []
                for pname, pmeta in prop_entries:
                    hint = _property_quality_hint(pmeta if isinstance(pmeta, dict) else None)
                    formatted.append(f"{pname}{hint}")
                    if isinstance(pmeta, dict) and (
                        pmeta.get("sentinelValues") or pmeta.get("numericLike")
                    ):
                        flagged_entity_props.append((label, pname, pmeta))
                prop_str = ", ".join(formatted) if formatted else "no properties"
            else:
                prop_str = "no properties"

            type_info = ""
            if style in ("LABEL", "GENERIC_WITH_TYPE") and ent.get("typeField"):
                type_info = f" [type discriminator: {ent['typeField']}={ent.get('typeValue', label)}]"

            count_info = ""
            est = entity_stats.get(label, {})
            if isinstance(est, dict) and "estimated_count" in est:
                count_info = f" — ~{est['estimated_count']:,} documents"

            lines.append(f"  Collection '{col}' (entity: {label}){type_info}{count_info}")
            lines.append(f"    Fields: {prop_str}")

    if isinstance(pm.get("relationships"), dict):
        lines.append("\nEdge collections:")
        for rtype, rel in pm["relationships"].items():
            if not isinstance(rel, dict):
                continue
            edge_col = rel.get("edgeCollectionName", rtype)
            style = rel.get("style", "DEDICATED_COLLECTION")
            domain = rel.get("domain", "?")
            range_ = rel.get("range", "?")
            props = rel.get("properties", {})
            prop_names = list(props.keys())[:8] if isinstance(props, dict) else []
            prop_str = ", ".join(prop_names) if prop_names else "no properties"

            type_info = ""
            if style == "GENERIC_WITH_TYPE" and rel.get("typeField"):
                type_info = f" [type discriminator: {rel['typeField']}={rel.get('typeValue', rtype)}]"

            domain_col = _resolve_collection_name(domain, pm) or domain
            range_col = _resolve_collection_name(range_, pm) or range_

            rs = rel_stats.get(rtype, {})
            cardinality_info = ""
            if isinstance(rs, dict) and rs.get("edge_count"):
                parts = [f"~{rs['edge_count']:,} edges"]
                if rs.get("avg_out_degree"):
                    parts.append(f"avg fan-out: {rs['avg_out_degree']}/{domain}")
                if rs.get("avg_in_degree"):
                    parts.append(f"avg fan-in: {rs['avg_in_degree']}/{range_}")
                if rs.get("cardinality_pattern"):
                    parts.append(f"pattern: {rs['cardinality_pattern']}")
                cardinality_info = "\n    Cardinality: " + ", ".join(parts)

            lines.append(
                f"  Edge collection '{edge_col}' (relationship: {rtype}){type_info}"
            )
            lines.append(f"    Connects: {domain}('{domain_col}') -> {range_}('{range_col}')")
            if prop_str != "no properties":
                lines.append(f"    Fields: {prop_str}")
            if cardinality_info:
                lines.append(cardinality_info)

    cs_rels = cs.get("relationships", [])
    if isinstance(cs_rels, list) and cs_rels:
        lines.append("\nGraph topology (for traversal queries):")
        for r in cs_rels:
            if not isinstance(r, dict):
                continue
            rtype = r.get("type", "")
            from_e = r.get("fromEntity", "?")
            to_e = r.get("toEntity", "?")
            edge_col = ""
            if isinstance(pm.get("relationships"), dict):
                pm_rel = pm["relationships"].get(rtype, {})
                edge_col = pm_rel.get("edgeCollectionName", "") if isinstance(pm_rel, dict) else ""
            if edge_col:
                lines.append(f"  {from_e} --[{edge_col}]--> {to_e}")

    if entity_stats or rel_stats:
        lines.append("\nQuery optimization hints:")
        lines.append("  - Start traversals from the SMALLER collection when filtering by a property.")
        lines.append("  - For 1:N relationships, traverse OUTBOUND from the '1' side to avoid scanning the 'N' side.")
        lines.append("  - For N:1 relationships, traverse INBOUND from the '1' side.")
        lines.append("  - Use LIMIT early when only a few results are needed from large collections.")

    if flagged_entity_props:
        lines.append("\nData-quality hints:")
        lines.append(
            "  - Fields marked 'sentinels: ...' store a string placeholder for "
            "missing values (e.g. literal 'NULL'). These are NOT real nulls; "
            "exclude them in FILTER, e.g. "
            "`FILTER t.COMPANY_SIZE != 'NULL' AND t.COMPANY_SIZE != null`."
        )
        lines.append(
            "  - Fields marked 'numeric-like string' hold numbers stored as "
            "strings. Cast before numeric comparison or ordering, e.g. "
            "`SORT TO_NUMBER(t.COMPANY_SIZE) DESC`."
        )
        lines.append(
            "  - For 'top-N by numeric X', combine both: filter sentinels, "
            "then SORT TO_NUMBER(...) DESC LIMIT N."
        )

    return "\n".join(lines)


def _resolve_collection_name(entity_label: str, pm: dict[str, Any]) -> str | None:
    """Resolve an entity label to its physical collection name."""
    if isinstance(pm.get("entities"), dict):
        ent = pm["entities"].get(entity_label, {})
        if isinstance(ent, dict):
            return ent.get("collectionName", entity_label)
    return None


def _extract_aql_from_response(text: str) -> tuple[str, dict[str, Any]]:
    """Extract AQL query from LLM response (handles code blocks).

    Returns (aql, bind_vars). Bind vars are currently empty since we
    ask the LLM to use literals.
    """
    m = re.search(r"```(?:aql)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip(), {}
    # Fallback: look for lines that look like AQL
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    aql_lines = []
    for line in lines:
        upper = line.upper()
        if any(kw in upper for kw in ("FOR", "RETURN", "FILTER", "LET", "SORT", "LIMIT", "COLLECT", "INSERT", "UPDATE", "REMOVE", "WITH")):
            aql_lines.append(line)
        elif aql_lines:
            aql_lines.append(line)
    return "\n".join(aql_lines) if aql_lines else text.strip(), {}


def _validate_aql_syntax(
    aql: str, *, known_collections: set[str] | None = None,
) -> tuple[bool, str]:
    """Structural AQL syntax check.

    Returns ``(ok, error_message)``.  Checks performed:
    1. At least one top-level AQL clause keyword present.
    2. Balanced parentheses, brackets, and braces.
    3. Every ``FOR`` is followed by a matching ``RETURN``, ``INSERT``,
       ``UPDATE``, or ``REMOVE``.
    4. Collection names referenced via ``FOR … IN <collection>`` or
       ``INTO <collection>`` match *known_collections* when provided.
    """
    if not aql or not aql.strip():
        return False, "empty AQL string"

    upper = aql.upper()
    has_clause = any(
        kw in upper for kw in ("FOR", "RETURN", "INSERT", "UPDATE", "REMOVE", "LET")
    )
    if not has_clause:
        return False, "no recognizable AQL clause keyword found"

    for open_ch, close_ch, name in [("(", ")", "parentheses"), ("[", "]", "brackets"), ("{", "}", "braces")]:
        depth = 0
        for ch in aql:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
            if depth < 0:
                return False, f"unbalanced {name}: unexpected closing '{close_ch}'"
        if depth != 0:
            return False, f"unbalanced {name}: {depth} unclosed '{open_ch}'"

    for_count = len(re.findall(r"\bFOR\b", upper))
    terminal_count = len(re.findall(r"\b(?:RETURN|INSERT|UPDATE|REMOVE)\b", upper))
    if for_count > 0 and terminal_count == 0:
        return False, "FOR clause without a corresponding RETURN/INSERT/UPDATE/REMOVE"

    if known_collections:
        mentioned = set()
        for m in re.finditer(r"\bFOR\s+\w+\s+IN\s+(\w+)", aql):
            mentioned.add(m.group(1))
        for m in re.finditer(r"\bINTO\s+(\w+)", aql):
            mentioned.add(m.group(1))
        built_in = {"OUTBOUND", "INBOUND", "ANY", "GRAPH"}
        bad = mentioned - known_collections - built_in
        if bad:
            return False, f"unknown collection(s): {', '.join(sorted(bad))}"

    return True, ""


def _call_llm_for_aql(
    question: str,
    schema_summary: str,
    provider: LLMProvider,
    max_retries: int = 2,
    known_collections: set[str] | None = None,
) -> NL2AqlResult | None:
    """Call the LLM to generate AQL directly, with validation and retry."""
    last_error = ""
    total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for attempt in range(1 + max_retries):
        try:
            prompt_question = question
            if attempt > 0 and last_error:
                prompt_question = (
                    f"{question}\n\n"
                    f"(Previous attempt produced invalid AQL: {last_error}. "
                    f"Please fix and try again.)"
                )

            result = provider.generate(prompt_question, schema_summary)
            if isinstance(result, tuple):
                content, usage = result
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
            else:
                content = result

            aql, bind_vars = _extract_aql_from_response(content)

            ok, err_msg = _validate_aql_syntax(
                aql, known_collections=known_collections,
            )
            if ok:
                return NL2AqlResult(
                    aql=aql,
                    bind_vars=bind_vars,
                    explanation=content,
                    confidence=0.8,
                    method="llm_direct",
                    schema_context=schema_summary,
                    prompt_tokens=total_usage["prompt_tokens"],
                    completion_tokens=total_usage["completion_tokens"],
                    total_tokens=total_usage["total_tokens"],
                )

            last_error = err_msg or "generated text did not parse as AQL"
            logger.info(
                "LLM AQL attempt %d/%d: validation failed (%s) for: %s",
                attempt + 1, 1 + max_retries, last_error, aql[:120],
            )
        except Exception as e:
            logger.warning("LLM AQL call failed (attempt %d): %s", attempt + 1, e)
            last_error = str(e)

    return None


class _AqlChatProvider:
    """Wraps a ``_BaseChatProvider`` to use the AQL system prompt."""

    def __init__(self, base: _BaseChatProvider) -> None:
        self._base = base

    def generate(self, question: str, schema_summary: str) -> tuple[str, dict[str, int]]:
        system = _AQL_SYSTEM_PROMPT.format(schema=schema_summary)
        return self._base._chat(system, question)


def _collect_known_collections(bundle: MappingBundle) -> set[str]:
    """Extract all physical collection names from the mapping for AQL validation."""
    pm = bundle.physical_mapping
    cols: set[str] = set()
    if isinstance(pm.get("entities"), dict):
        for ent in pm["entities"].values():
            if isinstance(ent, dict) and ent.get("collectionName"):
                cols.add(ent["collectionName"])
    if isinstance(pm.get("relationships"), dict):
        for rel in pm["relationships"].values():
            if isinstance(rel, dict) and rel.get("edgeCollectionName"):
                cols.add(rel["edgeCollectionName"])
    return cols


def nl_to_aql(
    question: str,
    *,
    mapping: MappingBundle | dict[str, Any] | None = None,
    llm_provider: LLMProvider | None = None,
    max_retries: int = 2,
) -> NL2AqlResult:
    """Translate a natural language question directly to AQL.

    Unlike :func:`nl_to_cypher`, this bypasses the intermediate Cypher
    representation and generates AQL directly by providing the LLM with
    the full physical schema (collection names, edge collections, field
    names, type discriminators).

    Requires an LLM — there is no rule-based fallback for direct AQL
    generation.

    Args:
        question: Plain English question about the graph.
        mapping: Schema mapping (MappingBundle or export dict).
        llm_provider: A custom LLM provider. If None, uses OpenAI
            provider from environment variables.
        max_retries: Number of retry attempts if LLM output fails validation.
    """
    if mapping is None:
        return NL2AqlResult(
            aql="",
            bind_vars={},
            explanation="No schema mapping provided. Cannot generate AQL without knowing the database structure.",
            confidence=0.0,
        )

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    schema_summary = _build_physical_schema_summary(bundle)

    base_provider = llm_provider or _get_default_provider()
    if base_provider is None:
        return NL2AqlResult(
            aql="",
            bind_vars={},
            explanation="No LLM provider configured. Direct NL→AQL requires an LLM. Set OPENAI_API_KEY in .env.",
            confidence=0.0,
            method="none",
        )

    aql_provider = (
        _AqlChatProvider(base_provider) if isinstance(base_provider, _BaseChatProvider) else base_provider
    )
    known_collections = _collect_known_collections(bundle)
    result = _call_llm_for_aql(
        question, schema_summary, aql_provider,
        max_retries=max_retries,
        known_collections=known_collections,
    )
    if result and result.aql:
        return result

    return NL2AqlResult(
        aql="",
        bind_vars={},
        explanation="LLM could not generate valid AQL. Try rephrasing the question.",
        confidence=0.0,
        method="llm_direct",
    )


def nl_to_cypher(
    question: str,
    *,
    mapping: MappingBundle | dict[str, Any] | None = None,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
    max_retries: int = 2,
) -> NL2CypherResult:
    """Translate a natural language question to Cypher.

    Args:
        question: Plain English question about the graph.
        mapping: Schema mapping (MappingBundle or export dict).
        use_llm: If True, attempt LLM translation first.
        llm_provider: A custom LLM provider (implements ``LLMProvider``).
            If None and ``use_llm`` is True, falls back to an OpenAI-compatible
            provider configured via ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
            ``OPENAI_MODEL`` environment variables.
        max_retries: Number of retry attempts if LLM output fails validation.
    """
    if mapping is None:
        return NL2CypherResult(
            cypher="",
            explanation="No schema mapping provided. Cannot generate Cypher without knowing the graph structure.",
            confidence=0.0,
        )

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    schema_summary = _build_schema_summary(bundle)
    ctx = _build_schema_context(bundle)

    if use_llm:
        provider = llm_provider or _get_default_provider()
        if provider is not None:
            result = _call_llm_with_retry(
                question, schema_summary, provider, max_retries=max_retries,
                ctx=ctx,
            )
            if result and result.cypher:
                return result

    return _rule_based_translate(question, bundle)


# ---------------------------------------------------------------------------
# Representative NL query suggestions (used to seed the UI "Ask" history)
# ---------------------------------------------------------------------------


_SUGGEST_SYSTEM_PROMPT = """You generate short, natural-language example questions that a user might ask about a property graph.

The user has just connected to a database with the following schema:

{schema}

Rules:
- Produce between 6 and 10 distinct questions.
- Each question must be answerable against this schema (use only labels, relationship types, and properties shown above).
- Mix question shapes: simple lookups, one-hop traversals, two-hop traversals, filters on properties, aggregations (counts, averages), ordering / top-k, and at least one question that uses a property filter.
- Keep each question under ~120 characters, phrased the way a human would type it (no SQL, no Cypher, no code fences).
- Do NOT prefix questions with numbers, bullets, or labels.
- Output ONLY the questions, one per line, nothing else.
"""


def _llm_suggest_nl_queries(
    bundle: MappingBundle,
    provider: _BaseChatProvider,
    count: int = 8,
) -> list[str]:
    """Ask the LLM to propose representative NL queries for the schema."""
    schema_summary = _build_schema_summary(bundle)
    system = _SUGGEST_SYSTEM_PROMPT.format(schema=schema_summary)
    user = (
        f"Generate {count} example natural-language questions for this graph. "
        "Return only the questions, one per line."
    )
    try:
        content, _usage = provider._chat(system, user)
    except Exception as exc:  # network / auth errors fall through to rule-based
        logger.info("LLM suggest_nl_queries failed: %s", exc)
        return []

    lines: list[str] = []
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s)
        s = s.strip().strip('"').strip("'")
        if len(s) < 4:
            continue
        if s.lower().startswith(("match ", "with ", "return ", "for ", "//", "#")):
            continue
        lines.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for q in lines:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out[:count]


def _rule_based_suggest_nl_queries(
    bundle: MappingBundle, count: int = 8,
) -> list[str]:
    """Generate representative NL questions from the schema without an LLM."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: list[dict[str, Any]] = []
    cs_ents = cs.get("entities", [])
    if isinstance(cs_ents, list) and cs_ents and isinstance(cs_ents[0], dict):
        for e in cs_ents:
            name = e.get("name") or ""
            props = [
                p.get("name", "") for p in e.get("properties", [])
                if isinstance(p, dict) and p.get("name")
            ]
            if name:
                entities.append({"name": name, "properties": props[:8]})
    if not entities and isinstance(pm.get("entities"), dict):
        for name, spec in pm["entities"].items():
            props = list((spec or {}).get("properties", {}).keys())[:8]
            entities.append({"name": name, "properties": props})

    rels: list[dict[str, Any]] = []
    cs_rels = cs.get("relationships", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rels.append({
                "type": r.get("type", ""),
                "from": r.get("fromEntity", ""),
                "to": r.get("toEntity", ""),
            })
    if not rels and isinstance(pm.get("relationships"), dict):
        for rtype, spec in pm["relationships"].items():
            spec = spec or {}
            rels.append({
                "type": rtype,
                "from": spec.get("domain") or "",
                "to": spec.get("range") or "",
            })

    def _humanize(token: str) -> str:
        if not token:
            return token
        stripped = token.replace("_", "").replace("-", "")
        if stripped.isupper():
            s = token.replace("_", " ").replace("-", " ")
        else:
            s = re.sub(r"(?<!^)(?=[A-Z])", " ", token)
            s = s.replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", s).strip().lower()

    def _plural(word: str) -> str:
        if not word:
            return word
        if word.endswith("s"):
            return word
        if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
            return word[:-1] + "ies"
        return word + "s"

    def _verbalize_rel(rtype: str) -> str:
        return _humanize(rtype).replace(" ", " ").lower()

    suggestions: list[str] = []

    for e in entities[:4]:
        label = _plural(_humanize(e["name"]))
        suggestions.append(f"Show 10 {label}")
        suggestions.append(f"How many {label} are there?")
        if e["properties"]:
            prop = _humanize(e["properties"][0])
            suggestions.append(f"List {label} ordered by {prop}")

    for r in rels[:4]:
        if not (r["type"] and r["from"] and r["to"]):
            continue
        verb = _verbalize_rel(r["type"])
        src = _humanize(r["from"])
        dst_plural = _plural(_humanize(r["to"]))
        suggestions.append(
            f"For each {src}, show the {dst_plural} they {verb}"
        )
        suggestions.append(
            f"Count {dst_plural} per {src}"
        )

    if len(rels) >= 2:
        r1, r2 = rels[0], rels[1]
        if r1.get("to") and r2.get("from") and r1["to"] == r2["from"]:
            suggestions.append(
                f"Find {_plural(_humanize(r2['to']))} connected to {_plural(_humanize(r1['from']))} "
                f"through {_humanize(r1['to'])}"
            )

    seen: set[str] = set()
    out: list[str] = []
    for q in suggestions:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= count:
            break
    return out


def suggest_nl_queries(
    mapping: MappingBundle | dict[str, Any] | None,
    *,
    count: int = 8,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
) -> list[str]:
    """Return a representative set of natural-language questions for the schema.

    Used to seed the UI "Ask" history after the user connects to a database
    and schema introspection completes. Falls back to rule-based generation
    when no LLM provider is configured or the LLM call fails.
    """
    if mapping is None:
        return []

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    if use_llm:
        provider = llm_provider or _get_default_provider()
        if provider is not None:
            llm_out = _llm_suggest_nl_queries(bundle, provider, count=count)
            if llm_out:
                return llm_out

    return _rule_based_suggest_nl_queries(bundle, count=count)
