import { describe, expect, it } from "vitest";

import { CATEGORICAL, categoricalColor, graphPalette, mixHex, tintedCard } from "./graphPalette";

describe("graphPalette", () => {
  it("uses Arango Green for selection and a light gray canvas by day", () => {
    const day = graphPalette("light");
    expect(day.accent).toBe("#006532");
    expect(day.canvas).toBe("#f8f8f8");
    expect(day.text).toBe("#282828");
  });

  it("keeps labels light on the night canvas", () => {
    const night = graphPalette("dark");
    expect(night.canvas).toBe("#1a1a1a");
    expect(night.text).toBe("#e5e5e5");
  });

  it("leads the categorical colours with brand green and never uses error red", () => {
    expect(CATEGORICAL[0]).toBe("#007339");
    expect(CATEGORICAL).not.toContain("#da1a20");
  });

  it("cycles categorical colours, including for negative indexes", () => {
    const p = graphPalette("light");
    expect(categoricalColor(p, 0)).toBe(CATEGORICAL[0]);
    expect(categoricalColor(p, CATEGORICAL.length)).toBe(CATEGORICAL[0]);
    expect(categoricalColor(p, -1)).toBe(CATEGORICAL[CATEGORICAL.length - 1]);
  });

  it("mixes hex colours, accepting short forms and clamping the weight", () => {
    expect(mixHex("#000000", "#ffffff", 0.5)).toBe("#808080");
    expect(mixHex("#fff", "#000", 1)).toBe("#ffffff");
    expect(mixHex("#123456", "#abcdef", 2)).toBe("#123456");
    expect(mixHex("#123456", "#abcdef", -1)).toBe("#abcdef");
  });

  it("tints cards pale by day and deep by night", () => {
    const day = tintedCard(graphPalette("light"), "#007339");
    const night = tintedCard(graphPalette("dark"), "#007339");
    const lum = (hex: string) => parseInt(hex.slice(1, 3), 16) + parseInt(hex.slice(3, 5), 16) + parseInt(hex.slice(5, 7), 16);
    expect(lum(day.fill)).toBeGreaterThan(600);
    expect(lum(night.fill)).toBeLessThan(150);
    expect(day.stroke).toBe("#007339");
  });
});
