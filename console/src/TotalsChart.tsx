// A Contract's CPU or memory over 14 days (slice 21.2; 21.5b: one argued series): usage in ink
// with a faint fill, requests a muted line, the waste a faint band between them, the Sizings the
// one accent line. uPlot draws on a canvas, so its colours come from the theme (theme.ts).
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Totals } from "./api";
import { columns } from "./series";
import { alpha, axes, token, useScheme } from "./theme";

export default function TotalsChart({ totals, res }: { totals: Totals; res: "cpu" | "memory" }) {
  const box = useRef<HTMLDivElement>(null);
  const scheme = useScheme();
  useEffect(() => {
    if (!box.current) return;
    const unit = res === "cpu" ? " cores" : " GiB";
    const ink = token("text");
    const plot = new uPlot(
      {
        width: Math.max(300, box.current.clientWidth),
        height: 220,
        legend: { show: false },
        cursor: { y: false, points: { size: 6 } },
        scales: { x: { time: true } },
        axes: axes((v) => `${v}${unit}`),
        series: [
          {},
          { label: "Requests", stroke: token("request"), width: 1.5 },
          { label: "Sizings", stroke: token("accent"), width: 2 },
          { label: "Usage", stroke: alpha(ink, 0.4), width: 1, fill: alpha(ink, 0.08) },
        ],
        bands: [{ series: [1, 2], fill: alpha(token("muted"), 0.1) }],
      },
      columns(totals, res),
      box.current,
    );
    return () => plot.destroy();
  }, [totals, res, scheme]);
  return <div className="min-w-0" ref={box} aria-label={`${res === "cpu" ? "CPU" : "Memory"} over time`} />;
}
