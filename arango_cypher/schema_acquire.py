"""Automatic mapping acquisition from a live ArangoDB database.

Provides three tiers of mapping acquisition:
1. Analyzer (primary): delegates to arangodb-schema-analyzer for full ontology
   extraction across PG, LPG, and hybrid schemas
2. Heuristic (fallback): fast classification + simple mapping construction when
   the analyzer is not installed
3. Auto (default): analyzer first, heuristic fallback on ImportError
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from arango_query_core import CoreError, MappingBundle, MappingSource

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arango.database import StandardDatabase

CACHE_TTL_SECONDS = 300

_mapping_cache: dict[str, tuple[MappingBundle, float, str]] = {}


def _cache_key(db: "StandardDatabase") -> str:
    """Stable cache key: database name only. Used as the dict key.

    The actual staleness check is done via ``_schema_fingerprint`` which
    includes collection names and counts.
    """
    try:
        return db.name
    except Exception:
        return ""


def _schema_fingerprint(db: "StandardDatabase") -> str:
    """Fast physical-schema fingerprint: names + counts + index count.

    This runs a single lightweight AQL query to capture the shape of the
    schema without sampling any documents.  If the fingerprint hasn't
    changed since the last introspection, we can safely return the
    cached mapping.
    """
    try:
        cols = db.collections()
    except Exception:
        return ""
    parts: list[str] = []
    for c in sorted(cols, key=lambda x: x.get("name", "") if isinstance(x, dict) else ""):
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        col_type = "edge" if c.get("type") in (3, "edge") else "doc"
        try:
            count = db.collection(name).count()
            idx_count = len(db.collection(name).indexes())
        except Exception:
            count = -1
            idx_count = -1
        parts.append(f"{name}:{col_type}:{count}:{idx_count}")
    raw = f"{db.name}|{'|'.join(parts)}"
    return hashlib.sha256(raw.encode()).hexdigest()


_IES_TO_Y_WORDS = {
    "companies", "cities", "categories", "stories", "bodies", "parties",
    "entries", "queries", "countries", "activities", "properties",
    "policies", "strategies", "histories", "industries", "libraries",
    "boundaries", "commodities", "entities", "identities", "priorities",
    "securities", "territories", "utilities", "vulnerabilities",
}

def _singularize(name: str) -> str:
    """Naive English singularization for collection-name-to-label conversion."""
    lower = name.lower()
    # "ies" → "y" only for known patterns (not "movies", "series", "species")
    if lower.endswith("ies") and len(name) > 4:
        if lower in _IES_TO_Y_WORDS:
            return name[:-3] + "y"
        # Heuristic: if the char before "ies" is a consonant pair or single consonant
        # and the result would be a short stem, prefer ies→y
        # Otherwise strip just the "s" to preserve the root (movies→movie)
        prefix = name[:-3]
        if len(prefix) >= 2 and prefix[-1].lower() not in "aeiou" and prefix[-2].lower() not in "aeiou":
            return prefix + "y"
        return name[:-1]
    if lower.endswith("ses") or lower.endswith("xes") or lower.endswith("zes") or lower.endswith("ches") or lower.endswith("shes"):
        return name[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and not lower.endswith("us"):
        return name[:-1]
    return name


def _pascal_case(name: str) -> str:
    parts = re.split(r"[_\-\s]+", name)
    return "".join(p.capitalize() for p in parts if p)


def _collection_label(collection_name: str) -> str:
    """Infer a conceptual label from a collection name (e.g., 'users' -> 'User').

    Preserves existing PascalCase/camelCase capitalization when there are
    no word separators (underscores, hyphens, spaces).  Only applies
    capitalize-each-part logic when separators are present.
    """
    singular = _singularize(collection_name)
    if re.search(r"[_\-\s]", singular):
        return _pascal_case(singular)
    # Already a single token — preserve internal caps (e.g. EdrThreat),
    # just ensure the first letter is upper.
    return singular[0].upper() + singular[1:] if singular else singular


def classify_schema(db: StandardDatabase) -> str:
    """Fast heuristic: sample collections and classify as 'pg', 'lpg', 'hybrid', or 'unknown'.

    Strategy:
    - List all document collections and edge collections
    - For document collections: sample N docs, check if they have a common 'type'/'labels' field
      - If all docs have a type field with varying values -> LPG
      - If collection names match conceptual types (no type field) -> PG
    - For edge collections: check if they're dedicated or have a type/relation field
    - If mixed -> hybrid
    - If unclear -> unknown
    """
    try:
        all_cols = db.collections()
    except Exception:
        return "unknown"

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    if not doc_cols:
        return "unknown"

    type_field_names = {"type", "_type", "label", "labels", "kind", "entityType"}
    sample_size = 20

    doc_signals: list[str] = []
    for col_name in doc_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars={"@col": col_name, "n": sample_size},
            )
            docs = list(cursor)
        except Exception:
            doc_signals.append("unknown")
            continue

        if not docs:
            doc_signals.append("unknown")
            continue

        found_type_field = None
        for tf in type_field_names:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            try:
                distinct_cursor = db.aql.execute(
                    f"FOR doc IN @@col COLLECT v = doc.`{found_type_field}` RETURN v",
                    bind_vars={"@col": col_name},
                )
                values = {str(v) for v in distinct_cursor if v is not None}
            except Exception:
                values = set()
            if len(values) > 1:
                doc_signals.append("lpg")
            else:
                doc_signals.append("pg")
        else:
            doc_signals.append("pg")

    edge_signals: list[str] = []
    edge_type_fields = {"type", "relation", "relType", "_type"}
    for col_name in edge_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars={"@col": col_name, "n": sample_size},
            )
            docs = list(cursor)
        except Exception:
            edge_signals.append("unknown")
            continue

        if not docs:
            edge_signals.append("pg")
            continue

        found_type_field = None
        for tf in edge_type_fields:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            edge_signals.append("lpg")
        else:
            edge_signals.append("pg")

    all_signals = doc_signals + edge_signals
    meaningful = [s for s in all_signals if s != "unknown"]
    if not meaningful:
        return "unknown"

    pg_count = meaningful.count("pg")
    lpg_count = meaningful.count("lpg")

    if lpg_count == 0:
        return "pg"
    if pg_count == 0:
        return "lpg"
    return "hybrid"


# ---------------------------------------------------------------------------
# Data-quality profiling (sentinel detection, numeric-like strings)
# ---------------------------------------------------------------------------

# Case-insensitive string values commonly used as "null" sentinels in dirty data.
_SENTINEL_TOKENS: set[str] = {
    "NULL", "NONE", "NIL", "N/A", "NA", "UNKNOWN",
    "TBD", "TBA", "#N/A", "(NULL)",
}

# A sentinel candidate must occupy at least this share of the sampled values
# to be reported. Prevents isolated "-" or "" values from spuriously flagging
# legitimate columns.
_SENTINEL_MIN_SHARE = 0.02

# Numeric-like detection: share of non-sentinel strings that parse as numbers.
_NUMERIC_LIKE_MIN_SHARE = 0.8

# How many distinct sample values to keep per property for LLM context.
_SAMPLE_VALUES_KEEP = 4
_SAMPLE_VALUE_MAXLEN = 48


def _is_sentinel_token(s: str) -> bool:
    """Return True if ``s`` is a well-known null-sentinel string."""
    return s.strip().upper() in _SENTINEL_TOKENS


def _is_numeric_like(s: str) -> bool:
    """Return True if ``s`` parses as a number (int or float)."""
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _infer_value_type(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int | float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return "string"


def _profile_property_values(
    values: list[Any], total_docs: int,
) -> dict[str, Any]:
    """Compute type / sentinel / numeric-like / sample metadata for one field.

    ``values`` is the list of raw values observed for this field across the
    sampled documents (same length as the number of docs where the field
    was present). ``total_docs`` is the total number of sampled docs
    (so ``required`` can be derived).
    """
    if not values:
        return {"field": "", "type": "string"}

    type_counts: dict[str, int] = {}
    for v in values:
        t = _infer_value_type(v)
        type_counts[t] = type_counts.get(t, 0) + 1

    dominant_type = max(type_counts, key=type_counts.get)  # type: ignore[arg-type]

    sentinel_counts: dict[str, int] = {}
    non_sentinel_strings: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            key = v.strip().upper()
            sentinel_counts[key] = sentinel_counts.get(key, 0) + 1
        else:
            non_sentinel_strings.append(v)

    min_count = max(1, int(_SENTINEL_MIN_SHARE * len(values)))
    sentinel_values = sorted(
        [k for k, n in sentinel_counts.items() if n >= min_count],
        key=lambda k: -sentinel_counts[k],
    )

    numeric_like = False
    if non_sentinel_strings:
        numeric_hits = sum(1 for s in non_sentinel_strings if _is_numeric_like(s))
        if numeric_hits / len(non_sentinel_strings) >= _NUMERIC_LIKE_MIN_SHARE:
            numeric_like = True

    sample_values: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            continue
        key = v[:_SAMPLE_VALUE_MAXLEN]
        if key in seen:
            continue
        seen.add(key)
        sample_values.append(key)
        if len(sample_values) >= _SAMPLE_VALUES_KEEP:
            break

    out: dict[str, Any] = {"type": dominant_type}
    if sentinel_values:
        out["sentinelValues"] = sentinel_values
    if numeric_like:
        out["numericLike"] = True
    if sample_values:
        out["sampleValues"] = sample_values
    if total_docs and len(values) == total_docs:
        out["required"] = True
    return out


def _sample_properties(
    db: StandardDatabase, collection_name: str, sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample docs and return enriched property profiles.

    Each entry contains the property ``name`` plus data-quality metadata:
    ``type``, ``sentinelValues`` (string sentinels like 'NULL'),
    ``numericLike`` (non-sentinel string values parse as numbers), and
    ``sampleValues`` (a few representative values for LLM context).
    """
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


