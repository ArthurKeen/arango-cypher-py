"""Entity resolution against a live ArangoDB.

The unit tests (``tests/test_nl2cypher_entity_resolution.py``) answer the
resolver's AQL with a fake that cannot parse it, so a query the server rejects
passes there. The resolver logs a rejected scan and reports no match, so the
failure is silent: the first version of the word-level pass bound a parameter
its query did not use (ERR 1552), and every typo it was meant to catch went
unresolved. Each test here runs every scan for real and checks none failed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.nl2cypher import EntityResolver
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.test_nl2cypher_entity_resolution import shop_mapping

pytestmark = pytest.mark.integration

_DB = "arango_cypher_entity_resolution_it"
_TITLES = ("The Matrix", "The Matrix Reloaded", "The Matrix Revolutions", "Forrest Gump")
_NAMES = ("Tom Hanks", "Keanu Reeves")


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        # The collections and fields the movies_pg mapping names.
        database.create_collection("movies").insert_many([{"title": t} for t in _TITLES])
        database.create_collection("persons").insert_many([{"name": n} for n in _NAMES])
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(scope="module")
def shop_db(db: Any) -> Iterator[Any]:
    """Customers and suppliers in the same database, both with one in Germany,
    for :func:`shop_mapping`."""
    db.create_collection("customers").insert_many(
        [
            {"companyName": "Alfreds Futterkiste", "country": "Germany"},
            {"companyName": "Around the Horn", "country": "UK"},
        ]
    )
    db.create_collection("suppliers").insert_many(
        [
            {"companyName": "Heli Süßwaren", "country": "Germany"},
            {"companyName": "Tokyo Traders", "country": "Japan"},
        ]
    )
    return db


def resolve(
    db: Any, question: str, caplog: pytest.LogCaptureFixture, mapping: Any = None
) -> list[tuple[str, str, str]]:
    with caplog.at_level(logging.INFO, logger="arango_cypher.nl2cypher.entity_resolution"):
        hits = EntityResolver(db=db, mapping=mapping or mapping_bundle_for("movies_pg")).resolve(question)
    failed = [r.getMessage() for r in caplog.records if "scan failed" in r.getMessage()]
    assert not failed, failed
    return [(h.label, h.property, h.value) for h in hits]


def test_an_exact_name_resolves(db: Any, caplog: pytest.LogCaptureFixture) -> None:
    assert resolve(db, "What did Tom Hanks act in?", caplog) == [("Person", "name", "Tom Hanks")]


def test_a_typo_in_the_whole_value_resolves_by_the_fuzzy_pass(
    db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    assert resolve(db, 'Who acted in "Forest Gump"?', caplog) == [("Movie", "title", "Forrest Gump")]


def test_a_typo_in_one_word_of_a_title_resolves_to_the_shortest(
    db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Pass 2 cannot match: "matrx" is 5 characters, "the matrix" 10. All three
    # Matrix titles contain the near word; the shortest is the one meant.
    assert resolve(db, "Who acted in the Matrx?", caplog) == [("Movie", "title", "The Matrix")]


def test_a_typo_across_two_words_resolves(db: Any, caplog: pytest.LogCaptureFixture) -> None:
    assert resolve(db, 'Who acted in "Matrx Reloded"?', caplog) == [("Movie", "title", "The Matrix Reloaded")]


def test_a_short_near_word_does_not_resolve(db: Any, caplog: pytest.LogCaptureFixture) -> None:
    # "Hanx" is two edits from "hanks": 0.6, under the word pass's 0.8.
    assert resolve(db, 'Who is "Hanx"?', caplog) == []


def test_an_unrelated_mention_resolves_nothing(db: Any, caplog: pytest.LogCaptureFixture) -> None:
    assert resolve(db, 'Who acted in "Zebra Crossing"?', caplog) == []


@pytest.mark.parametrize(
    ("question", "label"),
    [("Show suppliers from Germny", "Supplier"), ("Show customers from Germny", "Customer")],
)
def test_a_value_two_labels_hold_resolves_on_the_label_the_question_names(
    shop_db: Any, caplog: pytest.LogCaptureFixture, question: str, label: str
) -> None:
    # Both collections hold "Germany", so both match equally; without the
    # question's label the first label in the mapping (Customer) would win.
    assert resolve(shop_db, question, caplog, mapping=shop_mapping()) == [(label, "country", "Germany")]
