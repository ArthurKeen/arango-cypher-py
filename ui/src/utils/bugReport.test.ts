import { describe, expect, it } from "vitest";

import {
  DEFAULT_TITLE,
  MAX_ISSUE_URL_LENGTH,
  MAX_TITLE_LENGTH,
  PUBLIC_NOTE,
  QUERY_OMITTED,
  buildDetails,
  buildIssue,
  composeBody,
  maskValues,
  sliceChars,
  stripAttemptedQuery,
  type ReportContext,
} from "./bugReport";

const base: ReportContext = {
  appVersion: "0.2.0",
  analyzerVersion: "0.14.1",
  // As the Cypher parser reports it: the error echoes the query, values included.
  error: "Cypher syntax error at 1:34: no viable alternative at input 'MATCH (r:Role {name: 'prod-admin' 'x''",
  userAgent: "Mozilla/5.0 Test",
  includeQuery: false,
  cypher: "MATCH (r:AwsIamRole {name: 'prod-admin'}) RETURN r",
  aql: "FOR r IN @@collection FILTER r.name == @v RETURN r",
};

// As the NL pipeline reports a failed generation: the model's last Cypher is
// appended to the explanation.
const NL_FAILURE =
  "Could not produce valid Cypher after 3 attempts. Last attempted Cypher was:\n\n" +
  "MATCH (a:Account)-[:OWNED_BY]->(c:Customer) WHERE a.accountNumber = 4417123456789113 RETURN c";

describe("buildDetails", () => {
  it("leads with the versions", () => {
    const d = buildDetails(base);
    expect(d.startsWith("## Environment")).toBe(true);
    expect(d).toContain("- arango-cypher-py: 0.2.0");
    expect(d).toContain("- arangodb-schema-analyzer: 0.14.1");
  });

  it("masks values the error echoes from the query unless the query is included", () => {
    expect(buildDetails(base)).not.toContain("prod-admin");
    expect(buildDetails(base)).toContain("Cypher syntax error at 1:34");
    expect(buildDetails({ ...base, includeQuery: true })).toContain("prod-admin");
  });

  it("leaves the attempted Cypher of an NL failure out unless the query is included", () => {
    const masked = buildDetails({ ...base, error: NL_FAILURE });
    expect(masked).not.toContain("4417123456789113");
    expect(masked).not.toContain("OWNED_BY");
    expect(masked).toContain(QUERY_OMITTED);
    expect(buildDetails({ ...base, error: NL_FAILURE, includeQuery: true })).toContain("4417123456789113");
  });

  it("never includes the Cypher or AQL by default", () => {
    const d = buildDetails(base);
    expect(d).not.toContain("```cypher");
    expect(d).not.toContain("@@collection");
  });

  it("includes the Cypher and AQL when the user ticks it", () => {
    const d = buildDetails({ ...base, includeQuery: true });
    expect(d).toContain("```cypher\nMATCH (r:AwsIamRole {name: 'prod-admin'}) RETURN r\n```");
    expect(d).toContain("```aql\nFOR r IN @@collection");
  });

  it("keeps a code block intact when the text itself contains backticks", () => {
    expect(buildDetails({ ...base, error: "bad token ``` here", includeQuery: true })).toContain(
      "````text\nbad token ``` here\n````",
    );
  });

  it("treats a missing or blank error as none", () => {
    for (const error of [null, "", "  \n "]) {
      expect(buildDetails({ ...base, error })).toContain("(no error message shown)");
    }
  });
});

describe("stripAttemptedQuery", () => {
  it("cuts at either marker the NL pipeline uses", () => {
    expect(stripAttemptedQuery("Failed. Last attempt was:\n\nMATCH (n) RETURN n")).toBe(
      `Failed. Last attempt was: ${QUERY_OMITTED}`,
    );
    expect(stripAttemptedQuery("no marker here")).toBe("no marker here");
  });
});

