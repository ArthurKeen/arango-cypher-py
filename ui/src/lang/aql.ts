import {
  LanguageSupport,
  StreamLanguage,
  type StreamParser,
} from "@codemirror/language";
import {
  autocompletion,
  completionKeymap,
  type CompletionContext,
  type CompletionResult,
  type Completion,
  snippetCompletion,
} from "@codemirror/autocomplete";
import { keymap } from "@codemirror/view";

// ---------------------------------------------------------------------------
// Keywords & functions
// ---------------------------------------------------------------------------

const aqlKeywordList = [
  "FOR", "IN", "FILTER", "RETURN", "LET", "SORT", "LIMIT", "COLLECT",
  "INTO", "WITH", "COUNT", "INSERT", "UPDATE", "REPLACE", "REMOVE",
  "UPSERT", "GRAPH", "OUTBOUND", "INBOUND", "ANY", "ALL", "NONE",
  "SHORTEST_PATH", "K_SHORTEST_PATHS", "PRUNE", "SEARCH", "OPTIONS",
  "AGGREGATE", "LIKE", "NOT", "AND", "OR", "TRUE", "FALSE", "NULL",
  "DISTINCT", "ASC", "DESC", "WINDOW",
];

const aqlFunctionList = [
  "LENGTH", "COUNT", "SUM", "MIN", "MAX", "AVG", "MEDIAN", "STDDEV",
  "VARIANCE", "UNIQUE", "SORTED_UNIQUE", "FIRST", "LAST", "NTH",
  "PUSH", "POP", "APPEND", "UNSHIFT", "SHIFT", "SLICE", "REVERSE",
  "FLATTEN", "MERGE", "UNSET", "KEEP", "ATTRIBUTES", "VALUES", "ZIP",
  "HAS", "DOCUMENT", "PARSE_IDENTIFIER", "IS_NULL", "IS_STRING",
  "IS_NUMBER", "IS_BOOL", "IS_ARRAY", "IS_OBJECT", "TO_NUMBER",
  "TO_STRING", "TO_BOOL", "TO_ARRAY", "CONCAT", "CONCAT_SEPARATOR",
  "LIKE", "CONTAINS", "UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM",
  "SPLIT", "SUBSTITUTE", "SUBSTRING", "LEFT", "RIGHT",
  "REGEX_TEST", "REGEX_MATCHES", "REGEX_SPLIT", "REGEX_REPLACE",
  "DATE_NOW", "DATE_ISO8601", "DATE_TIMESTAMP", "DATE_ADD", "DATE_SUBTRACT",
  "DATE_DIFF", "DATE_COMPARE", "DATE_FORMAT", "DATE_YEAR", "DATE_MONTH",
  "DATE_DAY", "DATE_HOUR", "DATE_MINUTE", "DATE_SECOND", "DATE_DAYOFWEEK",
  "RAND", "RANGE", "UNION", "UNION_DISTINCT", "INTERSECTION", "MINUS",
  "BM25", "TFIDF", "ANALYZER", "TOKENS", "PHRASE", "STARTS_WITH", "EXISTS",
  "BOOST", "GEO_POINT", "GEO_DISTANCE", "GEO_CONTAINS", "GEO_INTERSECTS",
  "FULLTEXT", "NEAR", "WITHIN", "WITHIN_RECTANGLE",
  "COSINE_SIMILARITY", "L2_DISTANCE", "VALUE",
  "IS_SAME_COLLECTION", "COLLECTION_COUNT", "COLLECTIONS",
  "NOT_NULL", "POSITION", "JSON_STRINGIFY", "JSON_PARSE",
  "APPLY", "CALL", "ASSERT", "WARN",
];

const aqlKeywords = new Set(aqlKeywordList);
const aqlFunctions = new Set(aqlFunctionList);

// ---------------------------------------------------------------------------
// Completions
// ---------------------------------------------------------------------------

const keywordCompletions: Completion[] = aqlKeywordList.map((kw) => ({
  label: kw,
  type: "keyword",
  boost: kw === "RETURN" || kw === "FILTER" || kw === "FOR" ? 2 : 0,
}));

