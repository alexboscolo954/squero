// A container's requests, Sizing and usage basis over time (uPlot: canvas, no markup
// from data; uPlot styles its elements through the DOM, which the CSP allows). Slice 21.5b:
// the Sizing the one accent line, the theme's colours (theme.ts).
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Point } from "./api";
import { type Res, series } from "./series";
import { alpha, axes, token, useScheme } from "./theme";

const UNIT = { cpu: "m", memory: "Mi" };

export default function Chart({ points, res }: { points: Point[]; res: Res }) {
  const box = useRef<HTMLDivElement>(null);
  const scheme = useScheme();
  useEffect(() => {
    if (!box.current) return;
    const plot = new uPlot(
      {
        width: Math.max(320, box.current.clientWidth),
        height: 220,
        scales: { x: { time: true } },
        axes: axes((t) => `${t}${UNIT[res]}`),
        series: [
          {},
          { label: "request", stroke: token("request"), width: 1.5 },
          { label: "Sizing", stroke: token("accent"), width: 2 },
          { label: res === "cpu" ? "p95 (basis)" : "peak (basis)", stroke: alpha(token("text"), 0.5), dash: [4, 4] },
        ],
      },
      series(points, res),
      box.current,
    );
    return () => plot.destroy();
  }, [points, res, scheme]);
  return <div className="chart" ref={box} aria-label={`${res} over time`} />;
}
