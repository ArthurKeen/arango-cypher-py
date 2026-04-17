# Multi-Sub-Agent Implementation Prompts — arango-cypher-py v0.2

Date: 2026-04-11
Derived from: [`implementation_plan.md`](./implementation_plan.md), [`python_prd.md`](./python_prd.md)

---

## How to use this document

This document contains **ready-to-use prompts** for launching parallel sub-agents to implement the v0.2 work packages. Each prompt is self-contained: it includes all the context a sub-agent needs without reading prior conversation history.

### Orchestration steps

1. **Wave 1 (parallel)**: Launch WP-1, WP-2, WP-3 simultaneously — they have no dependencies on each other
2. **Merge Wave 1**: Review and merge each agent's output. Run `pytest -m "not integration and not tck"` to verify no regressions
3. **Wave 2 (parallel)**: Launch WP-5, WP-4, WP-8 simultaneously — WP-5 depends on WP-1 (CREATE), WP-4 depends on WP-3 (schema analyzer)
4. **Merge Wave 2**: Review and merge
5. **Wave 3 (sequential)**: Launch WP-6 (TCK coverage), then WP-7 (Movies expansion) — both depend on prior waves and are iterative/exploratory

### Launching a sub-agent

Use the Task tool with `subagent_type: "best-of-n-runner"` for isolated branches, or `subagent_type: "generalPurpose"` for direct workspace changes.

---

## Shared context block

> **Include this block at the top of every sub-agent prompt.** It provides the codebase layout, conventions, and quality rules that all agents must follow.

```
SHARED CONTEXT — arango-cypher-py

## Project overview
Python-native Cypher → AQL transpiler for ArangoDB. Translates openCypher queries into
ArangoDB Query Language (AQL) using a conceptual→physical schema mapping. Supports PG
(types-as-collections), LPG (generic collections + type field), and hybrid physical models.

## Repository layout
arango_cypher/          # Main transpiler package
  __init__.py           # Public API re-exports: translate, execute, validate_cypher_profile, get_cypher_profile
  api.py                # Public functions: translate(), execute(), validate_cypher_profile(), TranspiledQuery
  parser.py             # ANTLR4 wrapper: parse_cypher(cypher) -> parse tree
  translate_v0.py       # Core translator: translate_v0(cypher, mapping, params, options) -> AqlQuery
  service.py            # FastAPI app with 12 endpoints
  profile.py            # Cypher profile manifest builder
  extensions/           # arango.* extension compilers (search, vector, geo, document, procedures)
    __init__.py          # register_all_extensions(registry)

arango_query_core/      # Shared core types (no transpiler logic)
  __init__.py           # Exports: MappingBundle, MappingResolver, MappingSource, CoreError, AqlQuery, ExtensionRegistry, ExtensionPolicy
  mapping.py            # MappingBundle dataclass, MappingResolver class
  exec.py               # AqlExecutor (wraps python-arango cursor)
  aql.py                # AqlQuery dataclass
  errors.py             # CoreError exception class
  extensions.py         # ExtensionRegistry, ExtensionPolicy

grammar/Cypher.g4       # openCypher ANTLR4 grammar (source of truth for parser)

tests/
  helpers/
    corpus.py           # CorpusCase dataclass, load_cases_from_file(), iter_cases()
    mapping_fixtures.py # mapping_bundle_for(name) -> MappingBundle (loads tests/fixtures/mappings/{name}.export.json)
  fixtures/
    cases/              # YAML golden test cases (version: 1, cases: [{id, name, mapping_fixture, cypher, expected: {aql, bind_vars}}])
    mappings/           # JSON export mapping fixtures (pg.export.json, lpg.export.json, hybrid.export.json, etc.)
    datasets/           # Seed data for integration tests
  integration/          # Tests requiring ArangoDB (@pytest.mark.integration)
  tck/                  # openCypher TCK harness
  conftest.py           # Shared fixtures (corpus_cases loads all YAML cases)

ui/                     # React + TypeScript frontend (Vite)
  src/
    App.tsx
    api/client.ts       # HTTP client for FastAPI
    api/store.ts        # Zustand-like state (localStorage persistence)
    components/         # CypherEditor, AqlEditor, ResultsPanel, MappingPanel, ConnectionDialog
    lang/               # cypher.ts (syntax), aql.ts (syntax), cypher-completion.ts (autocompletion)

docs/
  python_prd.md         # PRD (§6.4 = Cypher subset, §7.5 = error taxonomy, §8.2 = TCK, §10 = roadmap)
  implementation_plan.md # Work packages with dependencies

## Key types
- MappingBundle(conceptual_schema, physical_mapping, metadata, source, owl_turtle)
- MappingResolver(mapping: MappingBundle) — .resolve_entity(label), .resolve_relationship(type), .resolve_properties(label), .edge_constrains_target(rel, label, dir), .schema_summary()
- AqlQuery(text, bind_vars, debug)
- TranspiledQuery(aql, bind_vars, warnings, debug)
- CoreError(message, code) — codes: UNSUPPORTED, INVALID_ARGUMENT, PARSE_ERROR, NOT_IMPLEMENTED
- ExtensionRegistry — .register_function(name, compiler), .register_procedure(name, compiler), .compile_function(name, args), .compile_procedure(name, args)

## Golden test conventions
- YAML fixture file in tests/fixtures/cases/{feature}.yml
- Format:
    version: 1
    cases:
      - id: C100  # unique across ALL fixture files
        name: descriptive name
        mapping_fixture: pg  # matches tests/fixtures/mappings/{name}.export.json
        extensions_enabled: false
        cypher: |
          MATCH (n:User) RETURN n
        expected:
          aql: |
            FOR n IN @@collection
              RETURN n
          bind_vars:
            "@collection": users

- Test runner file: tests/test_translate_{feature}_goldens.py
- Pattern:
    @pytest.mark.parametrize("case_id", ["C100", "C101", ...])
    def test_translate_{feature}_goldens(corpus_cases, case_id):
        case = next(c for c in corpus_cases if c.id == case_id)
        mapping = mapping_bundle_for(case.mapping_fixture)
        out = translate(case.cypher, mapping=mapping, params=case.params)
        assert out.aql.strip() == case.expected_aql.strip()
        assert out.bind_vars == case.expected_bind_vars

## Quality rules
1. All existing tests must pass after your changes: pytest -m "not integration and not tck"
2. Run ruff check . — no lint errors
3. Never string-interpolate collection names or user values into AQL — always use bind parameters (@@collection for collections, @param for values)
4. Do not add comments that narrate what code does — only explain non-obvious intent
5. Do not create documentation files unless the work package specifies it
6. Update the PRD §6.4 Cypher subset table if you add support for a new construct
7. SCHEMA ANALYZER NO-WORKAROUND POLICY: arangodb-schema-analyzer (~/code/arango-schema-mapper)
   is the canonical source for reverse-engineering ontologies from ArangoDB schemas.
   If the analyzer output is incomplete, incorrect, or missing a capability you need:
   - Do NOT work around it in transpiler code (no shims, no fallback heuristics, no special cases)
   - File a bug or feature report against ~/code/arango-schema-mapper with:
     the database schema, current analyzer output, what you need, and a Cypher example
   - Document the gap in the PRD §5.3 status table referencing the issue
   - Fail gracefully with CoreError(code="ANALYZER_GAP") until the upstream fix lands
```

---

## Wave 1 prompts (parallel, no dependencies)

---

### WP-1: CREATE clause

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-1 — Implement CREATE clause translation

You are implementing the CREATE clause for the Cypher→AQL transpiler. This is the highest-priority
work package because it unblocks TCK testing and dataset expansion.

### What to implement

Translate Cypher CREATE patterns into AQL INSERT statements:

1. Node creation: CREATE (n:Person {name: "Alice", age: 30})
   → INSERT {name: @v1, age: @v2} INTO @@collection LET n = NEW
   For LPG-style entities, inject the typeField/typeValue into the inserted document.

