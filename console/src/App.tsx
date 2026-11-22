// Workloads per Contract (slice 9.5a): each Workload's waste and risks from the
// Hub's latest stored Sizings, and one container's requests vs Sizing over time.
// Cost (slice 10.2): what each namespace's, team's or label value's requests and waste cost;
// the waste's cost is the available Savings, compared with N days ago on request (10.3a),
// and the realized Savings, requests lowered to their Sizings (10.3b).
// The Agenda (slice 11.4): what to look at now, most urgent first, above the Workloads.
// Investigations and the approver token page (slice 11.5a): Investigations.tsx.
// Automation and Revert (slice 12.6b): Automation.tsx.
// The frame around every page (slice 21.1): Shell.tsx.
// Everything from the API is rendered as text.
import { Boxes, Coins, Cpu, PiggyBank, Power, Recycle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import * as api from "./api";
import type { Allocation, Bills, Container, Point, Workload, Workloads } from "./api";
import Chart from "./Chart";
import { InvestigationList, InvestigationPage, TokenPage } from "./Investigations";
import { TeachingsPage } from "./Teachings";
import { AutomationPage } from "./Automation";
import { ChecksPage, HealthPage } from "./Checks";
import { ContractsPage } from "./Contracts";
import { FleetPage } from "./Fleet";
import { NodesPage } from "./Nodes";
import { NoSizings, OverviewPage } from "./Overview";
import { UsageCharts } from "./Usage";
import { amount, change, cores, cpu, gib, kept, memory, money, pct, rank, share, weigh, when } from "./format";
import { useParam } from "./params";
import { PageTools, Shell } from "./Shell";
import { Badge, Change } from "./ui/badge";
import { Button } from "./ui/button";
import { Chips } from "./ui/chips";
import { Bar, Card, Figures, Skeleton, Tile } from "./ui/card";

/** The pages across every Contract: no Kill switch. */
const ACROSS = ["contracts", "fleet"];

/** Where each cluster's price comes from, for a Contract priced from cloud list prices. */
function PriceNotes({ notes }: { notes?: { cluster: string; source: string; note: string }[] }) {
  return (
    <>
      {(notes ?? []).map((n) => (
        <p key={n.cluster} className={n.source === "none" ? "notice" : "hint"}>
          {n.cluster}: {n.note}
        </p>
      ))}
    </>
  );
}

/** A billed cluster's cloud bill beside its estimate at the Hub's prices, or why there is none. */
function BillLines({ bills }: { bills: Bills | null }) {
  return (
    <>
      {(bills?.clusters ?? []).map((b) => {
        const span = `${b.start} to ${b.end}`;
        if (b.gap == null || b.billed == null || b.estimated == null || !b.currency)
          return (
            <p key={b.cluster} className="notice">
              {b.cluster} bill ({span}): {b.note ?? "no estimate to compare with"}
            </p>
          );
        return (
          <p key={b.cluster} className={b.within === true ? "hint" : "notice"}>
            {b.cluster} bill ({span}): billed {amount(b.billed, b.currency)}, estimated{" "}
            {amount(b.estimated, b.currency)}, {b.gap >= 0 ? "+" : "-"}
            {(Math.abs(b.gap) * 100).toFixed(1)}%
            {b.within == null ? " (no verdict)" : b.within ? ` (within ${(bills!.tolerance * 100).toFixed(0)}%)` : ` (outside ${(bills!.tolerance * 100).toFixed(0)}%)`}
            {b.spot_billed != null && b.spot_estimated != null &&
              `; spot billed ${amount(b.spot_billed, b.currency)}, estimated ${amount(b.spot_estimated, b.currency)}`}
            {b.note ? `: ${b.note}` : ""}
          </p>
        );
      })}
    </>
  );
}

export default function App() {
  const [contracts, setContracts] = useState<api.ContractSummary[]>([]);
  const [contract, setContract] = useParam("contract");
  const [view] = useParam("view");
  const [by, setBy] = useParam("by");
  const [compare, setCompare] = useParam("compare");
  const [data, setData] = useState<Workloads | null>(null);
  const [allocation, setAllocation] = useState<Allocation | null>(null);
  const [bills, setBills] = useState<Bills | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    api.contracts().then(setContracts, setError);
  }, []);
  const [id] = useParam("id");
  const shown = contract || contracts[0]?.id || "";
  const costView = view === "cost";
  const workloadsView = !view;
  useEffect(() => {
    if (!shown) return;
    setData(null);
    setAllocation(null);
    setBills(null);
    if (costView) {
      api.cost(shown, by || "namespace", compare).then(setAllocation, setError);
      api.billing(shown).then(setBills, () => setBills(null)); // a Contract with no bill is no error
    }
    else if (workloadsView) api.workloads(shown).then(setData, setError);
  }, [shown, costView, workloadsView, by, compare]);
  const here = shown ? `contract=${encodeURIComponent(shown)}` : "";
  const pick = (id: string) => {
    setBy(""); // another Contract may not have this grouping
    setContract(id);
  };
  const ofOne = shown && view !== "investigation" && !ACROSS.includes(view); // the token page has none
  const frame = { view, here, contracts, shown, onContract: pick };
  if (view === "token")
    return (
      <Shell {...frame}>
        <TokenPage />
      </Shell>
    );

  return (
    <Shell {...frame} actions={ofOne ? <KillSwitch contract={shown} /> : null}>
        {error instanceof api.NeedsToken ? (
          <p className="notice">
            This browser hasn't got the approver token yet: <a href={error.tokenUrl}>paste it</a>,
            then come back.
          </p>
        ) : error ? (
          <p className="notice">The Hub didn't answer: {error.message}</p>
        ) : null}
        {view === "investigation" && id && <InvestigationPage id={id} />}
        {view === "contracts" && <ContractsPage />}
        {view === "fleet" && <FleetPage />}
        {data && workloadsView && <Page contract={shown} data={data} />}
        {shown && !data && workloadsView && !error && <Skeleton />}
        {shown && view === "overview" && <OverviewPage contract={shown} />}
        {shown && view === "investigations" && <InvestigationList contract={shown} />}
        {shown && view === "teachings" && <TeachingsPage contract={shown} />}
        {shown && view === "automation" && <AutomationPage contract={shown} />}
        {shown && view === "checks" && <ChecksPage contract={shown} />}
        {shown && view === "health" && <HealthPage contract={shown} />}
        {shown && view === "nodes" && <NodesPage contract={shown} />}
        {shown && !allocation && costView && !error && <Skeleton />}
        {allocation && costView && <CostPage data={allocation} bills={bills} setBy={setBy} compare={compare} setCompare={setCompare} />}
        {/* Outside the page, so regrouping or comparing never redraws them (slice 21.6b). */}
        {shown && costView && (
          <div className="mt-5">
            <UsageCharts contract={shown} />
          </div>
        )}
    </Shell>
  );
}

