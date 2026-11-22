import type { Point, Totals } from "./api";

export type Res = "cpu" | "memory";
const SCALE = { cpu: 1000, memory: 1 / 2 ** 20 }; // millicores, MiB

/** uPlot's columns: times, then request, Sizing and usage basis per point. */
export function series(points: Point[], res: Res): [number[], ...(number | null)[][]] {
  const pick = (f: "current" | "target" | "basis") =>
    points.map((p) => {
      const v = p[res]?.[f];
      return v == null ? null : v * SCALE[res];
    });
  return [points.map((p) => p.at), pick("current"), pick("target"), pick("basis")];
}

const GI = 2 ** 30;

/** uPlot's columns: times, then requests, Sizings and usage, in cores or GiB. */
export function columns(t: Totals, res: "cpu" | "memory"): [number[], number[], number[], number[]] {
  const scale = res === "cpu" ? 1 : 1 / GI;
  const pick = (f: "requests" | "sizings" | "usage") => t.points.map((p) => p[res][f] * scale);
  return [t.points.map((p) => p.at), pick("requests"), pick("sizings"), pick("usage")];
}