2. Relationship creation: CREATE (a)-[:KNOWS {since: 2020}]->(b)
   → INSERT {_from: a._id, _to: b._id, since: @v3} INTO @@edgeCollection

3. Multi-element CREATE: CREATE (a:Person {name: "Alice"}), (b:Person {name: "Bob"}), (a)-[:KNOWS]->(b)
   → Sequential INSERTs with LET bindings for created documents

4. CREATE after MATCH: MATCH (a:Person {name: "Alice"}) CREATE (a)-[:KNOWS]->(b:Person {name: "Bob"})
   → FOR loop for MATCH, then INSERT for new nodes/relationships

5. CREATE with RETURN: CREATE (n:Person {name: "Alice"}) RETURN n
   → INSERT ... LET n = NEW ... RETURN n

### Where to make changes

FILE: arango_cypher/translate_v0.py
- In _translate_single_query() around lines 112-114, there is a guard:
    if spq.oC_UpdatingClause():
        raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")
  Modify this to allow CREATE but still reject SET/DELETE/MERGE/REMOVE.

- Add a new function _compile_create(updating_clause, resolver, bind_vars, var_env) -> list[str]
  that compiles a single CREATE clause into AQL lines.

- Add a new function _compile_create_pattern(pattern, resolver, bind_vars, var_env) -> list[str]
  that handles a single node or relationship pattern within CREATE.

- Wire _compile_create into the query dispatch so that CREATE-only queries and MATCH+CREATE
  queries both work.

FILE: arango_query_core/aql.py (if needed)
- Add INSERT rendering helpers if the existing AqlQuery structure needs extension.

DO NOT MODIFY these functions (owned by WP-2):
- _compile_function_invocation
- _compile_agg_expr
- _append_return (for aggregation changes)
- _parse_skip_limit

### Mapping resolution for CREATE

Use MappingResolver to determine the physical collection:
- resolver.resolve_entity(label) returns the entity mapping with collectionName and style
- For COLLECTION style: INSERT INTO the named collection
- For LABEL style: INSERT INTO the generic collection AND set the typeField/typeValue on the document
- resolver.resolve_relationship(type) returns the relationship mapping with edgeCollectionName and style
- For DEDICATED_COLLECTION: INSERT INTO the edge collection
- For GENERIC_WITH_TYPE: INSERT INTO the generic edge collection AND set the type field

### Golden tests to create

FILE: tests/fixtures/cases/create.yml
Create at least these cases (use IDs C200-C219 to avoid conflicts):

- C200: CREATE single node with label and properties (PG mapping)
- C201: CREATE single node with label and properties (LPG mapping)
- C202: CREATE two nodes and a relationship between them
- C203: MATCH then CREATE relationship
- C204: CREATE with RETURN
- C205: CREATE node with parameter values: CREATE (n:User {name: $name})
- C206: CREATE relationship with properties
- C207: CREATE multiple disconnected nodes
- C208: MATCH+CREATE with WHERE filter on match side
- C209: CREATE node with multiple labels (LPG mapping, if supported, otherwise skip)

FILE: tests/test_translate_create_goldens.py
Follow the standard pattern from test_translate_basic_goldens.py.

### Integration test

FILE: tests/integration/test_create_smoke.py
- Seed an empty database
- Translate and execute a CREATE query
- Read back the created documents and verify they exist with correct properties
- Mark with @pytest.mark.integration

### Acceptance criteria
- All new golden tests pass
- All existing golden tests still pass (no regressions)
- Integration test passes against ArangoDB
- CREATE (n:Label {props}) RETURN n works end-to-end
- MATCH (a) CREATE (a)-[:REL]->(b:Label {props}) works end-to-end
- ruff check . passes
```

---

### WP-2: Aggregation in RETURN + built-in functions

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-2 — Aggregation in RETURN + built-in functions

You are completing the aggregation and built-in function support in the transpiler.

### Part 1: Aggregation in RETURN

Currently aggregation functions (COUNT, SUM, AVG, MIN, MAX, COLLECT) only work inside WITH
clauses via _compile_agg_expr(). They need to work in RETURN as well.

Examples:
  MATCH (n:Person) RETURN COUNT(n)
  → RETURN LENGTH(FOR n IN @@collection RETURN 1)
  or: FOR n IN @@collection COLLECT WITH COUNT INTO __count RETURN __count

  MATCH (n:Person) RETURN n.city, COUNT(n)
  → FOR n IN @@collection COLLECT city = n.city WITH COUNT INTO __count RETURN {city: city, count: __count}

  MATCH (n:Person) RETURN COUNT(DISTINCT n.city)
  → FOR n IN @@collection COLLECT city = n.city INTO __group RETURN LENGTH(__group) — or similar

Also fix RETURN DISTINCT with multiple projection items (currently only single-item):
  MATCH (n:Person) RETURN DISTINCT n.name, n.city
  → FOR n IN @@collection RETURN DISTINCT {name: n.name, city: n.city}

### Part 2: Built-in functions

Add these to _compile_function_invocation() in translate_v0.py (around line ~2647).
Currently it handles: size→LENGTH, tolower→LOWER, toupper→UPPER, coalesce→NOT_NULL.

Add:
  type(r)      → relationship type from the type field. This is mapping-dependent:
                  For DEDICATED_COLLECTION: return the Cypher relationship type as a string literal
                  For GENERIC_WITH_TYPE: return r[typeField]
                  NOTE: this is complex — you may need to pass the resolver/mapping context
                  into _compile_function_invocation or handle type() as a special case.
                  Simplest v0.2 approach: return r.relation or r.type (the physical type field).
  id(n)        → n._id
  labels(n)    → mapping-dependent. Simplest: [PARSE_IDENTIFIER(n._id).collection] for COLLECTION,
                  or n[typeField] wrapped in array for LABEL style.
                  Simplest v0.2 approach: return ATTRIBUTES(n) filtered, or just n._id-based.
                  NOTE: exact semantics are hard; start with id(n)→n._id and defer labels(n) if complex.
  keys(n)      → ATTRIBUTES(n)
  properties(n)→ UNSET(n, "_id", "_key", "_rev")
  toString(e)  → TO_STRING(e)
  toInteger(e) → TO_NUMBER(e)
  toFloat(e)   → TO_NUMBER(e)
  toBoolean(e) → TO_BOOL(e)

### Part 3: LIMIT/SKIP with expressions

In _parse_skip_limit() (around line ~2126), currently only integer literals are accepted.
Extend to accept:
- Parameter references: LIMIT $count → LIMIT @count
- Simple arithmetic: LIMIT 5 + 5 → LIMIT 10 (compile the expression)

### Where to make changes

FILE: arango_cypher/translate_v0.py
- _append_return: detect aggregation calls in RETURN items, compile using COLLECT
- _compile_function_invocation: add new function mappings
- _parse_skip_limit: accept expressions and parameters
- May need a helper _detect_aggregation(expr) → bool to check if an expression contains aggregation

DO NOT MODIFY these areas (owned by WP-1):
- The updating clause guard (lines 112-114)
- Do not add any CREATE/INSERT/write-related code

### Golden tests to create

FILE: tests/fixtures/cases/aggregation_return.yml (IDs C220-C234)
- C220: RETURN COUNT(n)
- C221: RETURN n.city, COUNT(n)
- C222: RETURN COUNT(DISTINCT n.name)
- C223: RETURN SUM(n.age), AVG(n.age)
- C224: RETURN DISTINCT n.name, n.city (multi-column distinct)
- C225: RETURN MIN(n.age), MAX(n.age)

FILE: tests/fixtures/cases/builtin_functions.yml (IDs C235-C249)
- C235: RETURN id(n)
- C236: RETURN keys(n)
- C237: RETURN properties(n)
- C238: RETURN toString(n.age)
- C239: RETURN toInteger(n.ageStr)
- C240: WHERE toUpper(n.name) = "ALICE" (already works — regression test)
- C241: WHERE size(n.friends) > 3 (already works — regression test)

FILE: tests/test_translate_aggregation_return_goldens.py
FILE: tests/test_translate_builtin_functions_goldens.py

### Acceptance criteria
- Aggregation works in RETURN, not just WITH
- RETURN DISTINCT works with multiple items
- All new built-in functions translate to correct AQL
- LIMIT $param works
- All existing golden tests still pass
- ruff check . passes
```

