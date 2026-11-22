// A Contract at a glance (slice 21.2; a grid of cards since 21.6b): one card for each of the
// Contract's pages, its numbers and a link to it. Every number is the Hub's own, from the
// Workloads, the Cost page's realized Savings, the stored totals, the Agenda, the nodes and the
// Investigations; money only where the Contract has prices, never converted. Each read but the
// Workloads may fail alone: its card says so, and the others stand.
import { ArrowRight, Bot, ListTodo, type LucideIcon, PiggyBank, Recycle, Search, Server } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { AgendaList } from "./Agenda";
import * as api from "./api";
import { cores, gib, pct, when, whole } from "./format";
import { state, tone } from "./Investigations";
import { PageTools } from "./Shell";
import { Badge } from "./ui/badge";
import { Card, Empty, Meter, Skeleton } from "./ui/card";

type Loaded = {
  workloads: api.Workloads;
  realized: api.Allocation["realized"] | null;
  /** null: the read failed (only an unpriced Contract's waste share needs it) */
  totals: api.Totals | null;
  agenda: api.Agenda | null;
  nodes: api.Nodes | null;
  investigations: api.Brief[] | null;
};

const both = (cpu: number, memory: number) => `${cores(cpu)}, ${gib(memory)}`;

/** The ready containers, and those a Sizing rule automates where an Actuator runs. */
export function automation(w: api.Workloads): { automated: number; ready: number } {
  const all = w.workloads.flatMap((x) => x.containers);
  return { automated: all.filter((c) => c.automated).length, ready: all.length };
}

/** The nodes at a glance (the Overview's card and the Nodes page's figures): those read, the
 * clusters not read (a problem, never 0 nodes), the nodes that could go now and after the Sizings
 * where the Hub judged it, and the Hub's own count of spot nodes where a cluster names a spot
 * label (none named: nothing said of spot). */
export function nodesAtGlance(n: api.Nodes) {
  const read = n.clusters.filter((c) => !c.problem);
  const judged = read.filter((c) => c.could_go);
  const count = (which: "now" | "after") => judged.reduce((s, c) => s + c.could_go![which].nodes.length, 0);
  return {
    nodes: read.reduce((s, c) => s + c.nodes.length, 0),
    clusters: read.length,
    unread: n.clusters.filter((c) => c.problem).map((c) => c.cluster),
    could: judged.length ? { now: count("now"), after: count("after") } : null,
    spot: n.clusters.some((c) => c.spot) ? n.clusters.reduce((s, c) => s + (c.spot?.nodes ?? 0), 0) : null,
  };
}

/** The four money figures' values: priced, the Hub's monthly costs; unpriced, cores and GiB
 * (the latest totals' requests, ready containers only), never a made-up amount. */
export function figures(d: Omit<Loaded, "nodes" | "investigations">) {
  const { workloads: w, realized, totals } = d;
  const cur = w.currency;
  if (cur && w.cost) {
    const share = w.cost.requests ? w.cost.waste / w.cost.requests : 0;
    return {
      cost: whole(w.cost.requests, cur),
      wasted: pct(share),
      share,
      available: whole(w.cost.waste, cur),
      realized: realized?.cost ? whole(realized.cost.rate, cur) : "none yet",
    };
  }
  const last = totals?.points.at(-1);
  const share = last && last.cpu.requests ? (w.total.cpu ?? 0) / last.cpu.requests : 0;
  return {
    cost: !totals ? "not read" : last ? both(last.cpu.requests, last.memory.requests) : "no reads yet",
    wasted: totals ? pct(share) : "not read", // a failed read is never "0%"
    share,
    available: both(w.total.cpu ?? 0, w.total.memory ?? 0),
    realized: realized ? both(realized.rate.cpu ?? 0, realized.rate.memory ?? 0) : "none yet",
  };
}

