"""UNION bind-variable naming: what the golden cases cannot express.

Each UNION branch is translated on its own and conflicting internal names are
renamed afterwards (see ``_merge_bind_vars``). A *user* parameter must never be
renamed — that would rebind the user's own reference to an internal value, a
silent wrong result — so a name clash is refused with a clear error.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher._translate_v0.core import _rename_bind_references
from arango_cypher.api import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

CLASH = (
    "MATCH (n:User) WHERE n.name = $typeValue RETURN n.name AS x "
    "UNION MATCH (n:Doc) WHERE n.name = $typeValue RETURN n.name AS x"
)


@pytest.mark.parametrize("fixture", ["lpg", "hybrid"])
def test_a_user_parameter_named_like_an_internal_one_is_refused(fixture: str) -> None:
    with pytest.raises(CoreError, match=r"Parameter \$typeValue has the same name as a bind variable"):
        translate(CLASH, mapping=mapping_bundle_for(fixture), params={"typeValue": "vdoc"})


def test_a_shared_user_parameter_with_one_value_stays_one_bind_variable() -> None:
    q = "MATCH (n:User) WHERE n.name = $q RETURN n.name AS x UNION MATCH (n:Person) WHERE n.name = $q RETURN n.name AS x"
    t = translate(q, mapping=mapping_bundle_for("pg"), params={"q": "alice"})
    assert t.bind_vars["q"] == "alice"
    assert t.aql.count("@q") == 2
    assert not any(k.startswith("q_u") for k in t.bind_vars)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FOR n IN @@collection RETURN n", "FOR n IN @@collection_u1 RETURN n"),
        ("FILTER n.name == 'a @@collection b'", "FILTER n.name == 'a @@collection b'"),
        ('FILTER n.name == "x @@collection"', 'FILTER n.name == "x @@collection"'),
        ("FILTER n.name == 'it\\'s @@collection'", "FILTER n.name == 'it\\'s @@collection'"),
        ("RETURN n.`@@collection`", "RETURN n.`@@collection`"),
        ("RETURN n.´@@collection´", "RETURN n.´@@collection´"),
        ("FOR n IN @@collection2 RETURN n", "FOR n IN @@collection2 RETURN n"),
        (
            "FOR n IN @@collection FILTER n.a == 'q' RETURN 'x'",
            "FOR n IN @@collection_u1 FILTER n.a == 'q' RETURN 'x'",
        ),
    ],
)
def test_renaming_skips_quoted_text_and_other_names(text: str, expected: str) -> None:
    assert _rename_bind_references(text, "@collection", "@collection_u1") == expected


def test_a_plain_key_does_not_match_inside_a_collection_reference() -> None:
    assert _rename_bind_references(
        "FOR n IN @@collection FILTER n.c == @collection", "collection", "c_u1"
    ) == ("FOR n IN @@collection FILTER n.c == @c_u1")
