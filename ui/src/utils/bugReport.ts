// "Report a problem": a pre-filled GitHub issue the user reviews and submits
// from their own browser. No token in the service, nothing sent without the
// user pressing Submit on GitHub — and the repository is public, so a report
// carries versions and the error by default; the query only when the user
// ticks "include my query" (it may contain names or values from their data).
//
// Error messages often quote the query ("…at input 'MATCH (r {name:
// 'prod-admin'…"), so unless the query is included, quoted values in the
// error are masked and the title is fixed rather than taken from the error.

export const ISSUE_REPOSITORY = "arango-solutions/arango-cypher";

// GitHub rejects new-issue URLs much past ~8 KB; stay well under it.
export const MAX_ISSUE_URL_LENGTH = 7000;
export const MAX_TITLE_LENGTH = 256;
export const DEFAULT_TITLE = "Workbench problem";

export interface ReportContext {
  appVersion: string | null;
  analyzerVersion: string | null;
  error: string | null;
  userAgent: string;
  includeQuery: boolean;
  cypher?: string;
  aql?: string;
}

// The first *n* characters of *s*, never cutting a character in half: a cut
// inside a surrogate pair (emoji, many CJK extensions) leaves a lone
// surrogate, and encodeURIComponent then throws — during render.
export function sliceChars(s: string, n: number): string {
  if (n <= 0) return "";
  const chars = Array.from(s);
  return chars.length <= n ? s : chars.slice(0, n).join("");
}

// Quoted text is what an error echoes from the user's data. Echoes nest —
// the parser quotes the query fragment, which has quotes of its own
// ("at input 'MATCH (r {name: 'prod-admin' 'x''"), so pairing quotes would
// leave values exposed. Instead, on each line everything from the first quote
// to the last is masked. Used only when the query itself is not included.
export function maskQuotedValues(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      const first = line.search(/['"]/);
      if (first < 0) return line;
      const last = Math.max(line.lastIndexOf("'"), line.lastIndexOf('"'));
      return last > first ? `${line.slice(0, first)}'…'${line.slice(last + 1)}` : `${line.slice(0, first)}'…'`;
    })
    .join("\n");
}

function fenced(language: string, text: string): string {
  // A fence longer than any backtick run inside keeps the block intact.
  const longest = Math.max(2, ...Array.from(text.matchAll(/`+/g), (m) => m[0].length));
  const fence = "`".repeat(longest + 1);
  return `${fence}${language}\n${text.trim()}\n${fence}`;
}

// The generated part of the report (everything but the user's own "What
// happened" text). Environment comes first so trimming a long report for
// GitHub's link limit cuts the query, never the versions.
export function buildDetails(ctx: ReportContext): string {
  const sections = [
    "## Environment\n\n" +
      `- arango-cypher-py: ${ctx.appVersion ?? "unknown"}\n` +
      `- arangodb-schema-analyzer: ${ctx.analyzerVersion ?? "unknown"}\n` +
      `- Browser: ${ctx.userAgent}`,
  ];
  const error = ctx.error?.trim();
  if (error) {
    const shown = ctx.includeQuery ? error : maskQuotedValues(error);
    sections.push("## Error\n\n" + fenced("text", shown));
  } else {
    sections.push("## Error\n\n(no error message shown)");
  }
  if (ctx.includeQuery) {
    if (ctx.cypher?.trim()) sections.push("## Cypher\n\n" + fenced("cypher", ctx.cypher));
    if (ctx.aql?.trim()) sections.push("## AQL\n\n" + fenced("aql", ctx.aql));
  }
  return sections.join("\n\n");
}

export const PUBLIC_NOTE = "<!-- This issue is public. Remove anything you do not want to share before submitting. -->";

export function composeBody(whatHappened: string, details: string): string {
  const said = whatHappened.trim() || "<!-- What were you doing, and what did you expect? -->";
  return `${PUBLIC_NOTE}\n\n## What happened\n\n${said}\n\n${details}`;
}

// Close a code fence left open by a cut, so the trimming note renders as text.
function closeOpenFence(text: string): string {
  let open: string | null = null;
  for (const line of text.split("\n")) {
    const m = /^(`{3,})/.exec(line);
    if (!m) continue;
    if (open === null) open = m[1];
    else if (m[1].length >= open.length) open = null;
  }
  return open === null ? text : `${text}\n${open}`;
}

// The new-issue URL for (title, body). The title is capped; the body is cut
// from the end (on whole characters, closing any open fence) until the URL
// fits MAX_ISSUE_URL_LENGTH, with a note saying it was shortened.
export function issueUrl(title: string, body: string, repository: string = ISSUE_REPOSITORY): string {
  const base = `https://github.com/${repository}/issues/new`;
  const t = sliceChars(title.trim() || DEFAULT_TITLE, MAX_TITLE_LENGTH);
  const make = (b: string) => `${base}?title=${encodeURIComponent(t)}&body=${encodeURIComponent(b)}`;
  const full = make(body);
  if (full.length <= MAX_ISSUE_URL_LENGTH) return full;
  const note = "\n\n_(Report shortened to fit GitHub's link length; use Copy for the full text.)_";
  const chars = Array.from(body);
  let keep = chars.length;
  while (keep > 0) {
    keep = Math.floor(keep * 0.9);
    const url = make(closeOpenFence(chars.slice(0, keep).join("")) + note);
    if (url.length <= MAX_ISSUE_URL_LENGTH) return url;
  }
  return make(note.trim());
}
