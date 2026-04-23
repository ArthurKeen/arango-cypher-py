import { useCallback, useReducer } from "react";
import type { SchemaWarning } from "./client";

export interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  token: string | null;
  url: string;
  database: string;
  username: string;
  password: string;
  databases: string[];
  error: string | null;
}

export type ResultTab = "table" | "json" | "graph" | "explain" | "profile";

export interface HistoryEntry {
  cypher: string;
  timestamp: number;
  aqlPreview: string;
}

export interface AppState {
  connection: ConnectionState;
  cypher: string;
  mapping: Record<string, unknown>;
  params: Record<string, unknown>;
  aql: string;
  bindVars: Record<string, unknown>;
  results: unknown[] | null;
  warnings: Array<{ message: string }>;
  explainPlan: unknown | null;
  profileData: { statistics: Record<string, unknown>; profile: unknown } | null;
  activeResultTab: ResultTab;
  error: string | null;
  introspecting: boolean;
  translating: boolean;
  executing: boolean;
  explaining: boolean;
  profiling: boolean;
  history: HistoryEntry[];
  translateMs: number | null;
  execMs: number | null;
  activeStatement: number;
  // Backend-supplied schema warnings (ANALYZER_NOT_INSTALLED etc.). The
  // banner reads from this; the dismissal-suppression list lives in
  // localStorage keyed by (url, database, code) so the same warning can
  // re-appear on a different connection without leaking dismissals.
  schemaWarnings: SchemaWarning[];
  // WP-30: tracks the provenance of the Cypher currently sitting in
  // the editor. ``"nl_pipeline"`` after a successful NL→Cypher, and
  // ``"user"`` after any user edit / paste / sample load. The
  // translate-error banner exposes a one-click regenerate action only
  // when this is ``"nl_pipeline"`` — hand-written Cypher that fails
  // Translate is the user's query and must not be silently replaced.
  editorCypherSource: "nl_pipeline" | "user" | null;
  // WP-30: the NL question that produced the editor's current Cypher,
  // when ``editorCypherSource === "nl_pipeline"``. Used as the
  // question argument on regenerate-with-hint. Null when no NL
  // produced the current editor contents (either hand-written or no
  // NL issued in this session).
  lastNlQuestion: string | null;
}

const STORAGE_KEY = "cypher-workbench";

const MAX_HISTORY = 50;

function loadSavedState(): Partial<AppState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const saved = JSON.parse(raw);
    return {
      cypher: saved.cypher ?? "",
      mapping: saved.mapping ?? {},
      params: saved.params ?? {},
      history: Array.isArray(saved.history) ? saved.history.slice(0, MAX_HISTORY) : [],
    };
  } catch {
    return {};
  }
}

function saveState(state: AppState) {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        cypher: state.cypher,
        mapping: state.mapping,
        params: state.params,
        history: state.history.slice(0, MAX_HISTORY),
      }),
    );
  } catch {
    // localStorage may be unavailable
  }
}

export const initialState: AppState = {
  connection: {
    status: "disconnected",
    token: null,
    url: "http://localhost:8529",
    database: "_system",
    username: "root",
    password: "",
    databases: [],
    error: null,
  },
  cypher: "MATCH (p1:Person)-[:KNOWS]->(p2:Person)\nRETURN p1, p2",
  mapping: {},
  params: {},
  aql: "",
  bindVars: {},
  results: null,
  warnings: [],
  explainPlan: null,
  profileData: null,
  activeResultTab: "table",
  error: null,
  introspecting: false,
  translating: false,
  executing: false,
  explaining: false,
  profiling: false,
  history: [],
  translateMs: null,
  execMs: null,
  activeStatement: 0,
  schemaWarnings: [],
  editorCypherSource: null,
  lastNlQuestion: null,
  ...loadSavedState(),
};

