"""Build a :class:`PredicateIndex` from a Cypher :class:`MappingBundle`.

Step 1 of porting ``arango-sparql-py``'s query-first synthetic few-shot bank
(Phase 07.5) to the Cypher front end.  The shared engine already carries the
language-agnostic half of that machinery — :mod:`arango_query_core.nl.synthbank`
promotes ``ShapeTemplate`` plus nine ``applies`` gates precisely so "a Cypher
front-end reuses them directly and supplies its own ``build_*`` renderers".

Those gates read :class:`~arango_query_core.nl.grounding.GroundedPredicate`, a
TBox notion the Cypher side has never produced: an ontology's
``rdfs:domain``/``rdfs:range`` declarations have no direct analogue here.  The
conceptual schema in a ``MappingBundle`` *is* that analogue — entities with
properties, relationships with ``fromEntity``/``toEntity`` — so this module
translates one into the other and unblocks the rest of the port.

**Shape derivation is a faithful port**, not a reinvention.  The SPARQL side
derives ``shape`` mechanically from declarations alone (never hand-curated per
schema); the same four rules restated over a conceptual schema, where the
"children" of a class are its properties (datatype children) and the
relationships leaving it (object children):

===================================  ==========================================
SPARQL rule                          Cypher equivalent
===================================  ==========================================
``kind == "datatype"``               a node property                    -> ``literal``
range class's children non-empty     range entity has >=1 property and
and *all* datatype                   *no* outgoing relationship         -> ``value_object``
range class has zero children        range entity has no properties and
(incl. undeclared range)             no outgoing relationships, or is
                                     undeclared                         -> ``category_instance``
otherwise (>=1 non-datatype child)   range entity has >=1 outgoing
                                     relationship                       -> ``linked_entity``
===================================  ==========================================

That last rule is the false-positive guard the SPARQL docstring calls out: a
naive "range has >=1 property" test would mislabel a richly-connected entity as
a ``value_object``.

**This module deliberately does not wire seam 7.**
:meth:`CypherAdapter.predicate_index` still returns ``None``.  Returning a real
index would make the engine inject a predicate prompt block on every NL call —
a prompt change that must be justified by an eval run
(``RUN_NL2CYPHER_EVAL=1``), not by the mere existence of an index.  The index
built here is a *build-time* input for bank generation, which is where the
measured CK25 gain came from.

Pure and offline by construction: a function of the ``MappingBundle`` alone, no
database and no LLM, so it is unit-testable and cheap to call.

See ``docs/cypher_tck_conformance_plan.md`` for the surrounding programme and
``arango_query_core.nl.synthbank`` for the promoted catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.grounding import GroundedPredicate, PredicateIndex

__all__ = [
    "CypherPredicateSignals",
    "build_predicate_index",
    "build_predicate_signals",
    "predicate_iri_for_property",
    "predicate_iri_for_relationship",
]

#: Property types whose values admit ``ORDER BY`` — the gate for the ``top_n``
#: and ``offset`` shapes.  Mirrors the SPARQL side's ordered-XSD set, widened to
#: the type spellings a schema analyzer emits.
_ORDERABLE_TYPES = frozenset(
    {
        "int",
        "integer",
        "long",
        "float",
        "double",
        "decimal",
        "number",
        "numeric",
        "date",
        "datetime",
        "timestamp",
        "time",
    }
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def predicate_iri_for_relationship(rel_type: str) -> str:
    """Opaque, stable id for a relationship predicate."""
    return f"rel:{rel_type}"


def predicate_iri_for_property(entity: str, field: str) -> str:
    """Opaque, stable id for a node-property predicate."""
    return f"prop:{entity}.{field}"


@dataclass(frozen=True)
class CypherPredicateSignals:
    """Cypher sibling of the SPARQL eval harness's ``PredicateSignals``.

    ``orderable`` is derivable offline from the declared property type.
    ``optional_relation`` needs instance data — whether a domain entity has
    both instances carrying the relationship and instances lacking it — so it
    is ``False`` whenever no statistics are supplied, exactly as the SPARQL
    contract specifies ("always False when no instance data is available —
    never a crash").
    """

    iri: str
    orderable: bool
    optional_relation: bool


def _humanize(name: str) -> str:
    """``ACTED_IN`` / ``actedIn`` / ``acted_in`` -> ``acted in``.

    Retrieval scores predicates by substring overlap between the question and
    ``(label, domain, range)``.  A raw ``ACTED_IN`` never overlaps "who acted
    in ...", so the schema's own spelling is kept as the id and a humanized
    form becomes the label.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", name.replace("_", " ").replace("-", " "))
    return " ".join(spaced.split()).lower()


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _entity_name(entity: Any) -> str:
    if not isinstance(entity, dict):
        return ""
    name = entity.get("name")
    if isinstance(name, str) and name:
        return name
    labels = _as_list(entity.get("labels"))
    return labels[0] if labels and isinstance(labels[0], str) else ""


def _property_entries(entity: Any) -> list[tuple[str, str]]:
    """``[(field, type)]`` for one entity; type defaults to ``string``."""
    out: list[tuple[str, str]] = []
    if not isinstance(entity, dict):
        return out
    for prop in _as_list(entity.get("properties")):
        if isinstance(prop, dict):
            field = prop.get("name") or prop.get("field")
            if isinstance(field, str) and field:
                ptype = prop.get("type")
                out.append((field, ptype if isinstance(ptype, str) and ptype else "string"))
        elif isinstance(prop, str) and prop:
            out.append((prop, "string"))
    return out


def _conceptual(mapping: MappingBundle | None) -> tuple[list[Any], list[Any]]:
    cs = getattr(mapping, "conceptual_schema", None)
    if not isinstance(cs, dict):
        return [], []
    return _as_list(cs.get("entities")), _as_list(cs.get("relationships"))


def _derive_shape(
    range_entity: str,
    props_by_entity: dict[str, list[tuple[str, str]]],
    outgoing_by_entity: dict[str, int],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Port of the SPARQL four-rule derivation; see the module docstring."""
    children = props_by_entity.get(range_entity, [])
    has_outgoing = outgoing_by_entity.get(range_entity, 0) > 0

    if has_outgoing:
        # >=1 non-datatype child — the false-positive guard.
        return "linked_entity", ()
    if children:
        # Children present and all of them datatype: an attribute-bearing leaf.
        return "value_object", tuple((_humanize(f), t) for f, t in children)
    # Zero children, or an undeclared range entity, degrades here rather than
    # raising — mirrors the SPARQL branch that QALD's DBpedia subset exercises.
    return "category_instance", ()


def build_predicate_index(mapping: MappingBundle | None) -> PredicateIndex:
    """Translate a mapping's conceptual schema into a :class:`PredicateIndex`.

    Emits one ``object`` predicate per relationship type and one ``datatype``
    predicate per node property.  Returns an empty index — never ``None`` and
    never raising — when the bundle carries no usable conceptual schema, so
    callers need no special-casing for heuristic or degraded mappings.
    """
    entities, relationships = _conceptual(mapping)

    props_by_entity: dict[str, list[tuple[str, str]]] = {}
    for entity in entities:
        name = _entity_name(entity)
        if name:
            props_by_entity[name] = _property_entries(entity)

    outgoing_by_entity: dict[str, int] = {}
    for rel in relationships:
        if isinstance(rel, dict):
            src = rel.get("fromEntity")
            if isinstance(src, str) and src:
                outgoing_by_entity[src] = outgoing_by_entity.get(src, 0) + 1

    predicates: list[GroundedPredicate] = []

    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        rel_type = rel.get("type")
        src = rel.get("fromEntity")
        dst = rel.get("toEntity")
        if not (isinstance(rel_type, str) and rel_type and isinstance(src, str) and src):
            continue
        dst = dst if isinstance(dst, str) else ""
        shape, detail = _derive_shape(dst, props_by_entity, outgoing_by_entity)
        predicates.append(
            GroundedPredicate(
                iri=predicate_iri_for_relationship(rel_type),
                label=_humanize(rel_type),
                kind="object",
                domain=src,
                range=dst,
                shape=shape,
                shape_detail=detail,
            )
        )

    for entity_name, fields in props_by_entity.items():
        for field, ptype in fields:
            predicates.append(
                GroundedPredicate(
                    iri=predicate_iri_for_property(entity_name, field),
                    label=_humanize(field),
                    kind="datatype",
                    domain=entity_name,
                    range=ptype,
                    shape="literal",
                )
            )

    return PredicateIndex(predicates)


def build_predicate_signals(
    mapping: MappingBundle | None,
    *,
    optional_relations: frozenset[str] | None = None,
) -> dict[str, CypherPredicateSignals]:
    """Signals keyed by predicate IRI, for the synthbank ``applies`` gates.

    ``optional_relations`` lets a caller that *has* instance statistics mark
    relationship IRIs whose domain entity has both instances carrying and
    instances lacking the relationship.  Omitted, every ``optional_relation``
    is ``False`` — the documented no-instance-data default.
    """
    optional = optional_relations or frozenset()
    signals: dict[str, CypherPredicateSignals] = {}

    for predicate in build_predicate_index(mapping).retrieve("", k=10_000, dump=True):
        orderable = predicate.kind == "datatype" and predicate.range.lower() in _ORDERABLE_TYPES
        signals[predicate.iri] = CypherPredicateSignals(
            iri=predicate.iri,
            orderable=orderable,
            optional_relation=predicate.iri in optional,
        )
    return signals
