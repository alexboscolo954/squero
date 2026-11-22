// Investigations (slice 11.5a): a Contract's list, one Investigation in full, and the
// approver token page. Reports, Answers and tool output are model and client text: each
// field is rendered as text, line breaks kept, never as markup, and an Evidence link
// stays text too. Deciding, Report feedback and Jira filing (slice 11.5b), and asking
// or following up a Question (slice 11.7a), post to the API and show the Investigation as
// it is after.
import { useEffect, useState } from "react";
import * as api from "./api";
import { Skeleton } from "./ui/card";
import { Badge } from "./ui/badge";
import { when } from "./format";

export const page = (id: string) => `/app/?view=investigation&id=${encodeURIComponent(id)}`;

function state(t: { status: string; failed: boolean }): string {
  return t.failed ? "failed" : t.status.replace("_", " ");
}

/** Ask a Question about this Contract; its page opens while it is answered. */
function AskForm({ contract }: { contract: string }) {
  const [question, setQuestion] = useState("");
  const [failed, setFailed] = useState("");
  return (
    <section className="panel">
      <h2>Ask a Question</h2>
      <p>
        <textarea
          rows={3}
          cols={80}
          maxLength={4000}
          aria-label="Question"
          placeholder="which pods in namespace demo are unhealthy?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
      </p>
      <button
        className="primary"
        onClick={() => {
          setFailed("");
          api.ask(contract, question).then(
            (t) => location.assign(page(t.id)),
            (e: Error) => setFailed(e.message),
          );
        }}
      >
        Ask
      </button>{" "}
      <span className="hint">It only reads, and its Answer opens here.</span>
      {failed && <p className="notice">Couldn't: {failed}</p>}
    </section>
  );
}

export function InvestigationList({ contract }: { contract: string }) {
  const [rows, setRows] = useState<api.Brief[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setRows(null);
    setError("");
    api.investigations(contract).then(setRows, (e: Error) => setError(e.message));
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!rows) return <Skeleton />;
  return (
    <>
      <h1 className="sr-only">{contract}: Investigations</h1>
      <AskForm contract={contract} />
      <p className="hint">Newest first. Passing Checks are left out.</p>
      {rows.length === 0 ? (
        <p>None yet.</p>
      ) : (
        <table className="investigations">
          <thead>
            <tr>
              <th>Started</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Title</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td className="hint">{when(t.started)}</td>
                <td>{t.kind}</td>
                <td>
                  <Badge tone={t.failed ? "bad" : t.status === "done" ? "good" : "muted"}>{state(t)}</Badge>
                </td>
                <td>
                  <a href={page(t.id)}>{t.title}</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function EvidenceList({ items }: { items: api.Evidence[] }) {
  return (
    <ul className="evidence">
      {items.map((e, n) => (
        <li key={n}>
          <code>{e.source}</code>: {e.finding}
          {e.link ? ` (${e.link})` : ""}
        </li>
      ))}
    </ul>
  );
}

function Report({ r, executed }: { r: api.TriageReport; executed: string[] }) {
  return (
    <section className="panel">
      <h2>Report</h2>
      <p className="text">
        <strong>{r.summary}</strong>
      </p>
      <p className="text">
        Root cause ({r.confidence} confidence, {r.category}): {r.probable_root_cause}
      </p>
      <p>
        Severity: {r.severity} · Affected: {r.affected.join(", ") || "-"}
      </p>
      <h3>Evidence</h3>
      <EvidenceList items={r.evidence} />
      <h3>Recommendations</h3>
      <ol>
        {r.recommended_actions.map((a, n) => (
          <li key={n} className="text">{a}</li>
        ))}
      </ol>
      {executed.length > 0 && (
        <>
          <h3>Executed actions</h3>
          <ul>
            {executed.map((a, n) => (
              <li key={n}>
                <code>{a}</code>
              </li>
            ))}
          </ul>
        </>
      )}
      {r.runbook && <p>Runbook: {r.runbook}</p>}
    </section>
  );
}

export function InvestigationPage({ id }: { id: string }) {
  const [t, setT] = useState<api.InvestigationPage | null>(null);
  const [error, setError] = useState("");
  const [failed, setFailed] = useState("");
  const [reason, setReason] = useState("");
  const [correction, setCorrection] = useState("");
  const [more, setMore] = useState("");
  useEffect(() => {
    setT(null);
    setError("");
    api.investigation(id).then(setT, (e: Error) => setError(e.message));
  }, [id]);
  useEffect(() => {
    if (t?.status !== "running") return;
    const timer = setTimeout(() => api.investigation(id).then(setT, () => {}), 5000); // as the Jinja page reloads
    return () => clearTimeout(timer);
  }, [id, t]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!t) return null;
  const act = (change: Promise<api.InvestigationPage>) => {
    setFailed("");
    change.then(setT, (e: Error) => setFailed(e.message));
  };
  const masked = Object.entries(t.masked);
  return (
    <>
      <h1>{t.title}</h1>
      <p className="meta">
        {state(t)} · {t.contract} · {t.kind} · started {when(t.started)} · <code>{t.id}</code>
        <br />
        Tokens: {t.usage.input_tokens} in, {t.usage.output_tokens} out · cost:{" "}
        {t.usage.cost_usd == null ? "-" : `$${t.usage.cost_usd.toFixed(4)}`}
        {masked.length > 0 &&
          ` · masked for the LLM: ${masked.map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`).join(", ")}`}
      </p>
      {t.error && <p className="notice">Failed: {t.error}</p>}
      {t.status === "running" && <p className="hint">Working on it: this page updates every 5 seconds.</p>}
      {failed && <p className="notice">Couldn't: {failed}</p>}
      {t.pending.length > 0 && t.deadline != null && (
        <section className="panel waiting">
          <h2>Waiting for your decision</h2>
          <p>Goes Stale at {when(t.deadline)}. Approving runs every one of these on the Contract's systems.</p>
          {t.pending.map((p, n) => (
            <pre key={n}>{p}</pre>
          ))}
          <p>
            <button className="primary" onClick={() => act(api.decide(t.id, t.deadline!, true, ""))}>Approve</button>
          </p>
          <p>
            <label>
              Reason (the agent reads it){" "}
              <input value={reason} onChange={(e) => setReason(e.target.value)} size={50} />
            </label>{" "}
            <button className="danger" onClick={() => act(api.decide(t.id, t.deadline!, false, reason))}>Reject</button>
          </p>
        </section>
      )}
      {t.check_result && (
        <section className="panel">
          <h2>Check</h2>
          <p className="text">
            <strong>{t.check_result.passed ? "Passed" : "Failed"}:</strong> {t.check_result.summary}
          </p>
          <h3>Evidence</h3>
          <EvidenceList items={t.check_result.evidence} />
        </section>
      )}
      {t.report && <Report r={t.report} executed={t.executed_actions} />}
      {t.exchange.map((x, n) => (
        <section key={n} className="panel">
          <p className="hint text">
            Asked {when(x.at)}: {x.question}
          </p>
          {x.answer ? (
            <>
              <p className="text">{x.answer.answer}</p>
              <h3>Evidence</h3>
              <EvidenceList items={x.answer.evidence} />
            </>
          ) : (
            <p className="hint">No Answer: that Run failed.</p>
          )}
        </section>
      ))}
      {t.exchange.length > 0 && t.asked && (
        <p className="text">
          <strong>Follow-up:</strong> {t.asked}
        </p>
      )}
      {t.answer && (
        <section className="panel">
          <h2>Answer</h2>
          <p className="text">{t.answer.answer}</p>
          <h3>Evidence</h3>
          <EvidenceList items={t.answer.evidence} />
        </section>
      )}
      {t.can_follow_up && (
        <section className="panel">
          <p>
            <textarea
              rows={2}
              cols={80}
              maxLength={4000}
              aria-label="Follow-up"
              placeholder="A follow-up"
              value={more}
              onChange={(e) => setMore(e.target.value)}
            />
          </p>
          <button className="primary" onClick={() => act(api.followUp(t.id, more).then((x) => (setMore(""), x)))}>Follow up</button>{" "}
          <span className="hint">The agent keeps this Question's earlier exchange.</span>
        </section>
      )}
      {t.jira && (
        <p>
          Jira:{" "}
          {t.jira.issue && t.jira.url ? (
            <>
              <a href={t.jira.url}>{t.jira.issue}</a>, filed by {t.jira.by}.
            </>
          ) : t.jira.since ? (
            `being filed by ${t.jira.by} since ${when(t.jira.since)}, or Jira didn't answer: check project ${t.jira.project} before filing it by hand.`
          ) : (
            <>
              <button onClick={() => act(api.fileJira(t.id))}>Create Jira issue</button>{" "}
              <span className="hint">The full Report, with Evidence, goes to Jira project {t.jira.project}.</span>
            </>
          )}
        </p>
      )}
      {t.scenario && (
        <p>
          <a href={`/api/v1/investigations/${encodeURIComponent(t.id)}/scenario.yaml`} download>
            Save as Scenario
          </a>{" "}
          <span className="hint">a draft to edit and commit to the contracts repo.</span>
        </p>
      )}
      {(t.can_judge || t.feedback.length > 0 || t.taught.length > 0) && (
        <section className="panel">
          <h2>Was this Report right?</h2>
          {t.can_judge && (
            <>
              <p>
                <button onClick={() => act(api.judge(t.id, true, ""))}>Right</button>
              </p>
              <p>
                <textarea
                  rows={3}
                  maxLength={2000}
                  placeholder="What was wrong?"
                  value={correction}
                  onChange={(e) => setCorrection(e.target.value)}
                />
                <br />
                <button onClick={() => act(api.judge(t.id, false, correction))}>Wrong</button>{" "}
                <span className="hint">The agent drafts a Teaching from it for your review.</span>
              </p>
            </>
          )}
          {t.feedback.map((f, n) => (
            <p key={n}>
              {when(f.at)} · {f.by}: {f.correct ? "right" : "wrong"}
              {f.correction ? `: ${f.correction}` : ""}
            </p>
          ))}
          {t.taught.map((x) => (
            <p key={x.id}>
              Teaching #{x.id} ({x.status}): {x.text}
            </p>
          ))}
        </section>
      )}
      {t.related.length > 0 && (
        <>
          <h2>Related alert groups</h2>
          <p className="hint">They joined this Triage instead of starting their own.</p>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Alert group</th>
                <th>Shares</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {t.related.map((r, n) => (
                <tr key={n}>
                  <td className="hint">{when(r.at)}</td>
                  <td>{r.title}</td>
                  <td>
                    {Object.entries(r.matched)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(" ")}
                  </td>
                  <td>
                    {r.louder && <strong>higher severity </strong>}
                    {r.separate_id && <a href={page(r.separate_id)}>triaged separately</a>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h2>Decisions</h2>
      {t.decisions.length === 0 ? (
        <p className="hint">None.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>By</th>
              <th>Decision</th>
              <th>Actions</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {t.decisions.map((d, n) => (
              <tr key={n}>
                <td className="hint">{when(d.at)}</td>
                <td>{d.decided_by}</td>
                <td>{d.approve ? "approved" : "rejected"}</td>
                <td>{d.actions.join(", ")}</td>
                <td>{d.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>Tool calls</h2>
      {t.tool_calls.length === 0 ? (
        <p className="hint">None.</p>
      ) : (
        <ol className="calls">
          {t.tool_calls.map((c, n) => (
            <li key={n}>
              <code>{c.tool}</code>
              {c.is_error && " (failed)"}
              <details>
                <summary>
                  <code>{JSON.stringify(c.args)}</code>
                </summary>
                <pre>{c.result}</pre>
              </details>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

/** The approver token, once per browser: it earns the cookie every API call needs. */
export function TokenPage() {
  const [token, setToken] = useState("");
  const [failed, setFailed] = useState("");
  return (
    <>
      <h1 className="sr-only">Approver token</h1>
      <p>
        Your Tailscale identity says who you are. To see anything else here, and to approve, reject
        or ask, the console also needs <code>SQUERO_APPROVER_TOKEN</code>, once per browser: it stays as
        a cookie that proves the token without holding it.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          api.saveToken(token).then(
            () => location.assign("/app/"),
            (err: Error) => setFailed(err.message),
          );
        }}
      >
        <label>
          Approver token{" "}
          <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </label>{" "}
        <button type="submit" className="primary">Save</button>
      </form>
      {failed && <p className="notice">Couldn't: {failed}</p>}
    </>
  );
}