---

### WP-3: Schema analyzer integration

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-3 — Wire up arangodb-schema-analyzer as an optional dependency

You are integrating the arangodb-schema-analyzer library to enable automatic mapping
acquisition from a live ArangoDB database.

### Background

The arangodb-schema-analyzer library (located at ~/code/arango-schema-mapper) provides:
- AgenticSchemaAnalyzer class with library API
- Tool contract: schema_analyzer.tool.run_tool({"operation": "export", ...}) → JSON export
- Tool contract: schema_analyzer.tool.run_tool({"operation": "owl", ...}) → OWL Turtle string
- The export JSON format matches the MappingBundle structure: conceptualSchema, physicalMapping, metadata

Currently the transpiler requires users to supply a MappingBundle manually. This WP adds
automatic mapping acquisition.

### What to implement

1. NEW FILE: arango_cypher/schema_acquire.py

   def classify_schema(db) -> str:
       """Fast heuristic: sample collections and classify as 'pg', 'lpg', 'hybrid', or 'unknown'.
       
       Strategy:
       - List all document collections and edge collections
       - For document collections: sample N docs, check if they have a common 'type'/'labels' field
         - If all docs have a type field with varying values → LPG
         - If collection names match conceptual types (no type field) → PG
       - For edge collections: check if they're dedicated or have a type/relation field
       - If mixed → hybrid
       - If unclear → unknown
       """

   def acquire_mapping_bundle(db, *, include_owl: bool = False) -> MappingBundle:
       """Call arangodb-schema-analyzer to produce a MappingBundle from a live database.
       
       Steps:
       1. Try to import arangodb_schema_analyzer (optional dependency)
       2. Call run_tool({"operation": "export", "connection": {...}}) or use AgenticSchemaAnalyzer
       3. Transform the export JSON into a MappingBundle
       4. If include_owl: also call operation="owl" and set MappingBundle.owl_turtle
       5. Return the populated MappingBundle
       
       If arangodb-schema-analyzer is not installed, raise ImportError with a helpful message.
       """

   def get_mapping(db, *, strategy: str = "auto", use_analyzer: bool = False) -> MappingBundle:
       """3-tier mapping acquisition.
       
       strategy="auto": classify_schema() first; if 'pg' or 'lpg', build simple mapping;
                        if 'hybrid' or 'unknown', call acquire_mapping_bundle()
       strategy="analyzer": always call acquire_mapping_bundle()
       strategy="heuristic": never call analyzer, build best-effort mapping from heuristics
       
       use_analyzer=True: force analyzer regardless of strategy
       """

   # In-memory cache
   _mapping_cache: dict[str, tuple[MappingBundle, float]] = {}
   CACHE_TTL_SECONDS = 300  # 5 minutes

   def _cache_key(db) -> str:
       """Schema fingerprint: hash of sorted collection names."""

2. FILE: arango_cypher/api.py — add get_mapping() re-export:
   from .schema_acquire import get_mapping  # re-export

3. FILE: arango_cypher/__init__.py — add get_mapping to __all__

4. FILE: pyproject.toml — add optional dependency group:
   [project.optional-dependencies]
   analyzer = ["arangodb-schema-analyzer"]
   
   Also add typer and rich for the CLI (WP-4 will use them):
   cli = ["typer>=0.9.0", "rich>=13.0.0"]

5. FILE: arango_cypher/service.py — update schema_introspect():
   When arangodb-schema-analyzer is available, use acquire_mapping_bundle() instead of
   the basic _sample_properties() approach. Fall back to current behavior if not installed.

### Building simple mappings from heuristics

When classify_schema returns 'pg' or 'lpg' and the user doesn't force the analyzer:

For PG:
- Each document collection → entity with style=COLLECTION, collectionName=collection_name
- Infer conceptual label from collection name (e.g., 'users' → 'User', 'persons' → 'Person')
- Each edge collection → relationship with style=DEDICATED_COLLECTION
- Sample docs for properties

For LPG:
- The document collection → entities with style=LABEL, typeField detected from samples
- The edge collection → relationships with style=GENERIC_WITH_TYPE, typeField detected

### Tests

FILE: tests/test_schema_acquire.py
- Test classify_schema with mocked collection/document data
- Test acquire_mapping_bundle with mocked analyzer (mock the import)
- Test get_mapping with strategy="auto" routing
- Test caching (second call returns cached result within TTL)
- Test ImportError when analyzer is not installed

DO NOT write integration tests that require the actual analyzer library — those are
for a separate integration test file.

### CRITICAL: No-workaround policy

The arangodb-schema-analyzer is the CANONICAL SOURCE for ontology extraction from ArangoDB
schemas. If you encounter ANY situation where the analyzer's output is incomplete, incorrect,
or missing a capability that the transpiler needs:

1. Do NOT work around it. No shims, no fallback logic that reimplements analyzer behavior,
   no special-case handling that papers over a gap.
2. File a bug or feature report against ~/code/arango-schema-mapper. Include:
   - The database schema (collections, sample documents) that triggered the gap
   - What the analyzer currently produces (or fails to produce)
   - What the transpiler needs
   - A Cypher query example that would benefit
3. In the transpiler code, raise CoreError("...", code="ANALYZER_GAP") with a message
   referencing the report.
4. Document the gap in PRD §5.3 implementation status table.

This policy ensures the analyzer improves at the source rather than being papered over
downstream. The heuristic classifier (classify_schema) is for ROUTING (deciding whether to
call the analyzer), not for REPLACING the analyzer's output.

