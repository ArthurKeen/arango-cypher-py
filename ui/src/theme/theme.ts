import { useSyncExternalStore } from "react";

// Day ("light") is the default; night ("dark") is opt-in and remembered per
// viewer. The active theme lives on <html data-theme>, which src/palette.css
// keys every colour token off. index.html applies the stored choice before
// the bundle loads so the page never flashes the wrong theme.
export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "arango-cypher.theme";
export const DEFAULT_THEME: Theme = "light";

export function parseTheme(value: unknown): Theme {
  return value === "dark" ? "dark" : DEFAULT_THEME;
}

// Storage can be unavailable (private windows, blocked site data); the theme
// then simply is not remembered.
export function storedTheme(): Theme {
  try {
    return parseTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return DEFAULT_THEME;
  }
}

export function currentTheme(): Theme {
  return parseTheme(document.documentElement.dataset.theme);
}

const listeners = new Set<() => void>();

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Not persisted; the choice still holds for this page.
  }
  listeners.forEach((listener) => listener());
}

export function useTheme(): [Theme, (theme: Theme) => void] {
  const theme = useSyncExternalStore(subscribeTheme, currentTheme, () => DEFAULT_THEME);
  return [theme, applyTheme];
}
