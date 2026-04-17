from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tck.gherkin import parse_feature
from tests.tck.runner import run_scenario

_FEATURES_DIR = Path(__file__).resolve().parent / "features"


def _collect_match_scenarios():
    """Collect all scenarios from Match*.feature files."""
    items = []
    for feat_file in sorted(_FEATURES_DIR.rglob("*.feature")):
        if feat_file.name == "sample.feature":
            continue
        if "Match" not in feat_file.name and "match" not in feat_file.name:
            continue
        feat = parse_feature(feat_file)
        for sc in feat.scenarios:
            items.append((feat_file.stem, sc))
    return items


_MATCH_SCENARIOS = _collect_match_scenarios()


@pytest.mark.tck
@pytest.mark.integration
@pytest.mark.parametrize(
    "feature_name,scenario",
    _MATCH_SCENARIOS,
    ids=[f"{fn}::{sc.name}" for fn, sc in _MATCH_SCENARIOS],
)
def test_tck_match_scenario(feature_name, scenario):
    if os.environ.get("RUN_TCK") != "1":
        pytest.skip("Set RUN_TCK=1")
    db_name = "tck_match_db"
    outcome = run_scenario(scenario, db_name=db_name, mapping_fixture="lpg")
    if outcome.status == "skipped":
        pytest.skip(outcome.reason or "skipped")
    elif outcome.status == "failed":
        pytest.fail(outcome.reason or "failed")
