// Investigations (slice 11.5a): a Contract's list, one Investigation in full, and the
// approver token page. Reports, Answers and tool output are model and client text: each
// field is rendered as text, line breaks kept, never as markup, and an Evidence link
// stays text too. Deciding, Report feedback and Jira filing (slice 11.5b), ServiceNow filing (19.5b), and asking
// or following up a Question (slice 11.7a), post to the API and show the Investigation as
// it is after. The page pattern (slice 21.6e): figures and filter chips over the list, and a
// header card with figures on an Investigation's own page.
import { CircleAlert, Coins, EyeOff, Hand, Hash, Loader, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "./api";
import { useParam } from "./params";
import { PageTools } from "./Shell";
import { Card, Figures, Skeleton, Tile } from "./ui/card";
import { Badge } from "./ui/badge";
import { Chips } from "./ui/chips";
import { when } from "./format";

export const page = (id: string) => `/app/?view=investigation&id=${encodeURIComponent(id)}`;

/** An Investigation's one status, for its badge, the figures and the chips: a failed Check and a
 * failed Run are both "failed". */
export function state(t: { status: string; failed: boolean }): string {
  return t.failed ? "failed" : t.status.replace("_", " ");
}

/** Its badge's colour: failed red, done green, the rest (and a status not known here) neutral. */
export const tone = (t: { status: string; failed: boolean }) =>
  state(t) === "failed" ? "bad" : t.status === "done" ? "good" : "muted";

const KINDS = [["", "All kinds"], ["triage", "Triages"], ["question", "Questions"], ["check", "Checks"]] as const;
// The badges' own words, capitalised: one vocabulary for a status on the page.
const STATES = [
  ["", "Any status"],
  ["awaiting approval", "Awaiting approval"],
  ["running", "Running"],
  ["failed", "Failed"],
  ["done", "Done"],
] as const;

/** Ask a Question about this Contract; its page opens while it is answered. */
function AskForm({ contract }: { contract: string }) {
  const [question, setQuestion] = useState("");
  const [failed, setFailed] = useState("");
  return (
    <section className="panel m-0">
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
  const [kind, setKind] = useParam("kind");
  const [status, setStatus] = useParam("status");
  useEffect(() => {
    setRows(null);
    setError("");
    api.investigations(contract).then(setRows, (e: Error) => setError(e.message));
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!rows) return <Skeleton />;
  const count = (s: string) => String(rows.filter((t) => state(t) === s).length);
  // The Hub lists its latest 100: say "latest" only when the list is that long.
  const listed = rows.length >= 100 ? `the latest ${rows.length}` : `${rows.length} listed`;
  const shown = rows.filter((t) => (!kind || t.kind === kind) && (!status || state(t) === status));
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>{listed}</span>
      </PageTools>
      {rows.length > 0 && (
        <Figures>
          <Tile icon={Hand} label="Awaiting approval" value={count("awaiting approval")} note={`of ${listed}`} />
          <Tile icon={Loader} label="Running" value={count("running")} />
          <Tile icon={CircleAlert} label="Failed" value={count("failed")} note="a failed Run or a failed Check" />
        </Figures>
      )}
      <AskForm contract={contract} />
      <Card className="flex flex-col gap-3 p-0">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-4 pt-4">
          <h2 className="m-0 mr-auto text-[15px]">Investigations</h2>
          {rows.length > 0 && <Chips label="Kind" value={kind} onChange={setKind} options={KINDS} />}
          {rows.length > 0 && <Chips label="Status" value={status} onChange={setStatus} options={STATES} />}
        </div>
        {rows.length === 0 ? (
          <p className="m-0 px-4">None yet.</p>
        ) : shown.length === 0 ? (
          <p className="hint m-0 px-4">No Investigation matches.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="investigations rounded-none border-x-0 border-b-0">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Kind</th>
                  <th>Status</th>
                  <th>Title</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <tr key={t.id}>
                    <td className="hint nowrap">{when(t.started)}</td>
                    <td>{t.kind}</td>
                    <td>
                      <Badge tone={tone(t)}>{state(t)}</Badge>
                    </td>
                    <td>
                      <a href={page(t.id)}>{t.title}</a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="hint m-0 px-4 pb-4">Newest first. Passing Checks are left out.</p>
      </Card>
    </div>
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
  if (!t) return <Skeleton />;
  const act = (change: Promise<api.InvestigationPage>) => {
    setFailed("");
    change.then(setT, (e: Error) => setFailed(e.message));
  };
  const masked = Object.entries(t.masked);
  const broke = t.tool_calls.filter((c) => c.is_error).length;
  const figures = (
    <Figures className="my-4">
      <Tile icon={Hash} label="Tokens" value={`${t.usage.input_tokens.toLocaleString("en-US")} in`} note={`${t.usage.output_tokens.toLocaleString("en-US")} out`} />
      <Tile icon={Coins} label="Cost" value={t.usage.cost_usd == null ? "-" : `$${t.usage.cost_usd.toFixed(4)}`} />
      <Tile icon={Wrench} label="Tool calls" value={String(t.tool_calls.length)} note={broke ? `${broke} failed` : undefined} />
      {masked.length > 0 && (
        <Tile
          icon={EyeOff}
          label="Masked for the LLM"
          value={masked.map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`).join(", ")}
        />
      )}
    </Figures>
  );
  return (
    <>
      {/* This page's own title row: the Shell draws none here (slice 21.6e). */}
      <Card className="flex flex-col gap-2">
        <h1 className="m-0">{t.title}</h1>
        <p className="meta m-0 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Badge tone={tone(t)}>{state(t)}</Badge>
          <span>{t.kind}</span>
          {/* its own Contract, never the one picked in the switcher */}
          <a href={`/app/?contract=${encodeURIComponent(t.contract)}&view=investigations`}>{t.contract}</a>
          <span>started {when(t.started)}</span>
          <code>{t.id}</code>
        </p>
      </Card>
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
      {/* Under the decision, so what waits for you stays first on the page. */}
      {figures}
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
      {(t.jira || t.servicenow || t.scenario) && (
        <section className="panel">
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
      {t.servicenow && (
        <p>
          ServiceNow:{" "}
          {t.servicenow.incident && t.servicenow.url ? (
            <>
              <a href={t.servicenow.url}>{t.servicenow.incident}</a>, filed by {t.servicenow.by}.
            </>
          ) : t.servicenow.since ? (
            `being filed by ${t.servicenow.by} since ${when(t.servicenow.since)}, or ServiceNow didn't answer: check the assignment group's incidents before filing it by hand.`
          ) : (
            <>
              <button onClick={() => act(api.fileServiceNow(t.id))}>Create ServiceNow incident</button>{" "}
              <span className="hint">The full Report, with Evidence, goes to the Contract's ServiceNow assignment group.</span>
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
        </section>
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
        <section className="panel">
          <h2>Related alert groups</h2>
          <p className="hint">They joined this Triage instead of starting their own.</p>
          <div className="overflow-x-auto">
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
          </div>
        </section>
      )}
      <section className="panel">
      <h2>Decisions</h2>
      {t.decisions.length === 0 ? (
        <p className="hint m-0">None.</p>
      ) : (
        <div className="overflow-x-auto">
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
                <td>
                  <Badge tone={d.approve ? "good" : "muted"}>{d.approve ? "approved" : "rejected"}</Badge>
                </td>
                <td>{d.actions.join(", ")}</td>
                <td>{d.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      </section>
      <section className="panel">
      <h2>Tool calls</h2>
      {t.tool_calls.length === 0 ? (
        <p className="hint m-0">None.</p>
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
      </section>
    </>
  );
}

/** The approver token, once per browser: it earns the cookie every API call needs. */
export function TokenPage() {
  const [token, setToken] = useState("");
  const [failed, setFailed] = useState("");
  return (
    <Card className="max-w-2xl">
      <p className="mt-0">
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
    </Card>
  );
}
