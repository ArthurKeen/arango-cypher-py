from __future__ import annotations

import pytest
from arango_query_core import CoreError, ExtensionPolicy, MappingBundle
from arango_query_core.aql import AqlFragment
from arango_query_core.exec import AqlExecutor
from arango_query_core.extensions import ExtensionRegistry
from arango_query_core.mapping import MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.core import _unique_projection_key
from arango_cypher._translate_v0.naming import _affix_name, _rewrite_vars
from arango_cypher.api import TranspiledQuery
from arango_cypher.translate_v0 import (
    _aql_collection_ref,
    _compile_type_of_relationship,
    _pick_bind_key,
    _pick_fresh_var,
    translate_v0,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_transpiled_query_to_aql_query_roundtrip():
    tq = TranspiledQuery(aql="RETURN 1", bind_vars={"x": 1}, warnings=[], debug={"k": "v"})
    q = tq.to_aql_query()
    assert q.text == "RETURN 1"
    assert q.bind_vars == {"x": 1}
    assert q.debug == {"k": "v"}


@pytest.mark.parametrize(
    "cypher,mapping",
    [
        ("", mapping_bundle_for("pg")),
        ("   ", mapping_bundle_for("pg")),
        ("MATCH (n:User) RETURN n", None),
    ],
)
def test_translate_api_input_validation(cypher: str, mapping):
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)  # type: ignore[arg-type]
    assert e.value.code == "INVALID_ARGUMENT"


def test_translate_v0_requires_mapping():
    with pytest.raises(CoreError) as e:
        translate_v0("MATCH (n:User) RETURN n", mapping=None)  # type: ignore[arg-type]
    assert e.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "cypher,code,msg_sub",
    [
        # Bare ``RETURN <literal>`` is now supported (→ AQL ``RETURN <expr>``);
        # only the row-stream-only forms remain rejected without a source.
        ("RETURN *", "UNSUPPORTED", "MATCH"),
        ("MATCH (n) RETURN n", "UNSUPPORTED", "label is required"),
        (
            # One name for a node and a relationship: invalid Cypher.
            "MATCH (u:User)-[u:FOLLOWS]->(v:User) RETURN u",
            "CYPHER_SEMANTIC_ERROR",
            "VariableTypeConflict",
        ),
        (
            "MATCH (u:User)-[:FOLLOWS $props]->(v:User) RETURN u",
            "NOT_IMPLEMENTED",
            "Parameterized relationship properties",
        ),
    ],
)
def test_translate_fail_fast_errors(cypher: str, code: str, msg_sub: str):
    mapping = mapping_bundle_for("pg")
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)
    assert e.value.code == code
    assert msg_sub.lower() in str(e.value).lower()


def test_aql_executor_execute_forwards_to_python_arango():
    calls: list[tuple[str, dict[str, object], int | None, dict[str, object]]] = []

    class FakeAql:
        def execute(self, text, *, bind_vars=None, batch_size=None, **kwargs):
            calls.append((text, bind_vars or {}, batch_size, kwargs))
            return [{"ok": True}]

    class FakeDb:
        aql = FakeAql()

    ex = AqlExecutor(db=FakeDb())
    out = ex.execute(
        query=TranspiledQuery(aql="RETURN 1", bind_vars={"x": 1}, warnings=[]).to_aql_query(),
        batch_size=123,
        ttl=5,
    )
    assert out == [{"ok": True}]
    assert calls == [("RETURN 1", {"x": 1}, 123, {"ttl": 5})]


def test_aql_fragment_add_joins_and_merges():
    a = AqlFragment("FOR x IN 1..2", {"a": 1})
    b = AqlFragment("  RETURN x", {"b": 2})
    c = a + b
    assert c.text == "FOR x IN 1..2\nRETURN x" or c.text == "FOR x IN 1..2\n  RETURN x"
    assert c.bind_vars == {"a": 1, "b": 2}


def test_aql_fragment_add_non_fragment_returns_type_error():
    a = AqlFragment("RETURN 1")
    with pytest.raises(TypeError):
        _ = a + 1  # type: ignore[operator]


def test_aql_fragment_bind_var_collision_raises():
    a = AqlFragment("RETURN 1", {"x": 1})
    b = AqlFragment("RETURN 2", {"x": 2})
    with pytest.raises(CoreError) as e:
        _ = a + b
    assert e.value.code == "BIND_VAR_COLLISION"


def test_extension_policy_disabled():
    pol = ExtensionPolicy(enabled=False)
    with pytest.raises(CoreError) as e:
        pol.check_allowed("x")
    assert e.value.code == "EXTENSIONS_DISABLED"


