import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  applyTheme,
  currentTheme,
  parseTheme,
  storedTheme,
  subscribeTheme,
} from "./theme";

// The two browser surfaces theme.ts touches, shaped like the real ones:
// Storage.getItem(key): string | null / setItem(key, value): void, and
// <html>.dataset (a DOMStringMap — string values keyed by name).
class MemoryStorage {
  private values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.has(key) ? (this.values.get(key) as string) : null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, String(value));
  }
}

// Private windows and blocked site data throw SecurityError on access.
class BlockedStorage {
  getItem(key: string): string | null {
    throw new DOMException(`The operation is insecure (read ${key}).`, "SecurityError");
  }
  setItem(key: string, value: string): void {
    throw new DOMException(`The operation is insecure (write ${key}=${value}).`, "SecurityError");
  }
}

let dataset: Record<string, string>;

function install(storage: MemoryStorage | BlockedStorage) {
  dataset = {};
  vi.stubGlobal("window", { localStorage: storage });
  vi.stubGlobal("document", { documentElement: { dataset } });
}

describe("theme", () => {
  beforeEach(() => install(new MemoryStorage()));
  afterEach(() => vi.unstubAllGlobals());

  it("defaults to day", () => {
    expect(DEFAULT_THEME).toBe("light");
    expect(storedTheme()).toBe("light");
    expect(currentTheme()).toBe("light");
  });

  it("accepts only 'dark' as night; anything else is day", () => {
    expect(parseTheme("dark")).toBe("dark");
    for (const v of ["light", "DARK", "", null, undefined, 1]) expect(parseTheme(v)).toBe("light");
  });

  it("applies the theme to <html> and remembers it", () => {
    applyTheme("dark");
    expect(dataset.theme).toBe("dark");
    expect(currentTheme()).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(storedTheme()).toBe("dark");
    applyTheme("light");
    expect(storedTheme()).toBe("light");
  });

  it("notifies subscribers until they unsubscribe", () => {
    const seen: string[] = [];
    const unsubscribe = subscribeTheme(() => seen.push(currentTheme()));
    applyTheme("dark");
    applyTheme("light");
    unsubscribe();
    applyTheme("dark");
    expect(seen).toEqual(["dark", "light"]);
  });

  it("still switches when storage is blocked, without remembering", () => {
    install(new BlockedStorage());
    expect(storedTheme()).toBe("light");
    expect(() => applyTheme("dark")).not.toThrow();
    expect(currentTheme()).toBe("dark");
    expect(storedTheme()).toBe("light");
  });
});
