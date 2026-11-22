/** Nodes (slice 15.1a): each cluster's nodes and their Room, as the latest hourly run read
 * them. Pods outside the Contract's namespaces are a count and a sum, never named (but for
 * the Actuator's own pod, where it blocks: audit A4). Each
 * node's verdict (slice 15.1b): whether it could be removed, by its cluster's rules, going
 * by its Actuator's latest report of Blockers. Placement (slice 15.2a): the cluster's Pinned
 * nodes, the Workloads placed on them, and which to restart to free a node. The page pattern
 * (slice 21.6d): figures on top, each cluster's facts as a list, a table that fits its card. */
import { ArrowDownToLine, CircleAlert, Server, Tag } from "lucide-react";
import * as api from "./api";
import { useLoaded } from "./Checks";
import { cores, gib, money, when } from "./format";
import { nodesAtGlance } from "./Overview";
import { PageTools } from "./Shell";
import { Badge, Change } from "./ui/badge";
import { Card, Figures, Skeleton, Tile } from "./ui/card";

const both = (v: { cpu?: number; memory?: number }) => `${cores(v.cpu ?? 0)}, ${gib(v.memory ?? 0)}`;

/** Cores and GiB in a table cell: the same text as `both`, breaking only between the two, so
 * the table's columns stay narrow (slice 21.6d). */
function Both({ v }: { v: { cpu?: number; memory?: number } }) {
  return (
    <>
      <span className="whitespace-nowrap">{cores(v.cpu ?? 0)},</span>{" "}
      <span className="whitespace-nowrap">{gib(v.memory ?? 0)}</span>
    </>
  );
}

type Blocker = NonNullable<api.Nodes["clusters"][number]["nodes"][number]["verdict"]["blockers"]>[number];

function Blockers({ found }: { found: Blocker[] }) {
  return (
    <ul>
      {found.map((b) => (
        <li key={`${b.namespace}/${b.pod}`}>
          {b.namespace}/{b.pod}: {b.reasons.join("; ")}
          {b.placement && <span className="chip">placed</span>}
          {b.note && <div className="hint">{b.note}.</div>}
          {(b.changes ?? []).map((c, i) => (
            <details key={i} className="change">
              <summary>To remove it: {c.reason}</summary>
              <div className="hint">{c.change}.</div>
              {c.manifest && <pre>{c.manifest}</pre>}
            </details>
          ))}
        </li>
      ))}
    </ul>
  );
}

type Cluster = api.Nodes["clusters"][number];

