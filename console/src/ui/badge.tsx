// A badge and a pill (slice 21.3a): a short state in colour, as ScaleOps marks policies
// (lavender pills) and Automation (green).
import type { ReactNode } from "react";
import { cn } from "./cn";

const TONE = {
  policy: "bg-money/15 text-money",
  good: "bg-saving/15 text-saving",
  bad: "bg-bad-bg text-bad",
  muted: "bg-line text-muted",
};

export function Badge({ tone = "muted", children }: { tone?: keyof typeof TONE; children: ReactNode }) {
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap", TONE[tone])}>
      {children}
    </span>
  );
}

/** "before → after", the new value in green, as ScaleOps shows a request and its Sizing. */
export function Change({ from, to }: { from: string; to: string }) {
  return (
    <span className="whitespace-nowrap tabular-nums">
      {from} <span className="text-muted">→</span> <span className="text-saving">{to}</span>
    </span>
  );
}
