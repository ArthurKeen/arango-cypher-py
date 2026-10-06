// Where keyboard focus goes when a dialog closes.
//
// A dialog records `document.activeElement` when it opens. When a menu item
// opened it, the menu has already closed by then, so the recorded element is
// the page body, which is still in the document: returning focus there loses
// the user's place. The body (or an opener that has left the page) therefore
// counts as no opener, and focus falls back to the settings button.

export const SETTINGS_BUTTON_SELECTOR = "button[aria-label='Settings']";

export function focusReturnTarget(
  opener: Element | null,
  doc: Pick<Document, "body" | "contains" | "querySelector">,
): HTMLElement | null {
  if (opener && opener !== doc.body && doc.contains(opener)) return opener as HTMLElement;
  return doc.querySelector<HTMLElement>(SETTINGS_BUTTON_SELECTOR);
}
