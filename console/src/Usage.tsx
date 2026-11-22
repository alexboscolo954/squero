// CPU and memory over 14 days (slice 21.2, on the Cost page since 21.6b): the ready containers'
// requests, Sizings and usage times their pods, from the Hub's stored hourly totals. It reads them
// itself, so a failing read leaves the rest of the page standing, and it sits beside the page's
// own content, so regrouping or comparing the cost never redraws it.
import { useEffect, useState } from "react";
import * as api from "./api";
import TotalsChart from "./TotalsChart";
import { Card, CardTitle, Empty } from "./ui/card";

export function UsageCharts({ contract }: { contract: string }) {
  const [totals, setTotals] = useState<api.Totals | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setTotals(null);
    setFailed(false);
    api.totals(contract).then(setTotals, () => setFailed(true));
  }, [contract]);
  const health = <a href={`/app/?contract=${encodeURIComponent(contract)}&view=health`}>Check Health</a>;
  if (failed)
    return (
      <Empty what="Couldn't read the hourly totals." why="The charts need the Hub's stored runs." action={health} />
    );
  if (!totals) return null;
  return (
    <Card className="usage">
      <div className="grid gap-6 md:grid-cols-2">
        {(["cpu", "memory"] as const).map((res) => (
          <div key={res} className="min-w-0">
            <CardTitle>{res === "cpu" ? "CPU over time" : "Memory over time"}</CardTitle>
            {totals.points.length ? (
              <TotalsChart totals={totals} res={res} />
            ) : (
              <Empty
                what="No hourly run stored in the last 14 days."
                why="The charts sum the Sizings each hourly run stores; a run needs the cluster's Prometheus."
                action={health}
              />
            )}
          </div>
        ))}
      </div>
      <ul className="m-0 mt-3 flex list-none flex-wrap justify-center gap-5 p-0 text-xs text-muted" aria-label="Legend">
        <li className="flex items-center gap-2"><span className="h-0.5 w-3 bg-accent" />Sizings</li>
        <li className="flex items-center gap-2"><span className="h-0.5 w-3 bg-request" />Requests</li>
        <li className="flex items-center gap-2"><span className="h-2 w-3 bg-muted/20" />Waste, between them</li>
        <li className="flex items-center gap-2"><span className="h-2 w-3 bg-ink/20" />Usage (the Sizings' basis)</li>
      </ul>
    </Card>
  );
}
