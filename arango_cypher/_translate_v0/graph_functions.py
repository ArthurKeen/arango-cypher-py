"""``keys()`` and ``properties()`` over stored documents.

A node or relationship is an ArangoDB document, which carries attributes that
are not Cypher properties: the system attributes (``_id``, ``_key``,
``_rev``, ``_from``, ``_to``) and the fields the mapping stores labels and
relationship types in (an LPG ``type`` field, a ``labels`` array). Cypher's
``keys(n)`` and ``properties(n)`` must not show them. A map argument is a
plain AQL object without ``_id`` and keeps every key.
"""

from __future__ import annotations

import re

from arango_query_core import MappingResolver

from .literals import _aql_string_literal

_SYSTEM_ATTRIBUTES = ("_id", "_key", "_rev", "_from", "_to")
_SIMPLE_REF = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def mapping_fields(resolver: MappingResolver | None) -> list[str]:
    """Document attributes the mapping uses to store labels and types."""
    fields: list[str] = []
    if resolver is not None:
        physical = resolver.bundle.physical_mapping or {}
        for section in ("entities", "relationships"):
            for spec in (physical.get(section) or {}).values():
                if not isinstance(spec, dict):
                    continue
                for key in ("typeField", "labelsField"):
                    value = spec.get(key)
                    if isinstance(value, str) and value:
                        fields.append(value)
    return list(dict.fromkeys(fields))


def _hidden(resolver: MappingResolver | None) -> str:
    names = [*_SYSTEM_ATTRIBUTES, *mapping_fields(resolver)]
    return "[" + ", ".join(_aql_string_literal(n) for n in dict.fromkeys(names)) + "]"


def _with_value(arg: str, body: str) -> str:
    """*body* (which reads ``_v``) over *arg*, evaluating *arg* once."""
    if _SIMPLE_REF.fullmatch(arg.strip()):
        return body.replace("_v", arg.strip())
    return f"FIRST(LET _v = {arg} RETURN {body})"


def compile_keys(arg: str, resolver: MappingResolver | None) -> str:
    """``keys(x)``: a document's property names, or a map's keys."""
    body = (
        f"(IS_OBJECT(_v) AND HAS(_v, '_id') ? ATTRIBUTES(_v)[* FILTER CURRENT NOT IN {_hidden(resolver)}] "
        ": ATTRIBUTES(_v))"
    )
    return _with_value(arg, body)


def compile_properties(arg: str, resolver: MappingResolver | None) -> str:
    """``properties(x)``: a document's properties as a map, or the map itself."""
    body = f"(IS_OBJECT(_v) AND HAS(_v, '_id') ? UNSET(_v, {_hidden(resolver)}) : _v)"
    return _with_value(arg, body)


def compile_labels(arg: str, resolver: MappingResolver | None, type_field: str) -> str | None:
    """``labels(n)`` for a mapping whose ``LABEL`` entities name a ``labelsField``.

    A multi-label node lists its labels in that array; another node of a
    type-discriminated collection has the one label of its type field, or none
    when the field is absent; a node of any other collection is labelled by
    its collection. ``None`` when no entity names a labels field (the caller
    keeps the type-field form).
    """
    if resolver is None:
        return None
    entities = (resolver.bundle.physical_mapping or {}).get("entities") or {}
    specs = [e for e in entities.values() if isinstance(e, dict) and e.get("style") == "LABEL"]
    fields = {e["labelsField"] for e in specs if isinstance(e.get("labelsField"), str) and e["labelsField"]}
    if len(fields) != 1:
        return None
    lf = next(iter(fields))
    collections = sorted({e["collectionName"] for e in specs if isinstance(e.get("collectionName"), str)})
    typed = "[" + ", ".join(_aql_string_literal(c) for c in collections) + "]"
    lf_ref = f"_v[{_aql_string_literal(lf)}]"
    tf_ref = f"_v[{_aql_string_literal(type_field)}]"
    body = (
        f"(_v == null ? null : IS_ARRAY({lf_ref}) ? {lf_ref} : {tf_ref} != null ? [{tf_ref}] "
        f": PARSE_IDENTIFIER(_v._id).collection IN {typed} ? [] : [PARSE_IDENTIFIER(_v._id).collection])"
    )
    return _with_value(arg, body)
