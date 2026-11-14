// The Hub's JSON API (/api/v1), same origin, with the approver cookie the browser
// already holds. Types come from the API's OpenAPI: `make console-types`.
import type { components } from "./api-types";

export type Schemas = components["schemas"];
export type Workloads = Schemas["Workloads"];
export type Workload = Schemas["Workload"];
export type Container = Schemas["Container"];
export type Point = Schemas["Point"];
export type ContractSummary = Schemas["ContractSummary"];
export type Allocation = Schemas["Allocation"];
export type Group = Schemas["Group"];
export type Change = Schemas["Change"];
export type Bills = Schemas["Bills"];
export type KillSwitch = Schemas["KillSwitch"];
export type Agenda = Schemas["Agenda"];
export type AgendaItem = Schemas["Item"];
export type Brief = Schemas["Brief"];
export type InvestigationPage = Schemas["InvestigationPage"];
export type TriageReport = Schemas["TriageReport"];
export type Evidence = Schemas["Evidence"];

/** The API answered 401: this browser hasn't pasted the approver token. */
export class NeedsToken extends Error {
  constructor(public tokenUrl: string) {
    super("paste the approver token first");
  }
}

async function get<T>(path: string, method: "GET" | "POST" | "DELETE" = "GET"): Promise<T> {
  const r = await fetch(`/api/v1${path}`, { method, credentials: "same-origin" });
  if (r.status === 401) {
    const body = await r.json().catch(() => ({}));
    throw new NeedsToken(body?.detail?.token_url ?? "/app/?view=token");
  }
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export const contracts = () => get<ContractSummary[]>("/contracts");

export const workloads = (contract: string) =>
  get<Workloads>(`/contracts/${encodeURIComponent(contract)}/workloads`);

export function cost(contract: string, by: string, compare: string) {
  const q = new URLSearchParams({ by });
  if (compare) q.set("compare", compare);
  return get<Allocation>(`/contracts/${encodeURIComponent(contract)}/cost?${q}`);
}

export const billing = (contract: string) =>
  get<Bills>(`/contracts/${encodeURIComponent(contract)}/billing`);

export const agenda = (contract: string) =>
  get<Agenda>(`/contracts/${encodeURIComponent(contract)}/agenda`);

export const investigations = (contract: string) =>
  get<Brief[]>(`/contracts/${encodeURIComponent(contract)}/investigations`);

export const investigation = (id: string) =>
  get<InvestigationPage>(`/investigations/${encodeURIComponent(id)}`);

/** A change (slice 11.5b): JSON in, the Investigation as it is now out; a refusal
 * carries the Hub's own words (Stale, the Kill switch, filed already). */
async function send<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/v1${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) throw new NeedsToken("/app/?view=token");
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({})))?.detail;
    throw new Error(typeof detail === "string" ? detail : `${r.status} ${r.statusText}`);
  }
  return (await r.json()) as T;
}

const at = (id: string) => `/investigations/${encodeURIComponent(id)}`;
export const decide = (id: string, deadline: number, approve: boolean, reason: string) =>
  send<InvestigationPage>(`${at(id)}/decision`, { deadline, approve, reason });
export const judge = (id: string, correct: boolean, correction: string) =>
  send<InvestigationPage>(`${at(id)}/feedback`, { correct, correction });
export const fileJira = (id: string) => send<InvestigationPage>(`${at(id)}/jira`);
/** A Question about this Contract (slice 11.7a): it opens running. */
export const ask = (contract: string, question: string) =>
  send<InvestigationPage>(`/contracts/${encodeURIComponent(contract)}/questions`, { question });
export const followUp = (id: string, question: string) =>
  send<InvestigationPage>(`${at(id)}/questions`, { question });

export type ContractOverview = Schemas["ContractOverview"];
export const overview = () => get<ContractOverview[]>("/overview");

export type Teachings = Schemas["Teachings"];
export type Teaching = Schemas["Teaching"];
const taught = (contract: string) => `/contracts/${encodeURIComponent(contract)}/teachings`;
export const teachings = (contract: string) => get<Teachings>(taught(contract));
export const writeTeaching = (contract: string, text: string, alerts: string[]) =>
  send<Teachings>(taught(contract), { text, alerts });
export const decideTeaching = (contract: string, id: number, approve: boolean, text: string) =>
  send<Teachings>(`${taught(contract)}/${id}/decision`, { approve, text });
export const retireTeaching = (contract: string, id: number) =>
  send<Teachings>(`${taught(contract)}/${id}/retire`);

export type Checks = Schemas["Checks"];
export type HealthPage = Schemas["HealthPage"];
export const checks = (contract: string) =>
  get<Checks>(`/contracts/${encodeURIComponent(contract)}/checks`);
export type Nodes = Schemas["Nodes"];
/** Each cluster's nodes and their Room, as the latest hourly run read them (slice 15.1a). */
export const nodes = (contract: string) =>
  get<Nodes>(`/contracts/${encodeURIComponent(contract)}/nodes`);
/** Checks the Contract's integrations now, through its own tools. */
export const health = (contract: string) =>
  get<HealthPage>(`/contracts/${encodeURIComponent(contract)}/health`);

/** Earns this browser the cookie: the token page's only call without it. */
export async function saveToken(token: string): Promise<void> {
  const r = await fetch("/api/v1/token", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (r.status === 403) throw new Error("that is not the approver token");
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
}

export function history(contract: string, c: Container, days: 14 | 400) {
  const q = new URLSearchParams({
    cluster: c.cluster,
    namespace: c.namespace,
    kind: c.kind,
    name: c.name,
    container: c.container,
    days: String(days),
  });
  return get<Schemas["History"]>(`/contracts/${encodeURIComponent(contract)}/history?${q}`);
}

export type AutomationPage = Schemas["AutomationPage"];
export type Held = Schemas["Held"];
const automated = (contract: string) => `/contracts/${encodeURIComponent(contract)}/automation`;
/** What each Actuator last did with each container (slice 12.6b). */
export const automation = (contract: string) => get<AutomationPage>(automated(contract));
/** The Operator's Revert of one container; refused under the Kill switch. */
export const revert = (contract: string, what: Schemas["RevertIn"]) =>
  send<AutomationPage>(`${automated(contract)}/revert`, what);

const switchPath = (contract: string) => `/contracts/${encodeURIComponent(contract)}/kill-switch`;
export const killSwitch = (contract: string) => get<KillSwitch>(switchPath(contract));
export const turnOnKillSwitch = (contract: string) => get<KillSwitch>(switchPath(contract), "POST");
/** The only way a Kill switch goes off. */
export const turnOffKillSwitch = (contract: string) =>
  get<KillSwitch>(switchPath(contract), "DELETE");
