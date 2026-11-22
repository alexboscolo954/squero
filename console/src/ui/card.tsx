// A card and a number tile (slice 21.2), after ScaleOps' product UI: white rounded panels, a big
// number over a small grey label.
import type { ComponentProps, ReactNode } from "react";
import { cn } from "./cn";

export function Card({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("rounded-xl border border-line bg-panel p-5", className)} {...props} />;
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="m-0 mb-3 text-base font-semibold">{children}</h2>;
}

const TONE = {
  money: "text-money",
  saving: "text-saving",
  waste: "text-waste",
  plain: "text-ink",
};

/** A number and its label; `bar` (0 to 1) draws a thin bar under it, as Wasted spend has. */
export function Tile({
  label,
  value,
  tone = "plain",
  bar,
  note,
}: {
  label: string;
  value: string;
  tone?: keyof typeof TONE;
  bar?: number;
  note?: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col items-center justify-center gap-1 border-line px-4 py-2 text-center not-first:border-l">
      <p className={cn("m-0 text-2xl font-semibold tabular-nums", TONE[tone])}>{value}</p>
      {bar !== undefined && (
        <div className="h-1.5 w-24 overflow-hidden rounded-full bg-line" aria-hidden="true">
          <div className="h-full rounded-full bg-waste" style={{ width: `${Math.min(1, Math.max(0, bar)) * 100}%` }} />
        </div>
      )}
      <p className="m-0 text-sm text-muted">{label}</p>
      {note && <p className="m-0 text-xs text-muted">{note}</p>}
    </div>
  );
}

/** A share as a ring, as ScaleOps shows Automation: `part` of `whole`. */
export function Ring({ part, whole, label }: { part: number; whole: number; label: string }) {
  const share = whole ? part / whole : 0;
  const r = 30;
  const round = 2 * Math.PI * r;
  return (
    <figure className="m-0 flex items-center gap-4" aria-label={`${label}: ${part} of ${whole}`}>
      <svg viewBox="0 0 72 72" className="size-20 -rotate-90" aria-hidden="true">
        <circle cx="36" cy="36" r={r} fill="none" strokeWidth="7" className="stroke-line" />
        <circle
          cx="36"
          cy="36"
          r={r}
          fill="none"
          strokeWidth="7"
          strokeLinecap="round"
          className="stroke-saving"
          strokeDasharray={`${share * round} ${round}`}
        />
      </svg>
      <figcaption className="flex flex-col">
        <span className="text-2xl font-semibold text-saving tabular-nums">{Math.round(share * 100)}%</span>
        <span className="text-sm text-muted">{label}</span>
        <span className="text-xs text-muted">
          {part} of {whole}
        </span>
      </figcaption>
    </figure>
  );
}