const functionCompletions: Completion[] = aqlFunctionList.map((fn) => ({
  label: fn + "()",
  displayLabel: fn,
  type: "function",
  apply: fn + "()",
  info: "function",
}));

const snippets: Completion[] = [
  snippetCompletion("FOR ${1:doc} IN ${2:collection}\n  ${3}", {
    label: "FOR ... IN",
    type: "keyword",
    detail: "loop",
    boost: 3,
  }),
  snippetCompletion("FOR ${1:v}, ${2:e}, ${3:p} IN ${4:1}..${5:1} OUTBOUND ${6:startVertex} ${7:edgeCollection}\n  ${8}", {
    label: "FOR ... OUTBOUND",
    type: "keyword",
    detail: "traversal",
    boost: 3,
  }),
  snippetCompletion("FOR ${1:v}, ${2:e}, ${3:p} IN ${4:1}..${5:1} INBOUND ${6:startVertex} ${7:edgeCollection}\n  ${8}", {
    label: "FOR ... INBOUND",
    type: "keyword",
    detail: "traversal",
    boost: 3,
  }),
  snippetCompletion("FILTER ${1:condition}", {
    label: "FILTER",
    type: "keyword",
    detail: "filter rows",
    boost: 2,
  }),
  snippetCompletion("COLLECT ${1:key} = ${2:expr} INTO ${3:groups}\n  ${4}", {
    label: "COLLECT ... INTO",
    type: "keyword",
    detail: "group by",
    boost: 2,
  }),
  snippetCompletion("COLLECT AGGREGATE ${1:name} = ${2:COUNT}(${3:doc})", {
    label: "COLLECT AGGREGATE",
    type: "keyword",
    detail: "aggregate",
    boost: 2,
  }),
  snippetCompletion("LET ${1:name} = ${2:expression}", {
    label: "LET ... =",
    type: "keyword",
    detail: "variable binding",
    boost: 2,
  }),
  snippetCompletion("LET ${1:name} = (\n  FOR ${2:doc} IN ${3:collection}\n    ${4:FILTER ...}\n    RETURN ${5:doc}\n)", {
    label: "LET ... = (subquery)",
    type: "keyword",
    detail: "subquery binding",
    boost: 2,
  }),
  snippetCompletion("SORT ${1:expr} ${2:ASC}", {
    label: "SORT ... ASC/DESC",
    type: "keyword",
    detail: "sort results",
    boost: 1,
  }),
  snippetCompletion("LIMIT ${1:offset}, ${2:count}", {
    label: "LIMIT offset, count",
    type: "keyword",
    detail: "paginate",
    boost: 1,
  }),
  snippetCompletion("RETURN {\n  ${1:key}: ${2:value}\n}", {
    label: "RETURN { ... }",
    type: "keyword",
    detail: "return object",
    boost: 1,
  }),
  snippetCompletion("RETURN DISTINCT ${1:expr}", {
    label: "RETURN DISTINCT",
    type: "keyword",
    detail: "unique results",
    boost: 1,
  }),
  snippetCompletion("INSERT ${1:doc} INTO ${2:collection}", {
    label: "INSERT ... INTO",
    type: "keyword",
    detail: "insert document",
  }),
  snippetCompletion("UPDATE ${1:doc} WITH ${2:obj} IN ${3:collection}", {
    label: "UPDATE ... WITH ... IN",
    type: "keyword",
    detail: "update document",
  }),
  snippetCompletion("REMOVE ${1:doc} IN ${2:collection}", {
    label: "REMOVE ... IN",
    type: "keyword",
    detail: "remove document",
  }),
  snippetCompletion("UPSERT ${1:search} INSERT ${2:insert} UPDATE ${3:update} IN ${4:collection}", {
    label: "UPSERT ... INSERT ... UPDATE ... IN",
    type: "keyword",
    detail: "upsert document",
  }),
  snippetCompletion("OPTIONS { indexHint: \"${1:indexName}\" }", {
    label: "OPTIONS { indexHint }",
    type: "keyword",
    detail: "query hint",
  }),
];