/** The Contract's Kill switch: on stops every change to its systems; only here does it go
 * off again (the Hub MCP server can only turn it on). */
function KillSwitch({ contract }: { contract: string }) {
  const [state, setState] = useState<api.KillSwitch | null>(null);
  const [failed, setFailed] = useState("");
  const [asking, setAsking] = useState(false); // turning it off asks first (slice 21.5a); on never
  useEffect(() => {
    setState(null);
    setAsking(false);
    setFailed("");
    api.killSwitch(contract).then(setState, () => setState(null));
  }, [contract]);
  if (!state) return null;
  const act = (change: (c: string) => Promise<api.KillSwitch>) =>
    change(contract).then(setState, (e: Error) => setFailed(e.message));
  const couldnt = failed && <span className="text-sm text-bad">Couldn't: {failed}</span>;
  // On, it takes a line of its own under the header's: nothing on the page may hide it.
  return state.on ? (
    <div role="status" className="order-last flex basis-full flex-wrap items-center gap-3 rounded-md bg-bad-solid px-4 py-2 text-sm text-on-solid">
      <Power className="size-4 shrink-0" aria-hidden="true" />
      <p className="m-0 mr-auto">
        Kill switch on since {when(state.since)} ({state.by}): approving is refused and no change
        to this Contract's systems runs.
      </p>
      {asking ? (
        <>
          <span>Changes to this Contract's systems will run again.</span>
          <Button size="sm" variant="outline" onClick={() => (setAsking(false), act(api.turnOffKillSwitch))}>
            Yes, turn it off
          </Button>
          <Button size="sm" variant="ghost" className="text-on-solid hover:bg-on-solid/10" onClick={() => setAsking(false)}>
            Keep it on
          </Button>
        </>
      ) : (
        <Button size="sm" variant="outline" onClick={() => setAsking(true)}>Turn it off</Button>
      )}
      {couldnt}
    </div>
  ) : (
    <>
      {couldnt}
      <Button size="sm" variant="danger-outline" onClick={() => act(api.turnOnKillSwitch)}>
        <Power aria-hidden="true" />
        Turn on the Kill switch
      </Button>
    </>
  );
}

