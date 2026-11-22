// The Fleet (slice 18.4): every Contract the Hub serves, a card each (slice 21.3b), read-only. Money in each
// Contract's own currency, totals per currency, never converted.
import { useEffect, useState } from "react";
import * as api from "./api";
import { money, whole } from "./format";
import { Badge } from "./ui/badge";
import { Card, Figures, Skeleton, Tile } from "./ui/card";

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
  if (!fleet) return <Skeleton />;
  return (
    <div className="fleet flex flex-col gap-5">
      <h1 className="sr-only">Fleet</h1>
      {fleet.totals.map((t) => (
        <Figures key={t.currency} className="total">
          <Tile hero label={`${t.currency}, ${t.contracts} Contract${t.contracts === 1 ? "" : "s"}: waste a month`} value={whole(t.waste, t.currency)} />
          <Tile label="Requests a month" value={whole(t.requests, t.currency)} />
          <Tile label="Idle GPUs a month" value={whole(t.idle_gpus, t.currency)} />
          <Tile label="Unrequested GPUs a month" value={whole(t.unrequested_gpus, t.currency)} />
        </Figures>
      ))}
      {fleet.rows.length === 0 ? (
        <p className="hint">No Contracts loaded.</p>
      ) : (
        <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
          {fleet.rows.map((r) => (
            <Card key={r.id} className="contract flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <a className="mr-auto text-base font-semibold" href={`/app/?contract=${encodeURIComponent(r.id)}`}>
                  {r.name}
                </a>
                {!r.error && (
                  <Badge tone={r.kill_switch ? "bad" : "muted"}>Kill switch {r.kill_switch ? "on" : "off"}</Badge>
                )}
              </div>
              {r.error ? (
                <p className="bad m-0">Couldn't read it: {r.error}</p>
              ) : (
                <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  <dt className="text-muted">Agenda</dt>
                  <dd className="m-0">{counts(r.agenda)}</dd>
                  <dt className="text-muted">Waste</dt>
                  <dd className={r.currency && r.cost ? "m-0 text-waste" : "m-0"}>{r.currency && r.cost ? money(r.cost.waste, r.currency) : "no prices"}</dd>
                  <dt className="text-muted">Requests</dt>
                  <dd className="m-0">{r.currency && r.cost ? money(r.cost.requests, r.currency) : "no prices"}</dd>
                  <dt className="text-muted">Idle GPUs</dt>
                  <dd className="m-0">{r.idle_gpus ? r.idle_gpus.text : "-"}</dd>
                  <dt className="text-muted">Unrequested GPUs</dt>
                  <dd className="m-0">{unrequested(r.unrequested_gpus, r.currency)}</dd>
                  <dt className="text-muted">Relays and Actuators</dt>
                  <dd className="m-0">
                    {(r.parts ?? []).length === 0
                      ? "-"
                      : (r.parts ?? []).map((p) => (
                          <div key={`${p.cluster}/${p.part}`} className={p.ok ? "" : "bad"}>
                            {p.cluster}: {p.text}
                          </div>
                        ))}
                  </dd>
                </dl>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
