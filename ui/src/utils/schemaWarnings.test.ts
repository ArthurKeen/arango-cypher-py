import { describe, expect, it } from "vitest";

import { bannerWarnings, dismissalKey } from "./schemaWarnings";

const url = "https://prod.demo";
const db = "IAM";

describe("bannerWarnings", () => {
  it("keeps info notes out of the banner", () => {
    const shown = bannerWarnings(
      [
        { code: "ANALYZER_BASELINE_NO_LLM", message: "LLM provider not configured", severity: "info" },
        { code: "ANALYZER_NOT_INSTALLED", message: "Install the analyzer", severity: "warning" },
      ],
      {},
      url,
      db,
    );
    expect(shown.map((w) => w.code)).toEqual(["ANALYZER_NOT_INSTALLED"]);
  });

  it("shows warnings without a severity (older servers)", () => {
    expect(bannerWarnings([{ code: "X", message: "m" }], {}, url, db)).toHaveLength(1);
  });

  it("hides only the dismissed warning, for this connection only", () => {
    const warnings = [
      { code: "A", message: "a", severity: "warning" as const },
      { code: "B", message: "b", severity: "warning" as const },
    ];
    const dismissed = { [dismissalKey(url, db, "A")]: 1 };
    expect(bannerWarnings(warnings, dismissed, url, db).map((w) => w.code)).toEqual(["B"]);
    expect(bannerWarnings(warnings, dismissed, url, "OTHER")).toHaveLength(2);
  });
});
