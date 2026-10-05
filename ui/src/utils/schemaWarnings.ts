import type { SchemaWarning } from "../api/client";

// Dismissals persist per (connection, warning.code).
export function dismissalKey(url: string, database: string, code: string): string {
  return `${url}::${database}::${code}`;
}

// The warnings the banner shows: not dismissed for this connection, and not
// info notes, which describe normal operation (e.g. the schema was read
// without an LLM, which is how the service always reads it).
export function bannerWarnings(
  warnings: SchemaWarning[],
  dismissed: Record<string, number>,
  url: string,
  database: string,
): SchemaWarning[] {
  return warnings.filter((w) => w.severity !== "info" && !dismissed[dismissalKey(url, database, w.code)]);
}
