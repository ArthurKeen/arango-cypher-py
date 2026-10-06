import { describe, expect, it } from "vitest";

import { SETTINGS_BUTTON_SELECTOR, focusReturnTarget } from "./focusReturn";

// A stand-in for the three Document members focusReturnTarget reads, typed as
// the real Pick<Document, ...> so a signature change breaks this test.
type Doc = Pick<Document, "body" | "contains" | "querySelector">;

function makeDoc(inPage: Element[], settings: Element | null): { doc: Doc; asked: string[] } {
  const body = { tagName: "BODY" } as unknown as HTMLElement;
  const asked: string[] = [];
  const doc: Doc = {
    body,
    contains: (node: Node | null) => node === body || inPage.includes(node as Element),
    querySelector: ((selector: string) => {
      asked.push(selector);
      return settings;
    }) as Doc["querySelector"],
  };
  return { doc, asked };
}

const el = (name: string) => ({ tagName: "BUTTON", name }) as unknown as HTMLElement;

describe("focusReturnTarget", () => {
  it("returns focus to the opener while it is still in the page", () => {
    const opener = el("Report");
    const { doc, asked } = makeDoc([opener], el("Settings"));
    expect(focusReturnTarget(opener, doc)).toBe(opener);
    expect(asked).toEqual([]);
  });

  it("treats the page body as no opener: a menu item that has closed", () => {
    const settings = el("Settings");
    const { doc, asked } = makeDoc([settings], settings);
    expect(focusReturnTarget(doc.body, doc)).toBe(settings);
    expect(asked).toEqual([SETTINGS_BUTTON_SELECTOR]);
  });

  it("falls back to the settings button when the opener has left the page", () => {
    const settings = el("Settings");
    const { doc } = makeDoc([settings], settings);
    expect(focusReturnTarget(el("gone"), doc)).toBe(settings);
  });

  it("falls back when nothing had focus, and gives up quietly without a settings button", () => {
    const { doc } = makeDoc([], null);
    expect(focusReturnTarget(null, doc)).toBeNull();
  });
});
