// The theme's colours for canvas charts (slice 21.5b): uPlot draws on a canvas, which can't
// read CSS variables, so a chart reads them when it draws and draws again when the system
// switches between light and dark.
import { useEffect, useState } from "react";

/** A token's value (`--accent` and the like), as the page resolves it now. */
export const token = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || "#888";

/** A #rrggbb colour at an opacity, for a fill. */
export function alpha(hex: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

/** "dark" or "light", following the system, so a chart redraws when it changes. */
export function useScheme(): string {
  const query = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;
  const [scheme, setScheme] = useState(query?.matches ? "dark" : "light");
  useEffect(() => {
    if (!query) return;
    const on = (e: MediaQueryListEvent) => setScheme(e.matches ? "dark" : "light");
    query.addEventListener("change", on);
    return () => query.removeEventListener("change", on);
  }, [query]);
  return scheme;
}

/** The axes the design brief asks for: horizontal rules only, no ticks, muted 13px labels. */
export function axes(unit: (v: number) => string) {
  const muted = token("muted");
  const font = '13px "Hanken Grotesk Variable", sans-serif';
  return [
    { stroke: muted, font, grid: { show: false }, ticks: { show: false } },
    {
      size: 80,
      stroke: muted,
      font,
      space: 50,
      grid: { stroke: token("line"), width: 1 },
      ticks: { show: false },
      values: (_u: unknown, ticks: number[]) => ticks.map(unit),
    },
  ];
}
