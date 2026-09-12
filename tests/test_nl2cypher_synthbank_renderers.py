"""Cypher renderers for the promoted synthbank catalog (step 2 of the port).

The load-bearing test here is :func:`test_every_shape_renders_translatable_cypher`.
A renderer that emits plausible-looking Cypher the transpiler cannot translate is
worse than no renderer: it would seed the few-shot bank with examples teaching a
model to produce queries this engine rejects. Rendering is therefore always
checked against a real shipped mapping, never against a string comparison.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher.nl2cypher.predicate_index_builder import (
    build_predicate_index,
    build_predicate_signals,
)
from arango_cypher.nl2cypher.synthbank_renderers import (
    CYPHER_RENDERERS,
    UnsupportedShapeError,
    render_cypher,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for

synthbank = pytest.importorskip("arango_query_core.nl.synthbank")

#: Shipped mappings searched for a predicate that satisfies each shape's gate.
#: Several shapes only fire on particular schema topologies — ``two_hop`` needs
#: two relational predicates sharing a domain, which ``movies_pg`` lacks.
_FIXTURES = ("movies_pg", "northwind_pg", "movies_lpg", "icij_paradise_papers")


def _binding(pred, sibling=None) -> dict:
    """A plausible pre-bound binding, as the data binder will later produce."""
    name = pred.iri.split(":", 1)[1]
    return {
        "predicate_name": name.split(".")[-1] if pred.kind == "datatype" else name,
        "domain_label": pred.domain,
        "range_label": pred.range if pred.kind == "object" else "",
        "far_label": sibling.range if sibling is not None else "",
        "hop_predicate_name": (
            sibling.iri.split(":", 1)[1]
            if sibling is not None
            else (pred.shape_detail[0][0].replace(" ", "_") if pred.shape_detail else "name")
        ),
        "anchor_property": "name",
        "filler_value": "Anchor Value",
        "threshold": 2,
    }


def _first_candidate(shape):
    """First (bundle, predicate, sibling) across the shipped fixtures whose
    predicate satisfies *shape*'s gate."""
    for fixture in _FIXTURES:
        try:
            bundle = mapping_bundle_for(fixture)
        except Exception:  # pragma: no cover - fixture set is stable
            continue
        index = build_predicate_index(bundle)
        predicates = index.retrieve("", k=10_000, dump=True)
        signals = build_predicate_signals(
            bundle,
            # two shapes gate on instance-derived signals; assume the optimistic
            # case so the renderer itself is what is under test here.
            optional_relations=frozenset(p.iri for p in predicates if p.kind == "object"),
        )
        for pred in predicates:
            if not shape.applies(pred, index, signals):
                continue
            sibling = next(
                (
                    o
                    for o in predicates
                    if o.iri != pred.iri
                    and o.domain == pred.domain
                    and o.shape in synthbank.RELATIONAL_SHAPES
                ),
                None,
            )
            return fixture, bundle, pred, sibling
    return None, None, None, None


def test_every_promoted_shape_has_a_cypher_renderer() -> None:
    """Catalog parity — a shape added upstream must be ported, not skipped."""
    catalog = {s.name for s in synthbank.SHAPE_CATALOG}

    assert catalog == set(CYPHER_RENDERERS), (
        f"renderers missing for {sorted(catalog - set(CYPHER_RENDERERS))}; "
        f"unknown renderers {sorted(set(CYPHER_RENDERERS) - catalog)}"
    )


@pytest.mark.parametrize("shape_name", sorted(CYPHER_RENDERERS))
def test_every_shape_renders_translatable_cypher(shape_name: str) -> None:
    """Each renderer's output must survive the real transpiler.

    Generated examples that cannot translate would teach a model to emit
    Cypher this engine rejects — the opposite of the bank's purpose.
    """
    shape = next(s for s in synthbank.SHAPE_CATALOG if s.name == shape_name)
    fixture, bundle, pred, sibling = _first_candidate(shape)

    assert pred is not None, (
        f"no predicate in {_FIXTURES} satisfies the {shape_name!r} gate, so the "
        "renderer is unverified — add a fixture covering that topology"
    )

    cypher = render_cypher(shape_name, _binding(pred, sibling))

    try:
        translate(cypher, mapping=bundle)
    except Exception as exc:  # noqa: BLE001 - surface the query in the failure
        pytest.fail(f"{shape_name} on {fixture} produced untranslatable Cypher:\n{cypher}\n{exc}")


def test_unknown_shape_is_an_explicit_error_not_a_silent_skip() -> None:
    with pytest.raises(UnsupportedShapeError) as excinfo:
        render_cypher("no_such_shape", {})

    assert "no_such_shape" in str(excinfo.value)


def test_undeclared_range_degrades_to_an_unlabelled_node() -> None:
    """An undeclared range must not emit ``(c:)``, which would not parse."""
    cypher = render_cypher(
        "category_filter",
        {"predicate_name": "IN_GENRE", "domain_label": "Movie", "range_label": ""},
    )

    assert "(c)" in cypher
    assert "(c:)" not in cypher


def test_missing_anchor_omits_the_property_map() -> None:
    cypher = render_cypher("lookup", {"predicate_name": "title", "domain_label": "Movie"})

    assert "{" not in cypher
    assert cypher.startswith("MATCH (x:Movie)")


def test_string_fillers_are_escaped() -> None:
    cypher = render_cypher(
        "lookup",
        {
            "predicate_name": "title",
            "domain_label": "Movie",
            "anchor_property": "name",
            "filler_value": 'The "Matrix"',
        },
    )

    assert r"\"Matrix\"" in cypher