_DOC_TYPE_FIELDS = ["type", "_type", "label", "labels", "kind", "entityType"]
_EDGE_TYPE_FIELDS = ["type", "relation", "relType", "_type", "label"]


def _detect_type_field(
    db: StandardDatabase,
    collection_name: str,
    candidates: list[str] | None = None,
) -> str | None:
    """Detect the type/label discriminator field in a collection, if any."""
    if candidates is None:
        candidates = _DOC_TYPE_FIELDS
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": 20},
        )
        docs = list(cursor)
    except Exception:
        return None

    if not docs:
        return None

    for tf in candidates:
        count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
        if count >= len(docs) * 0.8:
            return tf
    return None


def _type_field_values(db: StandardDatabase, collection_name: str, type_field: str) -> list[str]:
    """Get distinct values for a type field."""
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col COLLECT val = doc.`{type_field}` RETURN val",
            bind_vars={"@col": collection_name},
        )
        vals: list[str] = []
        for v in cursor:
            if v is None:
                continue
            if isinstance(v, list):
                vals.extend(str(x) for x in v)
            else:
                vals.append(str(v))
        return sorted(set(vals))
    except Exception:
        return []


def _sample_properties_filtered(
    db: StandardDatabase,
    collection_name: str,
    type_field: str,
    type_value: str,
    sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample documents matching a specific type value and return enriched
    property profiles (same shape as :func:`_sample_properties`).
    """
    skip_fields = {"_key", "_id", "_rev", "_from", "_to", type_field, "labels"}
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col FILTER doc.`{type_field}` == @val LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "val": type_value, "n": sample_size},
        )
        docs = list(cursor)
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key in skip_fields:
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


def _infer_lpg_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    type_field: str,
    type_value: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain and range entity labels for a specific LPG edge type.

    Samples edges matching the type_value, resolves the _from/_to documents,
    and looks up their type to find the correct conceptual entity label.
    """
    col_type_map: dict[str, tuple[str, str]] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        tf = pm.get("typeField")
        tv = pm.get("typeValue")
        if tf and tv:
            col_type_map[(col, tv)] = (label, tf)
        elif col:
            col_type_map[(col, "")] = (label, "")

    try:
        cursor = db.aql.execute(
            f"FOR e IN @@col FILTER e.`{type_field}` == @val LIMIT 10 RETURN {{f: e._from, t: e._to}}",
            bind_vars={"@col": edge_collection, "val": type_value},
        )
        samples = list(cursor)
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    def _resolve_label(doc_id: str) -> str:
        col = doc_id.split("/")[0] if "/" in doc_id else ""
        if (col, "") in col_type_map:
            return col_type_map[(col, "")][0]
        try:
            doc = db.document(doc_id)
        except Exception:
            return "Any"
        if not isinstance(doc, dict):
            return "Any"
        for ent_label, pm in entities_pm.items():
            tf = pm.get("typeField")
            tv = pm.get("typeValue")
            if tf and doc.get(tf) == tv and pm.get("collectionName") == col:
                return ent_label
        return "Any"

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        from_labels.add(_resolve_label(s["f"]))
        to_labels.add(_resolve_label(s["t"]))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


def _infer_dedicated_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain/range for a dedicated (PG-style) edge collection.

    Samples ``_from``/``_to`` document IDs, extracts their collection names,
    and maps those to entity labels via the physical mapping.
    """
    col_to_label: dict[str, str] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        if col:
            col_to_label[col] = label

    try:
        cursor = db.aql.execute(
            "FOR e IN @@col LIMIT 20 RETURN {f: e._from, t: e._to}",
            bind_vars={"@col": edge_collection},
        )
        samples = list(cursor)
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        f_id = s.get("f", "")
        t_id = s.get("t", "")
        f_col = f_id.split("/")[0] if "/" in f_id else ""
        t_col = t_id.split("/")[0] if "/" in t_id else ""
        from_labels.add(col_to_label.get(f_col, "Any"))
        to_labels.add(col_to_label.get(t_col, "Any"))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


def _build_heuristic_mapping(db: StandardDatabase, schema_type: str) -> MappingBundle:
    """Build a MappingBundle from heuristics for PG or LPG schemas."""
    try:
        all_cols = db.collections()
    except Exception as exc:
        raise CoreError("Failed to list collections", code="INVALID_ARGUMENT") from exc

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    entities_cs: list[dict[str, Any]] = []
    entities_pm: dict[str, Any] = {}
    relationships_cs: list[dict[str, Any]] = []
    relationships_pm: dict[str, Any] = {}

    def _props_to_pm(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Convert property list to physical-mapping properties dict.

        Preserves data-quality hints (sentinelValues, numericLike, sampleValues)
        emitted by :func:`_sample_properties` so downstream layers (NL prompts,
        result rendering) can surface them.
        """
        out: dict[str, dict[str, Any]] = {}
        for p in props:
            name = p.get("name", "")
            if not name:
                continue
            entry: dict[str, Any] = {
                "field": p.get("field", name),
                "type": p.get("type", "string"),
            }
            for k in ("sentinelValues", "numericLike", "sampleValues", "required"):
                if k in p:
                    entry[k] = p[k]
            out[name] = entry
        return out

    if schema_type == "pg":
        for col_name in doc_cols:
            label = _collection_label(col_name)
            props = _sample_properties(db, col_name)
            entities_cs.append({"name": label, "labels": [label], "properties": props})
            entities_pm[label] = {
                "style": "COLLECTION",
                "collectionName": col_name,
                "properties": _props_to_pm(props),
            }

        for col_name in edge_cols:
            rel_type = col_name.upper()
            props = _sample_properties(db, col_name)
            domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
            relationships_cs.append({
                "type": rel_type,
                "fromEntity": domain,
                "toEntity": range_,
                "properties": props,
            })
            relationships_pm[rel_type] = {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": col_name,
                "domain": domain,
                "range": range_,
                "properties": _props_to_pm(props),
            }
    elif schema_type in ("lpg", "hybrid"):
        for col_name in doc_cols:
            type_field = _detect_type_field(db, col_name)
            if type_field:
                values = _type_field_values(db, col_name, type_field)
                for val in values:
                    label = _pascal_case(val)
                    props = _sample_properties_filtered(db, col_name, type_field, val)
                    entities_cs.append({"name": label, "labels": [label], "properties": props})
                    entities_pm[label] = {
                        "style": "LABEL",
                        "collectionName": col_name,
                        "typeField": type_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                label = _collection_label(col_name)
                props = _sample_properties(db, col_name)
                entities_cs.append({"name": label, "labels": [label], "properties": props})
                entities_pm[label] = {
                    "style": "COLLECTION",
                    "collectionName": col_name,
                    "properties": _props_to_pm(props),
                }

        for col_name in edge_cols:
            detected_field = _detect_type_field(db, col_name, candidates=_EDGE_TYPE_FIELDS)

            if detected_field:
                values = _type_field_values(db, col_name, detected_field)
                for val in values:
                    domain, range_ = _infer_lpg_edge_endpoints(db, col_name, detected_field, val, entities_pm)
                    props = _sample_properties_filtered(db, col_name, detected_field, val)
                    relationships_cs.append({
                        "type": val,
                        "fromEntity": domain,
                        "toEntity": range_,
                        "properties": props,
                    })
                    relationships_pm[val] = {
                        "style": "GENERIC_WITH_TYPE",
                        "edgeCollectionName": col_name,
                        "typeField": detected_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                rel_type = col_name.upper()
                props = _sample_properties(db, col_name)
                domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
                relationships_cs.append({
                    "type": rel_type,
                    "fromEntity": domain,
                    "toEntity": range_,
                    "properties": props,
                })
                relationships_pm[rel_type] = {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": col_name,
                    "domain": domain,
                    "range": range_,
                    "properties": _props_to_pm(props),
                }

    _SKIP_INDEX_TYPES = {"primary", "edge"}
    col_indexes: dict[str, list[dict[str, Any]]] = {}
    for col_name in doc_cols + edge_cols:
        try:
            raw_indexes = db.collection(col_name).indexes()
            filtered = []
            for idx in raw_indexes:
                if not isinstance(idx, dict):
                    continue
                idx_type = idx.get("type", "")
                if idx_type in _SKIP_INDEX_TYPES:
                    continue
                filtered.append({
                    "type": idx_type,
                    "fields": idx.get("fields", []),
                    "unique": idx.get("unique", False),
                    "sparse": idx.get("sparse", False),
                    "name": idx.get("name", ""),
                })
            if filtered:
                col_indexes[col_name] = filtered
        except Exception:
            pass

    for pm_entry in entities_pm.values():
        col = pm_entry.get("collectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    for pm_entry in relationships_pm.values():
        col = pm_entry.get("edgeCollectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    conceptual_schema = {
        "entities": entities_cs,
        "relationships": relationships_cs,
    }
    physical_mapping = {
        "entities": entities_pm,
        "relationships": relationships_pm,
    }

    return MappingBundle(
        conceptual_schema=conceptual_schema,
        physical_mapping=physical_mapping,
        metadata={"source": "heuristic", "schemaType": schema_type},
        source=MappingSource(kind="heuristic", notes=f"Built from {schema_type} heuristic classification"),
    )


def acquire_mapping_bundle(db: StandardDatabase, *, include_owl: bool = False) -> MappingBundle:
    """Call arangodb-schema-analyzer to produce a MappingBundle from a live database.

    Uses AgenticSchemaAnalyzer with baseline inference (no LLM required).
    If arangodb-schema-analyzer is not installed, raises ImportError.
    """
    try:
        from schema_analyzer import AgenticSchemaAnalyzer, export_mapping
        from schema_analyzer.owl_export import export_conceptual_model_as_owl_turtle
    except ImportError:
        raise ImportError(
            "arangodb-schema-analyzer is not installed. "
            "Install it with: pip install arangodb-schema-analyzer  "
            "or: pip install -e ~/code/arango-schema-mapper"
        ) from None

    analyzer = AgenticSchemaAnalyzer()
    analysis_result = analyzer.analyze_physical_schema(db)

    analysis_dict = {
        "conceptualSchema": analysis_result.conceptual_schema,
        "physicalMapping": analysis_result.physical_mapping,
        "metadata": analysis_result.metadata.model_dump(by_alias=True),
    }

    export = export_mapping(analysis_dict, target="cypher")

    pm = export.get("physicalMapping", {})
    _normalize_analyzer_pm(pm)

    owl_turtle: str | None = None
    if include_owl:
        owl_turtle = export_conceptual_model_as_owl_turtle(analysis_dict)

    bundle = MappingBundle(
        conceptual_schema=export.get("conceptualSchema", {}),
        physical_mapping=pm,
        metadata=export.get("metadata", {}),
        owl_turtle=owl_turtle,
        source=MappingSource(
            kind="schema_analyzer_export",
            notes="Generated by arangodb-schema-analyzer (baseline)",
        ),
    )

    bundle = _fixup_dedicated_edges(bundle, db)
    bundle = _backfill_missing_collections(bundle, db)
    return bundle


def _backfill_missing_collections(
    bundle: MappingBundle,
    db: "StandardDatabase",
) -> MappingBundle:
    """Ensure every non-system collection in the database is represented.

    The external schema-analyzer may only discover collections that appear in
    a named graph definition, missing standalone document or edge collections.
    This post-processor lists all collections, compares with what the analyzer
    found, and fills in any gaps using the heuristic approach.
    """
    try:
        all_cols = db.collections()
    except Exception:
        return bundle

    db_doc_cols: set[str] = set()
    db_edge_cols: set[str] = set()
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            db_edge_cols.add(name)
        else:
            db_doc_cols.add(name)

    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema

    entities_pm = dict(pm.get("entities") or {})
    rels_pm = dict(pm.get("relationships") or {})
    entities_cs = list(cs.get("entities") or [])
    rels_cs = list(cs.get("relationships") or [])

    known_doc_cols: set[str] = set()
    for emap in entities_pm.values():
        col = emap.get("collectionName", "")
        if col:
            known_doc_cols.add(col)

    known_edge_cols: set[str] = set()
    for rmap in rels_pm.values():
        col = rmap.get("edgeCollectionName") or rmap.get("collectionName", "")
        if col:
            known_edge_cols.add(col)

    missing_doc = db_doc_cols - known_doc_cols
    missing_edge = db_edge_cols - known_edge_cols
    if not missing_doc and not missing_edge:
        return bundle

    if missing_doc or missing_edge:
        logger.info(
            "Backfilling %d missing document and %d missing edge collections",
            len(missing_doc), len(missing_edge),
        )

    def _props_to_pm(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in props:
            name = p.get("name", "")
            if not name:
                continue
            entry: dict[str, Any] = {
                "field": p.get("field", name),
                "type": p.get("type", "string"),
            }
            for k in ("sentinelValues", "numericLike", "sampleValues", "required"):
                if k in p:
                    entry[k] = p[k]
            out[name] = entry
        return out

    for col_name in sorted(missing_doc):
        type_field = _detect_type_field(db, col_name)
        if type_field:
            values = _type_field_values(db, col_name, type_field)
            for val in values:
                label = _pascal_case(val) if val else _collection_label(col_name)
                if label in entities_pm:
                    continue
                props = _sample_properties_filtered(db, col_name, type_field, val)
                entities_cs.append({"name": label, "labels": [label], "properties": props})
                entities_pm[label] = {
                    "style": "LABEL",
                    "collectionName": col_name,
                    "typeField": type_field,
                    "typeValue": val,
                    "properties": _props_to_pm(props),
                }
        else:
            label = _collection_label(col_name)
            if label in entities_pm:
                continue
            props = _sample_properties(db, col_name)
            entities_cs.append({"name": label, "labels": [label], "properties": props})
            entities_pm[label] = {
                "style": "COLLECTION",
                "collectionName": col_name,
                "properties": _props_to_pm(props),
            }

    for col_name in sorted(missing_edge):
        detected_field = _detect_type_field(db, col_name, candidates=_EDGE_TYPE_FIELDS)
        if detected_field:
            values = _type_field_values(db, col_name, detected_field)
            for val in values:
                if val in rels_pm:
                    continue
                domain, range_ = _infer_lpg_edge_endpoints(db, col_name, detected_field, val, entities_pm)
                props = _sample_properties_filtered(db, col_name, detected_field, val)
                rels_cs.append({
                    "type": val,
                    "fromEntity": domain,
                    "toEntity": range_,
                    "properties": props,
                })
                rels_pm[val] = {
                    "style": "GENERIC_WITH_TYPE",
                    "edgeCollectionName": col_name,
                    "typeField": detected_field,
                    "typeValue": val,
                    "properties": _props_to_pm(props),
                }
        else:
            rel_type = _collection_label(col_name).upper()
            if rel_type in rels_pm:
                continue
            props = _sample_properties(db, col_name)
            domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
            rels_cs.append({
                "type": rel_type,
                "fromEntity": domain,
                "toEntity": range_,
                "properties": props,
            })
            rels_pm[rel_type] = {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": col_name,
                "domain": domain,
                "range": range_,
                "properties": _props_to_pm(props),
            }

    new_pm = {**pm, "entities": entities_pm, "relationships": rels_pm}
    new_cs = {**cs, "entities": entities_cs, "relationships": rels_cs}
    return MappingBundle(
        conceptual_schema=new_cs,
        physical_mapping=new_pm,
        metadata=bundle.metadata,
        owl_turtle=bundle.owl_turtle,
        source=MappingSource(
            kind=bundle.source.kind if bundle.source else "heuristic",
            notes=(bundle.source.notes or "") + " + backfill" if bundle.source else "backfill",
        ),
    )


def _fixup_dedicated_edges(
    bundle: MappingBundle,
    db: StandardDatabase,
) -> MappingBundle:
    """Detect DEDICATED_COLLECTION edges that have a type discriminator and split them.

    The analyzer sometimes treats a multi-type edge collection as a single
    DEDICATED_COLLECTION.  This post-processor queries the database for a type
    discriminator field and, when found, replaces the single entry with per-type
    GENERIC_WITH_TYPE entries.
    """
    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    rels_pm = pm.get("relationships") or {}
    rels_cs = cs.get("relationships") or []
    entities_pm = pm.get("entities") or {}

    replacements: dict[str, list[tuple[str, dict, dict]]] = {}

    for rel_name, rmap in list(rels_pm.items()):
        if rmap.get("style") != "DEDICATED_COLLECTION":
            continue
        edge_col = rmap.get("edgeCollectionName") or ""
        if not edge_col:
            continue

        detected = _detect_type_field(db, edge_col, candidates=_EDGE_TYPE_FIELDS)
        if not detected:
            continue

        values = _type_field_values(db, edge_col, detected)
        if len(values) < 1:
            continue

        new_entries: list[tuple[str, dict, dict]] = []
        for val in values:
            domain, range_ = _infer_lpg_edge_endpoints(db, edge_col, detected, val, entities_pm)
            props = _sample_properties_filtered(db, edge_col, detected, val)
            new_pm = {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": edge_col,
                "typeField": detected,
                "typeValue": val,
                "properties": {p["name"]: {"field": p["name"], "type": "string"} for p in props},
            }
            new_cs = {
                "type": val,
                "fromEntity": domain,
                "toEntity": range_,
                "properties": props,
            }
            new_entries.append((val, new_pm, new_cs))

        if new_entries:
            replacements[rel_name] = new_entries

    if not replacements:
        return bundle

    new_rels_pm = {}
    new_rels_cs = [r for r in rels_cs if r.get("type") not in replacements]
    for rel_name, rmap in rels_pm.items():
        if rel_name in replacements:
            for val, new_pm, new_cs in replacements[rel_name]:
                new_rels_pm[val] = new_pm
                new_rels_cs.append(new_cs)
        else:
            new_rels_pm[rel_name] = rmap

    new_pm = {**pm, "relationships": new_rels_pm}
    new_cs = {**cs, "relationships": new_rels_cs}
    return MappingBundle(
        conceptual_schema=new_cs,
        physical_mapping=new_pm,
        metadata=bundle.metadata,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _normalize_analyzer_pm(pm: dict[str, Any]) -> None:
    """Normalize analyzer export keys to the transpiler's expected format.

    The analyzer uses ``collectionName`` for both entities and relationships,
    but the transpiler expects ``edgeCollectionName`` on relationships.
    The analyzer uses ``physicalFieldName`` in properties, but the transpiler
    expects ``field``.
    """
    for rmap in (pm.get("relationships") or {}).values():
        if "collectionName" in rmap and "edgeCollectionName" not in rmap:
            rmap["edgeCollectionName"] = rmap.pop("collectionName")
        _normalize_props(rmap)

    for emap in (pm.get("entities") or {}).values():
        _normalize_props(emap)


def _normalize_props(mapping_entry: dict[str, Any]) -> None:
    """Remap ``physicalFieldName`` → ``field`` in property dicts."""
    props = mapping_entry.get("properties")
    if not isinstance(props, dict):
        return
    for pname, pval in props.items():
        if not isinstance(pval, dict):
            continue
        if "physicalFieldName" in pval and "field" not in pval:
            pval["field"] = pval.pop("physicalFieldName")
        if "field" not in pval:
            pval["field"] = pname


def compute_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> dict[str, Any]:
    """Compute cardinality statistics for the physical model described by *bundle*.

    Returns a dict suitable for storing in ``MappingBundle.metadata["statistics"]``.
    Uses fast AQL ``LENGTH()`` for collection counts and derives per-relationship
    fan-out/fan-in metrics.
    """
    import datetime

    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    entities = pm.get("entities", {}) if isinstance(pm.get("entities"), dict) else {}
    rels = pm.get("relationships", {}) if isinstance(pm.get("relationships"), dict) else {}

    cs_rels = cs.get("relationships", []) if isinstance(cs.get("relationships"), list) else []
    cs_rel_lookup: dict[str, tuple[str, str]] = {}
    for cr in cs_rels:
        if isinstance(cr, dict):
            cs_rel_lookup[cr.get("type", "")] = (
                cr.get("fromEntity", ""),
                cr.get("toEntity", ""),
            )

    col_counts: dict[str, dict[str, Any]] = {}
    entity_counts: dict[str, dict[str, Any]] = {}
    rel_stats: dict[str, dict[str, Any]] = {}

    seen_collections: set[str] = set()

    for label, emap in entities.items():
        if not isinstance(emap, dict):
            continue
        col_name = emap.get("collectionName", label)
        if col_name not in seen_collections:
            try:
                cursor = db.aql.execute(f"RETURN LENGTH({col_name})")
                count = next(cursor, 0)
            except Exception:
                count = 0
            col_counts[col_name] = {"count": count, "is_edge": False}
            seen_collections.add(col_name)

        style = emap.get("style", "COLLECTION")
        type_field = emap.get("typeField")
        type_value = emap.get("typeValue")
        if style in ("LABEL", "GENERIC_WITH_TYPE") and type_field and type_value:
            try:
                aql = (
                    f"FOR d IN {col_name} "
                    f"FILTER d.`{type_field}` == @tv "
                    f"COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                entity_count = next(cursor, 0)
            except Exception:
                entity_count = col_counts.get(col_name, {}).get("count", 0)
        else:
            entity_count = col_counts.get(col_name, {}).get("count", 0)

        entity_counts[label] = {"estimated_count": entity_count}

    for rtype, rmap in rels.items():
        if not isinstance(rmap, dict):
            continue
        edge_col = rmap.get("edgeCollectionName") or rmap.get("collectionName", rtype)
        if not edge_col:
            continue

        if edge_col not in seen_collections:
            try:
                cursor = db.aql.execute(f"RETURN LENGTH({edge_col})")
                edge_count = next(cursor, 0)
            except Exception:
                edge_count = 0
            col_counts[edge_col] = {"count": edge_count, "is_edge": True}
            seen_collections.add(edge_col)

        style = rmap.get("style", "DEDICATED_COLLECTION")
        type_field = rmap.get("typeField")
        type_value = rmap.get("typeValue")

        if style == "GENERIC_WITH_TYPE" and type_field and type_value:
            try:
                aql = (
                    f"FOR e IN {edge_col} "
                    f"FILTER e.`{type_field}` == @tv "
                    f"COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                edge_count = next(cursor, 0)
            except Exception:
                edge_count = col_counts.get(edge_col, {}).get("count", 0)
        else:
            edge_count = col_counts.get(edge_col, {}).get("count", 0)

        domain_label = rmap.get("domain", "") or ""
        range_label = rmap.get("range", "") or ""
        if (not domain_label or not range_label) and rtype in cs_rel_lookup:
            cs_from, cs_to = cs_rel_lookup[rtype]
            if not domain_label:
                domain_label = cs_from
            if not range_label:
                range_label = cs_to
        source_count = entity_counts.get(domain_label, {}).get("estimated_count", 0) if domain_label else 0
        target_count = entity_counts.get(range_label, {}).get("estimated_count", 0) if range_label else 0

        avg_out = (edge_count / source_count) if source_count > 0 else 0.0
        avg_in = (edge_count / target_count) if target_count > 0 else 0.0

        if source_count > 0 and target_count > 0:
            selectivity = edge_count / (source_count * target_count)
        else:
            selectivity = 1.0

        pattern = _classify_cardinality(avg_out, avg_in)

        rel_stats[rtype] = {
            "edge_count": edge_count,
            "source_count": source_count,
            "target_count": target_count,
            "avg_out_degree": round(avg_out, 2),
            "avg_in_degree": round(avg_in, 2),
            "cardinality_pattern": pattern,
            "selectivity": round(selectivity, 6),
        }

    return {
        "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "collections": col_counts,
        "entities": entity_counts,
        "relationships": rel_stats,
    }


def _classify_cardinality(avg_out: float, avg_in: float) -> str:
    """Classify a relationship as 1:1, 1:N, N:1, or N:M based on average degrees."""
    out_is_one = avg_out <= 1.5
    in_is_one = avg_in <= 1.5
    if out_is_one and in_is_one:
        return "1:1"
    if not out_is_one and in_is_one:
        return "1:N"
    if out_is_one and not in_is_one:
        return "N:1"
    return "N:M"


def enrich_bundle_with_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> MappingBundle:
    """Return a new MappingBundle with cardinality statistics in metadata."""
    stats = compute_statistics(db, bundle)
    new_meta = {**bundle.metadata, "statistics": stats}
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def get_mapping(
    db: StandardDatabase,
    *,
    strategy: str = "auto",
    include_owl: bool = False,
) -> MappingBundle:
    """3-tier mapping acquisition.

    strategy="auto": analyzer first (all schema types: PG, LPG, hybrid);
                     heuristic fallback if analyzer not installed.
    strategy="analyzer": always call acquire_mapping_bundle() (raises if
                         not installed)
    strategy="heuristic": never call analyzer, build best-effort mapping
                          from classify_schema + heuristics
    """
    if strategy not in ("auto", "analyzer", "heuristic"):
        raise CoreError(
            f"Invalid strategy: {strategy!r}. Must be 'auto', 'analyzer', or 'heuristic'.",
            code="INVALID_ARGUMENT",
        )

    key = _cache_key(db)
    fp = _schema_fingerprint(db)

    if key and fp:
        cached = _mapping_cache.get(key)
        if cached is not None:
            bundle, _ts, cached_fp = cached
            if cached_fp == fp:
                logger.debug("Schema fingerprint unchanged for %s; using cached mapping", key)
                return bundle
            logger.info("Schema fingerprint changed for %s; re-introspecting", key)

    if strategy == "analyzer":
        bundle = acquire_mapping_bundle(db, include_owl=include_owl)
    elif strategy == "heuristic":
        schema_type = classify_schema(db)
        bundle = _build_heuristic_mapping(db, schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg")
    else:
        try:
            bundle = acquire_mapping_bundle(db, include_owl=include_owl)
        except ImportError:
            logger.info("arangodb-schema-analyzer not installed; using heuristic fallback")
            schema_type = classify_schema(db)
            bundle = _build_heuristic_mapping(db, schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg")

    if include_owl and not bundle.owl_turtle:
        try:
            from arango_query_core.owl_turtle import mapping_to_turtle
            owl_turtle = mapping_to_turtle(bundle)
            bundle = MappingBundle(
                conceptual_schema=bundle.conceptual_schema,
                physical_mapping=bundle.physical_mapping,
                metadata=bundle.metadata,
                owl_turtle=owl_turtle,
                source=bundle.source,
            )
        except Exception:
            logger.warning("Failed to generate OWL Turtle for heuristic mapping", exc_info=True)

    try:
        bundle = enrich_bundle_with_statistics(db, bundle)
    except Exception:
        logger.warning("Failed to compute cardinality statistics", exc_info=True)

    if key:
        _mapping_cache[key] = (bundle, time.time(), fp)

    return bundle
