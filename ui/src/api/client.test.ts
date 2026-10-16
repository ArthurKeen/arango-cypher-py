import { describe, expect, it } from "vitest";
import { afterEach, vi } from "vitest";
import {
  ApiError,
  CONNECTION_LOST_MESSAGE,
  parseErrorReport,
  reportNote,
  RESULT_ROW_LIMIT,
  apiBaseFor,
  examplesForGraph,
  executeAql,
  executeCypher,
  forceReacquireSchema,
  introspectSchema,
  watchSchemaRecheck,
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

describe("execute row cap", () => {
  afterEach(() => vi.unstubAllGlobals());

  // An unbounded result reached the browser as one 257 MB JSON document and
  // never arrived ("Failed to fetch"); every Run asks for at most this many.
  function stubExecute() {
    // Typed like fetch, so the test can read the URL and init it was given.
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({ results: [], aql: "", bind_vars: {}, warnings: [], truncated: true, row_limit: 1000 }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function sentBody(fetchMock: ReturnType<typeof stubExecute>): Record<string, unknown> {
    const init = fetchMock.mock.calls[0][1];
    return JSON.parse(String(init?.body));
  }

  it("asks /execute for at most RESULT_ROW_LIMIT rows", async () => {
    const fetchMock = stubExecute();
    const resp = await executeCypher({ cypher: "MATCH (n) RETURN n", mapping: {} }, "token");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/execute$/);
    expect(sentBody(fetchMock)).toMatchObject({ cypher: "MATCH (n) RETURN n", max_rows: RESULT_ROW_LIMIT });
    expect(resp.truncated).toBe(true);
  });

  it("asks /execute-aql for at most RESULT_ROW_LIMIT rows", async () => {
    const fetchMock = stubExecute();
    await executeAql("FOR n IN c RETURN n", { x: 1 }, "token");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/execute-aql$/);
    expect(sentBody(fetchMock)).toEqual({ aql: "FOR n IN c RETURN n", bind_vars: { x: 1 }, max_rows: RESULT_ROW_LIMIT });
  });
});

describe("a dropped connection", () => {
  afterEach(() => vi.unstubAllGlobals());

  // fetch reports a dropped connection as a bare TypeError ("Failed to
  // fetch") — even after a 200, while the body is still arriving.
  it("before any answer reads as a lost connection", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const err = await executeAql("RETURN 1", {}, "token").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe(CONNECTION_LOST_MESSAGE);
  });

  it("while a 200's body is arriving reads as a lost connection", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    const cut = {
      ok: true,
      status: 200,
      json: () => Promise.reject(new TypeError("network error")),
    } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => cut));
    const err = await executeAql("RETURN 1", {}, "token").catch((e: unknown) => e);
    expect((err as ApiError).message).toBe(CONNECTION_LOST_MESSAGE);
  });

  it("leaves other failures alone", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    const bad = { ok: true, status: 200, json: () => Promise.reject(new SyntaxError("bad json")) } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => bad));
    const err = await executeAql("RETURN 1", {}, "token").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SyntaxError);
  });
});

describe("errors the service reported to the team", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("say so, with the reference and how often it was seen", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () =>
        new Response(
          JSON.stringify({
            detail: { code: "GENERATED_AQL_REJECTED", message: "Arango Cypher translated this query into AQL that ArangoDB rejected." },
            report: { reference: "a1b2c3d4", seen: 3, issue: 7, status: "filed" },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const err = (await executeAql("RETURN 1", {}, "token").catch((e: unknown) => e)) as ApiError;
    expect(err.report).toEqual({ reference: "a1b2c3d4", seen: 3, issue: 7, status: "filed" });
    expect(err.code).toBe("GENERATED_AQL_REJECTED");
    expect(err.message).toBe(
      "Arango Cypher translated this query into AQL that ArangoDB rejected. " +
        "This was reported automatically to the Arango Cypher team (reference a1b2c3d4, seen 3 times so far).",
    );
  });

  it("calls the first one the first report", () => {
    expect(reportNote({ reference: "a1b2c3d4", seen: 1, issue: null, status: "pending" })).toContain("first report");
  });

  it("ignore a report that is not one", () => {
    expect(parseErrorReport(undefined)).toBeUndefined();
    expect(parseErrorReport({ reference: 5 })).toBeUndefined();
    expect(parseErrorReport({ reference: "x", seen: 2 })).toEqual({ reference: "x", seen: 2, issue: null, status: "pending" });
  });

  it("leave errors without a report unchanged", () => {
    expect(new ApiError(422, { message: "Unknown label" }).message).toBe("Unknown label");
  });
});

describe("watchSchemaRecheck", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const ready = (hash: string, rechecking: boolean) =>
    new Response(JSON.stringify({ status: "ready", entities: [], relationships: [], rechecking, schema_hash: hash }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  function stubPolls(...answers: Array<Response | Error>) {
    const fetchMock = vi.fn<typeof fetch>(async () => {
      const next = answers.shift();
      if (!next) throw new Error("unexpected poll");
      if (next instanceof Error) throw next;
      return next;
    });
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const current = { status: "ready" as const, entities: [], relationships: [], rechecking: true, schema_hash: "old" };

  it("swaps in the schema a finished recheck changed", async () => {
    vi.useFakeTimers();
    const fetchMock = stubPolls(ready("old", true), ready("new", false));
    const onChange = vi.fn();
    watchSchemaRecheck("t", current, onChange, { delayMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(onChange).not.toHaveBeenCalled(); // still rechecking
    await vi.advanceTimersByTimeAsync(1000);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ schema_hash: "new" }));
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(2); // and stops
  });

  it("leaves an unchanged schema alone", async () => {
    vi.useFakeTimers();
    stubPolls(ready("old", false));
    const onChange = vi.fn();
    watchSchemaRecheck("t", current, onChange, { delayMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does nothing when no recheck is running", async () => {
    vi.useFakeTimers();
    const fetchMock = stubPolls();
    watchSchemaRecheck("t", { ...current, rechecking: false }, vi.fn(), { delayMs: 1000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stops when cancelled or when a poll fails", async () => {
    vi.useFakeTimers();
    const fetchMock = stubPolls(new TypeError("Failed to fetch"));
    const onChange = vi.fn();
    watchSchemaRecheck("t", current, onChange, { delayMs: 1000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const cancel = watchSchemaRecheck("t", current, onChange, { delayMs: 1000 });
    cancel();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });
});
