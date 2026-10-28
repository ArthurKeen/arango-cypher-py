"""``… CREATE … WITH … MATCH … CREATE …``: CREATE before a WITH (LDBC INS1/4/6/7).

ArangoDB forbids writing a collection twice in one query, or reading it after
writing it (ERR 1579). Inserting where each CREATE stands does both as soon as
a later part creates into the same collection (every LPG edge lives in one
collection) or reads it (``MATCH (t:Tag …)`` reads the LPG node collection).
So reads come first and every collection is written once:

1. The query compiles as a chain of stages, one per CREATE group:
   ``LET _s1 = (<reads> LET p = <new document> LET _e0 = <new edge> RETURN
   {<variables>, _e0})``, then ``LET _s2 = (FOR _r IN _s1 <rebind> <WITH>
   <reads> … RETURN …)``. A new node gets its key up front (``UUID()``), so
   an edge can name it before it exists. A stage that matches nothing does
   not undo what an earlier stage created, as in Cypher.
2. Every new node and edge is then inserted with one INSERT per collection.
3. A RETURN reads the last stage's rows.

A part that reads what an earlier CREATE wrote — a MATCH on a created
variable, on a created label or over a created relationship type — would see
nothing here, where Cypher sees the new data, so it is refused: that needs
several statements (PRD §16).
"""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var, _strip_label_backticks
from .writes import create_node_doc, create_ops, create_rel_doc


def _core(name: str) -> Any:
    from . import core

    return getattr(core, name)


def is_create_pipeline(mpq: CypherParser.OC_MultiPartQueryContext) -> bool:
    """True when a CREATE stands before a WITH and every updating clause is a CREATE."""
    if not mpq.oC_UpdatingClause():
        return False
    tail = mpq.oC_SinglePartQuery()
    clauses = [*mpq.oC_UpdatingClause(), *((tail.oC_UpdatingClause() or []) if tail is not None else [])]
    return all(uc.oC_Create() is not None for uc in clauses)


def _match_names(match_ctx: CypherParser.OC_MatchContext) -> tuple[set[str], set[str], set[str]]:
    """Variables, labels and relationship types a MATCH names."""
    variables: set[str] = set()
    labels: set[str] = set()
    types: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, CypherParser.OC_NodePatternContext):
            if node.oC_Variable() is not None:
                variables.add(node.oC_Variable().getText().strip())
            if node.oC_NodeLabels() is not None:
                labels.update(
                    _strip_label_backticks(nl.oC_LabelName().getText().strip())
                    for nl in node.oC_NodeLabels().oC_NodeLabel()
                )
        elif isinstance(node, CypherParser.OC_RelationshipDetailContext):
            if node.oC_Variable() is not None:
                variables.add(node.oC_Variable().getText().strip())
            if node.oC_RelationshipTypes() is not None:
                types.update(
                    _strip_label_backticks(t.getText().strip())
                    for t in node.oC_RelationshipTypes().oC_RelTypeName()
                )
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i))

    walk(match_ctx.oC_Pattern())
    if match_ctx.oC_Where() is not None:
        walk(match_ctx.oC_Where())
    return variables, labels, types


