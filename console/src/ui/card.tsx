// A card and a number tile (slice 21.2), after ScaleOps' product UI: white rounded panels, a big
// number over a small grey label.
import type { ComponentProps, ReactNode } from "react";
import { cn } from "./cn";

export function Card({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("rounded-[10px] border border-line bg-panel p-5", className)} {...props} />;
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

/** A row of figures (slice 21.5b, after the design brief): no box around each, thin rules
 * between them, so the numbers carry the page and not the frames. */
export function Figures({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-y-4 border-y border-line py-4",
        "[&>*]:border-line [&>*:not(:first-child)]:border-l",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** A figure: its label above, the number, a note under it; `hero` sets the one that matters
 * largest; `bar` (0 to 1) draws a thin meter under it. */
export function Tile({
  label,
  value,
  tone = "plain",
  bar,
  note,
  hero = false,
}: {
  label: string;
  value: string;
  tone?: keyof typeof TONE;
  bar?: number;
  note?: ReactNode;
  hero?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 px-5 first:pl-1">
      <p className="m-0 text-[13px] font-medium text-muted">{label}</p>
      <p
        className={cn(
          "m-0 leading-none font-bold tabular-nums",
          hero ? "text-[40px] tracking-[-0.03em]" : "text-[28px] tracking-[-0.02em]",
          TONE[tone],
        )}
      >
        {value}
      </p>
      {bar !== undefined && (
        <div className="mt-1 h-1 w-28 overflow-hidden rounded-full bg-raised" aria-hidden="true">
          <div className="h-full rounded-full bg-muted" style={{ width: `${Math.min(1, Math.max(0, bar)) * 100}%` }} />
        </div>
      )}
      {note && <p className="m-0 text-[13px] text-muted">{note}</p>}
    </div>
  );
}

/** A flat block in a shape of what loads (slice 21.5b): no pulse, no spinner. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading">
      <div className="h-20 rounded-[10px] bg-raised" />
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-10 rounded-lg bg-raised" />
      ))}
    </div>
  );
}

/** What isn't there yet, why, and the one thing to do (slice 21.5b). */
export function Empty({ what, why, action }: { what: string; why: string; action?: ReactNode }) {
  return (
    <div className="rounded-[10px] border border-dashed border-line px-5 py-6">
      <p className="m-0 font-semibold">{what}</p>
      <p className="m-0 mt-1 text-sm text-muted">{why}</p>
      {action && <p className="m-0 mt-3 text-sm">{action}</p>}
    </div>
  );
}
