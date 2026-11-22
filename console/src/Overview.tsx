// A Contract at a glance (slice 21.2), laid out as ScaleOps' product UI is: number tiles, then
// CPU and memory over 14 days, then the Agenda's first items. Every number is the Hub's own,
// from the Workloads, the Cost page's realized Savings, the Agenda and the stored totals; money
// only where the Contract has prices, never converted.
import { useEffect, useState } from "react";
import { AgendaList } from "./Agenda";
import * as api from "./api";
import { cores, gib, pct, whole } from "./format";
import TotalsChart from "./TotalsChart";
import { Card, CardTitle, Ring, Tile } from "./ui/card";

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
  if (!data) return null;
  const f = figures(data);
  const auto = automation(data.workloads);
  const gpus = data.workloads.gpus;
  const priced = Boolean(data.workloads.currency && data.workloads.cost);
  return (
    <div className="flex flex-col gap-5">
      <div className="grid gap-5 lg:grid-cols-[1fr_auto]">
        <Card className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] gap-y-3 p-3">
          <Tile label={priced ? "Monthly cost" : "Requests"} value={f.cost} tone="money" />
          <Tile label="Wasted spend" value={f.wasted} tone="waste" bar={f.share} note={priced ? undefined : "of CPU requests"} />
          <Tile label="Savings available" value={f.available} tone="saving" note={priced ? "a month" : undefined} />
          <Tile label="Savings realized" value={f.realized} tone="saving" note={priced ? "a month, at today's prices" : "held less, now"} />
          {gpus && <Tile label="Idle GPUs" value={gpus.text} tone="waste" note={gpus.cost != null && data.workloads.currency ? `${whole(gpus.cost, data.workloads.currency)} a month` : undefined} />}
          <Tile label="On the Agenda" value={String(data.agenda?.items.length ?? 0)} />
        </Card>
        <Card className="flex items-center justify-center">
          <Ring part={auto.automated} whole={auto.ready} label="containers automated" />
        </Card>
      </div>
      <Card>
        <div className="grid gap-6 md:grid-cols-2">
          {(["cpu", "memory"] as const).map((res) => (
            <div key={res} className="min-w-0">
              <CardTitle>{res === "cpu" ? "CPU over time" : "Memory over time"}</CardTitle>
              {data.totals.points.length ? (
                <TotalsChart totals={data.totals} res={res} />
              ) : (
                <p className="hint">No hourly run stored in the last 14 days.</p>
              )}
            </div>
          ))}
        </div>
        <ul className="m-0 mt-3 flex list-none flex-wrap justify-center gap-5 p-0 text-sm text-muted" aria-label="Legend">
          <li className="flex items-center gap-2"><span className="size-2.5 rounded-full bg-usage" />Usage (the Sizings' basis)</li>
          <li className="flex items-center gap-2"><span className="size-2.5 rounded-full bg-request" />Requests</li>
          <li className="flex items-center gap-2"><span className="size-2.5 rounded-full bg-waste" />Waste</li>
          <li className="flex items-center gap-2"><span className="size-2.5 rounded-full bg-saving" />Sizings</li>
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
