// Checks and Health (slice 11.6b): a Contract's scheduled Checks and Deploy verifications,
// and its integrations checked now through its own tools. Errors and config are client
// and Hub text: shown as text. The page pattern (slice 21.6f): figures on top, each table in a
// card, verdicts as badges.
import { CircleAlert, CircleCheck, Coins, HeartPulse, ListChecks, Search } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "./api";
import { PageTools } from "./Shell";
import { Badge } from "./ui/badge";
import { Card, Figures, Skeleton, Tile } from "./ui/card";
import { cn } from "./ui/cn";
import { when } from "./format";
import { page } from "./Investigations";

function verdict(passed: boolean | null | undefined, status: string | null | undefined): string {
  if (passed) return "passed";
  if (passed === false) return "failed";
  return status ? status.replace("_", " ") : "-";
}

/** A verdict's badge, the link inside it: passed green, failed red, any other word neutral. */
function Verdict({ id, word }: { id: string; word: string }) {
  return (
    <Badge tone={word === "passed" ? "good" : word === "failed" ? "bad" : "muted"}>
      <a className="text-inherit" href={page(id)}>
        {word}
      </a>
    </Badge>
  );
}

/** Load once per Contract: the Hub's answer, or why it didn't answer. */
export function useLoaded<T>(load: (contract: string) => Promise<T>, contract: string): [T | null, string] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setData(null);
    setError("");
    load(contract).then(setData, (e: Error) => setError(e.message));
  }, [load, contract]);
  return [data, error];
}