export function OverviewPage({ contract }: { contract: string }) {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setData(null);
    setError("");
    Promise.all([
      api.workloads(contract),
      api.cost(contract, "namespace", "").then((a) => a.realized, () => null),
      api.totals(contract).catch(() => null),
      api.agenda(contract).catch(() => null),
      api.nodes(contract).catch(() => null),
      api.investigations(contract).catch(() => null),
    ]).then(
      ([workloads, realized, totals, agenda, nodes, investigations]) =>
        setData({ workloads, realized, totals, agenda, nodes, investigations }),
      (e: Error) => setError(e.message),
    );
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const f = figures(data);
  const auto = automation(data.workloads);
  const gpus = data.workloads.gpus;
  const cur = data.workloads.currency;
  const priced = Boolean(cur && data.workloads.cost);
  const clusters = Object.keys(data.workloads.clusters).length;
  const page = (view: string) => `/app/?contract=${encodeURIComponent(contract)}${view ? `&view=${view}` : ""}`;
  const n = data.nodes && nodesAtGlance(data.nodes);
  return (
    <div className="flex flex-col gap-4">
      {clusters === 0 && <NoSizings contract={contract} />}
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      <PageTools>
        <span>{clusters} cluster{clusters === 1 ? "" : "s"}</span>
        <a href={page("")}>
          {data.workloads.workloads.length} workload{data.workloads.workloads.length === 1 ? "" : "s"}
        </a>
      </PageTools>
      <Panel icon={PiggyBank} title="Savings" subtitle="What the Sizings save, and have saved" to={[page("cost"), "Cost"]}>
        <Big className="text-saving">{f.available}</Big>
        <p className="m-0 text-xs text-muted">available{priced ? " a month" : ""}</p>
        <p className="m-0 mt-2">
          Realized: <span className="font-semibold text-saving">{f.realized}</span>
          {f.realized !== "none yet" && (
            <span className="text-muted">{priced ? " a month, at today's prices" : ", held less now"}</span>
          )}
        </p>
      </Panel>
      <Panel icon={Recycle} title="Waste" subtitle="Requests above the Sizings" to={[page(""), "Workloads"]}>
        {priced || data.totals ? (
          <>
            <Big>{f.wasted}</Big>
            <Meter share={f.share} />
            <p className="m-0 text-xs text-muted">{priced ? `of ${f.cost} a month` : `of CPU requests (${f.cost})`}</p>
          </>
        ) : (
          <p className="m-0 text-muted">Couldn't read the hourly totals.</p>
        )}
        {gpus && (
          <p className="m-0 mt-2">
            Idle GPUs: {gpus.text}
            {gpus.cost != null && cur ? `, ${whole(gpus.cost, cur)} a month` : ""}
            <span className="text-muted">; beside the waste, never in it</span>
          </p>
        )}
      </Panel>
      <Panel icon={Bot} title="Automation" subtitle="Containers a Sizing rule automates" to={[page("automation"), "Automation"]}>
        <Big>{auto.ready ? pct(auto.automated / auto.ready) : "none"}</Big>
        <Meter share={auto.ready ? auto.automated / auto.ready : 0} />
        <p className="m-0 text-xs text-muted">
          {auto.automated} of {auto.ready} containers
        </p>
      </Panel>
      <Panel icon={Server} title="Nodes" subtitle="The nodes read, and those that could go" to={[page("nodes"), "Nodes"]}>
        {!n ? (
          <p className="m-0 text-muted">Couldn't read the nodes.</p>
        ) : data.nodes!.clusters.length === 0 ? (
          <p className="m-0 text-muted">No nodes read yet: the hourly run reads them.</p>
        ) : (
          <>
            <Big>{n.nodes}</Big>
            <p className="m-0 text-xs text-muted">
              node{n.nodes === 1 ? "" : "s"} in {n.clusters} cluster{n.clusters === 1 ? "" : "s"}
            </p>
            {n.could && (
              <p className="m-0 mt-2">
                Could go: {n.could.now} now, {n.could.after} after the Sizings
              </p>
            )}
            {n.unread.length > 0 && <p className="m-0 text-bad">Not read: {n.unread.join(", ")}</p>}
          </>
        )}
      </Panel>
      <Panel icon={ListTodo} title="Agenda" subtitle="What needs you now">
        {data.agenda ? (
          <>
            <Big>{data.agenda.items.length}</Big>
            <AgendaList contract={contract} agenda={data.agenda} limit={3} titled={false} />
          </>
        ) : (
          <p className="m-0 text-muted">Couldn't read the Agenda.</p>
        )}
      </Panel>
      <Panel icon={Search} title="Investigations" subtitle="Triages, Questions and Checks" to={[page("investigations"), "Investigations"]}>
        {!data.investigations ? (
          <p className="m-0 text-muted">Couldn't read the Investigations.</p>
        ) : data.investigations.length === 0 ? (
          <p className="m-0 text-muted">None yet.</p>
        ) : (
          <ul className="latest m-0 flex list-none flex-col gap-2 p-0">
            {data.investigations.slice(0, 3).map((t) => (
              <li key={t.id} className="flex min-w-0 flex-col gap-0.5">
                <a className="truncate" href={`/app/?view=investigation&id=${encodeURIComponent(t.id)}`}>
                  {t.title}
                </a>
                <p className="m-0 flex items-center gap-2">
                  <Badge tone={tone(t)}>{state(t)}</Badge>
                  <span className="text-xs text-muted">{when(t.started)}</span>
                </p>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
    </div>
  );
}

/** Nothing stored for the Contract yet: why, and where to look (the Workloads page's too). */
export function NoSizings({ contract }: { contract: string }) {
  return (
    <Empty
      what="No Sizings stored yet."
      why="The Hub stores them hourly, from each cluster's Prometheus."
      action={<a href={`/app/?contract=${encodeURIComponent(contract)}&view=health`}>Check Health</a>}
    />
  );
}

/** One of the Overview's cards: an icon, its name and what it shows, then a link to its page. */
function Panel({
  icon: Icon,
  title,
  subtitle,
  to,
  children,
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  to?: [string, string];
  children: ReactNode;
}) {
  return (
    <Card className="flex min-w-0 flex-col gap-4">
      <div className="flex items-start gap-3">
        <span className="rounded-lg bg-raised p-2 text-muted">
          <Icon className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <h2 className="m-0 text-[15px] font-semibold">{title}</h2>
          <p className="m-0 text-xs text-muted">{subtitle}</p>
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-1.5">{children}</div>
      {to && (
        <a className="inline-flex items-center gap-1 self-start" href={to[0]}>
          Open {to[1]}
          <ArrowRight className="size-3.5" aria-hidden="true" />
        </a>
      )}
    </Card>
  );
}

function Big({ className, children }: { className?: string; children: ReactNode }) {
  return <p className={`m-0 text-[28px] leading-tight font-bold tracking-[-0.02em] tabular-nums ${className ?? ""}`}>{children}</p>;
}