### Acceptance criteria
- get_mapping(db) works with strategy="auto" and strategy="heuristic"
- acquire_mapping_bundle(db) works when arangodb-schema-analyzer is installed
- Helpful ImportError when analyzer is not installed and strategy="analyzer"
- classify_schema correctly identifies PG/LPG for simple databases
- Caching works with TTL
- No workarounds for analyzer gaps — any gap raises CoreError(code="ANALYZER_GAP")
- All existing tests still pass
- ruff check . passes
```

---

## Wave 2 prompts (launch after Wave 1 merges)

---

### WP-5: TCK harness improvements

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-5 — Improve the TCK harness to run real openCypher scenarios

The TCK (Technology Compatibility Kit) harness exists at tests/tck/ but currently only runs
a trivial sample.feature. With CREATE now implemented (WP-1, already merged), the harness
can execute "Given having executed" setup steps. Your job is to make it robust.

### Current state

FILE: tests/tck/gherkin.py — parses .feature files into Feature/Scenario/Step dataclasses
FILE: tests/tck/runner.py — executes scenarios: setup → translate → execute → compare results
FILE: tests/tck/test_tck_harness_smoke.py — runs sample.feature
FILE: tests/tck/features/sample.feature — one trivial scenario
FILE: scripts/download_tck.py — downloads .feature files from GitHub

### What to implement

1. FILE: tests/tck/gherkin.py — Scenario Outline expansion
   Currently only handles Scenario:. Add:
   - Parse "Scenario Outline:" headers
   - Parse "Examples:" tables (pipe-delimited rows with a header row)
   - Expand each Examples row into a concrete Scenario by substituting <placeholder> tokens
     in the step text and doc strings
   Example:
     Scenario Outline: Return literal
       When executing query:
         """
         RETURN <literal>
         """
       Then the result should be:
         | value      |
         | <expected> |
     Examples:
       | literal | expected |
       | 1       | 1        |
       | 'foo'   | 'foo'    |
   → expands to 2 concrete Scenario objects

2. FILE: tests/tck/runner.py — Given having executed
   Currently "Given having executed" steps are SKIPPED. Change to:
   - Extract the Cypher query from the doc string
   - Translate using translate() with a TCK-specific LPG mapping (nodes/edges collections)
   - Execute the resulting AQL against the test database
   - Support multiple sequential "And having executed" steps
   - If translation fails (unsupported construct), mark scenario as SKIPPED (not FAILED)
   - If execution fails, mark as FAILED

3. NEW FILE: tests/tck/normalize.py — Result normalization
   TCK expected results use Neo4j conventions. Implement structural comparison:
   
   def normalize_expected_value(text: str) -> Any:
       """Parse a TCK expected value cell into a Python value.
       Handles: integers, floats, strings ('foo'), booleans (true/false),
       null, lists ([1, 2]), maps ({key: value}),
       node literals (:Label {prop: value}), relationship literals [:TYPE {prop: value}]"""
   
   def normalize_actual_value(value: Any) -> Any:
       """Normalize an ArangoDB result value for comparison.
       Strip _id, _key, _rev from documents. Normalize numeric types."""
   
   def results_match(actual_rows: list[dict], expected_table: list[dict], *, ordered: bool) -> tuple[bool, str]:
       """Compare actual query results against expected TCK table.
       Returns (match, explanation_if_mismatch)."""

4. FILE: tests/tck/runner.py — Error expectation scenarios
   Some TCK scenarios expect errors:
     Then a SyntaxError should be raised
     Then a TypeError should be raised at compile time
   Handle by catching CoreError or ArangoDB errors and comparing against expected category.

5. FILE: tests/tck/runner.py — Given parameters
   Parse "Given parameters are:" data tables into a dict and pass as params= to translate().

### TCK LPG mapping fixture

The TCK uses a simple model: all nodes in a "nodes" collection, all edges in an "edges" collection,
with type/relation fields. Create or verify:
FILE: tests/fixtures/mappings/tck_lpg.export.json
This should be a generic LPG mapping that the TCK runner uses for all scenarios.

### Tests

FILE: tests/tck/test_tck_harness_smoke.py — expand to test:
- Scenario Outline expansion produces correct number of scenarios
- Given having executed creates data that can be queried
- Result normalization handles integers, strings, null, nodes, relationships
- Error expectation scenarios correctly match

### Acceptance criteria
- Scenario Outline / Examples are expanded into concrete scenarios
- "Given having executed" runs CREATE queries to seed the graph
- Result comparison handles type normalization
- Error scenarios are handled (not just skipped)
- All existing tests still pass
- ruff check . passes
```

---

### WP-4: CLI

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-4 — Implement the CLI

Create a CLI entry point using typer + rich. The CLI provides four subcommands:
translate, run, mapping, doctor.

### Prerequisites
- WP-3 (schema analyzer) must be merged first — the mapping command uses get_mapping().
- typer and rich should be in pyproject.toml optional-dependencies (WP-3 adds them).

### What to implement

1. NEW FILE: arango_cypher/cli.py

   import typer
   from rich.console import Console
   from rich.table import Table
   
   app = typer.Typer(name="arango-cypher-py", help="Cypher → AQL transpiler for ArangoDB")
   
   @app.command()
   def translate(cypher: str = typer.Argument(None), ...):
       """Translate Cypher to AQL. Reads from stdin if no argument given."""
       # Read cypher from argument or stdin
       # Load mapping from --mapping-file or --mapping-json or acquire from DB
       # Call arango_cypher.translate()
       # Print AQL and bind vars (JSON)
   
   @app.command()
   def run(cypher: str = typer.Argument(None), ...):
       """Translate and execute Cypher against ArangoDB."""
       # Same as translate, but also connect and execute
       # Print results as rich Table (default) or JSON (--json flag)
   
   @app.command()
   def mapping(...):
       """Print mapping summary for a database."""
       # Connect to DB
       # Call get_mapping(db) (from WP-3)
       # Print summary (rich Table or JSON)
       # Optionally write OWL Turtle to file (--owl-output)
   
   @app.command()
   def doctor(...):
       """Check connectivity, collections, and schema analyzer availability."""
       # Test ArangoDB connection
       # List collections
       # Check if arangodb-schema-analyzer is importable
       # Report status with rich formatting

   Common options (shared across commands):
   --host (default: ARANGO_HOST env or localhost)
   --port (default: ARANGO_PORT env or 8529)
   --db (default: ARANGO_DB env or _system)
   --user (default: ARANGO_USER env or root)
   --password (default: ARANGO_PASSWORD env or empty)
   --mapping-file (path to JSON mapping file)
   --json (output as JSON instead of table)

2. FILE: pyproject.toml — add console_scripts:
   [project.scripts]
   arango-cypher-py = "arango_cypher.cli:app"

3. Stdin support: translate and run should detect piped input:
   if cypher is None:
       cypher = sys.stdin.read()

### Tests

FILE: tests/test_cli.py
Use typer.testing.CliRunner:
- test_translate_prints_aql: invoke translate with a simple MATCH query and mapping file
- test_translate_stdin: pipe cypher via stdin
- test_doctor_no_connection: doctor reports connection failure gracefully
- test_run_requires_connection: run without connection prints helpful error

### Acceptance criteria
- arango-cypher-py translate "MATCH (n:User) RETURN n" --mapping-file mapping.json prints AQL
- echo "MATCH (n:User) RETURN n" | arango-cypher-py translate --mapping-file mapping.json works
- arango-cypher-py doctor reports connectivity status
- All existing tests still pass
- ruff check . passes
```

---

### WP-8: UI parameter binding + query history

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-8 — Add parameter binding panel and query history to the UI

### Part 1: Parameter panel

NEW FILE: ui/src/components/ParameterPanel.tsx
- Scan the current Cypher text for $paramName tokens (regex: /\$([a-zA-Z_]\w*)/g)
- Display a list of detected parameters with JSON input fields
- When the user enters values, include them in the API request as a params object
- Persist parameter values in localStorage keyed by query hash

Integration:
- FILE: ui/src/App.tsx — add ParameterPanel below the CypherEditor
- FILE: ui/src/api/store.ts — add params: Record<string, unknown> to state
- FILE: ui/src/api/client.ts — include params in POST /translate and POST /execute requests
- FILE: arango_cypher/service.py — ensure TranslateRequest and ExecuteRequest models
  accept an optional params field and pass it to translate()

Also add a bind-vars display below the AQL editor showing the bind variables from the
translation result (read-only, formatted JSON).

### Part 2: Query history

NEW FILE: ui/src/components/QueryHistory.tsx
- Store last 50 queries in localStorage (cypher text + timestamp + truncated AQL preview)
- Display as a searchable list in a slide-out drawer
- Click to restore a previous query
- "Clear history" button

Integration:
- FILE: ui/src/api/store.ts — add history: Array<{cypher, timestamp, aqlPreview}> to state
- FILE: ui/src/App.tsx — add history drawer toggle button, save to history on each translate

### Part 3: Keyboard shortcuts

FILE: ui/src/components/CypherEditor.tsx or ui/src/App.tsx
Add CodeMirror keymaps or global event handlers:
- Ctrl/Cmd+Enter → Translate (call makeRequest with mode="translate")
- Shift+Enter → Execute (call makeRequest with mode="execute")

### Acceptance criteria
- Parameters detected from Cypher text are shown in the panel
- Parameter values are sent to the API and used in translation
- Query history persists across browser sessions
- Clicking a history entry restores the query
- Keyboard shortcuts work
- All existing tests still pass
- ruff check . passes (if applicable to Python changes)
```

---

## Wave 3 prompts (sequential, after Waves 1+2)

---

### WP-6: TCK Match coverage

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-6 — Achieve ≥ 40% TCK Match scenario pass rate

This is an iterative task. You will download real TCK Match features, run them, triage failures,
and fix translator bugs until the pass rate reaches 40%.

### Process

1. Download Match features:
   python scripts/download_tck.py --only-match Match

2. Run the TCK:
   RUN_INTEGRATION=1 RUN_TCK=1 pytest tests/tck/ -v 2>&1 | head -200
   