export function ChecksPage({ contract }: { contract: string }) {
  const [data, error] = useLoaded(api.checks, contract);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const s = (n: number) => (n === 1 ? "" : "s");
  // A Deploy verification's own word, as its row shows it: only one that ran has a verdict.
  const ran = data.deploys.filter((d) => d.investigation_id).map((d) => verdict(d.passed, d.run_status));
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>
          {data.checks.length} Check{s(data.checks.length)}
        </span>
        <span>
          {data.deploys.length} Deploy verification{s(data.deploys.length)}
        </span>
      </PageTools>
      {(data.checks.length > 0 || data.deploys.length > 0) && (
        <Figures>
          <Tile icon={ListChecks} label="Checks" value={String(data.checks.length)} note="scheduled" />
          <Tile
            icon={CircleAlert}
            label="Failing now"
            value={String(data.checks.filter((k) => k.failing_id).length)}
            note="Checks with an Open failure"
          />
          <Tile
            icon={CircleCheck}
            label="Deploy verifications passed"
            value={String(ran.filter((w) => w === "passed").length)}
            note={"of the latest " + data.deploys.length}
          />
          <Tile icon={CircleAlert} label="Deploy verifications failed" value={String(ran.filter((w) => w === "failed").length)} />
        </Figures>
      )}
      <Card className="p-0">
      <h2 className="m-0 px-4 pt-4 pb-3 text-[15px]">Checks</h2>
      {data.checks.length === 0 ? (
        <p className="hint m-0 px-4 pb-4">No Checks scheduled.</p>
      ) : (
        <div className="overflow-x-auto">
        <table className="investigations rounded-none border-x-0 border-b-0">
          <thead>
            <tr>
              <th>Check</th>
              <th>Schedule</th>
              <th>Next</th>
              <th>Last Run</th>
              <th>Open failure</th>
              <th>Last skipped</th>
            </tr>
          </thead>
          <tbody>
            {data.checks.map((k) => (
              <tr key={k.name}>
                <td>{k.name}</td>
                <td>
                  <code>{k.schedule}</code>
                </td>
                <td className="hint">{when(k.next_due)}</td>
                <td>
                  {k.last_id ? (
                    <>
                      <Verdict id={k.last_id} word={verdict(k.last_passed, k.last_status)} />{" "}
                      <span className="hint">{when(k.last_started)}</span>
                    </>
                  ) : (
                    "-"
                  )}
                </td>
                <td>
                  {k.failing_id ? (
                    <a className="bad" href={page(k.failing_id)}>
                      failing since this Run
                    </a>
                  ) : (
                    "-"
                  )}
                </td>
                <td>{k.skipped_at ? `${when(k.skipped_at)}: ${k.skipped_reason ?? ""}` : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      </Card>
      <Card className="p-0">
      <h2 className="m-0 px-4 pt-4 pb-3 text-[15px]">Deploy verifications</h2>
      {data.deploys.length === 0 ? (
        <p className="hint m-0 px-4 pb-4">
          None yet. A Contract opts in with <code>deploy_checks:</code>.
        </p>
      ) : (
        <div className="overflow-x-auto">
        <table className="investigations rounded-none border-x-0 border-b-0">
          <thead>
            <tr>
              <th>Where</th>
              <th>Changes</th>
              <th>Due</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {data.deploys.map((d, n) => (
              <tr key={n}>
                <td>
                  {d.cluster} / {d.namespace}
                </td>
                <td>{d.changes.map((x) => `${x.object} ${x.revision}`).join(", ")}</td>
                <td className="hint">{when(d.due)}</td>
                <td>
                  {d.investigation_id ? (
                    <Verdict id={d.investigation_id} word={verdict(d.passed, d.run_status)} />
                  ) : d.state === "skipped" ? (
                    `skipped: ${d.skipped_reason ?? ""}`
                  ) : (
                    d.state
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      </Card>
    </div>
  );
}

export function HealthPage({ contract }: { contract: string }) {
  const [data, error] = useLoaded(api.health, contract);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <p className="hint">Checking each integration now…</p>;
  const spent = data.spent_usd >= 1 ? data.spent_usd.toFixed(2) : data.spent_usd.toFixed(4);
  const ok = data.integrations.filter((p) => p.ok).length;
  const all = data.integrations.length;
  // No budget draws no meter (a Contract's budget is above 0); $0 spent of one draws an empty meter.
  const budget = data.budget_usd ?? null;
  const against =
    data.budget_usd == null
      ? "no budget set"
      : "of $" + data.budget_usd.toFixed(2) + (budget ? " (" + Math.round((100 * data.spent_usd) / budget) + "%)" : "");
  return (
    <>
      <PageTools>
        <span>{all} checked</span>
      </PageTools>
      <Figures className="mb-4">
        {/* The Hub's list holds integrations, Relays and Actuators: "ok", not "integrations ok". */}
        <Tile
          icon={HeartPulse}
          label="Ok now"
          value={ok + " of " + all}
          bar={all ? ok / all : undefined}
          note="integrations, Relays and Actuators"
        />
        <Tile
          icon={Coins}
          label="Model spend this month"
          value={"$" + spent}
          bar={budget ? data.spent_usd / budget : undefined}
          note={against}
        />
        <Tile
          icon={Search}
          label="Last successful Investigation"
          value={data.last ? when(data.last.started) : "none yet"}
          note={data.last ? <a href={page(data.last.id)}>{data.last.title}</a> : undefined}
        />
      </Figures>
      <p className="hint">Each check is one read through the Contract's own tools, run now.</p>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
        {data.integrations.map((p) => (
          <Card key={p.name} className={cn("flex flex-col gap-2 p-4", p.ok ? "" : "border-bad")}>
            <div className="flex items-center gap-2">
              <h2 className="m-0 mr-auto text-sm font-semibold">{p.name}</h2>
              <span className={p.ok ? "rounded-full bg-ok-bg px-2 py-0.5 text-xs font-medium text-ok" : "bad"}>
                {p.ok ? "ok" : "FAILING"}
              </span>
            </div>
            <p className="hint nowrap m-0">{p.ms} ms</p>
            {p.detail && <p className="text m-0 text-sm">{p.detail}</p>}
          </Card>
        ))}
      </div>
      <section className="panel">
        <h2>This Contract</h2>
        <details>
          <summary>Config</summary>
          <pre>{data.config}</pre>
          <p>
            Secrets (names only):{" "}
            {Object.keys(data.secrets).length === 0
              ? "none"
              : Object.entries(data.secrets)
                  .map(([name, ok]) => `${name} ${ok ? "loaded" : "MISSING"}`)
                  .join(", ")}
          </p>
        </details>
      </section>
      <section className="panel">
        <h2>Hub settings</h2>
        <details>
          <summary>Effective settings (secrets masked)</summary>
          <pre>{data.hub}</pre>
        </details>
      </section>
    </>
  );
}