const allStaticCompletions = [...snippets, ...keywordCompletions, ...functionCompletions];

// ---------------------------------------------------------------------------
// Scope analysis — extract variables and their collection bindings
// ---------------------------------------------------------------------------

interface VarBinding {
  name: string;
  collection: string;
  bindParam: string;
  kind: "for" | "let" | "collect" | "traversal_vertex" | "traversal_edge" | "traversal_path";
  line: number;
}

const FOR_TRAV_RE = /\bFOR\s+(\w+)(?:\s*,\s*(\w+))?(?:\s*,\s*(\w+))?\s+IN\s+\d+\.\.\d+\s+(?:OUTBOUND|INBOUND|ANY)\s+(\S+)\s+(@@\w+|\w+)/gi;
const FOR_IN_RE = /\bFOR\s+(\w+)\s+IN\s+(@@\w+|\w+)/gi;
const LET_RE = /\bLET\s+(\w+)\s*=/gi;
// COLLECT key = expr, COLLECT key = expr INTO group, COLLECT AGGREGATE name = FUNC(...)
const COLLECT_RE = /\bCOLLECT\b(.*)/gi;
const COLLECT_VAR_RE = /(\w+)\s*=/g;
const COLLECT_INTO_RE = /\bINTO\s+(\w+)/i;

function extractVariables(doc: string): VarBinding[] {
  const vars: VarBinding[] = [];
  const lines = doc.split("\n");

  for (let lineIdx = 0; lineIdx < lines.length; lineIdx++) {
    const line = lines[lineIdx];

    // Traversal FOR (must check before simple FOR)
    FOR_TRAV_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = FOR_TRAV_RE.exec(line)) !== null) {
      const [, vVar, eVar, pVar, , edgeColl] = m;
      vars.push({ name: vVar, collection: "", bindParam: "", kind: "traversal_vertex", line: lineIdx });
      if (eVar) vars.push({ name: eVar, collection: edgeColl, bindParam: edgeColl.startsWith("@@") ? edgeColl : "", kind: "traversal_edge", line: lineIdx });
      if (pVar) vars.push({ name: pVar, collection: "", bindParam: "", kind: "traversal_path", line: lineIdx });
    }

    // Simple FOR ... IN
    FOR_IN_RE.lastIndex = 0;
    while ((m = FOR_IN_RE.exec(line)) !== null) {
      const [, varName, collRef] = m;
      if (/\d+\.\.\d+/.test(line)) continue;
      vars.push({
        name: varName,
        collection: collRef.startsWith("@@") ? "" : collRef,
        bindParam: collRef.startsWith("@@") ? collRef : "",
        kind: "for",
        line: lineIdx,
      });
    }

    // LET bindings
    LET_RE.lastIndex = 0;
    while ((m = LET_RE.exec(line)) !== null) {
      vars.push({ name: m[1], collection: "", bindParam: "", kind: "let", line: lineIdx });
    }

    // COLLECT bindings: COLLECT key = expr, COLLECT AGGREGATE name = FUNC(...)
    COLLECT_RE.lastIndex = 0;
    while ((m = COLLECT_RE.exec(line)) !== null) {
      const tail = m[1];
      // Extract INTO group var
      const intoMatch = COLLECT_INTO_RE.exec(tail);
      if (intoMatch) {
        vars.push({ name: intoMatch[1], collection: "", bindParam: "", kind: "collect", line: lineIdx });
      }
      // Extract each assignment: name = ...
      // Split on INTO to avoid capturing the INTO variable as an assignment
      const beforeInto = intoMatch ? tail.slice(0, tail.toUpperCase().indexOf("INTO")) : tail;
      COLLECT_VAR_RE.lastIndex = 0;
      let vm: RegExpExecArray | null;
      while ((vm = COLLECT_VAR_RE.exec(beforeInto)) !== null) {
        const vname = vm[1];
        if (vname.toUpperCase() === "AGGREGATE") continue;
        vars.push({ name: vname, collection: "", bindParam: "", kind: "collect", line: lineIdx });
      }
    }
  }

  return vars;
}

