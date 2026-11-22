// A Contract at a glance (slice 21.2), laid out as ScaleOps' product UI is: number tiles, then
// CPU and memory over 14 days, then the Agenda's first items. Every number is the Hub's own,
// from the Workloads, the Cost page's realized Savings, the Agenda and the stored totals; money
// only where the Contract has prices, never converted.
import { Bot, Coins, Cpu, ListTodo, PiggyBank, Recycle, TrendingDown } from "lucide-react";
import { useEffect, useState } from "react";
import { AgendaList } from "./Agenda";
import * as api from "./api";
import { cores, gib, pct, whole } from "./format";
import { PageTools } from "./Shell";
import TotalsChart from "./TotalsChart";
import { Card, CardTitle, Empty, Figures, Skeleton, Tile } from "./ui/card";

type Loaded = {
  workloads: api.Workloads;
  realized: api.Allocation["realized"] | null;
  totals: api.Totals;
  agenda: api.Agenda | null;
};

const both = (cpu: number, memory: number) => `${cores(cpu)}, ${gib(memory)}`;

/** The ready containers, and those a Sizing rule automates where an Actuator runs. */
export function automation(w: api.Workloads): { automated: number; ready: number } {
  const all = w.workloads.flatMap((x) => x.containers);
  return { automated: all.filter((c) => c.automated).length, ready: all.length };
}

/** The four money tiles' values: priced, the Hub's monthly costs; unpriced, cores and GiB
 * (the latest totals' requests, ready containers only), never a made-up amount. */
export function figures(d: Loaded) {
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
  const last = totals.points.at(-1);
  const share = last && last.cpu.requests ? (w.total.cpu ?? 0) / last.cpu.requests : 0;
  return {
    cost: last ? both(last.cpu.requests, last.memory.requests) : "no reads yet",
    wasted: pct(share),
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
      api.totals(contract),
      api.agenda(contract).catch(() => null),
    ]).then(
      ([workloads, realized, totals, agenda]) => setData({ workloads, realized, totals, agenda }),
      (e: Error) => setError(e.message),
    );
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const f = figures(data);
  const auto = automation(data.workloads);
  const gpus = data.workloads.gpus;
  const priced = Boolean(data.workloads.currency && data.workloads.cost);
  const clusters = Object.keys(data.workloads.clusters).length;
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>{clusters} cluster{clusters === 1 ? "" : "s"}</span>
        <a href={`/app/?contract=${encodeURIComponent(contract)}`}>
          {data.workloads.workloads.length} workload{data.workloads.workloads.length === 1 ? "" : "s"}
        </a>
      </PageTools>
      <Figures>
        <Tile icon={PiggyBank} label="Savings available" value={f.available} tone="saving" note={priced ? "a month" : undefined} />
        <Tile icon={Coins} label={priced ? "Monthly cost" : "Requests"} value={f.cost} />
        <Tile icon={Recycle} label="Wasted spend" value={f.wasted} bar={f.share} note={priced ? `of ${f.cost} a month` : "of CPU requests"} />
        <Tile icon={TrendingDown} label="Savings realized" value={f.realized} tone="saving" note={priced ? "a month, at today's prices" : "held less, now"} />
        <Tile
          icon={Bot}
          label="Automated"
          value={auto.ready ? pct(auto.automated / auto.ready) : "none"}
          bar={auto.ready ? auto.automated / auto.ready : 0}
          note={`${auto.automated} of ${auto.ready} containers`}
        />
        {gpus && <Tile icon={Cpu} label="Idle GPUs" value={gpus.text} note={gpus.cost != null && data.workloads.currency ? `${whole(gpus.cost, data.workloads.currency)} a month` : undefined} />}
        <Tile icon={ListTodo} label="On the Agenda" value={String(data.agenda?.items.length ?? 0)} />
      </Figures>
      <Card>
        <div className="grid gap-6 md:grid-cols-2">
          {(["cpu", "memory"] as const).map((res) => (
            <div key={res} className="min-w-0">
              <CardTitle>{res === "cpu" ? "CPU over time" : "Memory over time"}</CardTitle>
              {data.totals.points.length ? (
                <TotalsChart totals={data.totals} res={res} />
              ) : (
                <Empty
                  what="No hourly run stored in the last 14 days."
                  why="The charts sum the Sizings each hourly run stores; a run needs the cluster's Prometheus."
                  action={<a href={`/app/?contract=${encodeURIComponent(contract)}&view=health`}>Check Health</a>}
                />
              )}
            </div>
          ))}
        </div>
        <ul className="m-0 mt-3 flex list-none flex-wrap justify-center gap-5 p-0 text-xs text-muted" aria-label="Legend">
          <li className="flex items-center gap-2"><span className="h-0.5 w-3 bg-accent" />Sizings</li>
          <li className="flex items-center gap-2"><span className="h-0.5 w-3 bg-request" />Requests</li>
          <li className="flex items-center gap-2"><span className="h-2 w-3 bg-muted/20" />Waste, between them</li>
          <li className="flex items-center gap-2"><span className="h-2 w-3 bg-ink/20" />Usage (the Sizings' basis)</li>
        </ul>
      </Card>
      {data.agenda && (
        <Card>
          <AgendaList contract={contract} agenda={data.agenda} limit={5} />
        </Card>
      )}
    </div>
  );
}