export type Action =
  // WP-30: ``source`` lets callers declare whether the write came
  // from the NL pipeline or user input. Omitting ``source`` defaults
  // to ``"user"`` so existing dispatchers (editor onChange, sample
  // loads, history replay, paste) correctly flip the provenance flag
  // without every call site needing to be updated.
  | { type: "SET_CYPHER"; cypher: string; source?: "nl_pipeline" | "user" }
  // WP-30: compound action emitted after a successful NL→Cypher
  // translation. Sets the editor contents, flags the provenance as
  // ``"nl_pipeline"``, and records the NL question so the translate-
  // error regenerate-with-hint action can reuse it as the question
  // argument. Prefer this over ``SET_CYPHER + source: "nl_pipeline"``
  // because it keeps the cypher + question bookkeeping atomic.
  | { type: "NL_SUCCESS"; cypher: string; question: string }
  | { type: "SET_MAPPING"; mapping: Record<string, unknown> }
  | { type: "SET_MAPPING_JSON"; json: string }
  | {
      type: "CONNECT_START";
      url: string;
      database: string;
      username: string;
    }
  | {
      type: "CONNECT_SUCCESS";
      token: string;
      databases: string[];
      url: string;
      database: string;
      username: string;
      password: string;
    }
  | { type: "CONNECT_ERROR"; error: string }
  | { type: "DISCONNECT" }
  | { type: "INTROSPECT_START" }
  | {
      type: "INTROSPECT_SUCCESS";
      mapping: Record<string, unknown>;
      warnings?: SchemaWarning[];
    }
  | { type: "INTROSPECT_ERROR"; error: string }
  | { type: "SCHEMA_WARNINGS_REPLACE"; warnings: SchemaWarning[] }
  | { type: "SCHEMA_WARNINGS_CLEAR" }
  | { type: "TRANSLATE_START" }
  | {
      type: "TRANSLATE_SUCCESS";
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      translateMs?: number | null;
    }
  | { type: "TRANSLATE_ERROR"; error: string }
  | { type: "EXECUTE_START" }
  | { type: "EXECUTE_SUCCESS"; results: unknown[]; warnings?: Array<{ message: string }>; execMs?: number | null }
  | { type: "EXECUTE_ERROR"; error: string }
  | { type: "EXPLAIN_START" }
  | { type: "EXPLAIN_SUCCESS"; plan: unknown }
  | { type: "EXPLAIN_ERROR"; error: string }
  | { type: "PROFILE_START" }
  | {
      type: "PROFILE_SUCCESS";
      results: unknown[];
      statistics: Record<string, unknown>;
      profile: unknown;
    }
  | { type: "PROFILE_ERROR"; error: string }
  | { type: "SET_RESULT_TAB"; tab: ResultTab }
  | { type: "CLEAR_ERROR" }
  | { type: "SET_PARAMS"; params: Record<string, unknown> }
  | { type: "ADD_HISTORY"; entry: HistoryEntry }
  | { type: "CLEAR_HISTORY" }
  | { type: "SET_ACTIVE_STATEMENT"; index: number };

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_CYPHER":
      return {
        ...state,
        cypher: action.cypher,
        editorCypherSource: action.source ?? "user",
      };
    case "NL_SUCCESS":
      return {
        ...state,
        cypher: action.cypher,
        editorCypherSource: "nl_pipeline",
        lastNlQuestion: action.question,
      };
    case "SET_MAPPING":
      return { ...state, mapping: action.mapping };
    case "SET_MAPPING_JSON":
      try {
        return { ...state, mapping: JSON.parse(action.json) };
      } catch {
        return state;
      }
    case "CONNECT_START":
      // Track the attempted url/database/username on the connection state
      // immediately. If the attempt fails, CONNECT_ERROR keeps these fields
      // (it spreads ...state.connection), so the form-reset useEffect in
      // ConnectionDialog will re-seed the form with what the user actually
      // tried — not the hardcoded localhost default. Without this the
      // dialog silently snaps back to localhost:8529 after every failed
      // auto-connect, which hides the real (e.g. cloud) URL the user
      // would otherwise edit and retry.
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "connecting",
          url: action.url,
          database: action.database,
          username: action.username,
          error: null,
        },
      };
    case "CONNECT_SUCCESS":
      return {
        ...state,
        connection: {
          status: "connected",
          token: action.token,
          url: action.url,
          database: action.database,
          username: action.username,
          password: action.password,
          databases: action.databases,
          error: null,
        },
      };
    case "CONNECT_ERROR":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          error: action.error,
        },
      };
    case "DISCONNECT":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          token: null,
          databases: [],
          error: null,
        },
        results: null,
        explainPlan: null,
        profileData: null,
        schemaWarnings: [],
        editorCypherSource: null,
        lastNlQuestion: null,
      };
    case "INTROSPECT_START":
      return { ...state, introspecting: true };
    case "INTROSPECT_SUCCESS":
      return {
        ...state,
        introspecting: false,
        mapping: action.mapping,
        schemaWarnings: action.warnings ?? [],
      };
    case "INTROSPECT_ERROR":
      return { ...state, introspecting: false, error: action.error };
    case "SCHEMA_WARNINGS_REPLACE":
      return { ...state, schemaWarnings: action.warnings };
    case "SCHEMA_WARNINGS_CLEAR":
      return { ...state, schemaWarnings: [] };
    case "TRANSLATE_START":
      return { ...state, translating: true, error: null, translateMs: null };
    case "TRANSLATE_SUCCESS":
      return {
        ...state,
        translating: false,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        // Preserve a previously measured transpile time when the
        // dispatching caller didn't supply a fresh one (e.g. Run /
        // Explain / Profile after a manual Translate). Without this
        // guard the transpile-time badge gets clobbered to null the
        // moment the user executes, hiding it behind the exec-time
        // badge.
        translateMs:
          action.translateMs !== undefined ? action.translateMs : state.translateMs,
        error: null,
      };
    case "TRANSLATE_ERROR":
      return { ...state, translating: false, error: action.error };
    case "EXECUTE_START":
      return { ...state, executing: true, error: null, execMs: null };
    case "EXECUTE_SUCCESS":
      return {
        ...state,
        executing: false,
        results: action.results,
        warnings: action.warnings ?? state.warnings,
        execMs: action.execMs ?? null,
        activeResultTab: "table",
        error: null,
      };
    case "EXECUTE_ERROR":
      return { ...state, executing: false, error: action.error };
    case "EXPLAIN_START":
      return { ...state, explaining: true, error: null };
    case "EXPLAIN_SUCCESS":
      return {
        ...state,
        explaining: false,
        explainPlan: action.plan,
        activeResultTab: "explain",
        error: null,
      };
    case "EXPLAIN_ERROR":
      return { ...state, explaining: false, error: action.error };
    case "PROFILE_START":
      return { ...state, profiling: true, error: null };
    case "PROFILE_SUCCESS":
      return {
        ...state,
        profiling: false,
        results: action.results,
        profileData: {
          statistics: action.statistics,
          profile: action.profile,
        },
        activeResultTab: "profile",
        error: null,
      };
    case "PROFILE_ERROR":
      return { ...state, profiling: false, error: action.error };
    case "SET_RESULT_TAB":
      return { ...state, activeResultTab: action.tab };
    case "CLEAR_ERROR":
      return { ...state, error: null };
    case "SET_PARAMS":
      return { ...state, params: action.params };
    case "ADD_HISTORY": {
      const exists = state.history.some((h) => h.cypher === action.entry.cypher);
      const updated = exists
        ? [action.entry, ...state.history.filter((h) => h.cypher !== action.entry.cypher)]
        : [action.entry, ...state.history];
      return { ...state, history: updated.slice(0, MAX_HISTORY) };
    }
    case "CLEAR_HISTORY":
      return { ...state, history: [] };
    case "SET_ACTIVE_STATEMENT":
      return { ...state, activeStatement: action.index };
    default:
      return state;
  }
}

// WP-30: re-export the pure reducer for unit tests. Kept as a
// named ``__reducerForTest`` alias so production imports explicitly
// opt in — the public entry point remains ``useAppState``. This
// lets ``store.test.ts`` exercise every action's state transition
// without a React tree or a mocked dispatcher.
export { reducer as __reducerForTest };

export function useAppState() {
  const [state, dispatch] = useReducer(reducer, initialState);

  const PERSIST_ACTIONS = new Set([
    "SET_CYPHER", "NL_SUCCESS", "SET_MAPPING", "SET_PARAMS",
    "ADD_HISTORY", "CLEAR_HISTORY",
  ]);

  const persistAndDispatch = useCallback(
    (action: Action) => {
      dispatch(action);
      if (PERSIST_ACTIONS.has(action.type)) {
        const next = reducer(state, action);
        saveState(next);
      }
    },
    [state],
  );

  return [state, persistAndDispatch] as const;
}
