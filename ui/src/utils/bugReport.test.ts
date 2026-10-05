import { describe, expect, it } from "vitest";

import {
  DEFAULT_TITLE,
  MAX_ISSUE_URL_LENGTH,
  MAX_TITLE_LENGTH,
  PUBLIC_NOTE,
  buildDetails,
  composeBody,
  issueUrl,
  maskQuotedValues,
  sliceChars,
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

describe("buildDetails", () => {
  it("leads with the versions so trimming never drops them", () => {
    const d = buildDetails(base);
    expect(d.startsWith("## Environment")).toBe(true);
    expect(d).toContain("- arango-cypher-py: 0.2.0");
    expect(d).toContain("- arangodb-schema-analyzer: 0.14.1");
  });

  it("masks values the error echoes from the query unless the query is included", () => {
    const masked = buildDetails(base);
    expect(masked).not.toContain("prod-admin");
    expect(masked).toContain("Cypher syntax error at 1:34");
    const shown = buildDetails({ ...base, includeQuery: true });
    expect(shown).toContain("prod-admin");
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
    expect(buildDetails({ ...base, error: "bad token ``` here" })).toContain("````text\nbad token ``` here\n````");
  });

  it("treats a missing or blank error as none", () => {
    for (const error of [null, "", "  \n "]) {
      expect(buildDetails({ ...base, error })).toContain("(no error message shown)");
    }
  });
});

describe("maskQuotedValues", () => {
  it("masks everything between the first and last quote on a line, nested echoes included", () => {
    expect(maskQuotedValues("no viable alternative at input 'MATCH (r {name: 'prod-admin' 'x''")).toBe(
      "no viable alternative at input '…'",
    );
    expect(maskQuotedValues(`unknown value "acme" near 'x' (pos 3)`)).toBe("unknown value '…' (pos 3)");
  });

  it("leaves lines without quotes alone and masks a dangling quote to the end", () => {
    expect(maskQuotedValues("collection or view not found\nnear 'abc")).toBe("collection or view not found\nnear '…'");
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
    const s = "a".repeat(79) + "😀 rest";
    const cut = sliceChars(s, 80);
    expect(cut.endsWith("😀")).toBe(true);
    expect(() => encodeURIComponent(cut)).not.toThrow();
  });
});

describe("issueUrl", () => {
  it("opens the repository's pre-filled new-issue page", () => {
    expect(issueUrl("T & x", "B", "acme/repo")).toBe("https://github.com/acme/repo/issues/new?title=T%20%26%20x&body=B");
  });

  it("uses the default title when the title is blank", () => {
    expect(decodeURIComponent(issueUrl("  ", "B"))).toContain(`title=${DEFAULT_TITLE}`);
  });

  it("does not throw on emoji at any cut point", () => {
    for (let n = 1500; n < 1700; n++) {
      const body = composeBody("", buildDetails({ ...base, includeQuery: true, cypher: "RETURN '" + "😀".repeat(n) + "'" }));
      expect(() => issueUrl("t", body)).not.toThrow();
    }
  });

  it("keeps the versions when a long query forces trimming", () => {
    const body = composeBody("", buildDetails({ ...base, includeQuery: true, aql: "FOR x IN y ".repeat(1200) }));
    const url = decodeURIComponent(issueUrl("t", body));
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(url).toContain("arangodb-schema-analyzer: 0.14.1");
    expect(url).toContain("This issue is public");
    expect(url).toContain("Report shortened to fit");
  });

  it("closes a code block the trim cut open, so the note is not inside it", () => {
    const body = composeBody("", buildDetails({ ...base, includeQuery: true, aql: "FOR x IN y ".repeat(1200) }));
    const decoded = decodeURIComponent(issueUrl("t", body).split("&body=")[1]);
    const fences = decoded.split("\n").filter((l) => /^`{3,}/.test(l)).length;
    expect(fences % 2).toBe(0);
  });

  it("caps a long title so the URL stays under the limit", () => {
    const url = issueUrl("t".repeat(9000), "b");
    expect(url.length).toBeLessThanOrEqual(MAX_ISSUE_URL_LENGTH);
    expect(decodeURIComponent(url.split("title=")[1].split("&")[0]).length).toBe(MAX_TITLE_LENGTH);
  });
});
