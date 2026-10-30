"""Unit tests for ``tests.ldbc.mapping_diff``."""

from __future__ import annotations

import copy
import dataclasses

from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.ldbc.mapping_diff import KNOWN_GAPS, differences


def _edit(bundle, fn):
    physical = copy.deepcopy(bundle.physical_mapping)
    conceptual = copy.deepcopy(bundle.conceptual_schema)
    fn(physical, conceptual)
    return dataclasses.replace(bundle, physical_mapping=physical, conceptual_schema=conceptual)


def test_a_fixture_does_not_differ_from_itself():
    fixture = mapping_bundle_for("ldbc_pg")
    assert differences(fixture, fixture) == []


def test_each_known_gap_is_classified():
    fixture = mapping_bundle_for("ldbc_pg")

    def analyzer_like(physical, conceptual):
        del physical["entities"]["Message"]
        physical["entities"]["Person"]["properties"]["id"]["type"] = "string"
        for side in ("domain", "range"):
            physical["relationships"]["HAS_CREATOR"].pop(side, None)
        conceptual["relationships"] = [
            r for r in conceptual.get("relationships", []) if r.get("type") != "HAS_CREATOR"
        ]
        physical["relationships"]["IS_LOCATED_IN"]["properties"].pop("creationDate", None)

    kinds = {kind for kind, _ in differences(_edit(fixture, analyzer_like), fixture)}
    assert {"missing_supertype", "missing_endpoints", "property_type", "sparse_property"} <= kinds
    assert kinds <= set(KNOWN_GAPS) | {"unexplained"}


def test_a_wrong_collection_is_unexplained():
    fixture = mapping_bundle_for("ldbc_pg")

    def wrong(physical, _conceptual):
        physical["entities"]["Person"]["collectionName"] = "people"

    found = differences(_edit(fixture, wrong), fixture)
    assert ("unexplained", "entity Person.collection: fixture 'person', acquired 'people'") in found
