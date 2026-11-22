// The Agenda (slice 11.4): what to look at now in a Contract, most urgent first, each item
// linking where to act. On the Workloads page in full, on the Overview its first few (21.2).
import { useEffect, useState } from "react";
import * as api from "./api";
import { when } from "./format";
import { page } from "./Investigations";

/** Where to act on an Agenda item: its Investigation, its Workload, the Cost page, or Health
 * (a part whose Protocol the Hub refused: its version and Protocol there). */
export function link(contract: string, i: api.AgendaItem): string | null {
  const here = `contract=${encodeURIComponent(contract)}`;
  if (i.id) return page(i.id);
  if (i.workload) return `/app/?${here}&workload=${encodeURIComponent(i.workload)}`;
  if (i.kind === "bill") return `/app/?${here}&view=cost`;
  if (i.kind === "protocol") return `/app/?${here}&view=health`;
  return null;
}

/** What to look at now in this Contract, most urgent first (the Hub ranks it). */
export function AgendaPanel({ contract }: { contract: string }) {
  const [agenda, setAgenda] = useState<api.Agenda | null>(null);
  useEffect(() => {
    setAgenda(null);
    api.agenda(contract).then(setAgenda, () => setAgenda(null));
  }, [contract]);
  return agenda && <AgendaList contract={contract} agenda={agenda} />;
}

/** An Agenda already read: its first `limit` items, and a link to all of them. */
export function AgendaList({ contract, agenda, limit }: { contract: string; agenda: api.Agenda; limit?: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? undefined : limit;
  if (agenda.items.length === 0) return <p className="hint">Nothing needs you now.</p>;
  return (
    <section className="agenda">
      <h2>Now</h2>
      <ol>
        {agenda.items.slice(0, shown).map((i, n) => {
          const href = link(contract, i);
          const where = i.cluster && !i.workload ? `${i.cluster}: ` : i.workload ? `${i.workload}: ` : "";
          const time = i.kind === "approval" ? ` (goes Stale ${when(i.at)})` : i.at ? ` (${when(i.at)})` : "";
          return (
            <li key={n} className={i.kind}>
              {href ? <a href={href}>{where}{i.title}</a> : `${where}${i.title}`}
              {time}
            </li>
          );
        })}
      </ol>
      {shown !== undefined && agenda.items.length > shown && (
        <button type="button" className="link" onClick={() => setAll(true)}>
          See all {agenda.items.length}
        </button>
      )}
    </section>
  );
}
