"""The mapping acquired from a loaded LDBC layout, diffed against its fixture.

PRD §12.1 LDBC-2 and the plan §5.5: the committed fixtures (``ldbc_pg``,
``ldbc_lpg``) are the contract. After a load, ``acquire_mapping_bundle`` runs
against the database and every difference from the fixture is either an
analyzer defect, filed upstream and listed in ``KNOWN_GAPS``, or a fixture
error. Both bundles are compared through ``MappingResolver.schema_summary()``,
so each sees the same normalised view.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import MappingBundle, MappingResolver

#: Differences the analyzer is known to produce, by kind.
KNOWN_GAPS: dict[str, str] = {
    # upstream: arango-solutions-org/arango-schema-analyzer#43 — remove when supertypes are detected
    "missing_supertype": "label hierarchies (Message, Place, Organisation) are not detected",
    # upstream: arango-solutions-org/arango-schema-analyzer#44 — remove when multi-label domain/range is exported
    "missing_endpoints": "relationships between several labels get no domain/range",
    # upstream: arango-solutions-org/arango-schema-analyzer#21 — remove when property types are inferred
    "property_type": "every property is typed string",
    # upstream: arango-solutions-org/arango-schema-analyzer#46 — remove when sparse properties are reported
    "sparse_property": "a property on only some documents of a collection is not reported",
}


#: Properties of the LDBC data that only some documents of their collection
#: carry (``Organisation_isLocatedIn_Place`` edges have no ``creationDate``).
_SPARSE = {("relationship IS_LOCATED_IN", "creationDate"): "sparse_property"}


def _view(bundle: MappingBundle) -> dict[str, dict[str, Any]]:
    summary = MappingResolver(bundle).schema_summary()
    view: dict[str, dict[str, Any]] = {}
    for e in summary["entities"]:
        view[f"entity {e['label']}"] = {
            "collection": e.get("collection"),
            "style": e.get("style"),
            "typeValue": e.get("typeValue"),
            "properties": {k: v.get("type") for k, v in (e.get("properties") or {}).items()},
        }
    for r in summary["relationships"]:
        view[f"relationship {r['type']}"] = {
            "edgeCollection": r.get("edgeCollection"),
            "style": r.get("style"),
            "typeValue": r.get("typeValue"),
            "domain": r.get("domain"),
            "range": r.get("range"),
            "properties": {k: v.get("type") for k, v in (r.get("properties") or {}).items()},
        }
    return view


def differences(acquired: MappingBundle, fixture: MappingBundle) -> list[tuple[str, str]]:
    """``(kind, description)`` for each way *acquired* differs from *fixture*.

    The kind is a key of ``KNOWN_GAPS``, or ``"unexplained"``.
    """
    got, want = _view(acquired), _view(fixture)
    supertypes = {
        f"entity {e['label']}"
        for e in MappingResolver(fixture).schema_summary()["entities"]
        if e.get("style") in ("UNION", "MULTI_LABEL")
    }
    out: list[tuple[str, str]] = []
    for name in sorted(set(want) - set(got)):
        out.append(("missing_supertype" if name in supertypes else "unexplained", f"{name}: not acquired"))
    for name in sorted(set(got) - set(want)):
        out.append(("unexplained", f"{name}: acquired, not in the fixture"))
    for name in sorted(set(want) & set(got)):
        w, g = want[name], got[name]
        for field in ("collection", "edgeCollection", "style", "typeValue"):
            if field in w and w[field] != g.get(field):
                out.append(
                    ("unexplained", f"{name}.{field}: fixture {w[field]!r}, acquired {g.get(field)!r}")
                )
        # A relationship with several labels on either side loses both ends.
        several = isinstance(w.get("domain"), list) or isinstance(w.get("range"), list)
        for field in ("domain", "range"):
            if field in w and w[field] != g.get(field):
                kind = "missing_endpoints" if g.get(field) is None and several else "unexplained"
                out.append((kind, f"{name}.{field}: fixture {w[field]!r}, acquired {g.get(field)!r}"))
        wp, gp = w["properties"], g["properties"]
        for prop in sorted(set(wp) - set(gp)):
            out.append((_SPARSE.get((name, prop), "unexplained"), f"{name}.{prop}: not acquired"))
        for prop in sorted(set(gp) - set(wp)):
            out.append(("unexplained", f"{name}.{prop}: acquired, not in the fixture"))
        for prop in sorted(set(wp) & set(gp)):
            if wp[prop] != gp[prop]:
                kind = "property_type" if gp[prop] == "string" else "unexplained"
                out.append((kind, f"{name}.{prop}: fixture {wp[prop]!r}, acquired {gp[prop]!r}"))
    return out