3. For each failing scenario, categorize:
   - TRANSLATOR BUG: the Cypher is in our supported subset but the AQL is wrong → fix translate_v0.py
   - MISSING CONSTRUCT: the Cypher uses a construct we don't support yet → skip, document in SKIP_REASONS.md
   - RUNNER BUG: the harness misparses or miscompares → fix tests/tck/runner.py or normalize.py
   - NORMALIZATION BUG: actual results are correct but comparison fails → fix normalize.py

4. Fix and re-run iteratively.

5. When done, create:
   FILE: tests/tck/SKIP_REASONS.md — document each skipped scenario category and why

### What you can modify
- arango_cypher/translate_v0.py — fix bugs in existing translation logic
- tests/tck/runner.py — fix runner bugs
- tests/tck/normalize.py — fix normalization bugs
- tests/tck/gherkin.py — fix parser bugs

### What you should NOT do
- Do not implement entirely new Cypher clauses (MERGE, DELETE, etc.) — those are future WPs
- Do not modify the golden test fixtures — if a golden test breaks, your translator fix is wrong

### Acceptance criteria
- ≥ 40% of Match*.feature scenarios pass (non-skipped)
- All existing golden tests still pass
- SKIP_REASONS.md documents remaining gaps
- ruff check . passes
```

---

### WP-7: Movies dataset expansion

```
{SHARED CONTEXT BLOCK — paste from above}

## Your task: WP-7 — Expand the Movies dataset to the full Neo4j Movies corpus

### What to implement

1. FILE: tests/fixtures/datasets/movies/lpg-data.json — EXPAND
   Convert the full Neo4j Movies dataset (~171 nodes, ~253 relationships) from
   the Neo4j CREATE script to LPG JSON format.
   
   Source: https://github.com/neo4j-graph-examples/movies/blob/main/scripts/import.cypher
   (or the CREATE statement version)
   
   Format (matches existing structure):
   {
     "nodes": [
       {"_key": "TomHanks", "type": "Person", "labels": ["Person", "Actor"], "name": "Tom Hanks", "born": 1956},
       {"_key": "ForrestGump", "type": "Movie", "labels": ["Movie"], "title": "Forrest Gump", "released": 1994, "tagline": "..."},
       ...
     ],
     "edges": [
       {"_from": "nodes/TomHanks", "_to": "nodes/ForrestGump", "relation": "ACTED_IN", "roles": ["Forrest"]},
       ...
     ]
   }

2. NEW FILE: tests/fixtures/datasets/movies/pg-data.json
   Same data in PG format: separate arrays per collection type.
   {
     "collections": {
       "persons": [{"_key": "TomHanks", "name": "Tom Hanks", "born": 1956}, ...],
       "movies": [{"_key": "ForrestGump", "title": "Forrest Gump", ...}, ...]
     },
     "edge_collections": {
       "acted_in": [{"_from": "persons/TomHanks", "_to": "movies/ForrestGump", "roles": ["Forrest"]}, ...],
       "directed": [{"_from": "persons/RobertZemeckis", "_to": "movies/ForrestGump"}, ...]
     }
   }

3. NEW FILE: tests/fixtures/datasets/movies/query-corpus.yml
   Extract ~15-20 example queries from Neo4j Movies documentation:
   - id: movies_001
     description: "Find actor by name"
     cypher: 'MATCH (a:Person {name: "Tom Hanks"}) RETURN a.name, a.born'
     dataset: movies
     mapping_fixture: movies_lpg
     expected_count: 1  # expected number of result rows

4. NEW FILE: tests/fixtures/mappings/movies_pg.export.json
   PG-style mapping for the Movies dataset with separate collections.

5. FILE: tests/integration/datasets.py — extend
   Add seed_movies_pg_dataset(db) that creates separate collections and seeds from pg-data.json.

6. NEW FILE: tests/integration/test_neo4j_movies_dataset.py
   Parametrized test that:
   - Loads query-corpus.yml
   - For each query, seeds the appropriate dataset (LPG or PG)
   - Translates the Cypher
   - Executes the AQL
   - Verifies result count matches expected_count
   - Run against BOTH movies_lpg and movies_pg mapping fixtures

### Acceptance criteria
- Full Movies dataset in both LPG and PG formats
- Query corpus with ≥ 15 queries
- All queries translate and execute correctly against both layouts
- All existing tests still pass
- ruff check . passes
```

---

## Wave 4 prompts — WP-25: NL→Cypher pipeline hardening

**Source of truth:** `docs/implementation_plan.md` WP-25, `docs/python_prd.md` §1.2.1, research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`.

**Orchestration:**
1. **Wave 4-pre (sequential, 1 agent, ~0.5 d):** PromptBuilder refactor. Lands on `main` before Wave 4a launches.
2. **Wave 4a (parallel, 4 agents):** WP-25.1 (few-shot), WP-25.2 (entity resolution), WP-25.3 (execution-grounded), WP-25.4 (caching). Launch on separate branches from `main` after 4-pre merges.
3. **Wave 4b (sequential):** merge 4a branches, resolve `nl2cypher.py` merge points, run full unit suite.
4. **Wave 4c (sequential, 1 agent):** WP-25.5 (evaluation harness + regression gate).

**Shared context for all Wave 4 sub-agents:**

```
{SHARED CONTEXT BLOCK — paste from top of this document}

## Wave 4 addenda — NL→Cypher module layout

ADDITIONAL FILES RELEVANT TO WAVE 4:
arango_cypher/nl2cypher.py         # Single large module today; being decomposed.
arango_query_core/mapping.py       # MappingBundle / MappingResolver
arango_query_core/exec.py          # AqlExecutor (wraps python-arango cursor)
tests/fixtures/datasets/movies/query-corpus.yml      # ~20 (description, cypher) pairs
tests/fixtures/datasets/northwind/query-corpus.yml   # ~14 (description, cypher) pairs
tests/fixtures/datasets/social/query-corpus.yml      # small, illustrative

## Current NL→Cypher pipeline (what you are hardening)

- nl_to_cypher(question, *, mapping, use_llm=True, llm_provider=None, max_retries=2)
  - Builds a conceptual-only schema summary via _build_schema_summary(bundle).
  - Calls _call_llm_with_retry() which:
    - Sends { _SYSTEM_PROMPT.format(schema=summary), question } to the provider.
    - Extracts a Cypher block from the response (code fences or heuristic).
    - Rewrites hallucinated labels via _fix_labels().
    - Parses the Cypher with the ANTLR parser; on failure feeds error back and retries.
  - Falls back to _rule_based_translate() when no LLM is configured.

## The §1.2 invariant (NON-NEGOTIABLE)

The LLM never sees physical details (collection names, type discriminators, field
names, AQL). It sees only the conceptual schema: entity labels, relationship types,
properties, domain/range, and data-quality hints. Few-shot examples MUST be
conceptual Cypher. Resolved entities are string VALUES (e.g. "Forrest Gump"), not
schema details.

## Quality rules specific to Wave 4

- Offline graceful degradation: every feature must behave sanely when no DB is
  connected and no LLM is configured. Specifically, unit tests MUST run without
  network access.
- Latency budget: each added round-trip must be justified. Cache aggressively within
  a request lifecycle; cache the per-mapping-bundle retriever across requests.
- No silent behaviour change when the feature's flag is off: the zero-shot /
  no-resolution / no-EXPLAIN path must be bit-identical to today's output on a
  given question + mapping + seeded LLM response.
```

---

### Wave 4-pre: PromptBuilder refactor (sequential, must land first)

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: Wave 4-pre — Refactor _SYSTEM_PROMPT into a composable PromptBuilder

### Goal
Replace the monolithic `_SYSTEM_PROMPT` string + ad-hoc retry-prompt construction in
`arango_cypher/nl2cypher.py` with a `PromptBuilder` class whose sections can be
extended independently by the Wave 4a sub-agents. Behaviour is preserved EXACTLY.

### Scope