// A Weekly profile's Backtest (slice 14.4) in words, left open for what its too many cost.
function off(b: { few?: number | null; many?: number | null }): string {
  const few = Math.round((b.few ?? 0) * 100);
  const many = Math.round((b.many ?? 0) * 100);
  // the total as the sum of its parts as shown, so the line adds up
  return `off by ${few + many}% last week (too few ${few}%, too many ${many}%`;
}

// Workloads with an autoscaler (slice 14.1b): each one's Floor against its HPA's minimum
// today, a saving when the Floor is lower, a risk when it is higher.
function Floors({ data, cluster, namespace }: { data: Workloads; cluster: string; namespace: string }) {
  const floors = (data.floors ?? []).filter(
    (f) => (!cluster || f.cluster === cluster) && (!namespace || f.namespace === namespace),
  );
  if (floors.length === 0) return null;
  const total = data.floors_total;
  return (
    <Card className="floors">
      <h2 className="m-0 mb-2 text-[15px]">Autoscaled Workloads</h2>
      {total && total.workloads > 0 && (
        <p className="total">
          Floors: {total.workloads}{" "}
          {!total.fixed
            ? "can lower their HPA minimum"
            : total.fixed === total.workloads
              ? "can run fewer replicas"
              : "can lower their HPA minimum or run fewer replicas"}
          , {total.replicas.toFixed(1)} replicas fewer on average
          {": "}
          <strong>
            {data.currency && total.cost != null
              ? money(total.cost, data.currency)
              : `${cores(total.saving.cpu ?? 0)}, ${gib(total.saving.memory ?? 0)}${total.gpus ? `, ${total.gpus.toFixed(2)} GPUs` : ""}`}
          </strong>{" "}
          <span className="hint">(at their Sizings, on top of the waste)</span>
        </p>
      )}
      <ul>
        {floors.map((f) => (
          <li key={f.workload}>
            <strong>
              {f.kind}/{f.name}
            </strong>{" "}
            <span className="hint">
              {f.cluster}/{f.namespace}
            </span>
            {": "}
            {f.state === "none" && `no Floor: ${f.why ?? ""}`}
            {f.state === "learning" && `Floor learning (${f.history_h ?? 0} h of ${f.needs_h ?? 0} h of Needs)`}
            {f.state === "ready" && (
              <>
                Floor <strong>{f.floor}</strong>;{" "}
                {f.fixed ? (
                  `no HPA scales it: it runs ${f.minimum} replicas`
                ) : (
                  <>
                    its HPA {f.hpa} has a minimum of {f.minimum}
                    {f.maximum != null && ` and a maximum of ${f.maximum}`}
                  </>
                )}
                {f.saving &&
                  `. At the Floor: ${f.replicas} ${f.replicas === 1 ? "replica" : "replicas"} fewer on average, ${
                    data.currency && f.cost != null
                      ? money(f.cost, data.currency)
                      : `${cores(f.saving.cpu ?? 0)}, ${gib(f.saving.memory ?? 0)}${f.gpu_saving ? `, ${f.gpu_saving.toFixed(2)} GPUs` : ""}`
                  }${f.fixed ? " (with an autoscaler holding the Floor, such as KEDA)" : ""}`}{" "}
                <span className="hint">
                  ({f.policy}: the lowest {Math.round(f.percentile * 100)}% of {f.history_h} h of Needs)
                </span>
              </>
            )}
            {f.risks.map((r, i) => (
              <div key={i}>
                <span className="chip">{r.kind}</span> {r.text}
              </div>
            ))}
            {f.profile?.state === "learning" && (
              <div className="hint">
                Weekly profile: learning ({f.profile.history_h} h of {f.profile.needs_h} h of Needs)
              </div>
            )}
            {f.profile?.state === "flat" && <div className="hint">No weekly pattern: its Floor holds all week.</div>}
            {f.profile?.state === "irregular" && f.profile.backtest && (
              <div className="hint">
                Load isn't weekly enough: {off(f.profile.backtest)}); it keeps its Floor.
              </div>
            )}
            {f.profile?.state === "ready" && (
              <div className="profile">
                Weekly profile ({f.profile.timezone}
                {f.profile.lead_time > 0 && `, each rise ${f.profile.lead_time / 60} min early`}):
                <ul>
                  {f.profile.blocks.map((b, i) => (
                    <li key={i}>
                      {b.since} to {b.until}: {b.replicas} replicas
                    </li>
                  ))}
                </ul>
                {f.profile.backtest?.state === "waiting" && (
                  <div className="hint">Not backtested yet (needs four weeks).</div>
                )}
                {f.profile.backtest?.state === "within" && (
                  <div className="hint">
                    Backtest: {off(f.profile.backtest)}
                    {!!f.profile.backtest.many &&
                      `: ${
                        f.profile.backtest.cost != null && data.currency
                          ? money(f.profile.backtest.cost, data.currency)
                          : `${cores(f.profile.backtest.excess?.cpu ?? 0)}, ${gib(f.profile.backtest.excess?.memory ?? 0)}`
                      }`}
                    ).
                  </div>
                )}
                {f.profile.note && <div className="hint">{f.profile.note}</div>}
                {f.profile.triggers && (
                  <details>
                    <summary>The cron triggers for its ScaledObject</summary>
                    <pre>{f.profile.triggers}</pre>
                  </details>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** A Workload's requests and Sizings per pod, summed over its containers (slice 21.3a). */
export function perPod(w: Workload, res: "cpu" | "memory"): { from: number; to: number; whole: boolean } | null {
  const sized = w.containers.filter((c) => c[res]);
  if (sized.length === 0) return null;
  const sum = (f: "current" | "target") => sized.reduce((t, c) => t + (c[res]?.[f] ?? 0), 0);
  // whole: every container's request is set, so the two sums are over the same containers
  return { from: sum("current"), to: sum("target"), whole: sized.every((c) => c[res]?.current != null) };
}

/** Whether a Workload's containers are automated: all, some, or none. */
export function automatedOf(w: Workload): "all" | "some" | "none" {
  const n = w.containers.filter((c) => c.automated).length;
  return n === 0 ? "none" : n === w.containers.length ? "all" : "some";
}

const AUTOMATED = { all: "automated", some: "partly", none: "no" };

function Page({ contract, data }: { contract: string; data: Workloads }) {
  const [cluster, setCluster] = useParam("cluster");
  const [namespace, setNamespace] = useParam("namespace");
  const [automated, setAutomated] = useParam("automated");
  const [query, setQuery] = useParam("q");
  const [picked, setPicked] = useParam("workload");
  const clusters = Object.keys(data.clusters).sort();
  const namespaces = [...new Set(data.workloads.map((w) => w.namespace))].sort();
  const rows = useMemo(
    () =>
      data.workloads
        .filter((w) => (!cluster || w.cluster === cluster) && (!namespace || w.namespace === namespace))
        .filter((w) => !automated || (automated === "yes") === (automatedOf(w) !== "none"))
        .filter((w) => !query || `${w.kind}/${w.name}`.toLowerCase().includes(query.toLowerCase()))
        .sort((a, b) => rank(b) - rank(a) || a.workload.localeCompare(b.workload)),
    [data, cluster, namespace, automated, query],
  );
  const selected = data.workloads.find((w) => w.workload === picked);
  const priced = Boolean(data.currency && data.cost);
  const paid = (w: Workload) => Boolean(data.currency && w.cost);
  const savings = (w: Workload) =>
    paid(w) ? money(w.cost!.waste, data.currency!) : `${cores(w.waste.cpu ?? 0)}, ${gib(w.waste.memory ?? 0)}`;
  // The rows shown, money and cores apart: they never share a scale or a sum (slice 21.6c).
  const size = (w: Workload) => (paid(w) ? w.cost!.waste : weigh(w.waste));
  const [inMoney, inCores] = [rows.filter(paid), rows.filter((w) => !paid(w))];
  const top = { money: Math.max(0, ...inMoney.map(size)), cores: Math.max(0, ...inCores.map(size)) };
  const sum = (ws: Workload[], f: (w: Workload) => number) => ws.reduce((t, w) => t + f(w), 0);
  const total = [
    inMoney.length > 0 && money(sum(inMoney, (w) => w.cost!.waste), data.currency!),
    inCores.length > 0 && `${cores(sum(inCores, (w) => w.waste.cpu ?? 0))}, ${gib(sum(inCores, (w) => w.waste.memory ?? 0))}`,
  ]
    .filter(Boolean)
    .join(" + ");

  return (
    <div className="flex flex-col gap-5">
      <ul className="clusters m-0 flex list-none flex-wrap gap-x-5 gap-y-1 p-0 text-sm">
        {clusters.map((name) => {
          const st = data.clusters[name];
          return (
            <li key={name} className={st.error ? "stale" : ""}>
              {name}: Sizings as of {when(st.at)}
              {st.error && `; stale since ${when(st.since)}: ${st.error}`}
            </li>
          );
        })}
      </ul>
      {clusters.length === 0 && <NoSizings contract={contract} />}
      <PageTools>
        <span>{clusters.length} cluster{clusters.length === 1 ? "" : "s"}</span>
        <span>{data.workloads.length} workload{data.workloads.length === 1 ? "" : "s"}</span>
      </PageTools>
      <Figures>
        <Tile
          icon={Recycle}
          label={priced ? "Waste a month" : "Waste"}
          value={priced ? amount(data.cost!.waste, data.currency!) : cores(data.total.cpu ?? 0)}
          note={priced ? `${cores(data.total.cpu ?? 0)}, ${gib(data.total.memory ?? 0)}` : gib(data.total.memory ?? 0)}
        />
        {priced && <Tile icon={Coins} label="Requests a month" value={amount(data.cost!.requests, data.currency!)} />}
        <Tile icon={Boxes} label="Workloads" value={String(data.workloads.length)} note={`${data.learning.length} learning`} />
        {data.gpus && <Tile icon={Cpu} label="Idle GPUs" value={data.gpus.text} />}
      </Figures>
      <p className="m-0 -mt-3 text-xs text-muted">
        {priced
          ? "Waste: requests above the Sizings; requests are what pods reserve, not the nodes."
          : "Waste: requests above the Sizings; a core ranks like 4 GiB."}
        {data.gpus && " Idle GPUs: busiest hour under 5% busy, each GPU by the share of the window it was held; beside the waste, never in it."}
      </p>
      {/* Where the prices come from, in a card under the figures (slice 21.6f): each line as it was. */}
      {(data.prices ?? []).length > 0 && (
        <section className="panel m-0">
          <h2>Prices</h2>
          <PriceNotes notes={data.prices} />
        </section>
      )}
      <Card className="flex flex-col gap-3 p-0">
        <div className="filters flex flex-wrap items-center gap-3 px-4 pt-4">
          <Chips
            label="Automation"
            value={automated}
            onChange={setAutomated}
            options={[["", "All"], ["yes", "Automated"], ["no", "Not automated"]] as const}
          />
          <label className="text-sm">
            Cluster{" "}
            <select value={cluster} onChange={(e) => setCluster(e.target.value)}>
              <option value="">all</option>
              {clusters.map((c) => <option key={c}>{c}</option>)}
            </select>
          </label>
          <label className="text-sm">
            Namespace{" "}
            <select value={namespace} onChange={(e) => setNamespace(e.target.value)}>
              <option value="">all</option>
              {namespaces.map((n) => <option key={n}>{n}</option>)}
            </select>
          </label>
          <input
            type="search"
            aria-label="Search Workloads"
            placeholder="Search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="ml-auto rounded-md border border-line bg-panel px-3 py-1 text-sm text-ink"
          />
        </div>
        <div className="overflow-x-auto">
        <table className="workloads rounded-none border-x-0 border-b-0">
          <thead>
            <tr>
              <th>Workload</th>
              <th>Savings</th>
              <th>CPU request → Sizing</th>
              <th>Memory request → Sizing</th>
              <th>Pods</th>
              <th>Policy</th>
              <th>Automated</th>
              {data.gpus && <th>Idle GPUs</th>}
              <th>Risks</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((w) => {
              const c = perPod(w, "cpu");
              const m = perPod(w, "memory");
              const auto = automatedOf(w);
              return (
                <tr key={w.workload} className={w.workload === picked ? "picked" : ""} onClick={() => setPicked(w.workload)}>
                  <td>
                    <button type="button" className="link">{w.kind}/{w.name}</button>
                    <div className="hint">{w.cluster}/{w.namespace}</div>
                  </td>
                  <td className="num text-saving">
                    {savings(w)}
                    <Bar accent share={share(size(w), paid(w) ? top.money : top.cores)} />
                  </td>
                  <td className="num">
                    {c ? <Change from={cpu(c.from)} to={cpu(c.to)} /> : "-"}
                    {c && <Bar share={c.whole ? kept(c.from, c.to) : null} />}
                  </td>
                  <td className="num">
                    {m ? <Change from={memory(m.from)} to={memory(m.to)} /> : "-"}
                    {m && <Bar share={m.whole ? kept(m.from, m.to) : null} />}
                  </td>
                  <td className="num">{w.pods}</td>
                  <td>
                    <span className="flex flex-wrap gap-1">
                      {[...new Set(w.containers.map((x) => x.policy))].sort().map((p) => (
                        <Badge key={p} tone="policy">{p}</Badge>
                      ))}
                    </span>
                  </td>
                  <td>
                    <Badge tone={auto === "none" ? "muted" : "good"}>{AUTOMATED[auto]}</Badge>
                  </td>
                  {data.gpus && <td>{w.gpus ? w.gpus.text : "-"}</td>}
                  <td>
                    {[...new Set(w.risks.map((r) => r.kind))].sort().map((k) => (
                      <span key={k} className="chip">{k}</span>
                    ))}
                  </td>
                </tr>
              );
            })}
          </tbody>
          {rows.length > 0 && (
            <tfoot>
              <tr>
                <td>
                  Total, {rows.length} workload{rows.length === 1 ? "" : "s"}
                </td>
                <td className="num text-saving">{total}</td>
                <td />
                <td />
                <td className="num">{sum(rows, (w) => w.pods)}</td>
                <td />
                <td />
                {data.gpus && <td />}
                <td />
              </tr>
            </tfoot>
          )}
        </table>
        </div>
        {rows.length === 0 && data.workloads.length > 0 && <p className="hint px-4">No Workload matches.</p>}
        <p className="hint m-0 px-4">
          Bars: under Savings, the row's share of the largest shown; under CPU and memory, the share of the request
          its Sizing keeps (none for a raise, or where a container has no request set). The Total adds the rows shown; a request and a Sizing are per pod, so
          they have no sum.
        </p>
        <p className="hint m-0 px-4 pb-4">
          Automation is changed by a pull request to the contracts repo: <code>propose_change</code> in Claude Code.
        </p>
      </Card>
      {selected && <Detail contract={contract} workload={selected} currency={data.currency} />}
      <Floors data={data} cluster={cluster} namespace={namespace} />
      {(data.learning.length > 0 || data.excluded.length > 0) && (
        <Card className="text-sm">
          {data.learning.length > 0 && (
            <p className="m-0">
              Learning:{" "}
              {data.learning
                .map((c) => `${c.workload} (${c.history_h ?? 0} h of ${c.needs_h ?? 0} h)`)
                .join(", ")}
            </p>
          )}
          {data.excluded.length > 0 && <p className="m-0">Excluded: {data.excluded.map((c) => c.workload).join(", ")}</p>}
        </Card>
      )}
    </div>
  );
}

const AGO = [1, 7, 30, 90];

function CostPage({
  data,
  bills,
  setBy,
  compare,
  setCompare,
}: {
  data: Allocation;
  bills: Bills | null;
  setBy: (v: string) => void;
  compare: string;
  setCompare: (v: string) => void;
}) {
  const currency = data.currency;
  const label = data.by.replace(/^label:/, "label ");
  const shown = (g: { cost?: { requests: number; waste: number } | null; requests: { cpu?: number; memory?: number }; waste: { cpu?: number; memory?: number } }, key: "requests" | "waste") =>
    currency && g.cost ? money(g.cost[key], currency) : `${cores(g[key].cpu ?? 0)}, ${gib(g[key].memory ?? 0)}`;
  const cmp = data.compare;
  const basis = [
    (data.prices ?? []).length > 0 && "Prices",
    (bills?.clusters ?? []).length > 0 && "Bills",
    cmp && "the comparison",
  ].filter(Boolean) as string[];
  if (basis[0] === "the comparison") basis[0] = "The comparison";
  // One scale per column: money when priced, else cores and GiB as one number (slice 21.6c).
  const size = (g: Allocation["groups"][number], key: "requests" | "waste") =>
    currency && g.cost ? g.cost[key] : weigh(g[key]);
  const top = {
    requests: Math.max(0, ...data.groups.map((g) => size(g, "requests"))),
    waste: Math.max(0, ...data.groups.map((g) => size(g, "waste"))),
  };
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
      <div className="ml-auto flex flex-wrap items-center gap-4 text-ink">
      {data.groupings.length > 1 && (
        <label>
          Group by{" "}
          <select value={data.by} onChange={(e) => setBy(e.target.value)}>
            {data.groupings.map((g) => (
              <option key={g} value={g}>{g.replace(/^label:/, "label ")}</option>
            ))}
          </select>
        </label>
      )}{" "}
      <label>
        Compare with{" "}
        <select value={compare} onChange={(e) => setCompare(e.target.value)}>
          <option value="">nothing</option>
          {AGO.map((d) => (
            <option key={d} value={String(d)}>{d === 1 ? "1 day ago" : `${d} days ago`}</option>
          ))}
        </select>
      </label>
      </div>
      </PageTools>
      {data.warnings.map((w) => (
        <p key={w} className="notice">{w}</p>
      ))}
      <Figures>
        <Tile icon={PiggyBank} label="Savings available" value={shown(data.total, "waste")} tone="saving" note={cmp ? change(cmp.change, "waste", currency) : undefined} />
        <Tile icon={Coins} label="Requests" value={shown(data.total, "requests")} note={cmp ? change(cmp.change, "requests", currency) : undefined} />
        {data.gpus && <Tile icon={Cpu} label="Idle GPUs" value={data.gpus.text} note="beside the Savings, never in them" />}
      </Figures>
      <p className="m-0 -mt-3 text-xs text-muted">
        Requests in the Contract's namespaces, not the nodes; the available Savings is what the requests above the
        Sizings cost.
      </p>
      {/* What the figures rest on, in one card under them (slice 21.6f): each line as it was,
          the title naming only what the card holds. */}
      {basis.length > 0 && (
        <section className="panel m-0">
          <h2>{basis.join(", ")}</h2>
          <PriceNotes notes={data.prices} />
          <BillLines bills={bills} />
          {cmp &&
            Object.entries(cmp.clusters).map(([cluster, c]) => (
              <p key={cluster} className={c.at == null ? "notice" : "hint"}>
                {c.at == null
                  ? `${cluster}: no run that old, oldest ${when(c.oldest)}; left out of the change`
                  : `${cluster}: compared with its run of ${when(c.at)}`}
              </p>
            ))}
          {cmp && cmp.left_out.length > 0 && (
            <p className="notice">Left out of the change, requests or labels unknown then: {cmp.left_out.join(", ")}</p>
          )}
        </section>
      )}
      <Card>
        <Realized data={data.realized} />
      </Card>
      {data.groups.length === 0 && <p>No Sizings stored yet: the Hub stores them hourly.</p>}
      <Card className="overflow-x-auto p-0">
      <table className="cost rounded-none border-0">
        <thead>
          <tr>
            <th>{label[0].toUpperCase() + label.slice(1)}</th>
            <th>Workloads</th>
            <th>Requests</th>
            <th>Savings available</th>
            {cmp && <th>Δ requests</th>}
            {cmp && <th>Δ savings</th>}
          </tr>
        </thead>
        <tbody>
          {data.groups.map((g) => {
            const [cluster, namespace] = g.group.split("/");
            const q = new URLSearchParams({ contract: data.contract, cluster, namespace });
            return (
              <tr key={g.group}>
                <td>{data.by === "namespace" ? <a href={`/app/?${q}`}>{g.group}</a> : g.group}</td>
                <td className="num">{g.workloads}</td>
                <td className="num">
                  {shown(g, "requests")}
                  <Bar share={share(size(g, "requests"), top.requests)} />
                </td>
                <td className="num text-saving">
                  {shown(g, "waste")}
                  <Bar accent share={share(size(g, "waste"), top.waste)} />
                </td>
                {cmp && <td className="num">{g.change ? change(g.change, "requests", currency) : "-"}</td>}
                {cmp && <td className="num">{g.change ? change(g.change, "waste", currency) : "-"}</td>}
              </tr>
            );
          })}
        </tbody>
        {data.groups.length > 0 && (
          <tfoot>
            <tr>
              <td>Total</td>
              <td className="num">{data.groups.reduce((t, g) => t + g.workloads, 0)}</td>
              <td className="num">{shown(data.total, "requests")}</td>
              <td className="num text-saving">{shown(data.total, "waste")}</td>
              {cmp && <td className="num">{change(cmp.change, "requests", currency)}</td>}
              {cmp && <td className="num">{change(cmp.change, "waste", currency)}</td>}
            </tr>
          </tfoot>
        )}
      </table>
      {data.groups.length > 0 && (
        <p className="hint m-0 px-3 py-3">Bars: each group's share of the largest; the Total is the Hub's own.</p>
      )}
      </Card>
    </div>
  );
}

function Realized({ data }: { data: Allocation["realized"] }) {
  const lowered = data.floors ?? [];
  if (data.drops.length === 0 && lowered.length === 0)
    return <p className="hint m-0">Savings realized: none yet. A request lowered to its Sizing shows after the next hourly run.</p>;
  const cur = data.currency;
  return (
    <p className="total m-0">
      Savings realized:{" "}
      {cur && data.cost ? (
        <>
          <strong>{money(data.cost.rate, cur)}</strong>, {amount(data.cost.accrued, cur)} so far,{" "}
          {money(data.cost.week, cur)} from this week's
        </>
      ) : (
        <>
          <strong>
            {cores(data.rate.cpu ?? 0)}, {gib(data.rate.memory ?? 0)}
          </strong>
          , {cores(data.week.cpu ?? 0)}, {gib(data.week.memory ?? 0)} from this week's
        </>
      )}{" "}
      ({data.drops.length} {data.drops.length === 1 ? "request" : "requests"} lowered to {data.drops.length === 1 ? "its Sizing" : "their Sizings"}
      {lowered.length > 0 &&
        `, ${lowered.length} HPA ${lowered.length === 1 ? "minimum" : "minimums"} lowered toward ${lowered.length === 1 ? "its Floor" : "their Floors"}`}
      )
    </p>
  );
}

function Detail({ contract, workload, currency }: { contract: string; workload: Workload; currency?: string | null }) {
  return (
    <section className="detail">
      <h2>{workload.kind}/{workload.name}</h2>
      <p className="hint">
        Waste: {cores(workload.waste.cpu ?? 0)} CPU, {gib(workload.waste.memory ?? 0)} memory
        {currency && workload.cost && `; ${money(workload.cost.waste, currency)} of ${money(workload.cost.requests, currency)} in requests`}
      </p>
      {workload.containers.map((c) => (
        <ContainerDetail key={c.container} contract={contract} c={c} />
      ))}
    </section>
  );
}

function ContainerDetail({ contract, c }: { contract: string; c: Container }) {
  const [days, setDays] = useState<14 | 400>(14);
  const [points, setPoints] = useState<Point[] | null>(null);
  useEffect(() => {
    setPoints(null);
    api.history(contract, c, days).then((h) => setPoints(h.points), () => setPoints([]));
  }, [contract, c, days]);
  return (
    <div className="one-container">
      <h3>
        {c.container} <span className="hint">({c.policy})</span>
      </h3>
      <p className="num whitespace-normal">
        CPU {c.cpu?.action ?? "-"} {cpu(c.cpu?.current)} → {cpu(c.cpu?.target)}; memory{" "}
        {c.memory?.action ?? "-"} {memory(c.memory?.current)} → {memory(c.memory?.target)}
      </p>
      {c.gpus && (
        <details className="types">
          <summary>
            GPUs ({c.gpus.gpus.length}), {c.gpus.asked} a pod
          </summary>
          <ul>
            {c.gpus.gpus.map((g) => (
              <li key={g.id}>
                {g.id} on {g.node} ({g.pod}): {g.verdict}
                {g.busiest != null && `, busiest hour ${pct(g.busiest)} by ${g.by}`} over {g.hours} h, held{" "}
                {pct(g.held)} of the window{g.memory != null && `, memory peak ${memory(g.memory)}`}
              </li>
            ))}
          </ul>
        </details>
      )}
      {(c.groups ?? []).length > 0 && (
        <details className="types" open={c.by_type}>
          <summary>
            {c.by_type
              ? `By instance type (${c.groups!.length}): what its Actuator applies, each pod by its node's type; the one Sizing above is not applied`
              : `By instance type (${c.groups!.length}): the one Sizing above is what is applied to every pod`}
          </summary>
          <ul>
            {c.groups!.map((g) => (
              <li key={g.type}>
                {g.type || "nodes with no instance type"} ({g.pods} pods):{" "}
                {g.state === "learning"
                  ? `learning (${g.history_h} h of usage)`
                  : `CPU ${g.cpu?.action ?? "-"} ${cpu(g.cpu?.current)} → ${cpu(g.cpu?.target)}; memory ${g.memory?.action ?? "-"} ${memory(g.memory?.current)} → ${memory(g.memory?.target)}`}
              </li>
            ))}
          </ul>
        </details>
      )}
      {(
        [
          ["CPU", c.cpu?.held],
          ["Memory", c.memory?.held],
        ] as const
      ).map(
        ([name, held]) =>
          held && (
            <details key={name} open>
              <summary>
                {name} is held by its HPA {held.hpa} ({held.percent}% of requests): the Actuator leaves this request
                alone{held.change ? ". The change that frees it:" : "."}
              </summary>
              {held.change && <pre>{held.change}</pre>}
            </details>
          ),
      )}
      {c.risks.length > 0 && (
        <ul className="risks">
          {c.risks.map((r, i) => (
            <li key={i}>
              <span className="chip">{r.kind}</span> {r.text}
            </li>
          ))}
        </ul>
      )}
      <div className="range">
        {([14, 400] as const).map((d) => (
          <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}>
            {d === 14 ? "14 days, hourly" : "400 days, daily"}
          </button>
        ))}
      </div>
      {points === null ? (
        <p className="hint">Loading…</p>
      ) : points.length === 0 ? (
        <p className="hint">No stored Sizings in this range.</p>
      ) : (
        <div className="charts">
          <Chart points={points} res="cpu" />
          <Chart points={points} res="memory" />
        </div>
      )}
    </div>
  );
}
