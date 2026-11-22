// A badge (slice 21.3a; 21.5a: status colours only, never the accent): a short state, its text
// always there, its colour a tint with the same hue's text.
import type { ReactNode } from "react";
import { cn } from "./cn";

const TONE = {
  policy: "bg-raised text-ink",
  good: "bg-ok-bg text-ok",
  bad: "bg-bad-bg text-bad",
  muted: "bg-raised text-muted",
};

export function Badge({ tone = "muted", children }: { tone?: keyof typeof TONE; children: ReactNode }) {
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-[13px] font-medium whitespace-nowrap", TONE[tone])}>
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
