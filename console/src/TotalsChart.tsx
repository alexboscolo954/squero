// A Contract's CPU or memory over 14 days (slice 21.2), as ScaleOps draws it: usage a blue fill,
// requests an amber line, Sizings a green line, the waste between them pink. uPlot draws on a
// canvas, so its colours are read from the theme's tokens.
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Totals } from "./api";
import { columns } from "./series";

const token = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || "#888";

export default function TotalsChart({ totals, res }: { totals: Totals; res: "cpu" | "memory" }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current) return;
    const unit = res === "cpu" ? " cores" : " GiB";
    const waste = token("waste-bg");
    const plot = new uPlot(
      {
        width: Math.max(300, box.current.clientWidth),
        height: 220,
        legend: { show: false },
        scales: { x: { time: true } },
        axes: [
          { stroke: token("muted"), grid: { stroke: token("line") } },
          { size: 80, stroke: token("muted"), grid: { stroke: token("line") }, values: (_u, ticks) => ticks.map((v) => `${v}${unit}`) },
        ],
        series: [
          {},
          { label: "Requests", stroke: token("request"), width: 2 },
          { label: "Sizings", stroke: token("saving"), width: 2 },
          { label: "Usage", stroke: token("usage"), width: 1, fill: `${token("usage")}33` },
        ],
        bands: [{ series: [1, 2], fill: waste }],
      },
      columns(totals, res),
      box.current,
    );
    return () => plot.destroy();
  }, [totals, res]);
  return <div className="min-w-0" ref={box} aria-label={`${res === "cpu" ? "CPU" : "Memory"} over time`} />;
}
