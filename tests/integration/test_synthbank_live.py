"""Synthbank generation against live databases (steps 3–5 of the port).

The binder's claims are only checkable against real data: that every kept
gold example translates and executes non-empty, that anchors identify one
entity, that the same schema in two physical models yields the same bank, and
that a generated bank actually reaches the NL prompt. Each dataset gets its
own database, created and dropped here, so the suite leaves no residue.

The CLI test drives ``arango-cypher-py synthbank`` end to end, which is also
how a bank is produced for real onboarding.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from arango import ArangoClient
from arango_query_core.nl.fewshot import FewShotIndex
from arango_query_core.nl.synthbank import SHAPE_CATALOG

from arango_cypher.nl2cypher.synthbank_binder import (
    TranspilingExecutor,
    _execution_nonempty,
    generate_bank_with_report,
    write_bank,
)
from arango_cypher.schema_acquire import acquire_mapping_bundle
from tests.integration.datasets import (
    seed_movies_lpg_dataset,
    seed_movies_pg_dataset,
    seed_northwind_dataset,
)

pytestmark = pytest.mark.integration

_PREFIX = "arango_cypher_synthbank_it_"
_SEEDS: dict[str, Callable[[Any], None]] = {
    "movies_pg": seed_movies_pg_dataset,
    "movies_lpg": seed_movies_lpg_dataset,
    "northwind": seed_northwind_dataset,
}
#: Keys that would let schema acquisition pick an LLM; unset for determinism.
_LLM_ENV = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


def _url() -> str:
    return os.environ.get("ARANGO_URL", "http://localhost:8529")


def _creds() -> tuple[str, str]:
    return os.environ.get("ARANGO_USER", "root"), os.environ.get("ARANGO_PASS", "openSesame")


@pytest.fixture(scope="module")
def datasets() -> Iterator[dict[str, tuple[Any, Any]]]:
    """``{name: (db, bundle)}`` for every seeded dataset."""
    user, password = _creds()
    client = ArangoClient(hosts=_url())
    sys_db = client.db("_system", username=user, password=password)
    out: dict[str, tuple[Any, Any]] = {}
    with pytest.MonkeyPatch.context() as mp:
        for name in _LLM_ENV:
            mp.delenv(name, raising=False)
        try:
            for name, seed in _SEEDS.items():
                db_name = _PREFIX + name
                if sys_db.has_database(db_name):
                    sys_db.delete_database(db_name)
                sys_db.create_database(db_name)
                db = client.db(db_name, username=user, password=password)
                seed(db)
                out[name] = (db, acquire_mapping_bundle(db))
            yield out
        finally:
            for name in _SEEDS:
                if sys_db.has_database(_PREFIX + name):
                    sys_db.delete_database(_PREFIX + name)


@pytest.fixture(scope="module")
def banks(datasets: dict[str, tuple[Any, Any]]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    return {
        name: generate_bank_with_report(bundle, TranspilingExecutor(db, bundle))
        for name, (db, bundle) in datasets.items()
    }


@pytest.mark.parametrize("name", sorted(_SEEDS))
def test_every_kept_example_executes_non_empty(datasets, banks, name: str) -> None:
    """The acceptance bar from the SPARQL spec: zero empty-result examples."""
    db, bundle = datasets[name]
    executor = TranspilingExecutor(db, bundle)
    bank, _report = banks[name]

    assert bank["examples"], f"{name}: no examples generated"
    empty = [e["question"] for e in bank["examples"] if not _execution_nonempty(executor, e["cypher"])]
    assert not empty, f"{name}: gold examples executing empty: {empty}"


@pytest.mark.parametrize("name", sorted(_SEEDS))
def test_every_catalog_shape_is_accounted_for(banks, name: str) -> None:
    """A shape yielding nothing is a recorded finding, never a silent gap."""
    _bank, report = banks[name]

    silent = [t.name for t in SHAPE_CATALOG if report[t.name]["kept"] == 0 and not report[t.name]["reasons"]]
    assert not silent, f"{name}: shapes with no yield and no reason: {silent}"


def test_the_richest_schema_covers_at_least_seven_shapes(banks) -> None:
    """The SPARQL spec's REQ-1 bar, on the fixture with the most predicates."""
    bank, _report = banks["northwind"]

    assert len({e["shape"] for e in bank["examples"]}) >= 7


def test_the_same_schema_in_two_physical_models_yields_the_same_bank(banks) -> None:
    """Sampling goes through the transpiler, so PG vs LPG must not matter."""
    assert banks["movies_pg"][0] == banks["movies_lpg"][0]


def test_generation_is_deterministic(datasets, banks) -> None:
    db, bundle = datasets["northwind"]

    again, _report = generate_bank_with_report(bundle, TranspilingExecutor(db, bundle))

    assert again == banks["northwind"][0]


@pytest.mark.parametrize("name", sorted(_SEEDS))
def test_anchored_lookups_name_exactly_one_entity(datasets, banks, name: str) -> None:
    """An anchor that matches several entities would make the gold ambiguous."""
    db, bundle = datasets[name]
    executor = TranspilingExecutor(db, bundle)
    lookups = [e for e in banks[name][0]["examples"] if e["shape"] == "lookup"]

    for example in lookups:
        pattern = example["cypher"].splitlines()[0]  # MATCH (x:Label {anchor: "value"})
        count = executor.run(f"{pattern} RETURN count(x) AS n")[0]
        count = count["n"] if isinstance(count, dict) else count
        assert count == 1, f"{name}: {pattern!r} matches {count} entities"


class _ScriptedProvider:
    """Mirrors ``LLMProvider.generate(system, user) -> (text, usage)``."""

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        question = user.split("\n\n", 1)[0]
        return f"Please tell me: {question}", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def test_paraphrases_flow_into_a_bank_the_index_retrieves(datasets, tmp_path: Path) -> None:
    db, bundle = datasets["movies_pg"]
    bank, _report = generate_bank_with_report(
        bundle,
        TranspilingExecutor(db, bundle),
        provider=_ScriptedProvider(),
        k_paraphrases=1,
        shapes={"lookup"},
    )
    path = tmp_path / "movies.yml"

    written = write_bank(bank, path)
    index = FewShotIndex.from_corpus_files([path], mode="bm25")

    assert written == 2 * len(bank["examples"])
    first = bank["examples"][0]
    assert (first["paraphrases"][0], first["cypher"]) in index.examples


def test_the_cli_writes_a_loadable_bank(datasets, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from arango_cypher.cli import app

    parsed = urlparse(_url())
    user, password = _creds()
    output, report = tmp_path / "northwind.yml", tmp_path / "report.json"

    result = CliRunner().invoke(
        app,
        [
            "synthbank",
            "--output", str(output),
            "--report", str(report),
            "--host", parsed.hostname or "localhost",
            "--port", str(parsed.port or 8529),
            "--db", _PREFIX + "northwind",
            "--user", user,
            "--password", password,
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert FewShotIndex.from_corpus_files([output], mode="bm25").examples
    assert report.is_file()
