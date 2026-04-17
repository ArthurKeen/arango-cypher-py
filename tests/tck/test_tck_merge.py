from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tck.gherkin import parse_feature
from tests.tck.runner import run_scenario

_FEATURES_DIR = Path(__file__).resolve().parent / "features"


def _collect_scenarios(subdirs: list[str], pattern: str):
    items = []
    for subdir in subdirs:
        feat_dir = _FEATURES_DIR / subdir if subdir else _FEATURES_DIR
        if not feat_dir.exists():
            continue
        for feat_file in sorted(feat_dir.rglob("*.feature")):
            if pattern.lower() not in feat_file.name.lower():
                continue
            feat = parse_feature(feat_file)
            for sc in feat.scenarios:
                items.append((feat_file.stem, sc))
    return items


_SCENARIOS = _collect_scenarios(["clauses/merge"], "merge")


@pytest.mark.tck
@pytest.mark.integration
@pytest.mark.parametrize(
    "feature_name,scenario",
    _SCENARIOS,
    ids=[f"{fn}::{sc.name}" for fn, sc in _SCENARIOS],
)
def test_tck_merge_scenario(feature_name, scenario):
    if os.environ.get("RUN_TCK") != "1":
        pytest.skip("Set RUN_TCK=1")
    outcome = run_scenario(scenario, db_name="tck_merge_db", mapping_fixture="lpg")
    if outcome.status == "skipped":
        pytest.skip(outcome.reason or "skipped")
    elif outcome.status == "failed":
        pytest.fail(outcome.reason or "failed")