1. Add a `PromptBuilder` class to `arango_cypher/nl2cypher.py`:

```python
@dataclass
class PromptBuilder:
    schema_summary: str
    few_shot_examples: list[tuple[str, str]] = field(default_factory=list)
    resolved_entities: list[str] = field(default_factory=list)
    retry_context: str = ""

    def render_system(self) -> str:
        # Schema-first so providers can cache this prefix. Existing wording of
        # _SYSTEM_PROMPT MUST be preserved byte-for-byte in the zero-shot case
        # (empty few_shot_examples and empty resolved_entities).
        ...

    def render_user(self, question: str) -> str:
        # retry_context appended if non-empty, as today.
        ...
```

2. Rewrite `_call_llm_with_retry()` to build a `PromptBuilder`, pass it through
   the attempt loop, and mutate only `retry_context` between attempts. Preserve
   the existing `_fix_labels()`, `_extract_cypher_from_response()`, and
   `_validate_cypher()` calls unchanged.

3. Update `OpenAIProvider.generate()` / `OpenRouterProvider.generate()` to
   accept the already-built system string (the builder renders once; the
   provider no longer does `_SYSTEM_PROMPT.format(...)`). This is a small
   provider-interface change — bump the protocol to
   `generate(system: str, user: str) -> (content, usage)`. Update the
   `_AqlChatProvider` wrapper likewise.

4. Do NOT introduce few-shot or entity resolution behaviour. Those are
   Wave 4a's job. Your job is only the shape change.

### Constraints

- Golden-shape test: add `tests/test_nl2cypher_prompt_builder.py` asserting
  that `PromptBuilder(schema_summary="X").render_system()` equals the
  pre-refactor `_SYSTEM_PROMPT.format(schema="X")` character-for-character.
- Mock-provider test: existing unit tests that mock `LLMProvider.generate()`
  must still pass unchanged. If they break because of the signature change,
  update the mocks minimally.

### Acceptance

- `pytest -m "not integration and not tck"` is bit-identical green.
- `ruff check .` passes.
- A grep for `_SYSTEM_PROMPT.format` returns zero hits.
- Diff touches only `arango_cypher/nl2cypher.py` and
  `tests/test_nl2cypher_prompt_builder.py` (plus any mock-fix lines in
  existing tests).
