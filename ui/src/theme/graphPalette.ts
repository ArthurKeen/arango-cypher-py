import type { Theme } from "./theme";

// Concrete colours for the canvases that cannot read CSS variables: Cytoscape
// styles and SVG presentation attributes. Mirrors the Arango UI rules for data
// visualisations — a light gray workspace, Arango Green for selected nodes and
// paths, labels that stay legible against the canvas.
export interface GraphPalette {
  canvas: string;
  surface: string;
  surfaceSelected: string;
  border: string;
  text: string;
  textSecondary: string;
  muted: string;
  edge: string;
  edgeStrong: string;
  accent: string;
  warning: string;
  // Entity / label colours, in assignment order. Brand green leads; red is
  // left out because it means "error" everywhere else in the UI.
  categorical: readonly string[];
}

// Theme-independent: mid-tone fills read on both canvases.
export const CATEGORICAL = [
  "#007339", // Arango brand green
  "#2f6fb3",
  "#b7791f",
  "#6b46c1",
  "#0e7c86",
  "#b83280",
  "#4a5568",
  "#6b8e23",
] as const;

const DAY: GraphPalette = {
  canvas: "#f8f8f8",
  surface: "#ffffff",
  surfaceSelected: "#f4fef2",
  border: "#e5e5e5",
  text: "#282828",
  textSecondary: "#5c5c5c",
  muted: "#9a9a9a",
  edge: "#9a9a9a",
  edgeStrong: "#5c5c5c",
  accent: "#006532",
  warning: "#b7791f",
  categorical: CATEGORICAL,
};

const NIGHT: GraphPalette = {
  canvas: "#1a1a1a",
  surface: "#0d0d0d",
  surfaceSelected: "#0b2a19",
  border: "#3d3d3d",
  text: "#e5e5e5",
  textSecondary: "#b3b3b3",
  muted: "#9a9a9a",
  edge: "#5c5c5c",
  edgeStrong: "#9a9a9a",
  accent: "#6fcf97",
  warning: "#f0b429",
  categorical: CATEGORICAL,
};

export function graphPalette(theme: Theme): GraphPalette {
  return theme === "dark" ? NIGHT : DAY;
}

export function categoricalColor(palette: GraphPalette, index: number): string {
  const n = palette.categorical.length;
  return palette.categorical[((index % n) + n) % n];
}

function parseHex(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const v = Number.parseInt(full, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

// `weight` of `a` mixed into `b`, as #rrggbb.
export function mixHex(a: string, b: string, weight: number): string {
  const [ar, ag, ab] = parseHex(a);
  const [br, bg, bb] = parseHex(b);
  const w = Math.min(1, Math.max(0, weight));
  const ch = (x: number, y: number) => Math.round(x * w + y * (1 - w)).toString(16).padStart(2, "0");
  return `#${ch(ar, br)}${ch(ag, bg)}${ch(ab, bb)}`;
}

// A card tinted with `color`: pale on day, deep on night, with readable text.
export function tintedCard(palette: GraphPalette, color: string): { fill: string; stroke: string; text: string } {
  const night = palette === NIGHT;
  return night
    ? { fill: mixHex(color, palette.surface, 0.18), stroke: mixHex(color, "#ffffff", 0.75), text: mixHex(color, "#ffffff", 0.3) }
    : { fill: mixHex(color, "#ffffff", 0.1), stroke: color, text: mixHex(color, "#282828", 0.35) };
}
