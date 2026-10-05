import { describe, expect, it } from "vitest";

import { MAX_ISSUE_URL_LENGTH, buildReport, issueUrl, type ReportContext } from "./bugReport";

const base: ReportContext = {
  appVersion: "0.2.0",
  analyzerVersion: "0.14.1",
  error: "Translation failed: A single label is required in v0 subset\nat line 2",
  userAgent: "Mozilla/5.0 Test",
  includeQuery: false,
  cypher: "MATCH (r:AwsIamRole {name: 'prod-admin'}) RETURN r",
  aql: "FOR r IN @@collection FILTER r.name == @v RETURN r",
};

describe("buildReport", () => {
  it("titles the issue with the error's first line", () => {
    expect(buildReport(base).title).toBe("Workbench: Translation failed: A single label is required in v0 subset");
  });

  it("carries versions and the error, and never the query by default", () => {
    const { body } = buildReport(base);
    expect(body).toContain("- arango-cypher-py: 0.2.0");
    expect(body).toContain("- arangodb-schema-analyzer: 0.14.1");
    expect(body).toContain("A single label is required");
    expect(body).not.toContain("prod-admin");
    expect(body).not.toContain("@@collection");
  });

  it("includes the Cypher and AQL only when the user ticks it", () => {
    const { body } = buildReport({ ...base, includeQuery: true });
    expect(body).toContain("```cypher\nMATCH (r:AwsIamRole {name: 'prod-admin'}) RETURN r\n```");
    expect(body).toContain("```aql\nFOR r IN @@collection");
  });

  it("keeps a code block intact when the text itself contains backticks", () => {
    const { body } = buildReport({ ...base, error: "bad token ``` here" });
    expect(body).toContain("````text\nbad token ``` here\n````");
  });

  it("says the issue is public", () => {
    expect(buildReport(base).body).toContain("This issue is public");
  });

  it("handles a report with no error and unknown versions", () => {
    const { title, body } = buildReport({ ...base, error: null, appVersion: null, analyzerVersion: null });
    expect(title).toBe("Workbench: ");
    expect(body).toContain("(no error message shown)");
    expect(body).toContain("- arango-cypher-py: unknown");
  });
});

describe("issueUrl", () => {
  it("opens the repository's pre-filled new-issue page", () => {
    const url = issueUrl({ title: "T & x", body: "B" }, "acme/repo");
    expect(url).toBe("https://github.com/acme/repo/issues/new?title=T%20%26%20x&body=B");
  });

  it("shortens a long body to fit GitHub's link limit and says so", () => {
    const url = issueUrl({ title: "Long", body: "x".repeat(20000) });
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(decodeURIComponent(url)).toContain("Report shortened to fit");
  });
});
