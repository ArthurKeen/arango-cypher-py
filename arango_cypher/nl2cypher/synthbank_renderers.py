"""Cypher renderers for the promoted synthbank shape catalog.

Step 2 of porting ``arango-sparql-py``'s query-first synthetic few-shot bank.
:mod:`arango_query_core.nl.synthbank` owns the nine shapes and their ``applies``
gates; its ``build_sparql`` closures are "the SPARQL-specific half", and this
module is the Cypher half — one renderer per shape, same shapes, same slots.

Binding vocabulary
------------------
The SPARQL renderers read IRIs. Cypher has labels, relationship types and
properties instead, so the binding keys are renamed rather than reused, one for
one:

======================  ==========================================
SPARQL binding key      Cypher binding key
======================  ==========================================
``predicate_iri``       ``predicate_name``  (rel type or property)
``hop_predicate_iri``   ``hop_predicate_name``
``domain_iri``          ``domain_label``
``range_iri``           ``range_label``
``filler_label``        ``filler_value`` + ``anchor_property``
``threshold``           ``threshold``
======================  ==========================================

**The name anchor is the one real asymmetry.** SPARQL anchors every entity slot
on ``rdfs:label`` — well-known and dataset-independent, which is what makes the
generator schema-agnostic. Cypher has no universal display property, so the
anchor property is *per entity* and must be supplied by the data binder in
``anchor_property`` (``PropertyInfo.role`` is the intended source). This module
never guesses it.

Deliberate deviation: type constraints
--------------------------------------
The SPARQL renderers omit the domain type constraint because TBox domains are
often abstract superclasses with zero direct instances. Cypher labels are
concrete — a node either carries the label or does not — so the deviation does
not apply and the label is included, which is both more precise and more
idiomatic. Recorded here so the divergence is a decision, not a drift.

Output style follows the shipped seed corpora
(``arango_cypher/nl2cypher/corpora/*.yml``): inline literals rather than bind
parameters, property projection, and a deterministic ``ORDER BY``. These are
gold *examples* shown to a model, not runtime queries — runtime user input
still travels as bind parameters, per the repo's safety rule.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["CYPHER_RENDERERS", "UnsupportedShapeError", "render_cypher"]


class UnsupportedShapeError(KeyError):
    """Raised when a shape name has no Cypher renderer.

    Explicit rather than a silent skip: a new shape added upstream must be
    ported deliberately, not quietly dropped from every generated bank.
    """


def _lit(value: Any) -> str:
    """Render *value* as a Cypher literal, quoting and escaping strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _label(binding: Mapping[str, Any], key: str) -> str:
    """``:Label`` suffix for a node pattern, or ``""`` when undeclared.

    An undeclared range degrades to an unlabelled node rather than emitting
    ``(c:)``, mirroring the ``category_instance`` shape's tolerance of
    undeclared ranges on the index side.
    """
    value = binding.get(key)
    return f":{value}" if isinstance(value, str) and value else ""


def _anchor(binding: Mapping[str, Any]) -> str:
    """``{prop: "value"}`` inline anchor, or ``""`` when no anchor is bound."""
    prop = binding.get("anchor_property")
    if not (isinstance(prop, str) and prop) or "filler_value" not in binding:
        return ""
    return f" {{{prop}: {_lit(binding['filler_value'])}}}"


def _render_lookup(b: Mapping[str, Any]) -> str:
    """``What is the {predicate} of {entity}?`` — a property off a named node."""
    return f"MATCH (x{_label(b, 'domain_label')}{_anchor(b)})\nRETURN x.{b['predicate_name']} AS result"


def _render_value_object(b: Mapping[str, Any]) -> str:
    """The extra-hop pattern: named node -> value node -> its property."""
    return (
        f"MATCH (x{_label(b, 'domain_label')}{_anchor(b)})"
        f"-[:{b['predicate_name']}]->(mid{_label(b, 'range_label')})\n"
        f"RETURN mid.{b['hop_predicate_name']} AS result"
    )


