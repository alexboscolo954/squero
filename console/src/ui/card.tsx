// A card and a figure card (slice 21.2; 21.6a after a live reference): rounded panels, each
// figure in its own card under a small uppercase label.
import type { LucideIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "./cn";

export function Card({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("rounded-[10px] border border-line bg-panel p-5", className)} {...props} />;
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="m-0 mb-3 text-[15px] font-semibold">{children}</h2>;
}

const TONE = {
  money: "text-money",
  saving: "text-saving",
  waste: "text-waste",
  plain: "text-ink",
};

/** A row of figure cards (slice 21.6a): each figure in its own card, as many to a row as fit. */
export function Figures({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3", className)}>{children}</div>;
}

/** A figure card: an icon and the label, the number, a thin meter where a share exists (`bar`,
 * 0 to 1), a footnote. The label and the number stay siblings: tests read one from the other. */
export function Tile({
  label,
  value,
  icon: Icon,
  tone = "plain",
  bar,
  note,
}: {
  label: string;
  value: string;
  icon?: LucideIcon;
  tone?: keyof typeof TONE;
  bar?: number;
  note?: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line bg-panel px-4 py-[15px]">
      <p className="m-0 flex items-center gap-1.5 text-[11px] font-semibold tracking-[0.05em] text-muted uppercase">
        {Icon && <Icon className="size-3.5 shrink-0" aria-hidden="true" />}
        {label}
      </p>
      <p className={cn("m-0 text-[28px] leading-tight font-bold tracking-[-0.02em] tabular-nums", TONE[tone])}>{value}</p>
      {bar !== undefined && <Meter share={bar} />}
      {note && <p className="m-0 text-xs text-muted">{note}</p>}
    </div>
  );
}

/** A thin meter for a share, 0 to 1; `accent` for a Savings. */
export function Meter({ share, accent = false, className }: { share: number; accent?: boolean; className?: string }) {
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-raised", className)} aria-hidden="true">
      <div
        className={cn("h-full rounded-full", accent ? "bg-accent" : "bg-muted")}
        style={{ width: `${Math.min(1, Math.max(0, share)) * 100}%` }}
      />
    </div>
  );
}

/** A table cell's bar under its number (slice 21.6c): nothing when there is no share to show.
 * It adds no text: the cell reads the same. */
export function Bar({ share, accent }: { share: number | null; accent?: boolean }) {
  return share == null ? null : <Meter share={share} accent={accent} className="bar mt-1 h-1 max-w-28 bg-line" />;
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
