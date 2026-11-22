// Teachings (slice 11.6a): a Contract's review queue, its approved and past Teachings,
// and one the Operator writes. A drafted Teaching is model text: shown as text, and
// edited as plain text before it is approved. The page pattern (slice 21.6f): figures on top,
// the approved and the past Teachings each in a card.
import { GraduationCap, Hourglass, Scissors } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "./api";
import { Card, Figures, Skeleton, Tile } from "./ui/card";
import { when } from "./format";
import { page } from "./Investigations";

const forAlerts = (t: api.Teaching) => t.alerts.join(", ") || "every alert";

function Proposed({ t, act, limit }: { t: api.Teaching; act: Act; limit: number }) {
  const [text, setText] = useState(t.text);
  return (
    <section className="panel">
      <p>
        <strong>#{t.id}</strong> for {forAlerts(t)}
        {t.source_id && (
          <>
            {" "}· from <a href={page(t.source_id)}>its Investigation</a>
          </>
        )}
        {t.contradicts != null && (
          <strong> · contradicts #{t.contradicts}: approving it retires that one</strong>
        )}
      </p>
      {t.correction && <p className="hint text">Your words: {t.correction}</p>}
      <p>
        <textarea
          rows={3}
          cols={80}
          maxLength={limit}
          aria-label={`Teaching #${t.id}`}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </p>
      <p>
        <button onClick={() => act((c) => api.decideTeaching(c, t.id, true, text))}>Approve this text</button>{" "}
        <button className="danger" onClick={() => act((c) => api.decideTeaching(c, t.id, false, ""))}>Reject</button>
      </p>
    </section>
  );
}

type Act = (change: (contract: string) => Promise<api.Teachings>) => Promise<boolean>;

export function TeachingsPage({ contract }: { contract: string }) {
  const [data, setData] = useState<api.Teachings | null>(null);
  const [error, setError] = useState("");
  const [failed, setFailed] = useState("");
  const [text, setText] = useState("");
  const [alerts, setAlerts] = useState("");
  useEffect(() => {
    setData(null);
    setError("");
    api.teachings(contract).then(setData, (e: Error) => setError(e.message));
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const act: Act = (change) => {
    setFailed("");
    return change(contract).then(
      (d) => (setData(d), true),
      (e: Error) => (setFailed(e.message), false),
    );
  };
  return (
    <>
      <Figures className="mb-4">
        <Tile icon={Hourglass} label="Waiting for review" value={String(data.proposed.length)} />
        <Tile icon={GraduationCap} label="Approved" value={String(data.approved.length)} note="used in this Contract's Investigations" />
        <Tile icon={Scissors} label="Left out of prompts" value={String(data.left_out)} note="approved, over a prompt's limit" />
      </Figures>
      <p className="hint">
        Lessons for this Contract's future Investigations. Only approved ones are used, and never by
        another Contract.
      </p>
      {failed && <p className="notice">Couldn't: {failed}</p>}
      {data.left_out > 0 && (
        <p className="notice">
          {data.left_out} approved Teaching{data.left_out === 1 ? "" : "s"} don't fit in a prompt (at most
          30 and 6,000 characters, newest first; a Triage gets those for its alert first). Retire the
          ones no longer needed.
        </p>
      )}
      <h2>Waiting for review</h2>
      {data.proposed.length === 0 ? (
        <p className="hint">None.</p>
      ) : (
        data.proposed.map((t) => <Proposed key={t.id} t={t} act={act} limit={data.limit} />)
      )}
      <Card className="mt-4 p-0">
      <h2 className="m-0 px-4 pt-4 pb-3">Approved</h2>
      {data.approved.length === 0 ? (
        <p className="hint m-0 px-4 pb-4">None.</p>
      ) : (
        <div className="overflow-x-auto">
        <table className="rounded-none border-x-0 border-b-0">
          <thead>
            <tr>
              <th>#</th>
              <th>For</th>
              <th>Teaching</th>
              <th>By</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.approved.map((t) => (
              <tr key={t.id}>
                <td>{t.id}</td>
                <td>{forAlerts(t)}</td>
                <td className="text">{t.text}</td>
                <td className="hint">
                  {t.decided_by}, {when(t.decided_at)}
                </td>
                <td>
                  <button className="danger" onClick={() => act((c) => api.retireTeaching(c, t.id))}>Retire</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      </Card>
      <section className="panel">
        <h2>Write one</h2>
        <p>
          <label>
            For alerts (comma-separated; empty: every alert){" "}
            <input value={alerts} onChange={(e) => setAlerts(e.target.value)} size={40} />
          </label>
        </p>
        <p>
          <textarea
            rows={3}
            cols={80}
            maxLength={data.limit}
            aria-label="New Teaching"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </p>
        <button
          className="primary"
          onClick={() =>
            act((c) => api.writeTeaching(c, text, alerts.split(","))).then((ok) => {
              if (ok) {
                setText("");
                setAlerts("");
              }
            })
          }
        >
          Add, approved
        </button>
      </section>
      <section className="panel">
      <h2>Rejected and retired</h2>
      {data.past.length === 0 ? (
        <p className="hint m-0">None.</p>
      ) : (
        <ul>
          {data.past.map((t) => (
            <li key={t.id} className="text">
              #{t.id} {t.status} by {t.decided_by}: {t.text}
            </li>
          ))}
        </ul>
      )}
      </section>
    </>
  );
}
