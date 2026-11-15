/** Nodes (slice 15.1a): each cluster's nodes and their Room, as the latest hourly run read
 * them. Pods outside the Contract's namespaces are a count and a sum, never named. Each
 * node's verdict (slice 15.1b): whether it could be removed, by its cluster's rules, going
 * by its Actuator's hourly report of Blockers. Placement (slice 15.2a): the cluster's Pinned
 * nodes, the Workloads placed on them, and which to restart to free a node. */
import * as api from "./api";
import { useLoaded } from "./Checks";
import { cores, gib, money, when } from "./format";

const both = (v: { cpu?: number; memory?: number }) => `${cores(v.cpu ?? 0)}, ${gib(v.memory ?? 0)}`;

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

export function NodesPage({ contract }: { contract: string }) {
  const [data, error] = useLoaded(api.nodes, contract);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return null;
  return (
    <>
      <h1>{contract}: Nodes</h1>
      <p className="hint">
        A node's Room is its allocatable less the requests of every Running or Pending pod on it. Pods outside this
        Contract's namespaces are not visible: they count as one sum per node.
      </p>
      {data.clusters.length === 0 && <p className="hint">No nodes read yet: the hourly run reads them.</p>}
      {data.clusters.map((c) => (
        <section key={c.cluster} className="nodes">
          <h2>{c.cluster}</h2>
          <p className="hint">
            Read {when(c.at)}. Autoscaler: {c.autoscaler ?? "none named in the Contract"}.
            {c.largest && ` No Sizing asks more than ${both(c.largest)}: the largest Room after DaemonSets.`}
          </p>
          <p className="hint">
            Whether a node could be removed goes by {c.rules}; its Actuator's last report of Blockers:{" "}
            {when(c.reported)}.
          </p>
          {c.placement && (
            <p className="hint placement">
              Placement: pods that can't move prefer the nodes labelled {c.placement.label} ({c.placement.pinned}{" "}
              pinned){c.placement.taint && `, tolerating ${c.placement.taint}`}.{" "}
              {c.placement.note && <span className="notice">{c.placement.note}. </span>}
              Placed: {c.placement.workloads.length ? c.placement.workloads.join(", ") : "none"}.{" "}
              {!c.placement.placing && "Its Actuator doesn't place them yet."}
            </p>
          )}
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
            <p className="could-go">
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
                        .
                      </>
                    )}
                  </span>
                );
              })}{" "}
              <span className="hint">
                An upper bound: affinity and spread rules can't be seen. Not part of Savings.
                {(c.judged.unknown ?? 0) > 0 && ` ${c.judged.unknown} of ${c.nodes.length} nodes not judged.`}
              </span>
            </p>
          )}
          {c.problem ? (
            <p className="notice">{c.problem}</p>
          ) : (
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
                      {n.cordoned && <span className="chip">cordoned</span>}{" "}
                      {n.pinned && (
                        <>
                          <span className="chip">pinned</span>{" "}
                        </>
                      )}
                      {n.taints.map((t) => (
                        <span key={t} className="chip">{t}</span>
                      ))}
                    </td>
                    <td>{both(n.allocatable)}</td>
                    <td>
                      <details>
                        <summary>
                          {n.mine.pods} pods: {both(n.mine)}
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
                    <td>
                      {n.others.pods} pods: {both(n.others)}
                    </td>
                    <td>{both(n.daemonsets)}</td>
                    <td>{both(n.room)}</td>
                    <td>
                      {n.verdict.state === "clear" && "yes: nothing on it blocks"}
                      {n.verdict.state === "unknown" && <span className="hint">unknown: {n.verdict.why}</span>}
                      {n.verdict.state === "pinned" && (
                        <>
                          no: pinned, kept for pods that can't move
                          <Blockers found={n.verdict.blockers ?? []} />
                        </>
                      )}
                      {n.verdict.state === "blocked" && (
                        <>
                          <span className="bad">blocked</span>
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
          )}
        </section>
      ))}
    </>
  );
}
