"""Data-bind the synthbank catalog against a live database (steps 3 and 5).

Step 3 of porting ``arango-sparql-py``'s query-first synthetic few-shot bank
(Phase 07.5). Step 1 (:mod:`.predicate_index_builder`) turned a conceptual
schema into a ``PredicateIndex``; step 2 (:mod:`.synthbank_renderers`) gave
every promoted shape a Cypher renderer. This module fills those renderers'
slots with **real values sampled from the database**, keeps only candidates
that execute non-empty, and writes the survivors as a few-shot bank that
``FewShotIndex.from_corpus_files`` loads unchanged.

Everything runs through the transpiler
--------------------------------------
Sampling, the execution filter and the ranking probe are all issued as
**Cypher** and executed via ``translate()`` + AQL against the live database
(:class:`TranspilingExecutor`). The SPARQL side samples with SPARQL against
its store for the same reason: the mapping is authoritative, so sampling in
the physical layer directly would hardcode PG-vs-LPG knowledge the
transpiler already owns — and a gold example whose Cypher does not
transpile is rejected here, before it can teach a model to write queries
this engine refuses.

Signals come from the data, not the declared types
--------------------------------------------------
The acquired movies bundle types ``Person.born`` as ``string`` and carries no
property ``role``; taken at face value no property is orderable and no entity
has a name to anchor on. So each property is profiled from sampled values
with :func:`arango_cypher.schema_acquire._profile_property_values` — the
same classifier schema acquisition uses — and:

* **orderable** = values are real numbers, or ISO dates (``temporal``), which
  sort correctly as strings. Numeric-looking *strings* are excluded: ``"10"``
  sorts before ``"9"``, so a top-N over them would be wrong gold.
* **anchor** (the name an entity slot is filled with, the Cypher analogue of
  ``rdfs:label``) = the entity's ``name``-role property, else its
  ``identifier``-role property; ties prefer an indexed property, then the
  alphabetically first — and only if its sampled values are near-unique
  (:data:`MIN_ANCHOR_DISTINCT_RATIO`), because an anchor must name one entity.
  An entity with no such property gets no anchored shapes — never a guessed
  field.
* **optional_relation** (the ``negation`` gate) = the domain has instances
  both with and without the relationship.

Identifier safety
-----------------
Labels, relationship types and property names are spliced into Cypher text
by the renderers. Any that is not a plain identifier is skipped with a
reason in the report rather than quoted and hoped for. Filler *values* go
through the renderers' escaping ``_lit``; the bank is gold examples shown to
a model, and runtime user input still travels as bind parameters.

Paraphrase is opt-in
--------------------
The SPARQL generator paraphrases whenever an API key is present in the
environment. Here a provider must be passed explicitly (see
:mod:`.synthbank_paraphrase`): a credential being configured is not consent
to spend on it — the same rule the live test tier follows (``RUN_LIVE=1``).

Wiring (step 5)
---------------
:func:`write_bank` flattens each paraphrase into its own
``(question, cypher)`` example. ``FewShotIndex.from_corpus_files`` reads only
``question`` + ``cypher``/``query``, so a nested ``paraphrases`` list would
be silently ignored by retrieval; flattened, every paraphrase is a real BM25
target. The engine loads generated banks from ``NL2CYPHER_FEWSHOT_BANKS``
(see :func:`arango_cypher.nl2cypher._core._get_default_fewshot_index`) —
opt-in, because a prompt change must be justified by an eval run.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.synthbank import RELATIONAL_SHAPES, SHAPE_CATALOG, ShapeTemplate

from .predicate_index_builder import CypherPredicateSignals, build_predicate_index
from .synthbank_renderers import render_cypher

logger = logging.getLogger(__name__)

__all__ = [
    "Executor",
    "PropertyProfile",
    "SchemaProfile",
    "TranspilingExecutor",
    "generate_bank",
    "generate_bank_with_report",
    "is_degenerate_value_label",
    "profile_schema",
    "write_bank",
]

#: Bounded, deterministic sampling caps — same values as the SPARQL generator.
MAX_FILLERS_PER_PREDICATE = 2
MAX_TWO_HOP_PARTNERS = 2
#: Values sampled per property when profiling.
PROFILE_SAMPLE = 200

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDERABLE_TYPES = frozenset({"number"})
_ORDERABLE_ROLES = frozenset({"temporal"})
_ANCHOR_ROLES = ("name", "identifier")
#: An anchor names one entity, so its sampled values must be (near-)unique.
#: The role classifier alone is not enough: on Northwind's nine employees,
#: ``country`` classifies as a name, and "what is the birthDate of USA?"
#: matches several rows.
MIN_ANCHOR_DISTINCT_RATIO = 0.95

_SHAPES_BY_NAME: dict[str, ShapeTemplate] = {t.name: t for t in SHAPE_CATALOG}


class Executor(Protocol):
    """Runs one Cypher query and returns its rows."""

    def run(self, cypher: str) -> list[Any]: ...


class TranspilingExecutor:
    """Translate Cypher through *mapping*, execute the AQL on *db*."""

    def __init__(self, db: Any, mapping: MappingBundle) -> None:
        self.db = db
        self.mapping = mapping

    def run(self, cypher: str) -> list[Any]:
        from arango_cypher.api import translate

        transpiled = translate(cypher, mapping=self.mapping)
        return list(self.db.aql.execute(transpiled.aql, bind_vars=transpiled.bind_vars))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scalar(row: Any, key: str = "v") -> Any:
    """A single-column result: a projection yields ``{key: value}``, a bare
    aggregate yields the value itself — observed from the transpiler."""
    if isinstance(row, dict):
        return row.get(key)
    return row


def _safe(*names: str) -> bool:
    return all(isinstance(n, str) and _IDENTIFIER.match(n) for n in names)


def _parse_predicate_iri(iri: str) -> tuple[str, str, str]:
    """``rel:ACTED_IN`` -> ``("rel", "", "ACTED_IN")``;
    ``prop:Person.born`` -> ``("prop", "Person", "born")``."""
    kind, _, rest = iri.partition(":")
    if kind == "prop":
        entity, _, prop = rest.partition(".")
        return kind, entity, prop
    return kind, "", rest


_CURRENCY_SYMBOLS = frozenset({"$", "€", "£", "¥"})
_NUMERIC_VALUE_TOKEN = re.compile(r"^[$€£¥]?-?\d+(?:[.,]\d+)*$")


def is_degenerate_value_label(label: str) -> bool:
    """True when *label* is itself a bare monetary/numeric value (``"0,38 EUR"``).

    Port of the SPARQL generator's guard: "what is the amount of 0,38 EUR?" has
    no faithful paraphrase. Conservative — a bare ``"42"`` is not flagged, as
    it could be a legitimate code; an explicit currency signal is required.
    """
    tokens = label.strip().split()
    if not tokens or len(tokens) > 2 or not _NUMERIC_VALUE_TOKEN.match(tokens[0]):
        return False
    if len(tokens) == 2:
        unit = tokens[1]
        if unit in _CURRENCY_SYMBOLS or unit == "%":
            return True
        return unit.isalpha() and unit.isupper() and 2 <= len(unit) <= 4
    return tokens[0][0] in _CURRENCY_SYMBOLS


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyProfile:
    entity: str
    field: str
    type: str
    role: str
    indexed: bool = False
    distinct_ratio: float = 0.0

    @property
    def orderable(self) -> bool:
        return self.type in _ORDERABLE_TYPES or self.role in _ORDERABLE_ROLES


@dataclass
class SchemaProfile:
    """What the data says about the schema: property types/roles, anchors,
    and which relationships are optional for their domain."""

    properties: dict[tuple[str, str], PropertyProfile] = field(default_factory=dict)
    anchors: dict[str, str] = field(default_factory=dict)
    optional_relations: set[tuple[str, str]] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)

    def anchor(self, entity: str) -> str | None:
        return self.anchors.get(entity)


def _conceptual_entities(mapping: MappingBundle) -> list[dict[str, Any]]:
    cs = mapping.conceptual_schema if isinstance(mapping.conceptual_schema, dict) else {}
    return [e for e in cs.get("entities") or [] if isinstance(e, dict) and e.get("name")]


def _conceptual_relationships(mapping: MappingBundle) -> list[dict[str, Any]]:
    cs = mapping.conceptual_schema if isinstance(mapping.conceptual_schema, dict) else {}
    return [r for r in cs.get("relationships") or [] if isinstance(r, dict) and r.get("type")]


def _count(executor: Executor, cypher: str) -> int:
    rows = executor.run(cypher)
    value = _scalar(rows[0], "n") if rows else 0
    return int(value or 0)


def profile_schema(mapping: MappingBundle, executor: Executor) -> SchemaProfile:
    """Profile every conceptual property and relationship from live data."""
    from arango_cypher.schema_acquire import _profile_property_values

    profile = SchemaProfile()
    for entity in _conceptual_entities(mapping):
        name = entity["name"]
        if not _safe(name):
            profile.skipped.append(f"entity {name!r}: not a plain identifier")
            continue
        for prop in entity.get("properties") or []:
            field_name = prop.get("name") if isinstance(prop, dict) else prop
            if not isinstance(field_name, str) or not _safe(field_name):
                profile.skipped.append(f"property {name}.{field_name!r}: not a plain identifier")
                continue
            rows = executor.run(
                f"MATCH (x:{name}) WHERE x.{field_name} IS NOT NULL "
                f"RETURN x.{field_name} AS v LIMIT {PROFILE_SAMPLE}"
            )
            values = [_scalar(r) for r in rows]
            if not values:
                continue
            meta = _profile_property_values(values, len(values))
            profile.properties[(name, field_name)] = PropertyProfile(
                entity=name,
                field=field_name,
                type=str(meta.get("type") or "string"),
                role=str(meta.get("role") or "other"),
                indexed=bool(isinstance(prop, dict) and prop.get("indexed")),
                distinct_ratio=len({str(v) for v in values}) / len(values),
            )

        candidates = [
            p
            for p in profile.properties.values()
            if p.entity == name and p.role in _ANCHOR_ROLES and p.distinct_ratio >= MIN_ANCHOR_DISTINCT_RATIO
        ]
        if candidates:
            best = min(candidates, key=lambda p: (_ANCHOR_ROLES.index(p.role), not p.indexed, p.field))
            profile.anchors[name] = best.field

    for rel in _conceptual_relationships(mapping):
        rtype, src = rel.get("type"), rel.get("fromEntity")
        if not isinstance(rtype, str) or not isinstance(src, str) or not _safe(rtype, src):
            profile.skipped.append(f"relationship {rtype!r} from {src!r}: not a plain identifier")
            continue
        total = _count(executor, f"MATCH (x:{src}) RETURN count(x) AS n")
        with_rel = _count(
            executor, f"MATCH (x:{src}) WHERE EXISTS {{ (x)-[:{rtype}]->() }} RETURN count(x) AS n"
        )
        if 0 < with_rel < total:
            profile.optional_relations.add((src, rtype))
    return profile


def _signals(index: Any, profile: SchemaProfile) -> dict[str, CypherPredicateSignals]:
    """The ``applies`` gates' signals, computed from the profile."""
    out: dict[str, CypherPredicateSignals] = {}
    for pred in index.retrieve("", k=10_000, dump=True):
        kind, entity, name = _parse_predicate_iri(pred.iri)
        if kind == "prop":
            prof = profile.properties.get((entity, name))
            out[pred.iri] = CypherPredicateSignals(pred.iri, bool(prof and prof.orderable), False)
        else:
            out[pred.iri] = CypherPredicateSignals(
                pred.iri, False, (pred.domain, name) in profile.optional_relations
            )
    return out


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _sample_values(
    executor: Executor,
    cypher: str,
    cap: int,
    rng: random.Random,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> list[Any]:
    """Distinct values from *cypher* (column ``v``): sorted, seeded-shuffled,
    then filtered, then capped.

    Shuffle before filtering, as the SPARQL generator does: every predicate of
    a shape shares one ``rng``, and filtering first would change this call's
    draw count and silently desync every later predicate's sample.
    """
    values = sorted({_scalar(r) for r in executor.run(cypher) if _scalar(r) is not None}, key=str)
    rng.shuffle(values)
    if exclude is not None:
        values = [v for v in values if not exclude(str(v))]
    return values[:cap]


Candidate = tuple[dict[str, Any], str, str | None]


@dataclass
class _Context:
    index: Any
    profile: SchemaProfile
    executor: Executor
    rng: random.Random
    predicates: list[Any]


def _prop_parts(pred: Any) -> tuple[str, str]:
    _kind, entity, field_name = _parse_predicate_iri(pred.iri)
    return entity, field_name


def _rel_name(pred: Any) -> str:
    return _parse_predicate_iri(pred.iri)[2]


def _candidates_lookup(pred: Any, ctx: _Context) -> list[Candidate]:
    entity, prop = _prop_parts(pred)
    anchor = ctx.profile.anchor(entity)
    if not anchor or anchor == prop:
        return []
    values = _sample_values(
        ctx.executor,
        f"MATCH (x:{entity}) WHERE x.{prop} IS NOT NULL AND x.{anchor} IS NOT NULL RETURN DISTINCT x.{anchor} AS v",
        MAX_FILLERS_PER_PREDICATE,
        ctx.rng,
        exclude=is_degenerate_value_label,
    )
    template = _SHAPES_BY_NAME["lookup"].question_template
    return [
        (
            {"domain_label": entity, "anchor_property": anchor, "filler_value": v, "predicate_name": prop},
            template.format(predicate=pred.label, entity=v),
            None,
        )
        for v in values
    ]


def _candidates_value_object(pred: Any, ctx: _Context) -> list[Candidate]:
    rel = _rel_name(pred)
    anchor = ctx.profile.anchor(pred.domain)
    hops = sorted(
        (p for p in ctx.predicates if p.kind == "datatype" and p.domain == pred.range),
        key=lambda p: p.iri,
    )
    if not anchor or not hops:
        return []
    hop = hops[0]
    _entity, hop_prop = _prop_parts(hop)
    values = _sample_values(
        ctx.executor,
        f"MATCH (x:{pred.domain})-[:{rel}]->(mid:{pred.range}) "
        f"WHERE mid.{hop_prop} IS NOT NULL AND x.{anchor} IS NOT NULL RETURN DISTINCT x.{anchor} AS v",
        MAX_FILLERS_PER_PREDICATE,
        ctx.rng,
        exclude=is_degenerate_value_label,
    )
    template = _SHAPES_BY_NAME["value_object"].question_template
    return [
        (
            {
                "domain_label": pred.domain,
                "range_label": pred.range,
                "predicate_name": rel,
                "hop_predicate_name": hop_prop,
                "anchor_property": anchor,
                "filler_value": v,
            },
            template.format(predicate=pred.label, hop_predicate=hop.label, entity=v),
            None,
        )
        for v in values
    ]


def _range_anchored(pred: Any, ctx: _Context, shape: str) -> list[Candidate]:
    """``category_filter`` / ``scalar_count``: anchor the range side by name."""
    rel = _rel_name(pred)
    anchor = ctx.profile.anchor(pred.range) if pred.range else None
    if not anchor:
        return []
    values = _sample_values(
        ctx.executor,
        f"MATCH (d:{pred.domain})-[:{rel}]->(x:{pred.range}) WHERE x.{anchor} IS NOT NULL RETURN DISTINCT x.{anchor} AS v",
        MAX_FILLERS_PER_PREDICATE,
        ctx.rng,
    )
    template = _SHAPES_BY_NAME[shape].question_template
    return [
        (
            {
                "domain_label": pred.domain,
                "range_label": pred.range,
                "predicate_name": rel,
                "anchor_property": anchor,
                "filler_value": v,
            },
            template.format(member_type=pred.domain, category=v),
            None,
        )
        for v in values
    ]


def _candidates_category_filter(pred: Any, ctx: _Context) -> list[Candidate]:
    return _range_anchored(pred, ctx, "category_filter")


def _candidates_scalar_count(pred: Any, ctx: _Context) -> list[Candidate]:
    return _range_anchored(pred, ctx, "scalar_count")


def _candidates_grouped_aggregation(pred: Any, ctx: _Context) -> list[Candidate]:
    rel = _rel_name(pred)
    if not pred.range:
        return []
    rows = ctx.executor.run(
        f"MATCH (x:{pred.domain})-[:{rel}]->(g:{pred.range}) WITH g, count(x) AS n RETURN n ORDER BY n DESC LIMIT 2"
    )
    counts = [int(_scalar(r, "n") or 0) for r in rows]
    # Two distinct groups with a strict gap, or HAVING(n > K) is empty or
    # trivially true for every group.
    if len(counts) < 2 or counts[0] <= counts[1]:
        return []
    threshold = counts[1]
    template = _SHAPES_BY_NAME["grouped_aggregation"].question_template
    return [
        (
            {
                "domain_label": pred.domain,
                "range_label": pred.range,
                "predicate_name": rel,
                "threshold": threshold,
            },
            template.format(group_type=pred.range, threshold=threshold, member_type=pred.domain),
            None,
        )
    ]


def _ranking(pred: Any, ctx: _Context, *, shape: str, skip: int) -> list[Candidate]:
    entity, prop = _prop_parts(pred)
    skip_clause = f"SKIP {skip} " if skip else ""
    probe = f"MATCH (r:{entity}) WHERE r.{prop} IS NOT NULL RETURN r.{prop} AS v ORDER BY r.{prop} DESC {skip_clause}LIMIT 2"
    if not _strict_extremum(ctx.executor, probe)[0]:
        return []
    slots = {
        "member_type": entity,
        "order_predicate": pred.label,
        "superlative": "highest",
        "direction": "descending",
    }
    if shape == "offset":
        slots["ordinal"] = "second"
    return [
        (
            {"domain_label": entity, "predicate_name": prop, "direction": "desc"},
            _SHAPES_BY_NAME[shape].question_template.format(**slots),
            probe,
        )
    ]


def _candidates_top_n(pred: Any, ctx: _Context) -> list[Candidate]:
    return _ranking(pred, ctx, shape="top_n", skip=0)


def _candidates_offset(pred: Any, ctx: _Context) -> list[Candidate]:
    return _ranking(pred, ctx, shape="offset", skip=1)


def _candidates_negation(pred: Any, ctx: _Context) -> list[Candidate]:
    template = _SHAPES_BY_NAME["negation"].question_template
    return [
        (
            {"domain_label": pred.domain, "range_label": pred.range, "predicate_name": _rel_name(pred)},
            template.format(member_type=pred.domain, predicate=pred.label),
            None,
        )
    ]


def _candidates_two_hop(pred: Any, ctx: _Context) -> list[Candidate]:
    rel = _rel_name(pred)
    anchor = ctx.profile.anchor(pred.range) if pred.range else None
    if not anchor:
        return []
    partners = sorted(
        (
            p
            for p in ctx.predicates
            if p.domain == pred.domain and p.iri != pred.iri and p.shape in RELATIONAL_SHAPES
        ),
        key=lambda p: p.iri,
    )
    if not partners:
        return []
    ctx.rng.shuffle(partners)
    template = _SHAPES_BY_NAME["two_hop"].question_template
    out: list[Candidate] = []
    for hop in partners[:MAX_TWO_HOP_PARTNERS]:
        hop_rel = _rel_name(hop)
        far = f":{hop.range}" if hop.range else ""
        values = _sample_values(
            ctx.executor,
            f"MATCH (c:{pred.range})<-[:{rel}]-(x:{pred.domain})-[:{hop_rel}]->(f{far}) "
            f"WHERE c.{anchor} IS NOT NULL RETURN DISTINCT c.{anchor} AS v",
            1,
            ctx.rng,
        )
        for v in values:
            out.append(
                (
                    {
                        "range_label": pred.range,
                        "domain_label": pred.domain,
                        "far_label": hop.range,
                        "predicate_name": rel,
                        "hop_predicate_name": hop_rel,
                        "anchor_property": anchor,
                        "filler_value": v,
                    },
                    template.format(
                        far_type=hop.range,
                        entity=v,
                        near_predicate=pred.label,
                        far_predicate=hop.label,
                        member_type=pred.domain,
                    ),
                    None,
                )
            )
    return out


_CANDIDATE_BUILDERS: dict[str, Callable[[Any, _Context], list[Candidate]]] = {
    "lookup": _candidates_lookup,
    "value_object": _candidates_value_object,
    "category_filter": _candidates_category_filter,
    "scalar_count": _candidates_scalar_count,
    "grouped_aggregation": _candidates_grouped_aggregation,
    "top_n": _candidates_top_n,
    "offset": _candidates_offset,
    "negation": _candidates_negation,
    "two_hop": _candidates_two_hop,
}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _strict_extremum(executor: Executor, probe: str) -> tuple[bool, str]:
    """Rank 1 must strictly exceed rank 2, or ``LIMIT 1`` / ``SKIP 1`` has no
    single right answer (the SPARQL side's ``weight_g`` saturation lesson)."""
    values = [_scalar(r) for r in executor.run(probe)]
    if len(values) < 2:
        return True, f"probe: only {len(values)} value(s)"
    try:
        top, second = float(values[0]), float(values[1])
    except (TypeError, ValueError):
        top, second = values[0], values[1]  # temporal: ISO strings compare correctly
    if top <= second:
        return False, f"rank1={values[0]!r} <= rank2={values[1]!r}"
    return True, f"rank1={values[0]!r} > rank2={values[1]!r}"


def _execution_nonempty(executor: Executor, cypher: str) -> bool:
    """Non-empty, where a lone ``0`` counts as empty: ``count()`` always
    returns one row, even when nothing matched."""
    rows = executor.run(cypher)
    if not rows:
        return False
    if len(rows) > 1:
        return True
    row = rows[0]
    if isinstance(row, dict):
        if len(row) != 1:
            return True
        (row,) = row.values()
    return not (isinstance(row, (int, float)) and not isinstance(row, bool) and row == 0)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _predicate_names_are_safe(pred: Any) -> bool:
    kind, entity, name = _parse_predicate_iri(pred.iri)
    names = [name, pred.domain] + ([pred.range] if kind == "rel" and pred.range else [])
    if kind == "prop":
        names.append(entity)
    return _safe(*names)


def generate_bank_with_report(
    mapping: MappingBundle,
    executor: Executor,
    *,
    k_paraphrases: int = 3,
    seed: int = 0,
    provider: Any = None,
    shapes: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate a bank for *mapping* against the data behind *executor*.

    Returns ``(bank, report)``. ``bank`` is ``{"version": 1, "examples": [...]}``
    with each example's ``question``, ``cypher``, ``shape`` and (when a
    *provider* was passed) ``paraphrases``. ``report`` is the per-shape yield —
    kept/dropped counts and the reason for every drop — so a shape yielding
    nothing is a recorded finding, never a silent gap.

    *provider* is an ``LLMProvider``; ``None`` (the default) skips paraphrase.
    *shapes* restricts generation to a subset of the catalog.
    """
    from .synthbank_paraphrase import paraphrase

    wanted = set(shapes) if shapes is not None else None
    index = build_predicate_index(mapping)
    profile = profile_schema(mapping, executor)
    signals = _signals(index, profile)
    predicates = sorted(index.retrieve("", k=10_000, dump=True), key=lambda p: p.iri)

    report: dict[str, Any] = {
        t.name: {"kept": 0, "dropped": 0, "reasons": []}
        for t in SHAPE_CATALOG
        if wanted is None or t.name in wanted
    }
    report["_profile"] = {
        "anchors": dict(sorted(profile.anchors.items())),
        "orderable": sorted(f"{p.entity}.{p.field}" for p in profile.properties.values() if p.orderable),
        "optional_relations": sorted(f"{d}-[:{r}]" for d, r in profile.optional_relations),
        "skipped": profile.skipped,
    }
    examples: list[dict[str, Any]] = []

    for shape in SHAPE_CATALOG:
        if wanted is not None and shape.name not in wanted:
            continue
        entry = report[shape.name]
        ctx = _Context(index, profile, executor, random.Random(f"{seed}:{shape.name}"), predicates)
        hit = False
        for pred in predicates:
            if not shape.applies(pred, index, signals):
                continue
            hit = True
            if not _predicate_names_are_safe(pred):
                entry["dropped"] += 1
                entry["reasons"].append(f"{pred.iri}: a label or name is not a plain identifier")
                continue
            candidates = _CANDIDATE_BUILDERS[shape.name](pred, ctx)
            if not candidates:
                entry["dropped"] += 1
                entry["reasons"].append(
                    f"{pred.iri}: no viable data-bound candidate "
                    "(no anchor property, empty filler pool, tied extremum, or no threshold gap)"
                )
                continue
            for binding, question, probe in candidates:
                cypher = render_cypher(shape.name, binding)
                try:
                    nonempty = _execution_nonempty(executor, cypher)
                except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                    entry["dropped"] += 1
                    entry["reasons"].append(
                        f"{pred.iri}: gold failed to execute ({type(exc).__name__}: {exc})"
                    )
                    logger.warning("synthbank: %s gold failed for %s: %s", shape.name, pred.iri, exc)
                    continue
                if not nonempty:
                    entry["dropped"] += 1
                    entry["reasons"].append(f"{pred.iri}: executed empty")
                    continue
                example: dict[str, Any] = {"question": question, "cypher": cypher, "shape": shape.name}
                if probe is not None:
                    example["probe"] = probe
                if provider is not None:
                    found = paraphrase(question, shape, binding, provider=provider, k=k_paraphrases)
                    if found:
                        example["paraphrases"] = found
                examples.append(example)
                entry["kept"] += 1
        if not hit:
            entry["reasons"].append("no predicate in this schema satisfies this shape's applies() gate")

    examples.sort(key=lambda e: (e["shape"], e["question"]))
    return {"version": 1, "examples": examples}, report


def generate_bank(mapping: MappingBundle, executor: Executor, **kwargs: Any) -> dict[str, Any]:
    """:func:`generate_bank_with_report` without the report."""
    bank, _report = generate_bank_with_report(mapping, executor, **kwargs)
    return bank


def write_bank(bank: dict[str, Any], path: Path, *, source: str = "") -> int:
    """Write *bank* as a corpus file ``FewShotIndex.from_corpus_files`` loads.

    Each paraphrase becomes its own example (``paraphrase_of`` names the
    template question), because the loader reads only ``question`` and
    ``cypher`` — a nested list would never reach retrieval. Returns the number
    of examples written.
    """
    import yaml

    flat: list[dict[str, Any]] = []
    for example in bank.get("examples", []):
        base = {"question": example["question"], "cypher": example["cypher"], "shape": example["shape"]}
        flat.append(base)
        for text in example.get("paraphrases", []):
            flat.append({**base, "question": text, "paraphrase_of": example["question"]})
    header = "# Generated by arango_cypher.nl2cypher.synthbank_binder — do not edit by hand.\n" + (
        f"# Source: {source}\n" if source else ""
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"version": 1, "examples": flat}, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(header + body, encoding="utf-8")
    return len(flat)
