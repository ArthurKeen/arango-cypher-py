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