/** Where a cluster's Workloads could run on spot, and those under a Spot ratio (slice 16.1c). */
function Spotting({ c, currency }: { c: Cluster; currency?: string | null }) {
  const found = c.candidates;
  const under = c.spot_workloads;
  return (
    <>
      {under && (
        <div className="spot-workloads">
          <p className="hint">
            Under a Spot ratio: {under.note && <span className="notice">{under.note}.</span>}
          </p>
          <ul>
            {under.workloads.map((w) => (
              <li key={`${w.namespace}/${w.kind}/${w.name}`}>
                {w.namespace}/{w.kind}/{w.name}:{" "}
                {w.ratio == null ? (
                  <>
                    its runs on spot, a retry off it
                    {w.actual && `; running ${w.actual.on_demand} on-demand and ${w.actual.spot} on spot`}
                  </>
                ) : (
                  <>
                    {w.replicas} replicas
                    {w.intended && `, meant ${w.intended.on_demand} on-demand and ${w.intended.spot} on spot`}
                    {w.actual ? `, running ${w.actual.on_demand} and ${w.actual.spot}` : ", where they run not read yet"}
                  </>
                )}
                {w.why && <span className="hint"> ({w.why})</span>}
                {w.risk && <div className="notice">{w.risk}.</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {found && (found.workloads.length > 0 || found.note) && (
        <details className="candidates">
          <summary>
            Could run on spot: {found.workloads.length + (found.more ?? 0)}
            {currency && found.saving != null && `, up to ${money(found.saving, currency)}`}
            {found.note && <span className="notice"> ({found.note})</span>}
          </summary>
          <p className="hint">
            Each with 70% of its replicas on spot and 2 on-demand at least: an upper bound, saved only where a spot
            pool exists and the autoscaler moves nodes to it. Not part of Savings.
          </p>
          <ul>
            {found.workloads.map((w) => (
              <li key={`${w.namespace}/${w.kind}/${w.name}`}>
                {w.namespace}/{w.kind}/{w.name}: {w.replicas} {w.spot == null ? "pods on average" : "replicas"} of{" "}
                {both(w.requests)},{" "}
                {w.spot == null ? "a run entirely on spot, if its Jobs allow a retry" : `${w.spot} on spot and ${w.on_demand} on-demand`}
                {currency && w.saving != null && `: ${money(w.saving, currency)}`}
              </li>
            ))}
            {(found.more ?? 0) > 0 && <li className="hint">and {found.more} more</li>}
          </ul>
        </details>
      )}
    </>
  );
}

export function NodesPage({ contract }: { contract: string }) {
  const [data, error] = useLoaded(api.nodes, contract);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const g = nodesAtGlance(data);
  const s = (n: number) => (n === 1 ? "" : "s");
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>
          {data.clusters.length} cluster{s(data.clusters.length)}
        </span>
      </PageTools>
      {data.clusters.length > 0 && (
        <Figures>
          <Tile icon={Server} label="Nodes read" value={String(g.nodes)} note={`in ${g.clusters} cluster${s(g.clusters)}`} />
          {/* In ink, never the accent: an upper bound, not part of Savings. */}
          <Tile
            icon={ArrowDownToLine}
            label="Nodes that could go now"
            value={g.could ? String(g.could.now) : "not judged"}
            note={g.could ? "an upper bound" : undefined}
          />
          <Tile
            icon={ArrowDownToLine}
            label="After the Sizings"
            value={g.could ? String(g.could.after) : "not judged"}
            note={g.could ? "nodes that could go, an upper bound" : undefined}
          />
          {g.spot != null && <Tile icon={Tag} label="Spot nodes" value={String(g.spot)} />}
          {g.unread.length > 0 && (
            <Tile icon={CircleAlert} label="Clusters not read" value={String(g.unread.length)} note={g.unread.join(", ")} />
          )}
        </Figures>
      )}
      <p className="hint m-0">
        A node's Room is its allocatable less the requests of every Running or Pending pod on it. Pods outside this
        Contract's namespaces are not visible: they count as one sum per node.
      </p>
      {data.clusters.length === 0 && <p className="hint">No nodes read yet: the hourly run reads them.</p>}
      {data.clusters.map((c) => (
        <Card key={c.cluster} className="nodes flex flex-col gap-2">
          <h2 className="m-0">{c.cluster}</h2>
          <dl className="facts m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted">Read</dt>
            <dd className="m-0">{when(c.at)}</dd>
            <dt className="text-muted">Autoscaler</dt>
            <dd className="m-0">{c.autoscaler ?? "none named in the Contract"}</dd>
            {c.largest && (
              <>
                <dt className="text-muted">Largest Room</dt>
                <dd className="m-0">{both(c.largest)} after DaemonSets: no Sizing asks more</dd>
              </>
            )}
            <dt className="text-muted">Could be removed</dt>
            <dd className="m-0">goes by {c.rules}</dd>
            <dt className="text-muted">Blockers</dt>
            <dd className="m-0">its Actuator's last report: {when(c.reported)}</dd>
            {c.placement && (
              <>
                <dt className="text-muted">Placement</dt>
                <dd className="placement m-0">
                  pods that can't move prefer the nodes labelled {c.placement.label} ({c.placement.pinned} pinned)
                  {c.placement.taint && `, tolerating ${c.placement.taint}`}.{" "}
                  {c.placement.note && <span className="notice">{c.placement.note}. </span>}
                  Placed: {c.placement.workloads.length ? c.placement.workloads.join(", ") : "none"}.{" "}
                  {!c.placement.placing && "Its Actuator doesn't place them yet."}
                </dd>
              </>
            )}
            {c.spot && (
              <>
                <dt className="text-muted">Spot</dt>
                <dd className="spot m-0">
                  nodes labelled {c.spot.label} ({c.spot.nodes}) are priced at {c.spot.discount} of on-demand.{" "}
                  {c.spot.note && <span className="notice">{c.spot.note}.</span>}
                </dd>
              </>
            )}
          </dl>
          <Spotting c={c} currency={data.currency} />
          {(c.settings ?? []).length > 0 && (
            <details className="settings">
              <summary>Its autoscaler's own settings to check (the Hub can't read them)</summary>
              <ul>
                {(c.settings ?? []).map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </details>
          )}
          {c.could_go && (
            <p className="m-0 mt-2 text-sm font-semibold">
              Nodes that could go: now{" "}
              <Change from={String(c.could_go.now.nodes.length)} to={`${c.could_go.after.nodes.length} after the Sizings`} />
            </p>
          )}
          {c.could_go && (
            <p className="could-go m-0 rounded-lg border border-saving/40 bg-saving/5 p-3">
              {(["now", "after"] as const).map((which) => {
                const went = c.could_go![which];
                return (
                  <span key={which}>
                    {which === "now" ? "Could go now: " : " After the Sizings: "}
                    {went.nodes.length === 0 ? (
                      "none."
                    ) : (
                      <>
                        up to {went.nodes.length} ({went.nodes.join(", ")}),{" "}
                        <strong>
                          {data.currency && went.cost != null ? money(went.cost, data.currency) : both(went)}
                        </strong>
                        {went.gpus ? ` with ${went.gpus} GPUs` : ""}
                        {data.currency && went.gpus && went.cost == null ? " (no GPU price: set gpu in its prices)" : ""}
                        .
                      </>
                    )}
                  </span>
                );
              })}{" "}
              {c.could_go.note && <span className="hint"> None counted: {c.could_go.note}.</span>}{" "}
              {c.could_go.gpu_note && <span className="notice"> {c.could_go.gpu_note}.</span>}{" "}
              <span className="hint">
                An upper bound: tolerations and spread rules can't be seen. Not part of Savings.
                {(c.judged.unknown ?? 0) > 0 && ` ${c.judged.unknown} of ${c.nodes.length} nodes not judged.`}
              </span>
            </p>
          )}
          {c.unrequested_gpus && c.unrequested_gpus.gpus > 0 && (
            <p className="could-go m-0 rounded-lg border border-line p-3">
              Unrequested GPUs:{" "}
              <strong>
                {c.unrequested_gpus.gpus} on {c.unrequested_gpus.nodes} nodes
                {data.currency && c.unrequested_gpus.cost != null && `, ${money(c.unrequested_gpus.cost, data.currency)}`}
              </strong>
              . <span className="hint">No pod asks for them; apart from idle GPUs and waste. Not part of Savings.</span>
            </p>
          )}
          {c.problem ? (
            <p className="notice">{c.problem}</p>
          ) : (
            <div className="mt-2 overflow-x-auto">
            <table className="investigations">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Allocatable</th>
                  <th>This Contract's pods</th>
                  <th>Not visible</th>
                  <th>DaemonSets</th>
                  <th>Room</th>
                  <th>Could be removed</th>
                </tr>
              </thead>
              <tbody>
                {c.nodes.map((n) => (
                  <tr key={n.node}>
                    <td>
                      {n.node} {n.type && <span className="hint">{n.type}</span>}{" "}
                      {n.cordoned && <span className="chip bg-line text-muted">cordoned</span>}{" "}
                      {n.gpus && (
                        <>
                          <span className="chip bg-line text-muted">
                            {n.gpus.requests} of {n.gpus.allocatable} GPUs asked{n.gpus.product ? `, ${n.gpus.product}` : ""}
                          </span>{" "}
                        </>
                      )}
                      {n.spot && (
                        <>
                          <span className="chip bg-line text-muted">spot</span>{" "}
                        </>
                      )}
                      {n.pinned && (
                        <>
                          <span className="chip bg-line text-muted">pinned</span>{" "}
                        </>
                      )}
                      {n.taints.map((t) => (
                        <span key={t} className="chip bg-line text-muted">{t}</span>
                      ))}
                    </td>
                    <td className="num whitespace-normal">
                      <Both v={n.allocatable} />
                    </td>
                    <td>
                      <details>
                        <summary className="num whitespace-normal">
                          <span className="whitespace-nowrap">{n.mine.pods} pods:</span> <Both v={n.mine} />
                        </summary>
                        <ul>
                          {n.own.map((p) => (
                            <li key={`${p.namespace}/${p.pod}`}>
                              {p.namespace}/{p.pod}: {both(p)}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </td>
                    <td className="num whitespace-normal">
                      <span className="whitespace-nowrap">{n.others.pods} pods:</span> <Both v={n.others} />
                    </td>
                    <td className="num whitespace-normal">
                      <Both v={n.daemonsets} />
                    </td>
                    <td className="num whitespace-normal">
                      <Both v={n.room} />
                    </td>
                    <td>
                      {n.verdict.state === "clear" && <Badge tone="good">yes: nothing on it blocks</Badge>}
                      {n.verdict.state === "unknown" && (
                        <span className="hint">
                          <Badge>unknown</Badge>: {n.verdict.why}
                        </span>
                      )}
                      {n.verdict.state === "pinned" && (
                        <>
                          <Badge tone="policy">no: pinned, kept for pods that can't move</Badge>
                          <Blockers found={n.verdict.blockers ?? []} />
                        </>
                      )}
                      {n.verdict.state === "blocked" && (
                        <>
                          <Badge tone="bad">blocked</Badge>
                          <Blockers found={n.verdict.blockers ?? []} />
                          {n.verdict.restart &&
                            (n.verdict.restart.workloads.length ? (
                              <div className="restart">Restart {n.verdict.restart.workloads.join(", ")} to free it.</div>
                            ) : (
                              <div className="hint">A restart wouldn't free it: {n.verdict.restart.why_not}.</div>
                            ))}
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