def test_extension_policy_denylist():
    pol = ExtensionPolicy(enabled=True, denylist={"x"})
    with pytest.raises(CoreError) as e:
        pol.check_allowed("x")
    assert e.value.code == "EXTENSION_DENIED"


def test_extension_policy_allowlist():
    pol = ExtensionPolicy(enabled=True, allowlist={"x"})
    with pytest.raises(CoreError) as e:
        pol.check_allowed("y")
    assert e.value.code == "EXTENSION_NOT_ALLOWED"
    pol.check_allowed("x")


def test_extension_registry_unknown_function_and_procedure():
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    with pytest.raises(CoreError) as e1:
        reg.compile_function("missingFn", call_ast=None, ctx=None)
    assert e1.value.code == "UNKNOWN_EXTENSION"
    with pytest.raises(CoreError) as e2:
        reg.compile_procedure("missingProc", call_ast=None, ctx=None)
    assert e2.value.code == "UNKNOWN_EXTENSION"


def test_extension_registry_compiles_registered_function_and_procedure():
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    reg.register_function("f", lambda call_ast, ctx: ("ok_fn", call_ast, ctx))
    reg.register_procedure("p", lambda call_ast, ctx: ("ok_proc", call_ast, ctx))
    assert reg.compile_function("f", call_ast={"x": 1}, ctx={"y": 2}) == ("ok_fn", {"x": 1}, {"y": 2})
    assert reg.compile_procedure("p", call_ast={"x": 1}, ctx={"y": 2}) == ("ok_proc", {"x": 1}, {"y": 2})


def test_mapping_resolver_mapping_not_found():
    bundle = MappingBundle(
        conceptual_schema={}, physical_mapping={"entities": {}, "relationships": {}}, metadata={}
    )
    r = MappingResolver(bundle)
    with pytest.raises(CoreError) as e:
        r.resolve_entity("Missing")
    assert e.value.code == "MAPPING_NOT_FOUND"
    with pytest.raises(CoreError) as e2:
        r.resolve_relationship("MISSING_REL")
    assert e2.value.code == "MAPPING_NOT_FOUND"


def test_mapping_resolver_non_dict_sections_are_treated_as_empty():
    bundle = MappingBundle(
        conceptual_schema={}, physical_mapping={"entities": [], "relationships": None}, metadata={}
    )
    r = MappingResolver(bundle)
    with pytest.raises(CoreError) as e:
        r.resolve_entity("User")
    assert e.value.code == "MAPPING_NOT_FOUND"


@pytest.mark.parametrize(
    "mutate,cypher,code_contains",
    [
        (
            lambda pm: pm["entities"]["User"].__setitem__("style", "BOGUS"),
            "MATCH (n:User) RETURN n",
            "Unsupported entity mapping style",
        ),
        (
            lambda pm: pm["entities"]["User"].__setitem__("collectionName", ""),
            "MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN u",
            "Invalid entity mapping collectionName",
        ),
        (
            lambda pm: pm["relationships"]["FOLLOWS"].__setitem__("edgeCollectionName", ""),
            "MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN u",
            "Invalid relationship mapping collection",
        ),
    ],
)
def test_translate_invalid_mapping_errors(mutate, cypher: str, code_contains: str):
    base = mapping_bundle_for("pg")
    pm = {
        "entities": {k: dict(v) for k, v in base.physical_mapping.get("entities", {}).items()},
        "relationships": {k: dict(v) for k, v in base.physical_mapping.get("relationships", {}).items()},
    }
    mutate(pm)
    bad = MappingBundle(conceptual_schema=base.conceptual_schema, physical_mapping=pm, metadata=base.metadata)
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=bad)
    assert e.value.code == "INVALID_MAPPING"
    assert code_contains.lower() in str(e.value).lower()


def test_pick_fresh_var_increments_suffix_until_free():
    forbidden = {"x", "x_1", "x_2"}
    assert _pick_fresh_var("x", forbidden_vars=forbidden) == "x_3"


def test_pick_bind_key_increments_numeric_suffix_until_free():
    bind_vars: dict[str, object] = {"@collection": "users", "@collection2": "users"}
    assert _pick_bind_key("@collection", bind_vars) == "@collection3"


