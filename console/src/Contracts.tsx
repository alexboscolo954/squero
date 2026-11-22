// Every Contract at a glance (slice 11.7a): where it runs, where it talks, which model,
// and this month's model spend against its budget.
import { useEffect, useState } from "react";
import * as api from "./api";
import { Skeleton } from "./ui/card";

const usd = (v: number) => `$${v >= 1 ? v.toFixed(2) : v.toFixed(4)}`;

export function ContractsPage() {
  const [rows, setRows] = useState<api.ContractOverview[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.overview().then(setRows, (e: Error) => setError(e.message));
  }, []);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!rows) return <Skeleton />;
  return (
    <>
      {rows.length === 0 ? (
        <p className="hint">No Contracts loaded.</p>
      ) : (
        <div className="overflow-x-auto">
        <table className="investigations">
          <thead>
            <tr>
              <th>Id</th>
              <th>Name</th>
              <th>Clusters</th>
              <th>Operator channel</th>
              <th>Model</th>
              <th>Spent this month</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id}>
                <td>
                  <a href={`/app/?contract=${encodeURIComponent(c.id)}`}>
                    <code>{c.id}</code>
                  </a>
                </td>
                <td>{c.name}</td>
                <td>{c.clusters.join(", ") || "-"}</td>
                <td>{c.operator_channel ?? "-"}</td>
                <td>
                  <code>{c.model ?? "Hub default"}</code>
                </td>
                <td className="num">
                  {usd(c.spent_usd)}
                  {c.budget_usd != null &&
                    ` of $${c.budget_usd.toFixed(2)} (${Math.round((100 * c.spent_usd) / c.budget_usd)}%)`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </>
  );
}
