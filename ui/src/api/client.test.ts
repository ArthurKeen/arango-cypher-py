import { describe, expect, it } from "vitest";
import { apiBaseFor, examplesForGraph, normalizeSchemaWarnings, type MinedExample } from "./client";

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
  it("turns a bare string into a renderable, dismissable warning", () => {
    expect(normalizeSchemaWarnings(["LLM provider not configured; returning deterministic baseline inference"])).toEqual([
      {
        code: "note:llm-provider-not-configured-returning-deterministic-baseline",
        message: "LLM provider not configured; returning deterministic baseline inference",
      },
    ]);
  });

  it("keeps structured warnings and drops unusable entries", () => {
    const w = { code: "ANALYZER_NOT_INSTALLED", message: "Install it", install_hint: "pip install x" };
    expect(normalizeSchemaWarnings([w, "", "  ", 42, null, { code: "x" }])).toEqual([w]);
  });

  it("gives a structured warning without a code one derived from its message", () => {
    expect(normalizeSchemaWarnings([{ code: "", message: "Heads up" }])).toEqual([{ code: "note:Heads up", message: "Heads up" }]);
  });

  it("treats a missing list as no warnings", () => {
    expect(normalizeSchemaWarnings(undefined)).toEqual([]);
  });
});
