// "Report a problem": a pre-filled GitHub issue the user reviews and submits
// from their own browser. No token in the service, nothing sent without the
// user pressing Submit on GitHub — and the repository is public, so a report
// carries versions and the error by default; the query only when the user
// ticks "include my query" (it may contain names or values from their data).

export const ISSUE_REPOSITORY = "arango-solutions/arango-cypher";

// GitHub rejects new-issue URLs much past ~8 KB; stay well under it.
export const MAX_ISSUE_URL_LENGTH = 7000;

export interface ReportContext {
  appVersion: string | null;
  analyzerVersion: string | null;
  error: string | null;
  userAgent: string;
  includeQuery: boolean;
  cypher?: string;
  aql?: string;
}

export interface Report {
  title: string;
  body: string;
}

function fenced(language: string, text: string): string {
  // A fence longer than any backtick run inside keeps the block intact.
  const longest = Math.max(2, ...Array.from(text.matchAll(/`+/g), (m) => m[0].length));
  const fence = "`".repeat(longest + 1);
  return `${fence}${language}\n${text.trim()}\n${fence}`;
}

export function buildReport(ctx: ReportContext): Report {
  const firstLine = (ctx.error ?? "").split("\n")[0].trim();
  const title = firstLine ? `Workbench: ${firstLine.slice(0, 80)}` : "Workbench: ";
  const sections = [
    "## What happened\n\n<!-- What were you doing, and what did you expect? -->\n",
    "## Error\n\n" + (ctx.error ? fenced("text", ctx.error) : "(no error message shown)"),
  ];
  if (ctx.includeQuery && (ctx.cypher?.trim() || ctx.aql?.trim())) {
    if (ctx.cypher?.trim()) sections.push("## Cypher\n\n" + fenced("cypher", ctx.cypher));
    if (ctx.aql?.trim()) sections.push("## AQL\n\n" + fenced("aql", ctx.aql));
  }
  sections.push(
    "## Environment\n\n" +
      `- arango-cypher-py: ${ctx.appVersion ?? "unknown"}\n` +
      `- arangodb-schema-analyzer: ${ctx.analyzerVersion ?? "unknown"}\n` +
      `- Browser: ${ctx.userAgent}`,
  );
  sections.push("<!-- This issue is public. Remove anything you do not want to share before submitting. -->");
  return { title, body: sections.join("\n\n") };
}

// The new-issue URL for *report*, trimming the body (never the title) so the
// whole URL stays under MAX_ISSUE_URL_LENGTH, with a note saying it was cut.
export function issueUrl(report: Report, repository: string = ISSUE_REPOSITORY): string {
  const base = `https://github.com/${repository}/issues/new`;
  const make = (body: string) =>
    `${base}?title=${encodeURIComponent(report.title)}&body=${encodeURIComponent(body)}`;
  let url = make(report.body);
  if (url.length <= MAX_ISSUE_URL_LENGTH) return url;
  const note = "\n\n_(Report shortened to fit GitHub's link length; use Copy for the full text.)_";
  let keep = report.body.length;
  while (keep > 0) {
    keep = Math.floor(keep * 0.9);
    url = make(report.body.slice(0, keep) + note);
    if (url.length <= MAX_ISSUE_URL_LENGTH) return url;
  }
  return make(note.trim());
}
