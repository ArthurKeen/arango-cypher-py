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
export const DEFAULT_TITLE = "Arango Cypher problem";

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

// NL -> Cypher failures end with the Cypher the model last tried
// (arango_cypher/nl2cypher/_core.py: "Last attempt was:" / "Last attempted
// Cypher was:"). That is query content, so it is cut unless the query is
// included.
const ATTEMPTED_QUERY_MARKERS = ["Last attempted Cypher was:", "Last attempt was:"];
export const QUERY_OMITTED = "[attempted query left out; tick \"include my query\" to add it]";

export function stripAttemptedQuery(error: string): string {
  for (const marker of ATTEMPTED_QUERY_MARKERS) {
    const at = error.indexOf(marker);
    if (at >= 0) return `${error.slice(0, at + marker.length)} ${QUERY_OMITTED}`;
  }
  return error;
}

// Quoted text is what an error echoes from the user's data, and echoes nest
// (the parser quotes a query fragment that has quotes of its own:
// "at input 'MATCH (r {name: 'prod-admin' 'x''"), so pairing quotes would
// leave values exposed. On each line, everything from the first opening quote
// to the last closing quote is masked. A quote only opens before a non-word
// character boundary and closes after one, so apostrophes in prose ("can't",
// "reply's") are not quotes. Long digit runs (account numbers, ids) are masked
// too. Used only when the query itself is not included.
const OPENING = /(?<![\p{L}\p{N}_])['"`‘“«]/u;
const CLOSING = /['"`’”»](?![\p{L}\p{N}_])/gu;

export function maskValues(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      let masked = line;
      const open = OPENING.exec(masked);
      if (open) {
        const from = open.index;
        let to = -1;
        for (const m of masked.matchAll(CLOSING)) if (m.index > from) to = m.index;
        masked = to > from ? `${masked.slice(0, from)}'…'${masked.slice(to + 1)}` : `${masked.slice(0, from)}'…'`;
      }
      return masked.replace(/\d{6,}/g, "…");
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
// happened" text), Environment first: when a report must be shortened for
// GitHub's link limit, the details are cut from the end, so the versions are
// the last thing to go.
export function buildDetails(ctx: ReportContext): string {
  const sections = [
    "## Environment\n\n" +
      `- arango-cypher-py: ${ctx.appVersion ?? "unknown"}\n` +
      `- arangodb-schema-analyzer: ${ctx.analyzerVersion ?? "unknown"}\n` +
      `- Browser: ${ctx.userAgent}`,
  ];
  const error = ctx.error?.trim();
  if (error) {
    const shown = ctx.includeQuery ? error : stripAttemptedQuery(maskValues(error));
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

// Close a code fence left open by a cut, so the trimming note renders as
// text. An opening fence may carry an info string ("```text"); a closing
// fence is bare and at least as long (CommonMark).
function closeOpenFence(text: string): string {
  let open: string | null = null;
  for (const line of text.split("\n")) {
    const m = /^(`{3,})(.*)$/.exec(line);
    if (!m) continue;
    if (open === null) open = m[1];
    else if (m[1].length >= open.length && m[2].trim() === "") open = null;
  }
  return open === null ? text : `${text}\n${open}`;
}

const SHORTENED_NOTE = "\n\n_(Report shortened to fit GitHub's link length; use Copy for the full text.)_";

export interface Issue {
  url: string;
  // True when the link carries less than the full report (use Copy for all).
  shortened: boolean;
}

// The new-issue link for a report. The title is capped. If the full report
// does not fit MAX_ISSUE_URL_LENGTH, the user's own text is shortened first,
// then the details from the end — Environment leads the details, so the
// versions survive. Cuts are on whole characters and close any open fence.
export function buildIssue(
  title: string,
  whatHappened: string,
  details: string,
  repository: string = ISSUE_REPOSITORY,
): Issue {
  const base = `https://github.com/${repository}/issues/new`;
  const t = sliceChars(title.trim() || DEFAULT_TITLE, MAX_TITLE_LENGTH);
  const make = (b: string) => `${base}?title=${encodeURIComponent(t)}&body=${encodeURIComponent(b)}`;
  const full = make(composeBody(whatHappened, details));
  if (full.length <= MAX_ISSUE_URL_LENGTH) return { url: full, shortened: false };

  const said = Array.from(whatHappened.trim());
  for (let keep = said.length; keep > 0; keep = Math.floor(keep * 0.8)) {
    const text = closeOpenFence(said.slice(0, keep).join("")) + "…";
    const url = make(composeBody(text, details) + SHORTENED_NOTE);
    if (url.length <= MAX_ISSUE_URL_LENGTH) return { url, shortened: true };
  }
  const body = Array.from(composeBody("…", details));
  for (let keep = body.length; keep > 0; keep = Math.floor(keep * 0.9)) {
    const url = make(closeOpenFence(body.slice(0, keep).join("")) + SHORTENED_NOTE);
    if (url.length <= MAX_ISSUE_URL_LENGTH) return { url, shortened: true };
  }
  return { url: make(SHORTENED_NOTE.trim()), shortened: true };
}
