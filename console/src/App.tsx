// Workloads per Contract (slice 9.5a): each Workload's waste and risks from the
// Hub's latest stored Sizings, and one container's requests vs Sizing over time.
// Cost (slice 10.2): what each namespace's, team's or label value's requests and waste cost;
// the waste's cost is the available Savings, compared with N days ago on request (10.3a),
// and the realized Savings, requests lowered to their Sizings (10.3b).
// The Agenda (slice 11.4): what to look at now, most urgent first, above the Workloads.
// Investigations and the approver token page (slice 11.5a): Investigations.tsx.
// Automation and Revert (slice 12.6b): Automation.tsx.
// Everything from the API is rendered as text.
import { useEffect, useMemo, useState } from "react";
import * as api from "./api";
import type { Allocation, Bills, Container, Point, Workload, Workloads } from "./api";
import Chart from "./Chart";
import { InvestigationList, InvestigationPage, TokenPage, page } from "./Investigations";
import { TeachingsPage } from "./Teachings";
import { AutomationPage } from "./Automation";
import { ChecksPage, HealthPage } from "./Checks";
import { ContractsPage } from "./Contracts";
import { NodesPage } from "./Nodes";
import { amount, change, cores, cpu, gib, memory, money, rank, when } from "./format";

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
            {b.note ? `: ${b.note}` : ""}
          </p>
        );
      })}
    </>
  );
}