def test_pick_bind_key_keeps_only_characters_aql_accepts_in_a_bind_name():
    """A quoted or non-ASCII Cypher variable embedded in a key was ERR 1501."""
    assert _pick_bind_key("`a b`TypeField", {}) == "_a_b_TypeField"
    assert _pick_bind_key("@`a b`Member", {}) == "@_a_b_Member"
    assert _pick_bind_key("äMembers", {}) == "_Members"
    assert _pick_bind_key("`a b`Members", {"_a_b_Members": ["post"]}) == "_a_b_Members2"


def test_aql_collection_ref_requires_at_prefix():
    with pytest.raises(CoreError) as e:
        _aql_collection_ref("collection")
    assert e.value.code == "INTERNAL_ERROR"


def test_compile_type_of_relationship_generic_requires_rel_type_field():
    with pytest.raises(CoreError) as e:
        _compile_type_of_relationship("FOLLOWS", "r", "GENERIC_WITH_TYPE", bind_vars={})
    assert e.value.code == "INVALID_MAPPING"


def test_compile_type_of_relationship_generic_reads_rel_type_field():
    assert (
        _compile_type_of_relationship("FOLLOWS", "r", "GENERIC_WITH_TYPE", bind_vars={"relTypeField": "type"})
        == "r[@relTypeField]"
    )


def test_rewrite_vars_never_rewrites_a_replacement():
    """Key by key, ``a.name`` -> ``name`` was rewritten again to ``name_g1``."""
    env = {"a.name": "name", "name": "name_g1", "b.name": "name_g1"}
    assert _rewrite_vars("a.name ASC, b.name", env) == "name ASC, name_g1"


def test_rewrite_vars_leaves_bind_parameters_alone():
    """Renaming a CALL subquery's ``id`` must not turn ``@id`` into ``@id_1``."""
    env = {"id": "id_1", "collection": "collection_1"}
    assert _rewrite_vars("FOR x IN @@collection FILTER x.id == @id RETURN id", env) == (
        "FOR x IN @@collection FILTER x.id == @id RETURN id_1"
    )


def test_an_aggregate_column_is_named_after_the_cypher_variable():
    """``a`` bound again after a WITH is ``a_1`` in AQL; its column is still ``a``."""
    out = translate(
        "MATCH (a:User)-[:FOLLOWS]->(b:User) WITH a, count(b) AS c "
        "MATCH (a)-[:FOLLOWS]->(b2:User) RETURN a, count(b2) AS d",
        mapping=mapping_bundle_for("pg"),
    )
    assert "RETURN {a: " in out.aql, out.aql


def test_unique_projection_key_prefers_var_and_property_then_a_suffix():
    used: set[str] = set()
    assert _unique_projection_key("name", "a.name", used) == "name"
    assert _unique_projection_key("name", "b.name", used) == "b_name"
    assert _unique_projection_key("name", "upper(c.name)", used) == "name_2"


def test_affix_name_composes_inside_backticks():
    assert _affix_name("n", prefix="_", suffix="_distinct") == "_n_distinct"
    assert _affix_name("`a b`", prefix="_", suffix="_distinct") == "`_a b_distinct`"
    assert _affix_name("`like`", suffix="_1") == "`like_1`"


def test_rewrite_vars_leaves_quoted_names_and_property_names_alone():
    env = {"a": "a_1", "`like`": "`like_1`"}
    assert _rewrite_vars("a.a + `a b`.x + `like`.y", env) == "a_1.a + `a b`.x + `like_1`.y"


def test_aql_reserved_variables_and_property_keys_are_quoted():
    """LDBC BI6 names a relationship ``like``: ``FOR person3, like IN`` is ERR 1501."""
    out = translate(
        "MATCH (a:User)-[like:FOLLOWS]->(b:User) WHERE b.limit > 1 RETURN {in: like.since} AS m",
        mapping=mapping_bundle_for("pg"),
    )
    assert "FOR b, `like` IN 1..1 OUTBOUND a" in out.aql
    assert "b.`limit` > 1" in out.aql
    assert "{`in`: `like`.since}" in out.aql


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{score: score, x: 1}", "{score: score_4, x: 1}"),
        ("{x: 1, score: score}", "{x: 1, score: score_4}"),
        ("c ? score : 0", "c ? score_4 : 0"),
        ("x ? 1 : score", "x ? 1 : score_4"),
        ("score == 'score'", "score_4 == 'score'"),
        ('score == "a score"', 'score_4 == "a score"'),
        ("f(score, a.name)", "f(score_4, name)"),
    ],
)
def test_rewrite_vars_leaves_object_keys_and_strings_alone(text: str, expected: str) -> None:
    assert _rewrite_vars(text, {"score": "score_4", "a": "a_1", "a.name": "name"}) == expected