```

---

### WP-25.1: Dynamic few-shot retrieval

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.1 — Dynamic few-shot retrieval for NL→Cypher

### Goal
Before the LLM call, retrieve the top-K most similar (NL question → Cypher answer)
examples from a curated seed corpus and inject them into the prompt.

### What to implement

1. Package layout:
   - `arango_cypher/nl2cypher/__init__.py` — re-export the existing public API
     (`nl_to_cypher`, `nl_to_aql`, `NL2CypherResult`, `NL2AqlResult`,
     `LLMProvider`, `OpenAIProvider`, `OpenRouterProvider`,
     `get_llm_provider`, `suggest_nl_queries`). Convert the current
     `arango_cypher/nl2cypher.py` file into a package — preserve every
     import path that exists today.
   - `arango_cypher/nl2cypher/fewshot.py` — new. Implement:
     ```python
     class Retriever(Protocol):
         def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]: ...

     class BM25Retriever(Retriever):
         def __init__(self, examples: list[tuple[str, str]]): ...
         def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]: ...

     class FewShotIndex:
         def __init__(self, retriever: Retriever): ...
         @classmethod
         def from_corpus_files(cls, paths: list[Path]) -> "FewShotIndex": ...
         def format_prompt_section(self, question: str, k: int = 3) -> str: ...
     ```
   - `arango_cypher/nl2cypher/corpora/` — new directory with:
     - `movies.yml`, `northwind.yml`, `social.yml`: mined from the `description`
       and `cypher` fields of `tests/fixtures/datasets/*/query-corpus.yml`.
       Format:
       ```yaml
       version: 1
       examples:
         - question: "Find all movies Tom Hanks acted in"
           cypher: 'MATCH (p:Person {name: "Tom Hanks"})-[:ACTED_IN]->(m:Movie) RETURN m.title'
       ```
     - Use the `description` field as `question` (lightly humanized if it
       reads more like a label than a question — e.g. append a question mark,
       replace "Count foo per bar" with "How many foo per bar").

2. Integration with `PromptBuilder` (from Wave 4-pre):
   - Add `FewShotIndex` invocation in `nl_to_cypher()` when `use_fewshot=True`
     (default). Retrieve K=3 examples; pass them to `PromptBuilder.few_shot_examples`.
   - Render as a `## Examples` block immediately after the schema, in the
     order produced by the retriever:
     ```
     ## Examples

     Q: <question>
     ```cypher
     <cypher>
     ```

     Q: <question>
     ...
     ```

3. Dependency: add `rank_bm25>=0.2.2` to the `[dev]` extra in `pyproject.toml`.
   Do NOT add it to core requirements — the corpora are conceptual-Cypher, so
   the retriever can live behind a lazy import and degrade to no-op if
   `rank_bm25` is unavailable at runtime.

### Tests (`tests/test_nl2cypher_fewshot.py`)

- `test_bm25_retriever_finds_similar_question()`: given a tiny corpus with
  a known best match, `retrieve("find movies for tom hanks", k=1)` returns
  the Tom Hanks example.
- `test_few_shot_index_from_corpus_files()`: loads the three yml corpora,
  checks `len(index.examples) > 30`.
- `test_prompt_section_format()`: asserts the rendered block matches a
  golden string (multi-line, deterministic order).
- `test_empty_corpus_graceful()`: `FewShotIndex(BM25Retriever([]))` yields
  an empty string from `format_prompt_section`, no exceptions.
- `test_nl_to_cypher_use_fewshot_false_is_bit_identical()`: with
  `use_fewshot=False`, the system string exactly matches the Wave 4-pre
  zero-shot baseline.

### Acceptance

- New unit tests pass.
- All existing unit tests pass unchanged.
- `ruff check .` clean.
- With `OPENAI_API_KEY` set, a quick manual sanity check produces a
  plausibly-better Cypher on the Movies fixture for a tricky question
  (document the before/after in the PR description, do not commit it).
```

---

### WP-25.2: Pre-flight entity resolution

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.2 — Pre-flight entity resolution for NL→Cypher

### Goal
Extract candidate entity mentions from the user question and rewrite each to its
database-correct form before the LLM call. So "who acted in Forest Gump?" gets
augmented with `User mentioned 'Forest Gump' — matched to Movie.title='Forrest Gump'.`

### What to implement

1. `arango_cypher/nl2cypher/entity_resolution.py` — new. Implement:
   ```python
   @dataclass
   class ResolvedEntity:
       mention: str           # "Forest Gump"
       label: str             # "Movie"
       property: str          # "title"
       value: str             # "Forrest Gump"
       score: float           # 0.0 - 1.0

   class EntityResolver:
       def __init__(
           self,
           *,
           db=None,           # python-arango StandardDatabase or None
           mapping: MappingBundle | None = None,
           max_candidates: int = 5,
       ): ...

       def extract_candidates(self, question: str) -> list[str]: ...
       def resolve(self, question: str) -> list[ResolvedEntity]: ...
       def format_prompt_section(self, resolved: list[ResolvedEntity]) -> str: ...
   ```

2. `extract_candidates()`: return quoted substrings, Title-Case multi-word
   phrases, and capitalized single words that don't match schema keywords.
   Conservative — better to miss a candidate than drown the LLM in garbage.

3. `resolve()`:
   - **DB path (preferred):** for each candidate, try ArangoSearch-backed
     lookup against string properties of entity collections (name, title,
     label — read property names from the `MappingBundle`). Use AQL
     `SEARCH ANALYZER(...)` with BM25 if a view exists; otherwise fall back
     to `FILTER LOWER(d.<prop>) CONTAINS LOWER(@mention)` against each
     relevant collection (cap at 50 rows per collection). Take the highest
     score per candidate above a threshold (e.g. 0.6 for search, strict
     equality/prefix for FILTER fallback).
   - **No-DB path:** return `[]`. Do NOT raise.
   - Cache results per `(mapping_id, question)` for the lifetime of the
     `EntityResolver` instance.

4. Integration with `PromptBuilder`:
   - In `nl_to_cypher()`, if `use_entity_resolution=True` (default) AND an
     `EntityResolver` is available on the call (either passed explicitly or
     derivable from a DB handle on the request), invoke it; pass the
     rendered section to `PromptBuilder.resolved_entities`.
   - Rendered as a `## Resolved entities` block between schema/examples and
     the question:
     ```
     ## Resolved entities

     - "Forest Gump" → Movie.title = "Forrest Gump" (similarity 0.92)
     ```

5. `arango_cypher/service.py`: when a DB connection is configured, wire an
   `EntityResolver` into the `/nl2cypher` handler's call to `nl_to_cypher()`.

### Tests (`tests/test_nl2cypher_entity_resolution.py`)

- `test_extract_candidates_quoted_string()`: extracts `The Matrix` from
  `Find movies similar to "The Matrix"`.
- `test_extract_candidates_title_case()`: extracts `Tom Hanks` from
  `Which movies did Tom Hanks act in?`.
- `test_extract_candidates_skips_schema_keywords()`: does NOT extract `Person`
  from `Find all Person nodes`.
- `test_resolve_with_mocked_db()`: mock the db handle to return a BM25-style
  result for "Forest Gump" → "Forrest Gump"; assert the ResolvedEntity shape.
- `test_resolve_no_db_returns_empty()`: `EntityResolver(db=None).resolve(q)` → `[]`.
- `test_prompt_section_format()`: golden assertion on rendered block.
- `test_nl_to_cypher_use_entity_resolution_false_is_bit_identical()`: with
  flag off, system string matches Wave 4-pre baseline.

### Acceptance

- New unit tests pass (all with mocked or no DB).
- All existing unit tests pass.
- Offline nl_to_cypher() behaviour is unchanged when entity resolution is off.
- `ruff check .` clean.
- Integration test (opt-in, RUN_INTEGRATION=1) against the Movies dataset:
  "who acted in Forest Gump" (deliberate typo) produces a Cypher whose
  property literal is "Forrest Gump".
```

---

### WP-25.3: Execution-grounded validation loop

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.3 — Execution-grounded validation for NL→Cypher

### Goal
Extend the LLM retry loop to catch semantic errors (nonexistent collection,
nonexistent property, invalid traversal direction) in addition to the syntactic
errors the ANTLR parser already catches. Use `POST /_api/explain` on the connected
database: no execution, just planning. Errors from `EXPLAIN` feed back into the
next retry prompt the same way ANTLR errors do today.

### What to implement

1. `arango_query_core/exec.py`: add a read-only helper:
   ```python
   def explain_aql(
       db,  # python-arango StandardDatabase
       aql: str,
       bind_vars: dict,
   ) -> tuple[bool, str]:
       """Return (ok, error_message). Empty message on success."""
   ```
   Use `db.aql.explain(aql, bind_vars=bind_vars)` from python-arango; map any
   `AQLQueryExplainError` / `ArangoServerError` into a short human-readable
   error string suitable for LLM feedback. Do NOT surface full HTTP payloads
   or stack traces.

2. `arango_cypher/nl2cypher/__init__.py` (or the split file that replaces it):
   extend `_call_llm_with_retry()`:
   - After ANTLR parse succeeds, call the existing `translate()` to produce AQL.
   - If a `db` is available on the call (passed through the `nl_to_cypher()`
     kwargs), call `explain_aql()`.
   - On `EXPLAIN` failure:
     - Build a retry message: `"Translated AQL failed EXPLAIN: <error>. The
       Cypher was: <cypher>. Please revise your Cypher."`
     - Feed it into `PromptBuilder.retry_context` on the next attempt.
     - Count against `max_retries` (same budget as ANTLR failures).
   - On `EXPLAIN` success, return the result.
   - If no `db` is configured, skip `EXPLAIN` entirely and preserve today's
     behaviour exactly.

3. `arango_cypher/service.py`: pipe the connected DB through the `/nl2cypher`
   handler. Respect existing connection lifecycle.

### Tests (`tests/test_nl2cypher_execution_grounded.py`)

- `test_explain_success_accepted()`: mock `explain_aql` to return `(True, "")`;
  result is identical to today's validation path.
- `test_explain_failure_triggers_retry()`: mock `explain_aql` to return
  `(False, "collection 'Persons' not found")` on attempt 1 and `(True, "")`
  on attempt 2; assert retry prompt contains the error and the final result
  uses the second Cypher.
- `test_no_db_skips_explain()`: with `db=None`, behaviour is bit-identical
  to Wave 4-pre baseline.
- `test_retry_budget_respected()`: `max_retries=2` with three failures total
  produces the best-of result with `retries=2`, not an infinite loop.

### Constraints

- NEVER execute the AQL — EXPLAIN only. Even on a read-only database, we
  respect the budget by not paying for row materialization.
- The feature is opt-in via the presence of a DB handle. No config flag.
- Unit tests MUST run without network access (mock everything).

### Acceptance

- New unit tests pass.
- Offline nl_to_cypher() is bit-identical to Wave 4-pre.
- `ruff check .` clean.
- Integration test (opt-in): a deliberately-bad Cypher like
  `MATCH (n:Persons) RETURN n` (collection is `Person`, not `Persons`) is
  self-healed when called via `/nl2cypher` with a live DB.
```

---

### WP-25.4: Prompt caching

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.4 — Schema-prefix prompt caching

### Goal
The schema block is static per mapping. Put it at the top of the system prompt so
OpenAI's automatic prompt caching kicks in above the token threshold, and add the
Anthropic `cache_control` hook so a future Anthropic provider gets caching for free.

### What to implement

1. `arango_cypher/nl2cypher/` (post-4-pre structure): confirm
   `PromptBuilder.render_system()` orders sections as:
   ```
   [role/rules prelude]       <- tiny, static
   [schema summary]           <- large, static per mapping: the cache target
   [few-shot examples]        <- medium, varies per question (WP-25.1)
   [resolved entities]        <- small, varies per question (WP-25.2)
   ```
   If Wave 4-pre shipped the sections in a different order, refactor to this
   order. Existing golden tests may need to be regenerated; keep the zero-shot
   case bit-identical EXCEPT for order, and update the Wave 4-pre golden
   accordingly.

2. OpenAI telemetry: in `OpenAIProvider._chat()`, capture
   `usage.prompt_tokens_details.cached_tokens` when the field is present and
   include it in the returned usage dict. Update `NL2CypherResult` / `NL2AqlResult`
   to expose `cached_tokens: int = 0`. Surface in the `/nl2cypher` JSON response
   so the UI can display cache-hit rate.

3. Anthropic cache_control hook: in the provider dispatch, if the provider is
   an Anthropic-compatible one (detect via base_url or explicit subclass),
   split the system prompt into a cached prefix (everything through the schema
   block) and an uncached suffix, and render to Anthropic's
   `{role: "system", content: [{type: "text", text: "...", cache_control: {...}}]}`
   message shape. Leave a `AnthropicProvider` stub class in
   `arango_cypher/nl2cypher/providers.py` even if it's not wired end-to-end —
   the shape of the hook is what we're committing to here.

4. Document the caching behaviour in `arango_cypher/nl2cypher/README.md`
   (new, short): who caches what, what token thresholds apply, how to read
   the `cached_tokens` field.

### Tests (`tests/test_nl2cypher_caching.py`)

- `test_prompt_ordering_schema_is_first_after_prelude()`: assert the schema
  block appears before examples and resolved-entities sections.
- `test_cached_tokens_propagates_from_usage()`: mock a provider response
  with `prompt_tokens_details.cached_tokens = 512`; assert the final
  `NL2CypherResult.cached_tokens == 512`.
- `test_cached_tokens_default_zero()`: provider returns no details; result
  has `cached_tokens == 0`.
- `test_anthropic_provider_shape_stub()`: assert the Anthropic cache_control
  prefix/suffix split is produced correctly for a sample prompt.

### Acceptance

- New unit tests pass.
- All existing unit tests pass (some may need golden updates for section
  ordering — keep the update minimal).
- No live API calls in the unit suite.
- `ruff check .` clean.
```

---

### WP-25.5: Evaluation harness + regression gate (Wave 4c, sequential)

```
{SHARED CONTEXT BLOCK — paste from top of document}
{Wave 4 addenda — paste from above}

## Your task: WP-25.5 — NL→Cypher evaluation harness + regression gate

### Goal
A repeatable measurement of the pipeline's accuracy, cost, and reliability — plus
a CI gate that prevents future regressions.

### What to implement

1. `tests/nl2cypher/__init__.py` — new package.
2. `tests/nl2cypher/eval/corpus.yml` — hand-curated evaluation set. ~40-60 cases
   across the three fixture datasets. Categories:
   - **Baseline**: simple NL→Cypher lookups with a known-good answer.
   - **Few-shot bait**: questions whose intent closely mirrors a seed corpus
     example (WP-25.1 should lift these).
   - **Typo cases**: intentional misspellings of property values
     (WP-25.2 should fix these).
   - **Hallucination bait**: questions phrased to tempt label/collection
     invention (WP-25.3 should self-heal these).
   Case format:
   ```yaml
   version: 1
   cases:
     - id: eval_001
       mapping_fixture: movies_lpg
       question: "Which movies did Tom Hanks act in?"
       expected_patterns:
         - "MATCH .*Person.*Tom Hanks.*ACTED_IN.*Movie"
       category: baseline
     - id: eval_042
       mapping_fixture: movies_lpg
       question: "Who acted in Forest Gump?"
       expected_patterns:
         - "Forrest Gump"   # should be resolved by WP-25.2
       category: typo
   ```

3. `tests/nl2cypher/eval/runner.py` — new. For each case in corpus.yml:
   - Run the pipeline with a named config.
   - Collect metrics:
     - `parse_ok`: ANTLR parse success on the returned Cypher.
     - `explain_ok`: AQL EXPLAIN success (requires DB; skip if unavailable).
     - `pattern_match`: regex check against `expected_patterns` — ALL must match.
     - `row_match`: (optional, requires seeded DB) AQL executes and returns
       at least 1 row.
     - `tokens`, `retries`, `latency_ms`, `cached_tokens`.
   - Aggregate into a `Report` dataclass and serialize both markdown and JSON.
   - Output to `tests/nl2cypher/eval/reports/<UTC-date>-<config>.{md,json}`.

4. `tests/nl2cypher/eval/configs.yml` — named configs:
   ```yaml
   - name: zero_shot
     use_fewshot: false
     use_entity_resolution: false
     use_execution_grounded: false
   - name: few_shot
     use_fewshot: true
     use_entity_resolution: false
     use_execution_grounded: false
   - name: few_shot_plus_entity
     use_fewshot: true
     use_entity_resolution: true
     use_execution_grounded: false
   - name: full
     use_fewshot: true
     use_entity_resolution: true
     use_execution_grounded: true
   ```

5. `tests/nl2cypher/eval/baseline.json` — the committed baseline report for
   the `full` config. Check in the initial baseline after the harness lands
   and the pipeline runs green.

6. `tests/test_nl2cypher_eval_gate.py` — gate. Loads baseline.json; asserts
   a fresh run's metrics are not worse by more than:
   - `parse_ok` rate: drop ≤ 5 pp.
   - `pattern_match` rate: drop ≤ 5 pp.
   - `tokens_mean`: increase ≤ 20%.
   - `retries_mean`: increase ≤ 0.3.
   Gated behind `RUN_NL2CYPHER_EVAL=1` because LLM calls cost money.

7. README section: add `tests/nl2cypher/eval/README.md` explaining how to run
   the harness, how to sweep configs, and how to refresh the baseline.

### Tests

- `test_eval_runner_runs_on_fixture()`: mock the provider; assert the runner
  produces a Report with expected fields.
- `test_pattern_match_regex()`: case-level pattern-matching logic is correct.
- `test_gate_fails_on_regression()`: synthetic "worse" report triggers assertion.
- `test_gate_passes_on_no_change()`: identical-to-baseline report passes.

### Acceptance

- Harness runs end-to-end with a mocked provider in the unit suite.
- `RUN_NL2CYPHER_EVAL=1 OPENAI_API_KEY=... pytest tests/test_nl2cypher_eval_gate.py`
  passes on a checkout that has all of WP-25.1 through .4 merged.
- `tests/nl2cypher/eval/baseline.json` committed.
- `ruff check .` clean.
- `docs/python_prd.md` §1.2.1 updated to link to the baseline report.
```

---

## Orchestrator checklist

Use this checklist when running the waves:

### Pre-flight
- [ ] All existing tests pass: `pytest -m "not integration and not tck"`
- [ ] Working tree is clean: `git status`
- [ ] Branch created for v0.2 work: `git checkout -b v0.2-dev`

### Wave 1
- [ ] Launch WP-1 (CREATE), WP-2 (aggregation), WP-3 (schema analyzer)
- [ ] Review WP-1 output: golden tests pass, integration test passes
- [ ] Review WP-2 output: golden tests pass, no regressions
- [ ] Review WP-3 output: unit tests pass, schema acquisition works
- [ ] Merge all three, resolve any conflicts in translate_v0.py
- [ ] Full test run: `pytest -m "not integration and not tck"` — all green

### Wave 2
- [ ] Launch WP-5 (TCK harness), WP-4 (CLI), WP-8 (UI)
- [ ] Review WP-5: Scenario Outline works, normalization works
- [ ] Review WP-4: CLI smoke tests pass
- [ ] Review WP-8: UI changes work (manual verification)
- [ ] Merge all three
- [ ] Full test run — all green

### Wave 3
- [ ] Launch WP-6 (TCK coverage) — iterative, may take multiple sessions
- [ ] Track pass rate: aim for ≥ 40% of Match scenarios
- [ ] Launch WP-7 (Movies expansion)
- [ ] Full test run including integration: `RUN_INTEGRATION=1 pytest`
- [ ] TCK run: `RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck`

### Post-flight
- [ ] Update PRD implementation status table
- [ ] Update PRD §6.4 Cypher subset table
- [ ] Update implementation_plan.md tracking table
- [ ] Update pyproject.toml version to 0.2.0
- [ ] Tag release: `git tag v0.2.0`

### Wave 4 (WP-25 — NL→Cypher hardening, separate release band)
- [ ] **Wave 4-pre**: land PromptBuilder refactor on `main` (1 agent, sequential). Verify `pytest -m "not integration and not tck"` is bit-identical green.
- [ ] **Wave 4a**: launch WP-25.1, WP-25.2, WP-25.3, WP-25.4 in parallel (4 agents, separate branches from `main`).
- [ ] Review each branch: unit tests pass, offline behaviour preserved, `ruff check .` clean.
- [ ] **Wave 4b**: merge 4a branches sequentially into an integration branch; resolve `nl2cypher/__init__.py` / `nl2cypher.py` conflicts (expect small overlaps in `PromptBuilder` section wiring and in `_call_llm_with_retry`).
- [ ] Full test run: `pytest -m "not integration and not tck"` — all green.
- [ ] **Wave 4c**: launch WP-25.5 (1 agent, sequential). Produce initial baseline report, commit `tests/nl2cypher/eval/baseline.json`.
- [ ] Manual smoke with a live LLM + DB on the Movies fixture: verify each of few-shot / entity resolution / execution-grounded actually fires on representative questions.
- [ ] Update PRD §1.2.1: mark implemented techniques with `*(implemented)*` and link the baseline report.
- [ ] Update `docs/implementation_plan.md` status table WP-25 row to **Done** with the merge date.