function useParam(name: string): [string, (v: string) => void] {
  const [value, set] = useState(() => new URLSearchParams(location.search).get(name) ?? "");
  const update = (v: string) => {
    const q = new URLSearchParams(location.search);
    if (v) q.set(name, v);
    else q.delete(name);
    history.replaceState(null, "", `${location.pathname}?${q}`);
    set(v);
  };
  return [value, update];
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
  const current = (v: string) => (view === v ? "page" : undefined);
  if (view === "token")
    return (
      <>
        <Header here={here} current={current} />
        <main>
          <TokenPage />
        </main>
      </>
    );

  return (
    <>
      <Header here={here} current={current} />
      <main>
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
        {view !== "investigation" && view !== "contracts" && contracts.length > 1 && (
          <label>
            Contract{" "}
            <select
              value={shown}
              onChange={(e) => {
                setBy(""); // another Contract may not have this grouping
                setContract(e.target.value);
              }}
            >
              {contracts.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </label>
        )}
        {shown && view !== "investigation" && view !== "contracts" && <KillSwitchBar contract={shown} />}
        {shown && workloadsView && <AgendaPanel contract={shown} />}
        {data && workloadsView && <Page contract={shown} data={data} />}
        {shown && view === "investigations" && <InvestigationList contract={shown} />}
        {shown && view === "teachings" && <TeachingsPage contract={shown} />}
        {shown && view === "automation" && <AutomationPage contract={shown} />}
        {shown && view === "checks" && <ChecksPage contract={shown} />}
        {shown && view === "health" && <HealthPage contract={shown} />}
        {shown && view === "nodes" && <NodesPage contract={shown} />}
        {allocation && costView && <CostPage data={allocation} bills={bills} setBy={setBy} compare={compare} setCompare={setCompare} />}
      </main>
    </>
  );
}

function Header({ here, current }: { here: string; current: (v: string) => "page" | undefined }) {
  return (
    <header>
      <a className="brand" href="/app/">squero</a>
      <nav>
        <a href="/app/?view=contracts" aria-current={current("contracts")}>Contracts</a>
        <a href={`/app/?${here}&view=investigations`} aria-current={current("investigations") ?? current("investigation")}>
          Investigations
        </a>
        <a href={`/app/?${here}&view=teachings`} aria-current={current("teachings")}>Teachings</a>
        <a href={`/app/?${here}&view=checks`} aria-current={current("checks")}>Checks</a>
        <a href={`/app/?${here}&view=health`} aria-current={current("health")}>Health</a>
        <a href={`/app/?${here}`} aria-current={current("")}>Workloads</a>
        <a href={`/app/?${here}&view=cost`} aria-current={current("cost")}>Cost</a>
        <a href={`/app/?${here}&view=nodes`} aria-current={current("nodes")}>Nodes</a>
        <a href={`/app/?${here}&view=automation`} aria-current={current("automation")}>Automation</a>
        <a href="/app/?view=token" aria-current={current("token")}>Approver token</a>
      </nav>
    </header>
  );
}

/** The Contract's Kill switch: on stops every change to its systems; only here does it go
 * off again (the Hub MCP server can only turn it on). */
function KillSwitchBar({ contract }: { contract: string }) {
  const [state, setState] = useState<api.KillSwitch | null>(null);
  const [failed, setFailed] = useState("");
  useEffect(() => {
    setState(null);
    setFailed("");
    api.killSwitch(contract).then(setState, () => setState(null));
  }, [contract]);
  if (!state) return null;
  const act = (change: (c: string) => Promise<api.KillSwitch>) =>
    change(contract).then(setState, (e: Error) => setFailed(e.message));
  return state.on ? (
    <p className="notice">
      Kill switch on since {when(state.since)} ({state.by}): approving is refused and no change
      to this Contract's systems runs.{" "}
      <button onClick={() => act(api.turnOffKillSwitch)}>Turn it off</button>
      {failed && ` Couldn't: ${failed}`}
    </p>
  ) : (
    <p>
      <button onClick={() => act(api.turnOnKillSwitch)}>Turn on the Kill switch</button>
      {failed && ` Couldn't: ${failed}`}
    </p>
  );
}

/** Where to act on an Agenda item: its Investigation, its Workload, or the Cost page. */
function link(contract: string, i: api.AgendaItem): string | null {
  const here = `contract=${encodeURIComponent(contract)}`;
  if (i.id) return page(i.id);
  if (i.workload) return `/app/?${here}&workload=${encodeURIComponent(i.workload)}`;
  if (i.kind === "bill") return `/app/?${here}&view=cost`;
  return null;
}

/** What to look at now in this Contract, most urgent first (the Hub ranks it). */
function AgendaPanel({ contract }: { contract: string }) {
  const [agenda, setAgenda] = useState<api.Agenda | null>(null);
  useEffect(() => {
    setAgenda(null);
    api.agenda(contract).then(setAgenda, () => setAgenda(null));
  }, [contract]);
  if (!agenda) return null;
  if (agenda.items.length === 0) return <p className="hint">Nothing needs you now.</p>;
  return (
    <section className="agenda">
      <h2>Now</h2>
      <ol>
        {agenda.items.map((i, n) => {
          const href = link(contract, i);
          const where = i.cluster && !i.workload ? `${i.cluster}: ` : i.workload ? `${i.workload}: ` : "";
          const time = i.kind === "approval" ? ` (goes Stale ${when(i.at)})` : i.at ? ` (${when(i.at)})` : "";
          return (
            <li key={n} className={i.kind}>
              {href ? <a href={href}>{where}{i.title}</a> : `${where}${i.title}`}
              {time}
            </li>
          );
        })}
      </ol>
    </section>
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
    <section className="floors">
      <h3>Autoscaled Workloads</h3>
      {total && total.workloads > 0 && (
        <p className="total">
          Floors: {total.workloads} can lower their HPA minimum, {total.replicas.toFixed(1)} replicas fewer on average
          {": "}
          <strong>
            {data.currency && total.cost != null
              ? money(total.cost, data.currency)
              : `${cores(total.saving.cpu ?? 0)}, ${gib(total.saving.memory ?? 0)}`}
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
                Floor <strong>{f.floor}</strong>; its HPA {f.hpa} has a minimum of {f.minimum}
                {f.maximum != null && ` and a maximum of ${f.maximum}`}
                {f.saving &&
                  `. At the Floor: ${f.replicas} ${f.replicas === 1 ? "replica" : "replicas"} fewer on average, ${
                    data.currency && f.cost != null
                      ? money(f.cost, data.currency)
                      : `${cores(f.saving.cpu ?? 0)}, ${gib(f.saving.memory ?? 0)}`
                  }`}{" "}
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
    </section>
  );
}

function Page({ contract, data }: { contract: string; data: Workloads }) {
  const [cluster, setCluster] = useParam("cluster");
  const [namespace, setNamespace] = useParam("namespace");
  const [picked, setPicked] = useParam("workload");
  const clusters = Object.keys(data.clusters).sort();
  const namespaces = [...new Set(data.workloads.map((w) => w.namespace))].sort();
  const rows = useMemo(
    () =>
      data.workloads
        .filter((w) => (!cluster || w.cluster === cluster) && (!namespace || w.namespace === namespace))
        .sort((a, b) => rank(b) - rank(a) || a.workload.localeCompare(b.workload)),
    [data, cluster, namespace],
  );
  const selected = data.workloads.find((w) => w.workload === picked);

  return (
    <>
      <h1>{data.contract}: Workloads</h1>
      <ul className="clusters">
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
      {clusters.length === 0 && <p>No Sizings stored yet: the Hub stores them hourly.</p>}
      <PriceNotes notes={data.prices} />
      {data.currency && data.cost ? (
        <p className="total">
          Waste: <strong>{money(data.cost.waste, data.currency)}</strong> ({cores(data.total.cpu ?? 0)},{" "}
          {gib(data.total.memory ?? 0)}) of {money(data.cost.requests, data.currency)} in requests{" "}
          <span className="hint">(requests above the Sizings; requests are what pods reserve, not the nodes)</span>
        </p>
      ) : (
        <p className="total">
          Waste: <strong>{cores(data.total.cpu ?? 0)}</strong>, <strong>{gib(data.total.memory ?? 0)}</strong>{" "}
          <span className="hint">(requests above the Sizings; a core ranks like 4 GiB)</span>
        </p>
      )}
      <div className="filters">
        <label>
          Cluster{" "}
          <select value={cluster} onChange={(e) => setCluster(e.target.value)}>
            <option value="">all</option>
            {clusters.map((c) => <option key={c}>{c}</option>)}
          </select>
        </label>
        <label>
          Namespace{" "}
          <select value={namespace} onChange={(e) => setNamespace(e.target.value)}>
            <option value="">all</option>
            {namespaces.map((n) => <option key={n}>{n}</option>)}
          </select>
        </label>
      </div>
      <table>
        <thead>
          <tr>
            <th>Workload</th>
            <th>Pods</th>
            <th>CPU waste</th>
            <th>Memory waste</th>
            {data.currency && <th>Waste cost</th>}
            {data.currency && <th>Requests cost</th>}
            <th>Risks</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((w) => (
            <tr
              key={w.workload}
              className={w.workload === picked ? "picked" : ""}
              onClick={() => setPicked(w.workload)}
            >
              <td>
                <button type="button" className="link">{w.kind}/{w.name}</button>
                <div className="hint">{w.cluster}/{w.namespace}</div>
              </td>
              <td>{w.pods}</td>
              <td>{cores(w.waste.cpu ?? 0)}</td>
              <td>{gib(w.waste.memory ?? 0)}</td>
              {data.currency && <td>{w.cost ? money(w.cost.waste, data.currency) : "-"}</td>}
              {data.currency && <td>{w.cost ? money(w.cost.requests, data.currency) : "-"}</td>}
              <td>
                {[...new Set(w.risks.map((r) => r.kind))].sort().map((k) => (
                  <span key={k} className="chip">{k}</span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && <Detail contract={contract} workload={selected} />}
      <Floors data={data} cluster={cluster} namespace={namespace} />
      {data.learning.length > 0 && (
        <p>
          Learning:{" "}
          {data.learning
            .map((c) => `${c.workload} (${c.history_h ?? 0} h of ${c.needs_h ?? 0} h)`)
            .join(", ")}
        </p>
      )}
      {data.excluded.length > 0 && <p>Excluded: {data.excluded.map((c) => c.workload).join(", ")}</p>}
    </>
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
  return (
    <>
      <h1>{data.contract}: Cost by {label}</h1>
      <p className="hint">
        Requests in the Contract's namespaces, not the nodes; the available Savings is what the requests above the
        Sizings cost.
      </p>
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
      {data.warnings.map((w) => (
        <p key={w} className="notice">{w}</p>
      ))}
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
      <p className="total">
        Requests: <strong>{shown(data.total, "requests")}</strong>, Savings available:{" "}
        <strong>{shown(data.total, "waste")}</strong>
        {cmp && ` (${change(cmp.change, "requests", currency)}, ${change(cmp.change, "waste", currency)})`}
      </p>
      <Realized data={data.realized} />
      {data.groups.length === 0 && <p>No Sizings stored yet: the Hub stores them hourly.</p>}
      <table>
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
                <td>{g.workloads}</td>
                <td>{shown(g, "requests")}</td>
                <td>{shown(g, "waste")}</td>
                {cmp && <td>{g.change ? change(g.change, "requests", currency) : "-"}</td>}
                {cmp && <td>{g.change ? change(g.change, "waste", currency) : "-"}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function Realized({ data }: { data: Allocation["realized"] }) {
  const lowered = data.floors ?? [];
  if (data.drops.length === 0 && lowered.length === 0)
    return <p className="hint">Savings realized: none yet. A request lowered to its Sizing shows after the next hourly run.</p>;
  const cur = data.currency;
  return (
    <p className="total">
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

function Detail({ contract, workload }: { contract: string; workload: Workload }) {
  return (
    <section className="detail">
      <h2>{workload.kind}/{workload.name}</h2>
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
    <div className="container">
      <h3>
        {c.container} <span className="hint">({c.policy})</span>
      </h3>
      <p>
        CPU {c.cpu?.action ?? "-"} {cpu(c.cpu?.current)} → {cpu(c.cpu?.target)}; memory{" "}
        {c.memory?.action ?? "-"} {memory(c.memory?.current)} → {memory(c.memory?.target)}
      </p>
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
