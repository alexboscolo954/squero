/** Nodes (slice 15.1a): each cluster's nodes and their Room, as the latest hourly run read
 * them. Pods outside the Contract's namespaces are a count and a sum, never named. */
import * as api from "./api";
import { useLoaded } from "./Checks";
import { cores, gib, when } from "./format";

const both = (v: { cpu?: number; memory?: number }) => `${cores(v.cpu ?? 0)}, ${gib(v.memory ?? 0)}`;

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
                </tr>
              </thead>
              <tbody>
                {c.nodes.map((n) => (
                  <tr key={n.node}>
                    <td>
                      {n.node} {n.type && <span className="hint">{n.type}</span>}{" "}
                      {n.cordoned && <span className="chip">cordoned</span>}{" "}
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