describe("maskValues", () => {
  it("masks from the first opening quote to the last closing quote, nested echoes included", () => {
    expect(maskValues("no viable alternative at input 'MATCH (r {name: 'prod-admin' 'x''")).toBe(
      "no viable alternative at input '…'",
    );
  });

  it("does not treat apostrophes in prose as quotes", () => {
    expect(maskValues("Can't resolve property ssn on Person")).toBe("Can't resolve property ssn on Person");
    expect(maskValues("the reply's JSON did not parse (no object)")).toBe(
      "the reply's JSON did not parse (no object)",
    );
  });

  it("masks backtick and typographic quotes", () => {
    expect(maskValues("unknown label `AcmeCustomers` here")).toBe("unknown label '…' here");
    expect(maskValues("value ‘secret’ and “x” and «y» end")).toBe("value '…' end");
  });

  it("masks long digit runs such as account numbers and ids", () => {
    expect(maskValues("document users/8812345 not found; card 4417123456789113")).toBe(
      "document users/… not found; card …",
    );
    expect(maskValues("line 12, column 34")).toBe("line 12, column 34");
  });

  it("masks a dangling quote to the end of its line only", () => {
    expect(maskValues("collection or view not found\nnear 'abc")).toBe("collection or view not found\nnear '…'");
  });
});

describe("composeBody", () => {
  it("puts the public notice and the user's text first", () => {
    const body = composeBody("It crashed when I clicked Run", "## Environment");
    expect(body.startsWith(PUBLIC_NOTE)).toBe(true);
    expect(body).toContain("## What happened\n\nIt crashed when I clicked Run\n\n## Environment");
  });
});

describe("sliceChars", () => {
  it("never splits a character in two", () => {
    const cut = sliceChars("a".repeat(79) + "😀 rest", 80);
    expect(cut.endsWith("😀")).toBe(true);
    expect(() => encodeURIComponent(cut)).not.toThrow();
  });
});

describe("buildIssue", () => {
  const details = buildDetails(base);

  it("opens the repository's pre-filled new-issue page", () => {
    const { url, shortened } = buildIssue("T & x", "B", "## Environment", "acme/repo");
    expect(url.startsWith("https://github.com/acme/repo/issues/new?title=T%20%26%20x&body=")).toBe(true);
    expect(shortened).toBe(false);
  });

  it("uses the default title when the title is blank", () => {
    expect(decodeURIComponent(buildIssue("  ", "", details).url)).toContain(`title=${DEFAULT_TITLE}`);
  });

  it("does not throw on emoji at any cut point", () => {
    for (let n = 1500; n < 1700; n += 7) {
      const d = buildDetails({ ...base, includeQuery: true, cypher: "RETURN '" + "😀".repeat(n) + "'" });
      expect(() => buildIssue("t", "😀👍🏽 ".repeat(n), d)).not.toThrow();
    }
  });

  it("shortens the user's text first, so a long description keeps the versions and the error", () => {
    const { url, shortened } = buildIssue("t", "x".repeat(20000), details);
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(shortened).toBe(true);
    const text = decodeURIComponent(url);
    expect(text).toContain("arangodb-schema-analyzer: 0.14.1");
    expect(text).toContain("## Error");
    expect(text).toContain("Report shortened to fit");
  });

  it("keeps the versions when a long query forces trimming", () => {
    const d = buildDetails({ ...base, includeQuery: true, aql: "FOR x IN y ".repeat(1200) });
    const { url } = buildIssue("t", "", d);
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(decodeURIComponent(url)).toContain("arangodb-schema-analyzer: 0.14.1");
    expect(decodeURIComponent(url)).toContain("This issue is public");
  });

  it("closes a code block the trim cut open, even after one the user left open", () => {
    const d = buildDetails({ ...base, includeQuery: true, aql: "FOR x IN y ".repeat(1200) });
    const decoded = decodeURIComponent(buildIssue("t", "see:\n```\nstack", d).url.split("&body=")[1]);
    const lines = decoded.split("\n");
    const noteLine = lines.findIndex((l) => l.includes("Report shortened to fit"));
    let open = false;
    for (const line of lines.slice(0, noteLine)) {
      const m = /^(`{3,})(.*)$/.exec(line);
      if (!m) continue;
      open = open ? m[2].trim() !== "" : true;
    }
    expect(open).toBe(false);
  });

  it("caps a long title so the URL stays under the limit", () => {
    const { url } = buildIssue("t".repeat(9000), "b", "d");
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(decodeURIComponent(url.split("title=")[1].split("&")[0]).length).toBe(MAX_TITLE_LENGTH);
  });
});