def _render_category_filter(b: Mapping[str, Any]) -> str:
    """Anchor the range side by name; project the domain-side members."""
    return (
        f"MATCH (result{_label(b, 'domain_label')})"
        f"-[:{b['predicate_name']}]->(c{_label(b, 'range_label')}{_anchor(b)})\n"
        "RETURN DISTINCT result"
    )


def _render_scalar_count(b: Mapping[str, Any]) -> str:
    """Same pool as category_filter, counted instead of listed."""
    return (
        f"MATCH (member{_label(b, 'domain_label')})"
        f"-[:{b['predicate_name']}]->(c{_label(b, 'range_label')}{_anchor(b)})\n"
        "RETURN count(DISTINCT member) AS result"
    )


def _render_grouped_aggregation(b: Mapping[str, Any]) -> str:
    """Cypher expresses SPARQL's ``HAVING`` as ``WITH ... WHERE``."""
    return (
        f"MATCH (x{_label(b, 'domain_label')})"
        f"-[:{b['predicate_name']}]->(result{_label(b, 'range_label')})\n"
        "WITH result, count(x) AS member_count\n"
        f"WHERE member_count > {_lit(b['threshold'])}\n"
        "RETURN result, member_count ORDER BY member_count DESC"
    )


def _render_top_n(b: Mapping[str, Any]) -> str:
    """Rank all instances of the domain by an orderable property."""
    prop = b["predicate_name"]
    direction = "ASC" if str(b.get("direction", "desc")).lower() == "asc" else "DESC"
    return (
        f"MATCH (result{_label(b, 'domain_label')})\n"
        f"WHERE result.{prop} IS NOT NULL\n"
        f"RETURN result, result.{prop} AS result_value\n"
        f"ORDER BY result.{prop} {direction}\n"
        "LIMIT 1"
    )


def _render_offset(b: Mapping[str, Any]) -> str:
    """``top_n`` shifted by one — the "second highest" shape."""
    prop = b["predicate_name"]
    direction = "ASC" if str(b.get("direction", "desc")).lower() == "asc" else "DESC"
    return (
        f"MATCH (result{_label(b, 'domain_label')})\n"
        f"WHERE result.{prop} IS NOT NULL\n"
        f"RETURN result, result.{prop} AS result_value\n"
        f"ORDER BY result.{prop} {direction}\n"
        "SKIP 1 LIMIT 1"
    )


def _render_negation(b: Mapping[str, Any]) -> str:
    """Domain instances lacking the relationship entirely."""
    return (
        f"MATCH (result{_label(b, 'domain_label')})\n"
        f"WHERE NOT EXISTS {{ (result)-[:{b['predicate_name']}]->({_label(b, 'range_label')}) }}\n"
        "RETURN DISTINCT result"
    )


def _render_two_hop(b: Mapping[str, Any]) -> str:
    """Range-anchored, then a second forward hop off the same domain node."""
    return (
        f"MATCH (c{_label(b, 'range_label')}{_anchor(b)})"
        f"<-[:{b['predicate_name']}]-(x{_label(b, 'domain_label')})"
        f"-[:{b['hop_predicate_name']}]->(result{_label(b, 'far_label')})\n"
        "RETURN DISTINCT result"
    )


#: Shape name -> Cypher renderer. Keys mirror ``SHAPE_CATALOG`` names exactly.
CYPHER_RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "lookup": _render_lookup,
    "value_object": _render_value_object,
    "category_filter": _render_category_filter,
    "scalar_count": _render_scalar_count,
    "grouped_aggregation": _render_grouped_aggregation,
    "top_n": _render_top_n,
    "offset": _render_offset,
    "negation": _render_negation,
    "two_hop": _render_two_hop,
}


def render_cypher(shape_name: str, binding: Mapping[str, Any]) -> str:
    """Render *binding* as gold Cypher for the shape called *shape_name*."""
    try:
        renderer = CYPHER_RENDERERS[shape_name]
    except KeyError as exc:
        raise UnsupportedShapeError(
            f"no Cypher renderer for shape {shape_name!r}; known shapes: {sorted(CYPHER_RENDERERS)}"
        ) from exc
    return renderer(binding)
