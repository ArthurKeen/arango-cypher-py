/**
 * WP-30 reducer state-machine tests.
 *
 * Scope: pin the ``editorCypherSource`` provenance field and
 * ``lastNlQuestion`` bookkeeping that the translate-error banner
 * uses to gate the "Regenerate from NL with error hint" action.
 *
 * These are pure-reducer tests — no React, no DOM. Vitest runs them
 * under ``npm run test`` (see package.json). Importing the private
 * ``reducer`` via its ``initialState + action → next state`` shape
 * keeps the test surface tight to the public action contract.
 */
import { describe, expect, it } from "vitest";

import {
  type Action,
  type AppState,
  initialState,
} from "./store";

// Re-derive the reducer from ``useAppState``'s public observation:
// ``useReducer`` calls the reducer on every dispatch, so we can
// re-run it via a tiny driver. We import the reducer by re-exporting
// it from the module under test; vite's esbuild supports this fine.
// If the module ever stops exporting it, the build will fail loudly.
import { __reducerForTest as reducer } from "./store";

function apply(state: AppState, ...actions: Action[]): AppState {
  return actions.reduce(reducer, state);
}

describe("reducer: editorCypherSource state machine (WP-30)", () => {
  it("starts as null on a fresh state", () => {
    expect(initialState.editorCypherSource).toBeNull();
    expect(initialState.lastNlQuestion).toBeNull();
  });

  it("SET_CYPHER without source defaults to 'user'", () => {
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "MATCH (n) RETURN n",
    });
    expect(next.editorCypherSource).toBe("user");
    expect(next.cypher).toBe("MATCH (n) RETURN n");
  });

  it("SET_CYPHER with source='user' is idempotent on provenance", () => {
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "x",
      source: "user",
    });
    expect(next.editorCypherSource).toBe("user");
  });

  it("NL_SUCCESS flips provenance to 'nl_pipeline' and records the question", () => {
    const next = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "MATCH (p:Person) RETURN p",
      question: "find people",
    });
    expect(next.editorCypherSource).toBe("nl_pipeline");
    expect(next.lastNlQuestion).toBe("find people");
    expect(next.cypher).toBe("MATCH (p:Person) RETURN p");
  });

  it("user edit after NL_SUCCESS flips provenance back to 'user'", () => {
    const next = apply(
      initialState,
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (p:Person) RETURN p",
        question: "find people",
      },
      { type: "SET_CYPHER", cypher: "MATCH (p:Person) RETURN p LIMIT 10" },
    );
    expect(next.editorCypherSource).toBe("user");
    // The NL question is preserved across user edits — the regenerate
    // button is gated on ``editorCypherSource`` alone, so retaining
    // ``lastNlQuestion`` after a user edit is harmless and avoids
    // losing it if the user edits-then-regenerates.
    expect(next.lastNlQuestion).toBe("find people");
  });

  it("repeated NL_SUCCESS overwrites the question with the latest one", () => {
    const next = apply(
      initialState,
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (p:Person) RETURN p",
        question: "find people",
      },
      {
        type: "NL_SUCCESS",
        cypher: "MATCH (m:Movie) RETURN m",
        question: "find movies",
      },
    );
    expect(next.lastNlQuestion).toBe("find movies");
    expect(next.cypher).toBe("MATCH (m:Movie) RETURN m");
    expect(next.editorCypherSource).toBe("nl_pipeline");
  });

  it("DISCONNECT resets both provenance and last question", () => {
    const withNl = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "x",
      question: "q",
    });
    expect(withNl.editorCypherSource).toBe("nl_pipeline");

    const next = apply(withNl, { type: "DISCONNECT" });
    expect(next.editorCypherSource).toBeNull();
    expect(next.lastNlQuestion).toBeNull();
  });

  it("SET_CYPHER explicit source='nl_pipeline' flips provenance without needing NL_SUCCESS", () => {
    // Not the recommended path (use NL_SUCCESS) but the union allows
    // it and the reducer must honour it so the type is not a lie.
    const next = apply(initialState, {
      type: "SET_CYPHER",
      cypher: "x",
      source: "nl_pipeline",
    });
    expect(next.editorCypherSource).toBe("nl_pipeline");
    // lastNlQuestion is NOT set by SET_CYPHER — callers that want
    // the banner's regenerate button should use NL_SUCCESS instead.
    expect(next.lastNlQuestion).toBeNull();
  });
});

describe("reducer: WP-30 regenerate-button gating invariants", () => {
  it("fresh state has the regenerate button hidden (source=null)", () => {
    const canRegenerate =
      initialState.editorCypherSource === "nl_pipeline" &&
      initialState.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });

  it("after NL_SUCCESS, regenerate is available", () => {
    const s = apply(initialState, {
      type: "NL_SUCCESS",
      cypher: "c",
      question: "q",
    });
    const canRegenerate =
      s.editorCypherSource === "nl_pipeline" && s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(true);
  });

  it("after NL_SUCCESS + user edit, regenerate is hidden (source=user)", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "SET_CYPHER", cypher: "c2" },
    );
    const canRegenerate =
      (s.editorCypherSource as string) === "nl_pipeline" &&
      s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });

  it("DISCONNECT removes the affordance even mid-session", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "DISCONNECT" },
    );
    const canRegenerate =
      s.editorCypherSource === "nl_pipeline" && s.lastNlQuestion !== null;
    expect(canRegenerate).toBe(false);
  });
});

describe("reducer: TRANSLATE_ERROR preserves provenance (WP-30)", () => {
  it("TRANSLATE_ERROR after NL_SUCCESS keeps source=nl_pipeline", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "TRANSLATE_START" },
      { type: "TRANSLATE_ERROR", error: "parse error at position 17" },
    );
    expect(s.editorCypherSource).toBe("nl_pipeline");
    expect(s.lastNlQuestion).toBe("q");
    expect(s.error).toBe("parse error at position 17");
    // The banner conditions on (error && source === "nl_pipeline"),
    // so this is the exact state where the regenerate button must
    // appear.
  });

  it("TRANSLATE_ERROR after user edit keeps source=user (no regenerate)", () => {
    const s = apply(
      initialState,
      { type: "NL_SUCCESS", cypher: "c", question: "q" },
      { type: "SET_CYPHER", cypher: "user typed this" },
      { type: "TRANSLATE_START" },
      { type: "TRANSLATE_ERROR", error: "parse error" },
    );
    expect(s.editorCypherSource).toBe("user");
  });
});