class _Pipeline:
    """The stages of a CREATE pipeline and what each one created."""

    def __init__(self, resolver: MappingResolver, bind_vars: dict[str, Any]) -> None:
        self.resolver = resolver
        self.bind_vars = bind_vars
        self.out: list[str] = []
        self.lines: list[str] = []
        self.var_env: dict[str, str] = {}
        self.forbidden: set[str] = set()
        self.stage = 0
        self.seen_match = False
        self.pending_creates = False
        # (stage variable, key in the stage's rows, collection bind key, is an edge)
        self.records: list[tuple[str, str, str, bool]] = []
        self.created_vars: set[str] = set()
        self.created_labels: set[str] = set()
        self.created_types: set[str] = set()
        self.edge_count = 0

    def refuse_reading_own_writes(self, match_ctx: CypherParser.OC_MatchContext) -> None:
        variables, labels, types = _match_names(match_ctx)
        clash = (
            (variables & self.created_vars) or (labels & self.created_labels) or (types & self.created_types)
        )
        if clash:
            raise CoreError(
                f"A MATCH after CREATE reads what the query created ({', '.join(sorted(clash))}); "
                "that needs several statements, which the translator does not emit",
                code="NOT_IMPLEMENTED",
            )

    def read(self, rc: CypherParser.OC_ReadingClauseContext) -> None:
        uw = rc.oC_Unwind()
        if uw is not None:
            self.var_env = _core("_compile_pipeline_unwind")(
                uw,
                bind_vars=self.bind_vars,
                forbidden_vars=self.forbidden,
                var_env=self.var_env,
                lines=self.lines,
            )
            return
        m = rc.oC_Match()
        if m is None:
            raise CoreError("Only MATCH and UNWIND are supported around CREATE", code="NOT_IMPLEMENTED")
        if m.OPTIONAL() is not None and not self.seen_match:
            raise CoreError("OPTIONAL MATCH cannot start a CREATE pipeline", code="NOT_IMPLEMENTED")
        self.refuse_reading_own_writes(m)
        if not self.seen_match and self.stage == 0 and not self.lines:
            self.lines, self.forbidden, self.var_env = _core("_compile_leading_match")(
                m, resolver=self.resolver, bind_vars=self.bind_vars
            )
        else:
            self.var_env, _ = _core("_compile_following_match")(
                m,
                resolver=self.resolver,
                bind_vars=self.bind_vars,
                forbidden_vars=self.forbidden,
                var_env=self.var_env,
                lines=self.lines,
            )
        self.seen_match = True

    def create(self, create_ctx: CypherParser.OC_CreateContext) -> None:
        before = dict(self.var_env)
        ops = create_ops(create_ctx, self.var_env)
        # An unnamed node is ``_anon``; an earlier stage's may still be declared.
        for op in ops:
            if op.kind == "node" and op.var in self.forbidden:
                fresh = _pick_fresh_var(op.var, forbidden_vars=set(self.forbidden) | set(self.var_env))
                del self.var_env[op.var]
                self.var_env[fresh] = fresh
                for other in ops:
                    if other.kind == "rel":
                        other.from_var = fresh if other.from_var == op.var else other.from_var
                        other.to_var = fresh if other.to_var == op.var else other.to_var
                op.var = fresh
        for op in ops:
            if op.kind == "node":
                doc, coll_key, _ = create_node_doc(
                    op.labels or [], op.node_ctx, resolver=self.resolver, bind_vars=self.bind_vars
                )
                name_key = _pick_bind_key(f"{coll_key[1:]}Name", self.bind_vars)
                self.bind_vars[name_key] = self.bind_vars[coll_key]
                key_var = f"_key_{op.var}".replace("`", "")
                self.forbidden.update({op.var, key_var})
                self.lines += [
                    f"  LET {key_var} = UUID()",
                    f'  LET {op.var} = MERGE({doc}, {{_key: {key_var}, _id: CONCAT(@{name_key}, "/", {key_var})}})',
                ]
                self.records.append(("", op.var, coll_key, False))
                self.created_vars.add(op.var)
                self.created_labels.update(_strip_label_backticks(lbl) for lbl in op.labels or [])
            else:
                from_ref = self.var_env.get(op.from_var, op.from_var)
                to_ref = self.var_env.get(op.to_var, op.to_var)
                doc, coll_key, _ = create_rel_doc(
                    op.rel_pat, from_ref, to_ref, resolver=self.resolver, bind_vars=self.bind_vars
                )
                named = bool(op.var) and not op.var.startswith("_c") and op.var not in before
                if named:
                    # A named relationship (``CREATE ()-[r:R]->()``) is its document.
                    record = _pick_fresh_var(op.var, forbidden_vars=set(self.forbidden))
                    self.created_vars.add(op.var)
                    self.var_env[op.var] = record
                else:
                    record = _pick_fresh_var(f"_e{self.edge_count}", forbidden_vars=set(self.forbidden))
                    self.edge_count += 1
                self.forbidden.add(record)
                self.lines.append(f"  LET {record} = {doc}")
                # Read back by the key the stage returns it under.
                self.records.append(("", op.var if named else record, coll_key, True))
                detail = op.rel_pat.oC_RelationshipDetail()
                self.created_types.update(
                    _strip_label_backticks(t.getText().strip())
                    for t in detail.oC_RelationshipTypes().oC_RelTypeName()
                )
        self.pending_creates = True

    def close_stage(self) -> str:
        """End the current stage: its rows carry every variable and new record."""
        self.stage += 1
        name = f"_s{self.stage}"
        fields = [f"{cy}: {aql}" for cy, aql in self.var_env.items()]
        stage_records = [r for r in self.records if r[0] == ""]
        fields += [f"{rec}: {rec}" for _, rec, _, _ in stage_records if rec not in self.var_env]
        self.records = [(name if s == "" else s, rec, key, edge) for s, rec, key, edge in self.records]
        body = self.lines or ["  LET _row = true"]
        self.out += [f"LET {name} = (", *body, f"  RETURN {{{', '.join(fields)}}}", ")"]
        self.lines = [f"  FOR _r IN {name}"] + [
            f"    LET {aql} = _r.{cy}" for cy, aql in self.var_env.items()
        ]
        self.pending_creates = False
        return name

    def writes(self) -> list[str]:
        """One INSERT per collection over every stage's records, nodes first."""
        statements: list[str] = []
        for want_edges in (False, True):
            # collection -> (the bind key the document builder gave it, sources)
            by_collection: dict[str, tuple[str, list[str]]] = {}
            for stage, rec, coll_key, is_edge in self.records:
                if is_edge == want_edges:
                    entry = by_collection.setdefault(self.bind_vars[coll_key], (coll_key, []))
                    entry[1].append(f"{stage}[*].{rec}")
            for key, sources in by_collection.values():
                docs = sources[0] if len(sources) == 1 else f"FLATTEN([{', '.join(sources)}])"
                doc = "UNSET(_d, '_id')" if not want_edges else "_d"
                statements.append(f"FOR _d IN {docs} INSERT {doc} INTO {_aql_collection_ref(key)}")
        return statements

    def drop_unused_collection_keys(self, text: str) -> None:
        """A builder binds a collection key per document; one per collection is
        written to, and AQL refuses a bind parameter it is passed but never
        reads (ERR 1552)."""
        for _, _, coll_key, _ in self.records:
            if coll_key in self.bind_vars and f"@@{coll_key[1:]}" not in text:
                del self.bind_vars[coll_key]


