// Checks and Health (slice 11.6b): a Contract's scheduled Checks and Deploy verifications,
// and its integrations checked now through its own tools. Errors and config are client
// and Hub text: shown as text.
import { useEffect, useState } from "react";
import * as api from "./api";
import { when } from "./format";
import { page } from "./Investigations";

function verdict(passed: boolean | null | undefined, status: string | null | undefined): string {
  if (passed) return "passed";
  if (passed === false) return "failed";
  return status ? status.replace("_", " ") : "-";
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
  if (!data) return null;
  return (
    <>
      <h1>{contract}: Checks</h1>
      {data.checks.length === 0 ? (
        <p className="hint">No Checks scheduled.</p>
      ) : (
        <table className="investigations">
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
                      <a href={page(k.last_id)}>{verdict(k.last_passed, k.last_status)}</a>{" "}
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
      )}
      <h2>Deploy verifications</h2>
      {data.deploys.length === 0 ? (
        <p className="hint">
          None yet. A Contract opts in with <code>deploy_checks:</code>.
        </p>
      ) : (
        <table className="investigations">
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
                    <a href={page(d.investigation_id)}>{verdict(d.passed, d.run_status)}</a>
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
      )}
    </>
  );
}

export function HealthPage({ contract }: { contract: string }) {
  const [data, error] = useLoaded(api.health, contract);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <p className="hint">Checking each integration now…</p>;
  const spent = data.spent_usd >= 1 ? data.spent_usd.toFixed(2) : data.spent_usd.toFixed(4);
  return (
    <>
      <h1>{contract}: Health</h1>
      <p className="hint">Each check is one read through the Contract's own tools, run now.</p>
      <table className="investigations">
        <thead>
          <tr>
            <th>Integration</th>
            <th>Status</th>
            <th>Time</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {data.integrations.map((p) => (
            <tr key={p.name}>
              <td>{p.name}</td>
              <td className={p.ok ? undefined : "bad"}>{p.ok ? "ok" : "FAILING"}</td>
              <td className="hint nowrap">{p.ms} ms</td>
              <td className="text">{p.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <section className="panel">
        <p>
          Last successful Investigation:{" "}
          {data.last ? (
            <>
              <a href={page(data.last.id)}>{data.last.title}</a>, {when(data.last.started)}
            </>
          ) : (
            "none yet"
          )}
        </p>
        <p>
          Model spend this month: ${spent}
          {data.budget_usd != null &&
            ` of $${data.budget_usd.toFixed(2)} (${Math.round((100 * data.spent_usd) / data.budget_usd)}%)`}
        </p>
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
      <h2>Hub settings</h2>
      <section className="panel">
        <details>
          <summary>Effective settings (secrets masked)</summary>
          <pre>{data.hub}</pre>
        </details>
      </section>
    </>
  );
}
