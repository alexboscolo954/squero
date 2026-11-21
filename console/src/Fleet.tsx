// The Fleet (slice 18.4): every Contract the Hub serves, a row each, read-only. Money in each
// Contract's own currency, totals per currency, never converted.
import { useEffect, useState } from "react";
import * as api from "./api";
import { money } from "./format";

const counts = (agenda: Record<string, number>) =>
  Object.entries(agenda)
    .map(([kind, count]) => `${kind} ${count}`)
    .join(", ") || "nothing";

/** A GPU count held for part of the window: 1, 0.25, 2.14, as the Hub words it. */
const n = (v: number) => String(+v.toFixed(2));

function unrequested(g: api.Fleet["rows"][number]["unrequested_gpus"], currency: string | null | undefined) {
  if (!g) return "-";
  const said = `${n(g.gpus)} on ${g.nodes} nodes`;
  if (!currency) return said;
  return g.cost == null ? `${said}, no GPU price` : `${said}, ${money(g.cost, currency)}`;
}

export function FleetPage() {
  const [fleet, setFleet] = useState<api.Fleet | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.fleet().then(setFleet, (e: Error) => setError(e.message));
  }, []);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!fleet) return null;
  return (
    <>
      <h1>Fleet</h1>
      {fleet.rows.length === 0 ? (
        <p className="hint">No Contracts loaded.</p>
      ) : (
        <table className="investigations fleet">
          <thead>
            <tr>
              <th>Contract</th>
              <th>Kill switch</th>
              <th>Agenda</th>
              <th>Waste</th>
              <th>Requests</th>
              <th>Idle GPUs</th>
              <th>Unrequested GPUs</th>
              <th>Relays and Actuators</th>
            </tr>
          </thead>
          <tbody>
            {fleet.rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <a href={`/app/?contract=${encodeURIComponent(r.id)}`}>{r.name}</a>
                </td>
                {r.error ? (
                  <td colSpan={7} className="bad">
                    Couldn't read it: {r.error}
                  </td>
                ) : (
                  <>
                    <td className={r.kill_switch ? "bad" : ""}>{r.kill_switch ? "on" : "off"}</td>
                    <td>{counts(r.agenda)}</td>
                    <td>{r.currency && r.cost ? money(r.cost.waste, r.currency) : "no prices"}</td>
                    <td>{r.currency && r.cost ? money(r.cost.requests, r.currency) : "no prices"}</td>
                    <td>{r.idle_gpus ? r.idle_gpus.text : "-"}</td>
                    <td>{unrequested(r.unrequested_gpus, r.currency)}</td>
                    <td>
                      {(r.parts ?? []).length === 0
                        ? "-"
                        : (r.parts ?? []).map((p) => (
                            <div key={`${p.cluster}/${p.part}`} className={p.ok ? "" : "bad"}>
                              {p.cluster}: {p.text}
                            </div>
                          ))}
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
          {fleet.totals.length > 0 && (
            <tfoot>
              {fleet.totals.map((t) => (
                <tr key={t.currency}>
                  <th>
                    Total, {t.currency} ({t.contracts} Contract{t.contracts === 1 ? "" : "s"})
                  </th>
                  <td />
                  <td />
                  <td>{money(t.waste, t.currency)}</td>
                  <td>{money(t.requests, t.currency)}</td>
                  <td>{money(t.idle_gpus, t.currency)}</td>
                  <td>{money(t.unrequested_gpus, t.currency)}</td>
                  <td />
                </tr>
              ))}
            </tfoot>
          )}
        </table>
      )}
    </>
  );
}
