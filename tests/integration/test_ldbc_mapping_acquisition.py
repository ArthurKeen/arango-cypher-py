"""LDBC live mapping coverage: the bundle acquired from each loaded layout,
diffed against its committed fixture (PRD §12.1 LDBC-2; plan §5.5).

Every difference must be a known analyzer gap, filed upstream and listed in
``tests/ldbc/mapping_diff.KNOWN_GAPS``. A new difference is either an analyzer
regression or a fixture error, and fails here. Needs the optional schema
analyzer (``arango-cypher-py[analyzer]``); skipped without it.
"""

from __future__ import annotations

import pytest

from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.ldbc.execution import loaded_databases
from tests.ldbc.mapping_diff import KNOWN_GAPS, differences

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def acquired(ldbc_arango_url):
    pytest.importorskip("schema_analyzer", reason="the schema analyzer is an optional extra")
    from arango_cypher.schema_acquire import acquire_mapping_bundle

    return {key: acquire_mapping_bundle(db) for key, db in loaded_databases(ldbc_arango_url).items()}


def test_every_difference_from_the_fixture_is_a_known_analyzer_gap(acquired):
    unexplained = {
        f"{dataset}/{layout}": [
            d
            for kind, d in differences(bundle, mapping_bundle_for(f"ldbc_{layout}"))
            if kind == "unexplained"
        ]
        for (dataset, layout), bundle in acquired.items()
    }
    unexplained = {k: v for k, v in unexplained.items() if v}
    assert not unexplained, (
        "the acquired LDBC mapping differs from its fixture in ways no filed analyzer issue explains "
        f"(known: {', '.join(KNOWN_GAPS)}):\n"
        + "\n".join(f"  {k}: {d}" for k, ds in unexplained.items() for d in ds)
    )