// ---------------------------------------------------------------------------
// Schema context for property resolution
// ---------------------------------------------------------------------------

export interface AqlSchemaContext {
  /** mapping.physical_mapping.entities: { Label: { collectionName, properties: { propName: {...} } } } */
  entities: Record<string, { collectionName: string; properties: Record<string, unknown> }>;
  /** mapping.physical_mapping.relationships: { Type: { edgeCollectionName, properties: { propName: {...} } } } */
  relationships: Record<string, { edgeCollectionName: string; properties: Record<string, unknown> }>;
  /** bind vars: { "@@uCollection": "Device", ... } */
  bindVars: Record<string, unknown>;
}

function resolveProperties(
  varBinding: VarBinding,
  schema: AqlSchemaContext | null,
): Completion[] {
  if (!schema) return [];

  let collectionName = varBinding.collection;

  // Resolve bind param to actual collection name
  if (!collectionName && varBinding.bindParam) {
    const paramKey = varBinding.bindParam.replace(/^@@/, "@");
    collectionName = (schema.bindVars[paramKey] as string) || "";
  }

  if (!collectionName) return [];

  // Find entity by collection name
  for (const [label, ent] of Object.entries(schema.entities)) {
    if (ent.collectionName === collectionName || label === collectionName) {
      return Object.keys(ent.properties).map((prop) => ({
        label: prop,
        type: "property" as const,
        detail: label,
        boost: 5,
      }));
    }
  }

  // Find relationship by edge collection name
  for (const [relType, rel] of Object.entries(schema.relationships)) {
    if (rel.edgeCollectionName === collectionName || relType === collectionName) {
      const props = Object.keys(rel.properties).map((prop) => ({
        label: prop,
        type: "property" as const,
        detail: relType,
        boost: 5,
      }));
      // Edge documents always have _from, _to, _key, _id, _rev
      props.push(
        { label: "_from", type: "property" as const, detail: "system", boost: 3 },
        { label: "_to", type: "property" as const, detail: "system", boost: 3 },
        { label: "_key", type: "property" as const, detail: "system", boost: 2 },
        { label: "_id", type: "property" as const, detail: "system", boost: 2 },
      );
      return props;
    }
  }

  return [];
}

const SYSTEM_PROPS: Completion[] = [
  { label: "_key", type: "property", detail: "system", boost: 3 },
  { label: "_id", type: "property", detail: "system", boost: 3 },
  { label: "_rev", type: "property", detail: "system", boost: 1 },
];

// ---------------------------------------------------------------------------
// Context-aware completion source
// ---------------------------------------------------------------------------

let _schemaCtx: AqlSchemaContext | null = null;

export function setAqlSchemaContext(ctx: AqlSchemaContext | null) {
  _schemaCtx = ctx;
}

