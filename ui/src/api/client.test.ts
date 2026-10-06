import { describe, expect, it } from "vitest";
import { afterEach, vi } from "vitest";
import {
  apiBaseFor,
  examplesForGraph,
  forceReacquireSchema,
  introspectSchema,
  normalizeSchemaWarnings,
  noteCode,
  type MinedExample,
} from "./client";

describe("apiBaseFor", () => {
  it("uses the platform mount when the SPA is served at the service root", () => {
    // The platform app launcher opens the bare mount. An empty base here sent
    // /connect to the cluster root: ArangoDB's `unknown path '/connect'`.
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py/")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("treats a mount opened without its trailing slash as a directory", () => {
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("drops an index.html page from the path", () => {
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py/index.html")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("strips the /frontend and /ui sub-mounts to reach the API", () => {
    expect(apiBaseFor("/_service/uds/_db/d/app/frontend/")).toBe("/_service/uds/_db/d/app");
    expect(apiBaseFor("/_service/uds/_global/app/ui/index.html")).toBe("/_service/uds/_global/app");
    expect(apiBaseFor("/frontend/")).toBe("");
    expect(apiBaseFor("/ui")).toBe("");
  });

  it("is empty for local dev at the origin root", () => {
    expect(apiBaseFor("/")).toBe("");
    expect(apiBaseFor("")).toBe("");
  });

  it("compares whole segments, not substrings", () => {
    // The old indexOf("/ui") matched an instance named `ui-demo` and cut the
    // mount in half.
    expect(apiBaseFor("/_service/uds/_db/d/ui-demo/")).toBe("/_service/uds/_db/d/ui-demo");
    expect(apiBaseFor("/_service/uds/_db/frontend-db/app/")).toBe("/_service/uds/_db/frontend-db/app");
  });
});

describe("examplesForGraph", () => {
  const ex = (question: string, graph: string | null): MinedExample => ({
    question,
    cypher: "MATCH (n:A) RETURN n",
    params: {},
    aql: "FOR n IN a RETURN n",
    graph,
    source: { collection: "_queries", key: question, name: question, description: "" },
    verification: { verdict: "identical", verified_at: "2026-10-05T00:00:00+00:00" },
  });
  const all = [ex("iam", "IAM_DEMO"), ex("docs", "AWS_Security_Docs_CorpusGraph"), ex("editor", null)];

  it("keeps every example when no graph is selected", () => {
    expect(examplesForGraph(all, null).map((e) => e.question)).toEqual(["iam", "docs", "editor"]);
  });

  it("keeps the selected graph's examples and graph-less ones", () => {
    expect(examplesForGraph(all, "IAM_DEMO").map((e) => e.question)).toEqual(["iam", "editor"]);
  });
});

describe("normalizeSchemaWarnings", () => {
  it("makes a bare string renderable, dismissable and — for the baseline note — info", () => {
    const [w] = normalizeSchemaWarnings(["LLM provider not configured; returning deterministic baseline inference"]);
    expect(w.message).toBe("LLM provider not configured; returning deterministic baseline inference");
    expect(w.severity).toBe("info");
    expect(w.code).toMatch(/^note:llm-provider-not-configured-returning-de-[0-9a-f]{8}$/);
  });

  it("treats other strings as warnings", () => {
    expect(normalizeSchemaWarnings(["edge collection x has no endpoints"])[0].severity).toBe("warning");
  });

  it("keeps a server's structured warning and its install hint", () => {
    const w = {
      code: "ANALYZER_NOT_INSTALLED",
      message: "Install it",
      severity: "warning",
      install_hint: "pip install x",
    };
    expect(normalizeSchemaWarnings([w])).toEqual([w]);
  });

  it("drops entries with no usable message and never keeps non-string fields", () => {
    const out = normalizeSchemaWarnings([
      "",
      "  ",
      42,
      null,
      { code: "Z" },
      { code: "X", message: "  " },
      { code: 42, message: "m", severity: "loud", install_hint: { bad: true } },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].message).toBe("m");
    expect(out[0].code).toMatch(/^note:m-[0-9a-f]{8}$/);
    expect(out[0].severity).toBe("warning");
    expect(out[0].install_hint).toBeUndefined();
  });

  it("treats a missing list as no warnings", () => {
    expect(normalizeSchemaWarnings(undefined)).toEqual([]);
  });
});

describe("noteCode", () => {
  it("never collides for non-Latin messages or a shared prefix", () => {
    const codes = new Set([
      noteCode("スキーマ警告"),
      noteCode("Неизвестная коллекция"),
      noteCode("x".repeat(60) + " first"),
      noteCode("x".repeat(60) + " second"),
    ]);
    expect(codes.size).toBe(4);
  });

  it("is stable for the same message", () => {
    expect(noteCode("same text")).toBe(noteCode("same text"));
  });
});

describe("introspectSchema", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("normalizes the warnings it returns", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ entities: [], relationships: [], warnings: ["LLM provider not configured; x"] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const result = await introspectSchema("token");
    expect(result.warnings?.[0]).toMatchObject({ message: "LLM provider not configured; x", severity: "info" });
  });
});

describe("forceReacquireSchema", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("normalizes the warnings it returns", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ source: { kind: null, notes: null }, warnings: ["odd"], entity_count: 0, relationship_count: 0 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const result = await forceReacquireSchema("token");
    expect(result.warnings[0]).toMatchObject({ message: "odd", severity: "warning" });
  });
});