def translate_create_pipeline(
    mpq: CypherParser.OC_MultiPartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a CREATE pipeline (module docstring)."""
    pipe = _Pipeline(resolver, bind_vars)
    tail = mpq.oC_SinglePartQuery()
    children = [*(mpq.children or []), *((tail.children or []) if tail is not None else [])]
    for child in children:
        if isinstance(child, CypherParser.OC_WithContext):
            if pipe.pending_creates:
                pipe.close_stage()
            pipe.var_env = _core("_apply_with")(
                child,
                lines=pipe.lines,
                bind_vars=bind_vars,
                forbidden_vars=pipe.forbidden,
                incoming_env=pipe.var_env,
            )
        elif isinstance(child, CypherParser.OC_ReadingClauseContext):
            pipe.read(child)
        elif isinstance(child, CypherParser.OC_UpdatingClauseContext):
            pipe.create(child.oC_Create())
    last = pipe.close_stage()

    writes = pipe.writes()
    ret = tail.oC_Return() if tail is not None else None
    if ret is None:
        if not writes:
            raise CoreError("A CREATE pipeline that creates nothing", code="INTERNAL_ERROR")
        lines = [*pipe.out, *(f"LET _inserted{i} = ({w})" for i, w in enumerate(writes[:-1])), writes[-1]]
        pipe.drop_unused_collection_keys("\n".join(lines))
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)
    lines = [*pipe.out, *(f"LET _inserted{i} = ({w})" for i, w in enumerate(writes))]
    # NOOPT: inlined into this loop, the last stage drew the INSERT subqueries
    # above into it, and they ran once per row (ERR 1210, ArangoDB 3.11).
    lines.append(f"FOR _r IN NOOPT({last})")
    lines += [f"  LET {aql} = _r.{cy}" for cy, aql in pipe.var_env.items()]
    _core("_append_return")(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars, var_env=pipe.var_env)
    pipe.drop_unused_collection_keys("\n".join(lines))
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)