function aqlCompletion(context: CompletionContext): CompletionResult | null {
  const doc = context.state.doc.toString();
  const pos = context.pos;
  const cursorLine = context.state.doc.lineAt(pos).number - 1;
  const vars = extractVariables(doc);

  // --- Property access: var.prop ---
  const dotAccess = context.matchBefore(/(\w+)\.\w*/);
  if (dotAccess) {
    const dotMatch = dotAccess.text.match(/^(\w+)\.(\w*)$/);
    if (dotMatch) {
      const [, varName, partial] = dotMatch;
      const binding = vars.find((v) => v.name === varName && v.line <= cursorLine);
      if (binding) {
        let propOptions = resolveProperties(binding, _schemaCtx);
        if (propOptions.length === 0) {
          propOptions = [...SYSTEM_PROPS];
        } else {
          propOptions = [...propOptions, ...SYSTEM_PROPS];
        }

        const from = dotAccess.from + varName.length + 1; // after the dot
        if (partial) {
          const upper = partial.toUpperCase();
          propOptions = propOptions.filter((o) => o.label.toUpperCase().startsWith(upper));
        }
        if (propOptions.length === 0) return null;

        return {
          from,
          options: propOptions,
          validFor: /^[a-zA-Z_]\w*$/,
        };
      }
    }
  }

  // --- Variable / keyword / function completions ---
  const word = context.matchBefore(/[a-zA-Z_]\w*/);
  if (!word) {
    if (!context.explicit) return null;
    const varCompletions = vars
      .filter((v) => v.line <= cursorLine)
      .map((v) => ({
        label: v.name,
        type: "variable" as const,
        detail: v.collection || v.bindParam || v.kind,
        boost: 10,
      }));
    return { from: context.pos, options: [...varCompletions, ...allStaticCompletions] };
  }
  if (word.from === word.to && !context.explicit) return null;

  // Don't suggest if we're right after a dot (handled above)
  const charBefore = word.from > 0 ? doc[word.from - 1] : "";
  if (charBefore === ".") return null;

  const prefix = word.text.toUpperCase();

  // In-scope variables
  const varCompletions: Completion[] = vars
    .filter((v) => v.line <= cursorLine)
    .filter((v) => v.name.toUpperCase().startsWith(prefix))
    .map((v) => ({
      label: v.name,
      type: "variable" as const,
      detail: v.collection || v.bindParam || v.kind,
      boost: 10,
    }));

  // Deduplicate variable names
  const seenVars = new Set<string>();
  const uniqueVars = varCompletions.filter((v) => {
    if (seenVars.has(v.label)) return false;
    seenVars.add(v.label);
    return true;
  });

  const staticOptions = allStaticCompletions.filter((o) => {
    const label = (o.displayLabel || o.label).toUpperCase();
    return label.startsWith(prefix) || label.includes(prefix);
  });

  const options = [...uniqueVars, ...staticOptions];
  if (options.length === 0) return null;

  return {
    from: word.from,
    options,
    validFor: /^[a-zA-Z_]\w*$/,
  };
}

// ---------------------------------------------------------------------------
// Stream parser (syntax highlighting)
// ---------------------------------------------------------------------------

const parser: StreamParser<{ inString: string | null; inComment: boolean }> = {
  startState() {
    return { inString: null, inComment: false };
  },

  token(stream, state) {
    if (state.inComment) {
      if (stream.match("*/")) {
        state.inComment = false;
        return "blockComment";
      }
      stream.next();
      return "blockComment";
    }

    if (state.inString) {
      const quote = state.inString;
      while (!stream.eol()) {
        const ch = stream.next();
        if (ch === "\\") {
          stream.next();
        } else if (ch === quote) {
          state.inString = null;
          return "string";
        }
      }
      return "string";
    }

    if (stream.match("//")) {
      stream.skipToEnd();
      return "lineComment";
    }

    if (stream.match("/*")) {
      state.inComment = true;
      return "blockComment";
    }

    if (stream.match(/^"/) || stream.match(/^'/)) {
      state.inString = stream.current();
      return "string";
    }

    if (stream.match(/^@@[a-zA-Z_]\w*/)) {
      return "variableName.special";
    }
    if (stream.match(/^@[a-zA-Z_]\w*/)) {
      return "variableName.special";
    }

    if (stream.match(/^-?\d+(\.\d+)?([eE][+-]?\d+)?/)) {
      return "number";
    }

    if (stream.match(/^[a-zA-Z_]\w*/)) {
      const word = stream.current();
      const upper = word.toUpperCase();
      if (aqlKeywords.has(upper)) return "keyword";
      if (aqlFunctions.has(upper)) return "function";
      return "variableName";
    }

    if (stream.match(/^[<>=!]+/) || stream.match(/^[-+*/%]/)) {
      return "operator";
    }

    if (stream.match(/^[()[\]{}]/)) {
      return "bracket";
    }

    if (stream.match(/^[,;.:]/)) {
      return "punctuation";
    }

    stream.next();
    return null;
  },
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function aql() {
  return new LanguageSupport(StreamLanguage.define(parser), [
    autocompletion({
      override: [aqlCompletion],
      activateOnTyping: true,
      maxRenderedOptions: 30,
    }),
    keymap.of(completionKeymap),
  ]);
}
