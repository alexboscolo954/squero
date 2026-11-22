// Every Contract at a glance (slice 11.7a): where it runs, where it talks, which model,
// and this month's model spend against its budget. The page pattern (slice 21.6f): figures on
// top, the table in a card, a meter under a spend that has a budget.
import { Coins, ScrollText } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "./api";
import { PageTools } from "./Shell";
import { Card, Figures, Meter, Skeleton, Tile } from "./ui/card";

const usd = (v: number) => `$${v >= 1 ? v.toFixed(2) : v.toFixed(4)}`;

export function ContractsPage() {
  const [rows, setRows] = useState<api.ContractOverview[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.overview().then(setRows, (e: Error) => setError(e.message));
  }, []);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!rows) return <Skeleton />;
  const budgeted = rows.filter((c) => c.budget_usd != null).length;
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>
          {rows.length} Contract{rows.length === 1 ? "" : "s"}
        </span>
      </PageTools>
      {rows.length > 0 && (
        <Figures>
          <Tile icon={ScrollText} label="Contracts" value={String(rows.length)} note={budgeted + " with a model budget"} />
          {/* The model's spend, in USD whatever a Contract's prices: never added to its costs. */}
          <Tile
            icon={Coins}
            label="Model spend this month"
            value={usd(rows.reduce((t, c) => t + c.spent_usd, 0))}
            note="all Contracts, in USD"
          />
        </Figures>
      )}
      <Card className="p-0">
      {rows.length === 0 ? (
        <p className="hint m-0 p-4">No Contracts loaded.</p>
      ) : (
        <div className="overflow-x-auto">
        <table className="investigations rounded-none border-0">
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
                  {/* $0 of a budget draws an empty meter; no budget, none (a budget is above 0) */}
                  {c.budget_usd != null && (
                    <Meter share={c.spent_usd / c.budget_usd} className="bar mt-1 h-1 max-w-28 bg-line" />
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
