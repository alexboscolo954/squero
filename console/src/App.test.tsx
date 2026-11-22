import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Point, Workloads } from "./api";
import { AgendaPanel } from "./Agenda";
import App from "./App";
import { series } from "./series";

vi.mock("./Chart", () => ({ default: () => <div>chart</div> })); // jsdom has no canvas
vi.mock("./TotalsChart", () => ({ default: () => <div>totals chart</div> }));

const GI = 2 ** 30;
const container = {
  workload: "kind/lab/Deployment/api/app",
  cluster: "kind",
  namespace: "lab",
  kind: "Deployment",
  name: "api",
  container: "app",
  policy: "production",
  state: "ready" as const,
  pods: 3,
  cpu: { action: "lower", current: 1, target: 0.115, basis: 0.1 },
  memory: { action: "lower", current: GI, target: 96 * 2 ** 20, basis: 80 * 2 ** 20 },
  waste: { cpu: 2.655, memory: 2.7 * GI },
  risks: [{ kind: "oom_killed", text: '<img src=x onerror="alert(1)">' }],
  groups: [] as NonNullable<import("./api").Container["groups"]>, // slice 15.3a: per instance type
  by_type: false as boolean, // slice 15.3c: its Actuator applies the groups
  automated: false as boolean, // slice 21.2
};

function workload(name: string, cpu: number, memory: number, risks = container.risks) {
  return {
    workload: `kind/lab/Deployment/${name}`,
    cluster: "kind",
    namespace: "lab",
    kind: "Deployment",
    name,
    pods: 1,
    waste: { cpu, memory },
    risks: risks.map((r) => ({ ...r, container: "app" })),
    containers: [{ ...container, name, workload: `kind/lab/Deployment/${name}/app`, risks }],
  };
}

const DATA: Workloads = {
  contract: "demo-shop",
  prices: [],
  clusters: { kind: { at: 1_700_000_000, error: null, since: null } },
  total: { cpu: 1.5, memory: 5 * GI },
  workloads: [workload("cpu-heavy", 1, 0, []), workload("memory-heavy", 0, 5 * GI)],
  learning: [{ ...container, name: "db", workload: "kind/lab/StatefulSet/db/app", state: "learning", history_h: 24, needs_h: 72 }],
  excluded: [],
  floors: [],
};

function answer(routes: Record<string, [number, unknown]>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      const [status, body] = routes[path] ?? [404, {}];
      return new Response(JSON.stringify(body), { status });
    }),
  );
}

beforeEach(() => history.replaceState(null, "", "/app/"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("model and Hub text renders as text, never as markup", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
    "/api/v1/contracts/demo-shop/history": [200, { points: [] }],
  });
  const { container: page } = render(<App />);
  fireEvent.click(await screen.findByText("Deployment/memory-heavy"));
  expect(await screen.findByText(/onerror="alert\(1\)"/)).toBeTruthy();
  expect(page.querySelector("img")).toBeNull();
});

test("a request its HPA divides by shows as held, with the change that frees it as text", async () => {
  const change = "# HorizontalPodAutoscaler lab/api, its cpu metric:\ntarget:\n  type: AverageValue\n  averageValue: 500m <b>";
  const held = workload("held", 1, 0, []);
  held.containers[0].cpu = { ...container.cpu, held: { hpa: "api", percent: 50, average: 0.5, change } } as typeof container.cpu;
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, workloads: [held, workload("free", 0.5, 0, [])] }],
    "/api/v1/contracts/demo-shop/history": [200, { points: [] }],
  });
  const { container: page } = render(<App />);
  fireEvent.click(await screen.findByText("Deployment/held"));
  expect(await screen.findByText(/CPU is held by its HPA api \(50% of requests\)/)).toBeTruthy();
  expect(page.querySelector("pre")?.textContent).toBe(change);
  expect(page.querySelector("pre b")).toBeNull();
  expect(screen.queryByText(/Memory is held/)).toBeNull();
});

test("a DaemonSet's container its Actuator applies by instance type says the one Sizing is not applied", async () => {
  const agent = workload("agent", 1, 0, []);
  const typed = { action: "lower", current: 0.5, target: 0.06, basis: 0.05 };
  agent.containers[0].groups = [{ type: "m5.large", pods: 2, state: "ready", history_h: 48, cpu: typed }];
  agent.containers[0].by_type = true;
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, workloads: [agent] }],
    "/api/v1/contracts/demo-shop/history": [200, { points: [] }],
  });
  const { container: page } = render(<App />);
  fireEvent.click(await screen.findByText("Deployment/agent"));
  const types = page.querySelector("details.types") as HTMLDetailsElement;
  expect(types.querySelector("summary")?.textContent).toBe(
    "By instance type (1): what its Actuator applies, each pod by its node's type; the one Sizing above is not applied",
  );
  expect(types.open).toBe(true);
});

test("a DaemonSet's container lists its Sizing per instance type under the one that is applied", async () => {
  const agent = workload("agent", 1, 0, []);
  const typed = { action: "lower", current: 0.5, target: 0.06, basis: 0.05 };
  agent.containers[0].groups = [
    { type: "", pods: 1, state: "learning", history_h: 1 },
    { type: "m5.<b>large</b>", pods: 2, state: "ready", history_h: 48, cpu: typed, memory: { ...typed, current: 256 * 2 ** 20, target: 77 * 2 ** 20 } },
  ];
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, workloads: [agent, workload("plain", 0.5, 0, [])] }],
    "/api/v1/contracts/demo-shop/history": [200, { points: [] }],
  });
  const { container: page } = render(<App />);
  fireEvent.click(await screen.findByText("Deployment/agent"));
  const types = page.querySelector("details.types");
  expect(types?.querySelector("summary")?.textContent).toBe("By instance type (2): the one Sizing above is what is applied to every pod");
  const lines = [...(types?.querySelectorAll("li") ?? [])].map((li) => li.textContent);
  expect(lines).toEqual([
    "nodes with no instance type (1 pods): learning (1 h of usage)",
    "m5.<b>large</b> (2 pods): CPU lower 500m → 60m; memory lower 256Mi → 77Mi",
  ]);
  expect(page.querySelector("details.types b")).toBeNull();
  expect((types as HTMLDetailsElement).open).toBe(false);
  fireEvent.click(screen.getByText("Deployment/plain"));
  await screen.findByText(/CPU lower 1000m/);
  expect(page.querySelector("details.types")).toBeNull(); // a container without groups shows none
});

test("autoscaled Workloads show their Floor, its saving and its risks as text", async () => {
  const where = { cluster: "kind", namespace: "lab", kind: "Deployment", hpa: "h", policy: "production", percentile: 0.1, risks: [], at: 1 };
  const floors = [
    { ...where, workload: "kind/lab/Deployment/api", name: "api", minimum: 4, maximum: 9, state: "ready", history_h: 120, needs_h: 24, floor: 2, replicas: 1.5, saving: { cpu: 0.3, memory: GI }, cost: 12.5,
      profile: { state: "ready", timezone: "Europe/Rome", lead_time: 900, history_h: 600, needs_h: 504, note: null,
        blocks: [{ since: "Mon 08:45", until: "Mon 12:00", replicas: 7, start: "45 8 * * 1", end: "0 12 * * 1" }],
        triggers: "- type: cron\n  metadata: <i>x</i>",
        backtest: { state: "within", error: 0.12, few: 0.04, many: 0.08, hours: 168, replicas: 0.4, excess: { cpu: 0.1, memory: GI }, cost: 3.1 } } },
    { ...where, workload: "kind/lab/Deployment/web", name: "web", minimum: 2, maximum: 5, state: "ready", history_h: 48, needs_h: 24, floor: 3, risks: [{ kind: "minimum_below_floor", text: "its minimum of 2 is <b>low</b>" }],
      profile: { state: "flat", timezone: "UTC", lead_time: 900, history_h: 600, needs_h: 504, blocks: [], triggers: null, note: null } },
    { ...where, workload: "kind/lab/Deployment/new", name: "new", minimum: 2, maximum: 5, state: "learning", history_h: 3, needs_h: 24 },
    { ...where, workload: "kind/lab/Deployment/queue", name: "queue", minimum: 1, maximum: 5, state: "none", why: "it scales to zero: left alone" },
    { ...where, workload: "kind/lab/Deployment/odd", name: "odd", hpa: "keda-hpa-odd", minimum: 2, maximum: 5, state: "ready", history_h: 168, needs_h: 24, floor: 2,
      profile: { state: "irregular", timezone: "UTC", lead_time: 900, history_h: 672, needs_h: 504, blocks: [], triggers: null, note: null,
        backtest: { state: "over", error: 0.34, few: 0.21, many: 0.13, hours: 168, replicas: 0.4, excess: { cpu: 0.1, memory: GI }, cost: 3.1 } } },
    { ...where, workload: "kind/lab/Deployment/young", name: "young", minimum: 2, maximum: 5, state: "ready", history_h: 168, needs_h: 24, floor: 2,
      profile: { state: "ready", timezone: "UTC", lead_time: 0, history_h: 504, needs_h: 504, triggers: null, note: null,
        blocks: [{ since: "Tue 08:00", until: "Tue 12:00", replicas: 4, start: "0 8 * * 2", end: "0 12 * * 2" }],
        backtest: { state: "waiting" } } },
    { ...where, workload: "kind/other/Deployment/far", namespace: "other", name: "far", minimum: 1, maximum: 5, state: "none", why: "elsewhere" },
  ];
  const floors_total = { workloads: 1, replicas: 1.5, saving: { cpu: 0.3, memory: GI }, cost: 12.5 };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, currency: "EUR", cost: { waste: 1, requests: 2 }, floors, floors_total }],
  });
  history.replaceState(null, "", "/app/?namespace=lab");
  const { container: page } = render(<App />);
  await screen.findByText("Autoscaled Workloads");
  const lines = [...page.querySelectorAll(".floors > ul > li")].map((li) => li.textContent);
  expect(lines).toHaveLength(6); // the other namespace's is filtered out with the Workloads
  expect(lines[0]).toContain("Deployment/api kind/lab: Floor 2; its HPA h has a minimum of 4 and a maximum of 9");
  expect(lines[0]).toContain("1.5 replicas fewer on average, 12.50 EUR/month");
  expect(lines[0]).toContain("(production: the lowest 10% of 120 h of Needs)");
  expect(lines[1]).toContain("Floor 3; its HPA h has a minimum of 2");
  expect(lines[1]).toContain("minimum_below_floor its minimum of 2 is <b>low</b>");
  expect(lines[2]).toContain("Floor learning (3 h of 24 h of Needs)");
  expect(lines[3]).toContain("no Floor: it scales to zero: left alone");
  expect(page.querySelector(".floors b")).toBeNull();
  expect(lines[0]).toContain("Weekly profile (Europe/Rome, each rise 15 min early):");
  expect(lines[0]).toContain("Mon 08:45 to Mon 12:00: 7 replicas");
  expect(page.querySelector(".floors pre")?.textContent).toBe("- type: cron\n  metadata: <i>x</i>");
  expect(page.querySelector(".floors pre i")).toBeNull();
  expect(lines[1]).toContain("No weekly pattern: its Floor holds all week.");
  expect(lines[2]).not.toContain("Weekly profile");
  // slice 14.4: the Backtest beside the profile, or in its place over the budget
  expect(lines[0]).toContain("Backtest: off by 12% last week (too few 4%, too many 8%: 3.10 EUR/month).");
  expect(lines[4]).toContain("Load isn't weekly enough: off by 34% last week (too few 21%, too many 13%); it keeps its Floor.");
  expect(lines[4]).not.toContain("Weekly profile");
  expect(lines[4]).not.toContain("EUR");
  expect(lines[5]).toContain("Tue 08:00 to Tue 12:00: 4 replicas");
  expect(lines[5]).toContain("Not backtested yet (needs four weeks).");
  expect(lines[1]).not.toContain("acktest");
  expect(page.querySelector(".floors .total")?.textContent).toContain("Floors: 1 can lower their HPA minimum, 1.5 replicas fewer on average: 12.50 EUR/month");
});

test("a Contract without an autoscaled Workload shows no Floors", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
  });
  render(<App />);
  await screen.findByText("Deployment/memory-heavy");
  expect(screen.queryByText("Autoscaled Workloads")).toBeNull();
});

test("Workloads rank by waste, a core like 4 GiB", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
  });
  render(<App />);
  await screen.findByText("Deployment/memory-heavy");
  const names = [...document.querySelectorAll("tbody button")].map((b) => b.textContent);
  expect(names.slice(0, 2)).toEqual(["Deployment/memory-heavy", "Deployment/cpu-heavy"]);
  expect(screen.getByText(/Learning: kind\/lab\/StatefulSet\/db\/app \(24 h of 72 h\)/)).toBeTruthy();
});

test("with prices, Workloads rank by the monthly cost of their waste and show it", async () => {
  // cpu-heavy wastes 1 core, memory-heavy 5 GiB: by cost, CPU's price puts it first
  const priced: Workloads = {
    ...DATA,
    currency: "EUR",
    cost: { waste: 1234.5, requests: 5000 },
    workloads: [
      { ...workload("memory-heavy", 0, 5 * GI), cost: { waste: 15.33, requests: 40 } },
      { ...workload("cpu-heavy", 1, 0, []), cost: { waste: 1219.17, requests: 4960 } },
    ],
  };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, priced],
  });
  render(<App />);
  await screen.findByText("Deployment/memory-heavy");
  const names = [...document.querySelectorAll("tbody button")].map((b) => b.textContent);
  expect(names.slice(0, 2)).toEqual(["Deployment/cpu-heavy", "Deployment/memory-heavy"]);
  expect(screen.getByText("1,234.50 EUR").previousElementSibling?.textContent).toBe("Waste a month"); // its figure
  expect(screen.getByText("1,219.17 EUR/month")).toBeTruthy(); // the row's Savings
  fireEvent.click(screen.getByText("Deployment/cpu-heavy"));
  expect(screen.getByText(/of 4,960\.00 EUR\/month in requests/)).toBeTruthy(); // its detail
  expect(screen.queryByText(/a core ranks like 4 GiB/)).toBeNull();
});

test("the Cost page lists namespaces by what their requests cost, each linking its Workloads", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      {
        contract: "demo-shop",
        by: "namespace",
        currency: "USD",
        groups: [
          { group: "kind/lab", workloads: 9, requests: { cpu: 5, memory: 8 * GI }, waste: { cpu: 2, memory: GI }, cost: { requests: 148.5, waste: 74.65 } },
          { group: "kind/demo", workloads: 1, requests: { cpu: 0.1, memory: 0 }, waste: { cpu: 0, memory: 0 }, cost: { requests: 2.31, waste: 0 } },
        ],
        groupings: ["namespace"],
        warnings: [],
        total: { requests: { cpu: 5.1, memory: 8 * GI }, waste: { cpu: 2, memory: GI }, cost: { requests: 150.81, waste: 74.65 } },
        realized: {
          currency: "USD",
          rate: { cpu: 2.655, memory: 0 },
          accrued: { cpu: 26.55, memory: 0 },
          week: { cpu: 2.655, memory: 0 },
          cost: { rate: 77.53, accrued: 1.06, week: 77.53 },
          drops: [{ workload: "kind/lab/Deployment/api/app", resource: "cpu", at: 0, before: 1, after: 0.115, sizing: 0.115, pods: 3, saved: 2.655, cost: { rate: 77.53, accrued: 1.06 } }],
        },
      },
    ],
  });
  render(<App />);
  const lab = await screen.findByRole("link", { name: "kind/lab" });
  expect(lab.getAttribute("href")).toBe("/app/?contract=demo-shop&cluster=kind&namespace=lab");
  expect(screen.getAllByText("74.65 USD/month")).toHaveLength(2); // kind/lab's and the total Savings
  expect(screen.getByText("150.81 USD/month")).toBeTruthy(); // the total of the requests
  expect(screen.queryByText("Δ requests")).toBeNull(); // nothing compared
  expect(screen.getByText("77.53 USD/month")).toBeTruthy(); // realized
  expect(screen.getByText(/1\.06 USD so far, 77\.53 USD\/month from this week's/)).toBeTruthy();
  expect(screen.getByText(/1 request lowered to its Sizing/)).toBeTruthy();
  const rows = screen.getAllByRole("link").map((a) => a.textContent).filter((t) => t?.startsWith("kind/"));
  expect(rows).toEqual(["kind/lab", "kind/demo"]);
});

test("the Cost page groups by team on request, with no Workloads links and the Hub's warnings as text", async () => {
  history.replaceState(null, "", "/app/?view=cost&by=team");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      {
        contract: "demo-shop",
        by: "team",
        currency: null,
        groups: [
          { group: "<b>payments</b>", workloads: 3, requests: { cpu: 2, memory: 4 * GI }, waste: { cpu: 1, memory: GI }, cost: null },
          { group: "(no team)", workloads: 1, requests: { cpu: 0.5, memory: 0 }, waste: { cpu: 0, memory: 0 }, cost: null },
        ],
        groupings: ["namespace", "team", "label:team"],
        warnings: ["kind: no pod has label team; kube-state-metrics needs --metric-labels-allowlist=pods=[team]"],
        total: { requests: { cpu: 2.5, memory: 4 * GI }, waste: { cpu: 1, memory: GI }, cost: null },
        realized: { currency: null, rate: { cpu: 0, memory: 0 }, accrued: { cpu: 0, memory: 0 }, week: { cpu: 0, memory: 0 }, cost: null, drops: [] },
      },
    ],
  });
  const { container: page } = render(<App />);
  expect(await screen.findByText("demo-shop: Cost by team")).toBeTruthy();
  expect(screen.getByText("<b>payments</b>")).toBeTruthy();
  expect(page.querySelector("b")).toBeNull();
  expect(screen.getByText(/no pod has label team/)).toBeTruthy();
  expect(screen.getByText(/Savings realized: none yet/)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "(no team)" })).toBeNull();
  const calls = (fetch as unknown as { mock: { calls: string[][] } }).mock.calls.map((c) => c[0]);
  expect(calls).toContain("/api/v1/contracts/demo-shop/cost?by=team");
  const options = [...screen.getByLabelText("Group by").querySelectorAll("option")].map((o) => o.textContent);
  expect(options).toEqual(["namespace", "team", "label team"]);
});

test("an HPA minimum lowered toward its Floor counts in the realized Savings", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  const zero = { cpu: 0, memory: 0 };
  const total = { requests: zero, waste: zero, cost: null };
  const saved = { cpu: 3.6, memory: 3 * GI };
  const lowered = { workload: "kind/lab/Deployment/api", at: 0, before: 6, after: 3, floor: 3, replicas: 3, saved, cost: null };
  const realized = { currency: null, rate: saved, accrued: zero, week: saved, cost: null, drops: [], floors: [lowered] };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      { contract: "demo-shop", by: "namespace", currency: null, groups: [], groupings: ["namespace"], warnings: [], total, realized, prices: [] },
    ],
  });
  render(<App />);
  expect(await screen.findByText(/0 requests lowered to their Sizings, 1 HPA minimum lowered toward its Floor/)).toBeTruthy();
  expect(screen.getByText("3.60 cores, 3.00 GiB")).toBeTruthy();
  expect(screen.queryByText(/none yet/)).toBeNull();
});

test("the Cost page says where each cluster's price comes from, as text", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  const zero = { cpu: 0, memory: 0 };
  const total = { requests: zero, waste: zero, cost: null };
  const realized = { currency: null, rate: zero, accrued: zero, week: zero, cost: null, drops: [] };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      {
        contract: "demo-shop", by: "namespace", currency: null, groups: [], groupings: ["namespace"], warnings: [], total, realized,
        prices: [
          { cluster: "eu", source: "list", note: "list price of 3 x aws <i>m5.large</i> in us-east-1" },
          { cluster: "kind", source: "none", note: "no list price: 1 of 1 nodes lack an instance type" },
        ],
      },
    ],
  });
  const { container: page } = render(<App />);
  expect(await screen.findByText(/list price of 3 x aws/)).toBeTruthy();
  expect(page.querySelector("i")).toBeNull();
  expect(screen.getByText(/eu: list price/).className).toBe("hint");
  expect(screen.getByText(/kind: no list price/).className).toBe("notice");
});

test("another Contract's Cost page starts by namespace", async () => {
  history.replaceState(null, "", "/app/?view=cost&by=team");
  const zero = { cpu: 0, memory: 0 };
  const total = { requests: zero, waste: zero, cost: null };
  const realized = { currency: null, rate: zero, accrued: zero, week: zero, cost: null, drops: [] };
  const body = { contract: "demo-shop", by: "team", currency: null, groups: [], groupings: ["namespace", "team"], warnings: [], total, realized };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }, { id: "demo-bank", name: "demo-bank" }]],
    "/api/v1/contracts/demo-shop/cost": [200, body],
    "/api/v1/contracts/demo-bank/cost": [200, { ...body, contract: "demo-bank", by: "namespace", groupings: ["namespace"] }],
  });
  render(<App />);
  fireEvent.change(await screen.findByLabelText("Contract"), { target: { value: "demo-bank" } });
  await screen.findByText("demo-bank: Cost by namespace");
  const calls = (fetch as unknown as { mock: { calls: string[][] } }).mock.calls.map((c) => c[0]);
  expect(calls).toContain("/api/v1/contracts/demo-bank/cost?by=namespace");
});

test("comparing shows each group's change, a group gone since and what was left out", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  const zero = { cpu: 0, memory: 0 };
  const change = (requests: number, waste: number) => ({ requests: zero, waste: zero, cost: { requests, waste } });
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      {
        contract: "demo-shop",
        by: "namespace",
        currency: "USD",
        groups: [
          { group: "kind/lab", workloads: 9, requests: zero, waste: zero, cost: { requests: 147.29, waste: 74.65 }, change: change(-36.5, -18.25) },
          { group: "kind/old", workloads: 0, requests: zero, waste: zero, cost: { requests: 0, waste: 0 }, change: change(-9.12, 0) },
        ],
        groupings: ["namespace"],
        warnings: [],
        total: { requests: zero, waste: zero, cost: { requests: 147.29, waste: 74.65 } },
        realized: { currency: "USD", rate: zero, accrued: zero, week: zero, cost: { rate: 0, accrued: 0, week: 0 }, drops: [] },
        compare: {
          since: 86400,
          clusters: { kind: { at: 86400, oldest: 0 }, eu: { at: null, oldest: 172800 } },
          change: change(-45.62, -18.25),
          left_out: ["kind/lab/StatefulSet/db/db"],
        },
      },
    ],
  });
  render(<App />);
  fireEvent.change(await screen.findByLabelText("Compare with"), { target: { value: "7" } });
  expect(await screen.findByText("-36.50 USD/month")).toBeTruthy();
  expect(screen.getByText("-9.12 USD/month")).toBeTruthy(); // kind/old, gone since
  expect(screen.getAllByText("+0.00 USD/month")).toHaveLength(1);
  expect(screen.getByText("-45.62 USD/month")).toBeTruthy(); // the requests' change, under its tile
  expect(screen.getAllByText("-18.25 USD/month").length).toBeGreaterThan(0); // the Savings'
  expect(screen.getByText("kind: compared with its run of 1970-01-02 00:00 UTC")).toBeTruthy();
  expect(screen.getByText(/eu: no run that old, oldest 1970-01-03 00:00 UTC/)).toBeTruthy();
  expect(screen.getByText(/kind\/lab\/StatefulSet\/db\/db/)).toBeTruthy();
  const calls = (fetch as unknown as { mock: { calls: string[][] } }).mock.calls.map((c) => c[0]);
  expect(calls).toContain("/api/v1/contracts/demo-shop/cost?by=namespace&compare=7");
  expect(location.search).toContain("compare=7");
});

test("without the approver cookie it links the token page", async () => {
  answer({ "/api/v1/contracts": [401, { detail: { token_url: "/console/token" } }] });
  render(<App />);
  const link = await screen.findByRole("link", { name: "paste it" });
  expect(link.getAttribute("href")).toBe("/console/token");
});

test("a chart plots request, Sizing and basis in millicores or MiB", () => {
  const points: Point[] = [
    { at: 1, cpu: { action: "keep", current: 0.5, target: 0.5, basis: 0.25 } },
    { at: 2, memory: { action: "set", current: null, target: 2 ** 21, basis: 2 ** 20 } },
  ];
  expect(series(points, "cpu")).toEqual([[1, 2], [500, null], [500, null], [250, null]]);
  expect(series(points, "memory")).toEqual([[1, 2], [null, null], [null, 2], [null, 1]]);
});

const COST = {
  contract: "demo-shop",
  by: "namespace",
  currency: "USD",
  groups: [],
  groupings: ["namespace"],
  warnings: [],
  total: { requests: { cpu: 0, memory: 0 }, waste: { cpu: 0, memory: 0 }, cost: { requests: 0, waste: 0 } },
  realized: { currency: null, rate: { cpu: 0, memory: 0 }, accrued: { cpu: 0, memory: 0 }, week: { cpu: 0, memory: 0 }, cost: null, drops: [] },
};
const BILL = { cluster: "kind", tag: "squero-cluster=kind", start: "2026-10-19", end: "2026-10-25", days: 7, currency: "USD", hours: 168, sampled: 168, coverage: 1, note: null };

test("the Cost page sets a cluster's AWS bill beside its estimate, flagged outside the tolerance", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [200, COST],
    "/api/v1/contracts/demo-shop/billing": [
      200,
      {
        tolerance: 0.05,
        min_coverage: 0.9,
        clusters: [
          { ...BILL, billed: 15.4, estimated: 15.456, gap: 0.0036, within: true },
          { ...BILL, cluster: "<b>eu</b>", billed: 14, estimated: 15.456, gap: 0.104, within: false },
          { ...BILL, cluster: "us", billed: null, estimated: null, gap: null, within: null, note: "no bill for 2 of 7 days yet" },
          { ...BILL, cluster: "lab", billed: 15.4, estimated: 8.4, gap: -0.45, within: null, sampled: 92, coverage: 0.55, note: "the estimate rests on 92 of 168 sampled hours" },
          { ...BILL, cluster: "spotty", billed: 10, estimated: 10.2, gap: 0.02, within: true, spot_billed: 3.5, spot_estimated: 3.6 },
        ],
      },
    ],
  });
  render(<App />);
  expect(await screen.findByText(/kind bill \(2026-10-19 to 2026-10-25\): billed 15.40 USD, estimated 15.46 USD, \+0.4% \(within 5%\)/)).toBeTruthy();
  const eu = screen.getByText(/<b>eu<\/b> bill .*\+10.4% \(outside 5%\)/); // text, never markup
  expect(eu.className).toBe("notice");
  expect(screen.getByText(/us bill .*no bill for 2 of 7 days yet/).className).toBe("notice");
  const lab = screen.getByText(/lab bill .*-45.0% \(no verdict\): the estimate rests on 92 of 168 sampled hours/);
  expect(lab.className).toBe("notice"); // a gap without a verdict is no "within"
  // slice 16.4b: the bill's spot part beside the estimate's
  expect(screen.getByText(/spotty bill .*\(within 5%\); spot billed 3.50 USD, estimated 3.60 USD/)).toBeTruthy();
});

test("a Contract with no billed cluster shows no bill line and no error", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [200, COST],
  });
  render(<App />);
  await screen.findByText(/Cost by namespace/);
  expect(screen.queryByText(/ bill \(/)).toBeNull();
  expect(screen.queryByText(/404/)).toBeNull();
});

test("the Kill switch shows who turned it on, and only here does it go off", async () => {
  const sent: string[] = [];
  const on = { on: true, by: "mcp:alex@example.com", since: 1_700_000_000 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      const method = init?.method ?? "GET";
      if (path.endsWith("/kill-switch")) {
        sent.push(method);
        const body = method === "DELETE" ? { on: false } : on;
        return new Response(JSON.stringify(body), { status: 200 });
      }
      const routes: Record<string, unknown> = {
        "/api/v1/contracts": [{ id: "demo-shop", name: "demo-shop" }],
        "/api/v1/contracts/demo-shop/workloads": DATA,
      };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
  render(<App />);
  expect(await screen.findByText(/Kill switch on since 2023-11-14 22:13 UTC \(mcp:alex@example.com\)/)).toBeTruthy();
  fireEvent.click(screen.getByText("Turn it off"));
  fireEvent.click(screen.getByText("Keep it on")); // slice 21.5a: off asks first
  expect(sent).toEqual(["GET"]); // nothing sent
  fireEvent.click(screen.getByText("Turn it off"));
  expect(screen.getByText("Changes to this Contract's systems will run again.")).toBeTruthy();
  fireEvent.click(screen.getByText("Yes, turn it off"));
  expect(await screen.findByText("Turn on the Kill switch")).toBeTruthy();
  fireEvent.click(screen.getByText("Turn on the Kill switch"));
  expect(await screen.findByText("Turn it off")).toBeTruthy();
  expect(sent).toEqual(["GET", "DELETE", "POST"]);
});

test("a question to turn the Kill switch off never follows you to another Contract", async () => {
  const on = { on: true, by: "console:op", since: 1_700_000_000 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      if (path.endsWith("/kill-switch")) return new Response(JSON.stringify(on), { status: 200 });
      const routes: Record<string, unknown> = {
        "/api/v1/contracts": [{ id: "demo-shop", name: "demo-shop" }, { id: "demo-bank", name: "demo-bank" }],
      };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
  history.replaceState(null, "", "/app/?contract=demo-shop&view=teachings");
  render(<App />);
  fireEvent.click(await screen.findByText("Turn it off"));
  expect(screen.getByText("Yes, turn it off")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Contract"), { target: { value: "demo-bank" } });
  expect(await screen.findByText("Turn it off")).toBeTruthy(); // asked anew, for demo-bank
  expect(screen.queryByText("Yes, turn it off")).toBeNull();
});

test("the Kill switch is on a Contract's own pages only, never across Contracts or on one Investigation", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      if (path.endsWith("/kill-switch")) return new Response(JSON.stringify({ on: false }), { status: 200 });
      const body = path === "/api/v1/contracts" ? [{ id: "demo-shop", name: "demo-shop" }] : {};
      return new Response(JSON.stringify(body), { status: path === "/api/v1/contracts" ? 200 : 404 });
    }),
  );
  for (const view of ["fleet", "contracts", "token", "investigation&id=i1"]) {
    history.replaceState(null, "", `/app/?contract=demo-shop&view=${view}`);
    render(<App />);
    await new Promise((r) => setTimeout(r, 20)); // the Contracts' and the Kill switch's fetches
    expect(screen.queryByText("Turn on the Kill switch")).toBeNull();
    cleanup();
  }
  history.replaceState(null, "", "/app/?contract=demo-shop&view=teachings");
  render(<App />);
  expect(await screen.findByText("Turn on the Kill switch")).toBeTruthy();
});

test("the Agenda lists what to look at now, in the Hub's order, each linking where to act", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
    "/api/v1/contracts/demo-shop/agenda": [
      200,
      {
        contract: "demo-shop",
        items: [
          { kind: "approval", title: "1 Proposed action wait for a decision: <b>pods</b>", at: 1_700_000_000, id: "t1", cluster: null, workload: null },
          { kind: "stale", title: "Sizing reads failing: down", at: 1_700_000_000, id: null, cluster: "kind", workload: null },
          { kind: "protocol", title: "Actuator in kind speaks Protocol 3, this Hub 1: upgrade the Hub", at: 1_700_000_000, id: null, cluster: "kind", workload: null },
          { kind: "bill", title: "Bill +8.0% from the estimate", at: null, id: null, cluster: "aws", workload: null },
          { kind: "waste", title: "Waste: 2000m and 0Mi above its Sizings", at: null, id: null, cluster: "kind", workload: "kind/lab/Deployment/api" },
        ],
      },
    ],
  });
  const { container: page } = render(<AgendaPanel contract="demo-shop" />); // on the Overview from 21.3a
  await screen.findByText("Now");
  const items = [...page.querySelectorAll(".agenda li")].map((li) => li.textContent);
  expect(items).toEqual([
    "1 Proposed action wait for a decision: <b>pods</b> (goes Stale 2023-11-14 22:13 UTC)",
    "kind: Sizing reads failing: down (2023-11-14 22:13 UTC)",
    "kind: Actuator in kind speaks Protocol 3, this Hub 1: upgrade the Hub (2023-11-14 22:13 UTC)",
    "aws: Bill +8.0% from the estimate",
    "kind/lab/Deployment/api: Waste: 2000m and 0Mi above its Sizings",
  ]);
  const hrefs = [...page.querySelectorAll(".agenda li")].map((li) => li.querySelector("a")?.getAttribute("href") ?? null);
  expect(hrefs).toEqual([
    "/app/?view=investigation&id=t1",
    null,
    "/app/?contract=demo-shop&view=health",
    "/app/?contract=demo-shop&view=cost",
    "/app/?contract=demo-shop&workload=kind%2Flab%2FDeployment%2Fapi",
  ]);
  expect(page.querySelector(".agenda b")).toBeNull(); // Hub text is text
});

test("a quiet Contract's Agenda says so", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
    "/api/v1/contracts/demo-shop/agenda": [200, { contract: "demo-shop", items: [] }],
  });
  render(<AgendaPanel contract="demo-shop" />);
  expect(await screen.findByText("Nothing needs you now.")).toBeTruthy();
});

const REPORT = {
  summary: "checkout is OOM-killed",
  category: "resource_limits",
  probable_root_cause: "memory limit 64Mi\nagainst a 200Mi heap",
  confidence: "high",
  severity: "high",
  affected: ["shop/Deployment/checkout"],
  evidence: [{ source: "kubernetes_pod_logs", finding: "<img src=x onerror=alert(1)>", link: "https://evil.example/x" }],
  recommended_actions: ["Raise the limit to 256Mi"],
  runbook: null,
};

const PAGE = {
  id: "t1",
  contract: "demo-shop",
  kind: "triage",
  title: "<b>OOM</b> in checkout",
  status: "awaiting_approval",
  failed: false,
  started: 1_700_000_000,
  error: null,
  deadline: 1_700_000_600,
  report: REPORT,
  answer: null,
  check_result: null,
  pending: ["evict shop/checkout-1"],
  executed_actions: [],
  usage: { input_tokens: 10, output_tokens: 5, cost_usd: 0.0012 },
  masked: { name: 2 },
  decisions: [{ approve: false, actions: ["kubernetes_evict_pod"], message: "not now", decided_by: "slack:U1", at: 1_700_000_100 }],
  related: [],
  tool_calls: [{ tool: "kubernetes_list_pods", args: { namespace: "shop" }, result: "<script>x</script>", is_error: false }],
  feedback: [],
  taught: [],
  scenario: false,
  jira: null,
  can_judge: false,
  can_follow_up: false,
  asked: null,
  exchange: [],
};

test("a Contract's Investigations list links each one, a failed Check as failed", async () => {
  history.replaceState(null, "", "/app/?view=investigations");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/investigations": [
      200,
      [
        { id: "c1", contract: "demo-shop", kind: "check", title: "api answers", status: "done", failed: true, started: 1_700_000_000 },
        { id: "t1", contract: "demo-shop", kind: "triage", title: "OOM", status: "awaiting_approval", failed: false, started: 1_700_000_000 },
      ],
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Investigations");
  const rows = [...page.querySelectorAll(".investigations tbody tr")].map((tr) => tr.textContent);
  expect(rows).toEqual([
    "2023-11-14 22:13 UTCcheckfailedapi answers",
    "2023-11-14 22:13 UTCtriageawaiting approvalOOM",
  ]);
  expect(screen.getByText("OOM").getAttribute("href")).toBe("/app/?view=investigation&id=t1");
});

test("an Investigation shows its Report as text, an Evidence link never as a link", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/investigations/t1": [200, PAGE],
  });
  const { container: page } = render(<App />);
  expect(await screen.findByText("<b>OOM</b> in checkout")).toBeTruthy();
  const cause = [...page.querySelectorAll("p.text")].find((p) => p.textContent?.includes("64Mi"));
  expect(cause?.textContent).toContain("memory limit 64Mi\nagainst a 200Mi heap"); // its line break kept
  expect(screen.getByText(/onerror=alert\(1\)> \(https:\/\/evil.example\/x\)/)).toBeTruthy();
  expect(page.querySelector("img")).toBeNull();
  expect(page.querySelector("b")).toBeNull();
  expect(page.querySelector('a[href^="https://evil"]')).toBeNull();
  expect(screen.getByText("evict shop/checkout-1")).toBeTruthy();
  expect(screen.getByText(/Goes Stale at 2023-11-14 22:23 UTC/)).toBeTruthy();
  expect(screen.getByText(/masked for the LLM: 2 names/)).toBeTruthy();
  expect(screen.getByText("not now")).toBeTruthy();
  expect(screen.getByText("<script>x</script>")).toBeTruthy();
  expect(screen.queryByText("Save as Scenario")).toBeNull();
});

test("a finished Investigation offers its Scenario download", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/investigations/t1": [200, { ...PAGE, status: "done", pending: [], deadline: null, scenario: true }],
  });
  render(<App />);
  const link = await screen.findByText("Save as Scenario");
  expect(link.getAttribute("href")).toBe("/api/v1/investigations/t1/scenario.yaml");
  expect(screen.queryByText("Waiting for a decision")).toBeNull();
});

test("the token page posts the token and says when it is wrong", async () => {
  history.replaceState(null, "", "/app/?view=token");
  const sent: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/token") {
        sent.push(JSON.parse(String(init?.body)));
        return new Response(null, { status: 403 });
      }
      return new Response("{}", { status: 401 });
    }),
  );
  const { container: page } = render(<App />);
  const input = page.querySelector('input[type="password"]') as HTMLInputElement;
  fireEvent.change(input, { target: { value: "guess" } });
  fireEvent.click(screen.getByText("Save"));
  expect(await screen.findByText("Couldn't: that is not the approver token")).toBeTruthy();
  expect(sent).toEqual([{ token: "guess" }]);
  expect(screen.queryByText(/paste it/)).toBeNull(); // the token page needs no cookie
});

function acting(answers: Record<string, [number, unknown]>, sent: [string, unknown][]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, init.body ? JSON.parse(String(init.body)) : null]);
        const [status, body] = answers[path] ?? [404, {}];
        return new Response(JSON.stringify(body), { status });
      }
      const routes: Record<string, unknown> = {
        "/api/v1/contracts": [{ id: "demo-shop", name: "demo-shop" }],
        "/api/v1/investigations/t1": PAGE,
      };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
}

test("approving posts the pause it showed, and the page shows what came back", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  const sent: [string, unknown][] = [];
  const after = { ...PAGE, status: "running", pending: [], deadline: null };
  acting({ "/api/v1/investigations/t1/decision": [200, after] }, sent);
  render(<App />);
  fireEvent.click(await screen.findByText("Approve"));
  expect(await screen.findByText(/Working on it/)).toBeTruthy();
  expect(screen.queryByText("Waiting for your decision")).toBeNull();
  expect(sent).toEqual([["/api/v1/investigations/t1/decision", { deadline: 1_700_000_600, approve: true, reason: "" }]]);
});

test("a rejection sends its reason, and a refusal shows the Hub's words", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  const sent: [string, unknown][] = [];
  acting({ "/api/v1/investigations/t1/decision": [409, { detail: "already decided or Stale: nothing to decide" }] }, sent);
  const { container: page } = render(<App />);
  await screen.findByText("Reject");
  const input = page.querySelector(".waiting input") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "not during the sale" } });
  fireEvent.click(screen.getByText("Reject"));
  expect(await screen.findByText("Couldn't: already decided or Stale: nothing to decide")).toBeTruthy();
  expect(sent).toEqual([["/api/v1/investigations/t1/decision", { deadline: 1_700_000_600, approve: false, reason: "not during the sale" }]]);
});

test("a finished Report takes feedback and files into Jira, then links the issue", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  const done = { ...PAGE, status: "done", pending: [], deadline: null, can_judge: true, jira: { project: "OPS", issue: null, url: null, by: null, since: null } };
  const filed = { ...done, jira: { project: "OPS", issue: "OPS-1", url: "https://acme.atlassian.net/browse/OPS-1", by: "console:alex@example.com", since: 1 } };
  const judged = { ...done, feedback: [{ correct: true, correction: null, by: "console:alex@example.com", at: 1_700_000_000 }] };
  const sent: [string, unknown][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, init.body ? JSON.parse(String(init.body)) : null]);
        return new Response(JSON.stringify(path.endsWith("/jira") ? filed : judged), { status: 200 });
      }
      const body = path === "/api/v1/contracts" ? [{ id: "demo-shop", name: "demo-shop" }] : done;
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByText("Right"));
  expect(await screen.findByText(/console:alex@example.com: right/)).toBeTruthy();
  fireEvent.click(screen.getByText("Create Jira issue"));
  const issue = await screen.findByText("OPS-1");
  expect(issue.getAttribute("href")).toBe("https://acme.atlassian.net/browse/OPS-1");
  expect(sent).toEqual([
    ["/api/v1/investigations/t1/feedback", { correct: true, correction: "" }],
    ["/api/v1/investigations/t1/jira", null],
  ]);
});

const TEACHING = { alerts: [], source_id: null, correction: null, contradicts: null, decided_by: null, decided_at: null };
const TEACHINGS = {
  limit: 500,
  left_out: 2,
  proposed: [{ ...TEACHING, id: 3, status: "proposed", text: "<b>check the node</b>", source_id: "t1", correction: "it was the node", contradicts: 1 }],
  approved: [{ ...TEACHING, id: 1, status: "approved", text: "restart the cache", alerts: ["KubePodCrashLooping"], decided_by: "slack:U1", decided_at: 1_700_000_000 }],
  past: [{ ...TEACHING, id: 2, status: "rejected", text: "ignore it", decided_by: "slack:U1", decided_at: 1_700_000_000 }],
};

function teaching(answers: Record<string, [number, unknown]>, sent: [string, unknown][]) {
  history.replaceState(null, "", "/app/?view=teachings");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, init.body ? JSON.parse(String(init.body)) : null]);
        const [status, body] = answers[path] ?? [404, {}];
        return new Response(JSON.stringify(body), { status });
      }
      const routes: Record<string, unknown> = {
        "/api/v1/contracts": [{ id: "demo-shop", name: "demo-shop" }],
        "/api/v1/contracts/demo-shop/teachings": TEACHINGS,
      };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
}

test("Teachings show the queue as text, and approving sends the text as edited", async () => {
  const sent: [string, unknown][] = [];
  const after = { ...TEACHINGS, left_out: 0, proposed: [], approved: [{ ...TEACHINGS.approved[0], id: 3, text: "check the node first" }] };
  teaching({ "/api/v1/contracts/demo-shop/teachings/3/decision": [200, after] }, sent);
  const { container: page } = render(<App />);
  const box = (await screen.findByLabelText("Teaching #3")) as HTMLTextAreaElement;
  expect(box.value).toBe("<b>check the node</b>"); // drafted model text, edited as text
  expect(page.querySelector("b")?.textContent).not.toBe("check the node");
  expect(screen.getByText("its Investigation").getAttribute("href")).toBe("/app/?view=investigation&id=t1");
  expect(screen.getByText(/contradicts #1/)).toBeTruthy();
  expect(screen.getByText("Your words: it was the node")).toBeTruthy();
  expect(screen.getByText(/2 approved Teachings don't fit in a prompt/)).toBeTruthy();
  expect(screen.getByText(/#2 rejected by slack:U1: ignore it/)).toBeTruthy();
  fireEvent.change(box, { target: { value: "check the node first" } });
  fireEvent.click(screen.getByText("Approve this text"));
  expect(await screen.findByText("check the node first")).toBeTruthy();
  expect(screen.queryByText(/don't fit/)).toBeNull();
  expect(sent).toEqual([["/api/v1/contracts/demo-shop/teachings/3/decision", { approve: true, text: "check the node first" }]]);
});

test("rejecting and retiring post for that Teaching, and a refusal shows the Hub's words", async () => {
  const sent: [string, unknown][] = [];
  teaching(
    {
      "/api/v1/contracts/demo-shop/teachings/3/decision": [409, { detail: "already decided" }],
      "/api/v1/contracts/demo-shop/teachings/1/retire": [200, { ...TEACHINGS, approved: [] }],
    },
    sent,
  );
  render(<App />);
  fireEvent.click(await screen.findByText("Reject"));
  expect(await screen.findByText("Couldn't: already decided")).toBeTruthy();
  fireEvent.click(screen.getByText("Retire"));
  await waitFor(() => expect(screen.queryByText("restart the cache")).toBeNull());
  expect(screen.queryByText("Couldn't: already decided")).toBeNull();
  expect(sent).toEqual([
    ["/api/v1/contracts/demo-shop/teachings/3/decision", { approve: false, text: "" }],
    ["/api/v1/contracts/demo-shop/teachings/1/retire", null],
  ]);
});

test("the Operator writes a Teaching for some alerts, and the form clears once it is added", async () => {
  const sent: [string, unknown][] = [];
  const after = { ...TEACHINGS, approved: [{ ...TEACHINGS.approved[0], id: 4, text: "check the node first" }, ...TEACHINGS.approved] };
  teaching({ "/api/v1/contracts/demo-shop/teachings": [200, after] }, sent);
  render(<App />);
  const box = (await screen.findByLabelText("New Teaching")) as HTMLTextAreaElement;
  const alerts = screen.getByLabelText(/For alerts/) as HTMLInputElement;
  fireEvent.change(box, { target: { value: "check the node first" } });
  fireEvent.change(alerts, { target: { value: "KubeNodeNotReady, KubeletDown" } });
  fireEvent.click(screen.getByText("Add, approved"));
  expect(await screen.findByText("check the node first")).toBeTruthy();
  expect(box.value).toBe("");
  expect(alerts.value).toBe("");
  expect(sent).toEqual([["/api/v1/contracts/demo-shop/teachings", { text: "check the node first", alerts: ["KubeNodeNotReady", " KubeletDown"] }]]);
});

const CHECKS = {
  checks: [
    { name: "api answers", schedule: "*/5 * * * * UTC", next_due: 1_700_000_300, last_id: "r1", last_status: "done", last_passed: false, last_started: 1_700_000_000, failing_id: "r1", skipped_at: null, skipped_reason: null },
    { name: "backups", schedule: "0 3 * * * UTC", next_due: 1_700_000_600, last_id: null, last_status: null, last_passed: null, last_started: null, failing_id: null, skipped_at: 1_700_000_000, skipped_reason: "over budget" },
  ],
  deploys: [
    { cluster: "kind", namespace: "shop", changes: [{ namespace: "shop", object: "Deployment/api", revision: "4" }], due: 1_700_000_000, state: "skipped", investigation_id: null, run_status: null, passed: null, skipped_reason: "<b>Kill switch</b> on" },
    { cluster: "kind", namespace: "shop", changes: [{ namespace: "shop", object: "Deployment/api", revision: "3" }], due: 1_700_000_000, state: "started", investigation_id: "r2", run_status: "done", passed: true, skipped_reason: null },
  ],
};

test("Checks show each one's last Run and Open failure, and Deploy verifications their state", async () => {
  history.replaceState(null, "", "/app/?view=checks");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/checks": [200, CHECKS],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Checks");
  expect(screen.getByText("failed").getAttribute("href")).toBe("/app/?view=investigation&id=r1");
  expect(screen.getByText("failing since this Run").getAttribute("href")).toBe("/app/?view=investigation&id=r1");
  expect(screen.getByText("2023-11-14 22:13 UTC: over budget")).toBeTruthy();
  expect(screen.getByText("passed").getAttribute("href")).toBe("/app/?view=investigation&id=r2");
  expect(screen.getByText("skipped: <b>Kill switch</b> on")).toBeTruthy(); // as text
  expect(page.querySelector("main b")).toBeNull();
  expect(screen.getByText("Deployment/api 4")).toBeTruthy();
});

test("Nodes shows each node's Room, the Contract's pods by name and the rest as a sum", async () => {
  history.replaceState(null, "", "/app/?view=nodes");
  const node = { type: "", taints: [], cordoned: false, daemonsets: { cpu: 0.1, memory: GI / 4 }, pods: 4, bound: 1 };
  const blocked = { state: "blocked", why: null, blockers: [{ namespace: "shop", pod: "db-0", workload: "StatefulSet/db", reasons: ["PodDisruptionBudget <i>db</i> allows no disruption", "local storage (data)"],
    changes: [
      { reason: "PodDisruptionBudget <i>db</i> allows no disruption", change: "check first that its pods are healthy", manifest: null },
      { reason: "local storage (data)", change: "name every one of them in the annotation below", manifest: "# data is lost\nspec:\n  template: <u>x</u>" },
    ] }] };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/nodes": [
      200,
      {
        currency: "EUR",
        clusters: [
          { cluster: "kind", at: 1_700_000_000, autoscaler: "karpenter", problem: null, largest: { cpu: 7.5, memory: 30 * GI },
            rules: "Karpenter's rules", reported: 1_700_000_600, settings: ["NodePool spec.disruption.budgets (10% of nodes by default)"], judged: { clear: 1, blocked: 1, unknown: 1 },
            could_go: { now: { nodes: ["n2"], cpu: 4, memory: 16 * GI, cost: 120 }, after: { nodes: ["n2", "n<b>4</b>"], cpu: 8, memory: 32 * GI, cost: null }, note: "its Actuator may not read the <b>labels</b>" },
            nodes: [
              { ...node, node: "n2", verdict: { state: "clear", why: null, blockers: [] }, allocatable: { cpu: 4, memory: 16 * GI }, requests: { cpu: 0, memory: 0 },
                room: { cpu: 4, memory: 16 * GI }, mine: { pods: 0, cpu: 0, memory: 0 }, others: { pods: 0, cpu: 0, memory: 0 }, own: [] },
              { ...node, node: "n3", verdict: { state: "unknown", why: "2 pods not visible", blockers: [] }, allocatable: { cpu: 4, memory: 16 * GI }, requests: { cpu: 0, memory: 0 },
                room: { cpu: 4, memory: 16 * GI }, mine: { pods: 0, cpu: 0, memory: 0 }, others: { pods: 2, cpu: 0, memory: 0 }, own: [] },
              { ...node, node: "n1", type: "m5.xlarge", verdict: blocked, taints: ["gpu=<b>true</b>:NoSchedule"], cordoned: true,
                allocatable: { cpu: 4, memory: 16 * GI }, requests: { cpu: 1.85, memory: 6 * GI }, room: { cpu: 2.15, memory: 10 * GI },
                mine: { pods: 2, cpu: 1.5, memory: 5 * GI }, others: { pods: 2, cpu: 0.35, memory: GI },
                own: [{ namespace: "shop", pod: "api-1", cpu: 0.5, memory: GI }, { namespace: "shop", pod: "db-0", cpu: 1, memory: 4 * GI }] },
            ] },
          { cluster: "edge", at: 1_700_000_000, autoscaler: null, largest: null, nodes: [], could_go: null, judged: {}, rules: "kubectl drain's rules (the Contract names no autoscaler)", reported: null,
            problem: "the store is shared with another Contract and has no cluster labels: node series would cross Contracts" },
        ],
      },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Nodes");
  const [kind, edge] = [...page.querySelectorAll("section.nodes")];
  expect(kind?.textContent).toContain("Read 2023-11-14 22:13 UTC. Autoscaler: karpenter.");
  expect(kind?.textContent).toContain("No Sizing asks more than 7.50 cores, 30.00 GiB: the largest Room after DaemonSets.");
  // slice 15.1b: each node's verdict, by its cluster's rules
  expect(kind?.textContent).toContain("goes by Karpenter's rules; its Actuator's last report of Blockers: 2023-11-14 22:23 UTC.");
  const verdicts = [...(kind?.querySelectorAll("tbody tr") ?? [])].map((tr) => tr.lastElementChild?.textContent);
  expect(verdicts[0]).toBe("yes: nothing on it blocks");
  expect(verdicts[1]).toBe("unknown: 2 pods not visible");
  expect(verdicts[2]).toContain("blockedshop/db-0: PodDisruptionBudget <i>db</i> allows no disruption; local storage (data)");
  // slice 15.6: the change that removes each Blocker, with a manifest only where there is one
  const changes = [...(kind?.querySelectorAll("details.change") ?? [])];
  expect(changes).toHaveLength(2);
  expect(changes[0]?.textContent).toBe("To remove it: PodDisruptionBudget <i>db</i> allows no disruptioncheck first that its pods are healthy.");
  expect(changes[0]?.querySelector("pre")).toBeNull();
  expect(changes[1]?.querySelector("pre")?.textContent).toBe("# data is lost\nspec:\n  template: <u>x</u>");
  expect(page.querySelector("section.nodes u")).toBeNull();
  expect(kind?.querySelector("details.settings")?.textContent).toContain("NodePool spec.disruption.budgets (10% of nodes by default)");
  expect(edge?.querySelector("details.settings")).toBeNull();
  expect(page.querySelector("section.nodes i")).toBeNull();
  expect(edge?.textContent).toContain("its Actuator's last report of Blockers: never.");
  // slice 15.4: the nodes that could go, now and after the Sizings, as a figure of its own
  const could = kind?.querySelector(".could-go")?.textContent;
  expect(could).toContain("Could go now: up to 1 (n2), 120.00 EUR/month.");
  expect(kind?.querySelector(".could-go")?.previousElementSibling?.textContent).toBe("Nodes that could go: now 1 → 2 after the Sizings");
  expect(could).toContain("After the Sizings: up to 2 (n2, n<b>4</b>), 8.00 cores, 32.00 GiB.");
  expect(could).toContain("An upper bound: tolerations and spread rules can't be seen. Not part of Savings. 1 of 3 nodes not judged.");
  // audit A3: why none is counted, as text
  expect(could).toContain("None counted: its Actuator may not read the <b>labels</b>.");
  expect(page.querySelector(".could-go b")).toBeNull();
  expect(edge?.querySelector(".could-go")).toBeNull();
  const cells = [...(kind?.querySelectorAll("tbody tr:last-child td") ?? [])].map((td) => td.textContent);
  expect(cells[0]).toContain("n1 m5.xlarge cordoned gpu=<b>true</b>:NoSchedule");
  expect(page.querySelector("section.nodes b")).toBeNull();
  expect(cells[1]).toBe("4.00 cores, 16.00 GiB");
  expect(cells[2]).toContain("2 pods: 1.50 cores, 5.00 GiB");
  expect(cells[2]).toContain("shop/api-1: 0.50 cores, 1.00 GiB");
  expect(cells[3]).toBe("2 pods: 0.35 cores, 1.00 GiB");
  expect(cells[4]).toBe("0.10 cores, 0.25 GiB");
  expect(cells[5]).toBe("2.15 cores, 10.00 GiB");
  expect(edge?.textContent).toContain("Autoscaler: none named in the Contract.");
  expect(edge?.textContent).toContain("node series would cross Contracts");
  expect(edge?.querySelector("table")).toBeNull();
  expect(screen.getByText("Nodes", { selector: "nav a span" }).closest("a")?.getAttribute("aria-current")).toBe("page");
  expect(kind?.querySelector(".placement")).toBeNull(); // no Placement: nothing said of it
  expect(kind?.querySelector(".spot")).toBeNull(); // no spot label: nothing said of it
});

test("Nodes shows a cluster's Placement, its Pinned nodes and which Workload to restart", async () => {
  history.replaceState(null, "", "/app/?view=nodes");
  const base = { type: "", taints: [], cordoned: false, daemonsets: { cpu: 0, memory: 0 }, pods: 1, bound: 0, allocatable: { cpu: 4, memory: 16 * GI },
    requests: { cpu: 1, memory: GI }, room: { cpu: 3, memory: 15 * GI }, mine: { pods: 1, cpu: 1, memory: GI }, others: { pods: 0, cpu: 0, memory: 0 }, own: [] };
  const keeps = (pod: string, more = {}) => ({ namespace: "shop", pod, workload: "Deployment/api", reasons: ["annotated safe-to-evict: false"],
    placement: "shop/Deployment/api", ...more });
  const restart = (workloads: string[], why_not: string | null = null) => ({ workloads, why_not });
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/nodes": [
      200,
      { currency: null, clusters: [{ cluster: "kind", at: 1_700_000_000, autoscaler: "cluster-autoscaler", problem: null, largest: null, could_go: null,
        spot: { label: "karpenter.sh/capacity-type=spot", discount: 0.35, nodes: 1, note: null },
        rules: "Cluster Autoscaler's rules", reported: 1_700_000_600, judged: { clear: 0, blocked: 2, unknown: 0, pinned: 1 },
        placement: { label: "squero.io/pinned=yes", taint: "squero.io/pinned=yes:NoSchedule", pinned: 1, note: null, workloads: ["shop/Deployment/api"], placing: true },
        nodes: [
          { ...base, node: "p1", pinned: true, verdict: { state: "pinned", why: null, blockers: [keeps("api-0", { changes: [] })] } },
          { ...base, node: "w1", spot: true, verdict: { state: "blocked", why: null, restart: restart(["shop/Deployment/api"]),
            blockers: [keeps("api-1", { note: "placed, on w1: the pinned nodes had no room when it started" })] } },
          { ...base, node: "w2", verdict: { state: "blocked", why: null, restart: restart([], "pod shop/web-1 would still hold it"),
            blockers: [keeps("api-2"), { namespace: "shop", pod: "web-1", reasons: ["PodDisruptionBudget web allows no disruption"], placement: null }] } },
        ] }] },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Nodes");
  expect(page.querySelector(".placement")?.textContent).toBe(
    "Placement: pods that can't move prefer the nodes labelled squero.io/pinned=yes (1 pinned), tolerating squero.io/pinned=yes:NoSchedule. Placed: shop/Deployment/api. ",
  );
  const rows = [...page.querySelectorAll("tbody tr")];
  expect(rows[0]?.firstElementChild?.querySelector(".chip")?.textContent).toBe("pinned");
  expect(rows[1]?.firstElementChild?.querySelector(".chip")?.textContent).toBe("spot"); // slice 16.1a
  expect(page.querySelector(".spot")?.textContent).toBe(
    "Spot: nodes labelled karpenter.sh/capacity-type=spot (1) are priced at 0.35 of on-demand. ",
  );
  const verdicts = rows.map((tr) => tr.lastElementChild?.textContent);
  expect(verdicts[0]).toBe("no: pinned, kept for pods that can't moveshop/api-0: annotated safe-to-evict: falseplaced");
  expect(verdicts[1]).toContain("placed, on w1: the pinned nodes had no room when it started.");
  expect(verdicts[1]).toContain("Restart shop/Deployment/api to free it.");
  expect(verdicts[2]).toContain("A restart wouldn't free it: pod shop/web-1 would still hold it.");
  expect(rows[0]?.querySelector("details.change")).toBeNull();
});

test("Nodes shows the Workloads that could run on spot and those under a Spot ratio", async () => {
  history.replaceState(null, "", "/app/?view=nodes");
  const cand = (name: string, saving: number) => ({ namespace: "shop", kind: "Deployment", name, replicas: 4,
    requests: { cpu: 0.5, memory: GI }, on_demand: 2, spot: 2, saving });
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/nodes": [
      200,
      { currency: "EUR", clusters: [{ cluster: "kind", at: 1_700_000_000, autoscaler: null, problem: null, largest: null, could_go: null,
        rules: "Cluster Autoscaler's rules", reported: null, judged: {}, placement: null, nodes: [],
        spot: { label: "karpenter.sh/capacity-type=spot", discount: 0.35, nodes: 1, note: null },
        candidates: { workloads: [cand("api", 12.5), cand("web", 3), { namespace: "shop", kind: "CronJob", name: "batch",
          replicas: 0.5, requests: { cpu: 1, memory: GI }, on_demand: null, spot: null, saving: 0.5 }], more: 1, saving: 16, note: null },
        spot_workloads: { note: null, workloads: [
          { namespace: "shop", kind: "Deployment", name: "cart", replicas: 10, ratio: 0.7, on_demand: 2, why: null,
            intended: { on_demand: 3, spot: 7 }, actual: { on_demand: 1, spot: 9 },
            risk: "1 on-demand, below its minimum of 2: restart it to repair" },
          { namespace: "shop", kind: "StatefulSet", name: "db", replicas: 3, ratio: 0.7, on_demand: 2, why: "a StatefulSet never runs on spot",
            intended: null, actual: null },
          { namespace: "shop", kind: "CronJob", name: "nightly", replicas: 1, ratio: null, on_demand: null, why: null,
            intended: null, actual: { on_demand: 0, spot: 1 }, risk: null },
          { namespace: "shop", kind: "CronJob", name: "once", replicas: 0, ratio: null, on_demand: null, why: "its Jobs allow no retry",
            intended: null, actual: null, risk: null },
        ] } }] },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Nodes");
  const under = [...page.querySelectorAll(".spot-workloads li")].map((li) => li.textContent);
  expect(under).toEqual([
    "shop/Deployment/cart: 10 replicas, meant 3 on-demand and 7 on spot, running 1 and 9" +
      "1 on-demand, below its minimum of 2: restart it to repair.",
    "shop/StatefulSet/db: 3 replicas, where they run not read yet (a StatefulSet never runs on spot)",
    "shop/CronJob/nightly: its runs on spot, a retry off it; running 0 on-demand and 1 on spot",
    "shop/CronJob/once: its runs on spot, a retry off it (its Jobs allow no retry)",
  ]);
  expect(page.querySelector(".candidates summary")?.textContent).toBe("Could run on spot: 4, up to 16.00 EUR/month");
  const listed = [...page.querySelectorAll(".candidates li")].map((li) => li.textContent);
  expect(listed[0]).toBe("shop/Deployment/api: 4 replicas of 0.50 cores, 1.00 GiB, 2 on spot and 2 on-demand: 12.50 EUR/month");
  expect(listed[2]).toBe(
    "shop/CronJob/batch: 0.5 pods on average of 1.00 cores, 1.00 GiB, a run entirely on spot, if its Jobs allow a retry: 0.50 EUR/month",
  );
  expect(listed[3]).toBe("and 1 more");
});

test("Health shows each integration now, the error as text, spend against the budget, secrets by name", async () => {
  history.replaceState(null, "", "/app/?view=health");
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/health": [
      200,
      {
        integrations: [
          { name: "kubernetes kind", ok: true, ms: 12, detail: "" },
          { name: "prometheus", ok: false, ms: 5003, detail: "<img src=x> timed out" },
        ],
        last: { id: "t1", contract: "demo-shop", kind: "triage", title: "OOM", status: "done", failed: false, started: 1_700_000_000 },
        config: "id: demo-shop\n",
        secrets: { "prometheus-token": true, "jira-token": false },
        spent_usd: 2.5,
        budget_usd: 10,
        hub: "approver_token: '**********'\n",
      },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Health");
  expect(screen.getByText("FAILING").className).toBe("bad");
  expect(screen.getByText("<img src=x> timed out")).toBeTruthy();
  expect(page.querySelector("main img")).toBeNull();
  expect(screen.getByText("OOM").getAttribute("href")).toBe("/app/?view=investigation&id=t1");
  expect(screen.getByText("Model spend this month: $2.50 of $10.00 (25%)")).toBeTruthy();
  expect(screen.getByText(/prometheus-token loaded, jira-token MISSING/)).toBeTruthy();
  expect(screen.getByText(/approver_token: '\*\*\*\*\*\*\*\*\*\*'/)).toBeTruthy();
});

test("asking a Question posts it for the Contract shown, and a refusal shows the Hub's words", async () => {
  history.replaceState(null, "", "/app/?view=investigations");
  const sent: [string, unknown][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, JSON.parse(String(init.body))]);
        return new Response(JSON.stringify({ detail: "ask a question of 1 to 4000 characters" }), { status: 422 });
      }
      const body = path === "/api/v1/contracts" ? [{ id: "demo-shop", name: "demo-shop" }] : [];
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  render(<App />);
  const box = (await screen.findByLabelText("Question")) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "is checkout healthy?" } });
  fireEvent.click(screen.getByText("Ask"));
  expect(await screen.findByText("Couldn't: ask a question of 1 to 4000 characters")).toBeTruthy();
  expect(sent).toEqual([["/api/v1/contracts/demo-shop/questions", { question: "is checkout healthy?" }]]);
});

test("a Question no longer answering takes a follow-up, and the page shows it answering", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  const question = { ...PAGE, kind: "question", status: "done", report: null, pending: [], deadline: null, answer: { answer: "it is fine", evidence: [] }, can_follow_up: true };
  const sent: [string, unknown][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, JSON.parse(String(init.body))]);
        return new Response(JSON.stringify({ ...question, status: "running", can_follow_up: false }), { status: 200 });
      }
      const body = path === "/api/v1/contracts" ? [{ id: "demo-shop", name: "demo-shop" }] : question;
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  render(<App />);
  const box = (await screen.findByLabelText("Follow-up")) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "and now?" } });
  fireEvent.click(screen.getByText("Follow up"));
  expect(await screen.findByText(/Working on it/)).toBeTruthy();
  expect(screen.queryByLabelText("Follow-up")).toBeNull();
  expect(sent).toEqual([["/api/v1/investigations/t1/questions", { question: "and now?" }]]);
});

test("a followed-up Question shows its earlier turns, then the follow-up its Answer answers", async () => {
  history.replaceState(null, "", "/app/?view=investigation&id=t1");
  const question = {
    ...PAGE, kind: "question", status: "done", title: "which namespaces?", report: null, pending: [], deadline: null,
    answer: { answer: "one cluster, kind", evidence: [] }, can_follow_up: true, asked: "and which clusters?",
    exchange: [
      { question: "which namespaces?", at: 1_700_000_000, answer: { answer: "demo, lab and orders", evidence: [{ source: "kubernetes_list_pods", finding: "3 namespaces", link: null }] } },
      { question: "<b>and pods?</b>", at: 1_700_000_100, answer: null },
    ],
  };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/investigations/t1": [200, question],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo, lab and orders");
  const texts = [...page.querySelectorAll("main p.text, main p.hint")].map((p) => p.textContent);
  expect(texts).toContain("Asked 2023-11-14 22:13 UTC: which namespaces?");
  expect(texts).toContain("Asked 2023-11-14 22:15 UTC: <b>and pods?</b>"); // as text
  expect(texts).toContain("No Answer: that Run failed.");
  expect(texts).toContain("Follow-up: and which clusters?");
  expect(screen.getByText("one cluster, kind")).toBeTruthy();
  expect(page.querySelector("main b")).toBeNull();
});

test("the Contracts page lists every Contract with its spend against its budget", async () => {
  history.replaceState(null, "", "/app/?view=contracts");
  answer({
    "/api/v1/contracts": [200, [{ id: "bank", name: "Bank" }, { id: "shop", name: "Shop" }]],
    "/api/v1/overview": [
      200,
      [
        { id: "bank", name: "Bank", clusters: ["east", "west"], operator_channel: "C0BANK000", model: "anthropic:claude-haiku-5-5", spent_usd: 2.5, budget_usd: 10 },
        { id: "shop", name: "Shop", clusters: [], operator_channel: null, model: null, spent_usd: 0.0052, budget_usd: null },
      ],
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("Contracts", { selector: "h1" });
  const rows = [...page.querySelectorAll(".investigations tbody tr")].map((tr) => tr.textContent);
  expect(rows).toEqual([
    "bankBankeast, westC0BANK000anthropic:claude-haiku-5-5$2.50 of $10.00 (25%)",
    "shopShop--Hub default$0.0052",
  ]);
  expect(screen.getByText("bank").closest("a")?.getAttribute("href")).toBe("/app/?contract=bank");
  expect(screen.queryByText("Turn on the Kill switch")).toBeNull(); // no one Contract here
});

test("the Fleet lists every Contract in its own currency, totals per currency, read-only", async () => {
  history.replaceState(null, "", "/app/?view=fleet");
  const idle = { idle: 1.5, of: 8, cost: 100, not_judged: {}, text: "1.5 of 8 GPUs idle, 100.00 USD/month" };
  answer({
    "/api/v1/contracts": [200, [{ id: "bank", name: "Bank" }, { id: "shop", name: "Shop" }]],
    "/api/v1/fleet": [
      200,
      {
        rows: [
          {
            id: "bank", name: "Bank", kill_switch: true, agenda: { kill_switch: 1, protocol: 1 }, currency: "EUR",
            cost: { waste: 36.5, requests: 73 }, idle_gpus: { ...idle, cost: null, text: "1.5 of 8 GPUs idle (no GPU price: set `gpu` in its prices)" }, unrequested_gpus: { gpus: 1.256, nodes: 1, cost: null },
            parts: [
              { cluster: "eu", part: "Relay", text: "Relay 0.63.0, Protocol 1", ok: true },
              { cluster: "eu", part: "Actuator", text: "Actuator in eu speaks Protocol 3, this Hub 1: upgrade the Hub", ok: false },
            ],
            error: null,
          },
          { id: "dark", name: "Dark", kill_switch: false, agenda: {}, currency: null, cost: null, idle_gpus: null, unrequested_gpus: null, parts: [], error: null },
          { id: "gone", name: "<b>Gone</b>", kill_switch: false, agenda: {}, parts: [], error: "RuntimeError: Prometheus unreachable" },
          {
            id: "shop", name: "Shop", kill_switch: false, agenda: { waste: 3 }, currency: "USD", cost: { waste: 10, requests: 20 },
            idle_gpus: idle, unrequested_gpus: { gpus: 1, nodes: 1, cost: 40 }, parts: [], error: null,
          },
        ],
        totals: [
          { currency: "EUR", contracts: 1, waste: 36.5, requests: 73, idle_gpus: 0, unrequested_gpus: 0 },
          { currency: "USD", contracts: 1, waste: 10, requests: 20, idle_gpus: 100, unrequested_gpus: 40 },
        ],
      },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("Fleet", { selector: "h1" });
  // a card per Contract: its name and Kill switch, then each value under its name
  const cards = [...page.querySelectorAll(".fleet section.contract")].map((c) => c.textContent);
  expect(cards).toEqual([
    "BankKill switch onAgendakill_switch 1, protocol 1Waste36.50 EUR/monthRequests73.00 EUR/monthIdle GPUs1.5 of 8 GPUs idle (no GPU price: set `gpu` in its prices)" +
      "Unrequested GPUs1.26 on 1 nodes, no GPU priceRelays and Actuatorseu: Relay 0.63.0, Protocol 1eu: Actuator in eu speaks Protocol 3, this Hub 1: upgrade the Hub",
    "DarkKill switch offAgendanothingWasteno pricesRequestsno pricesIdle GPUs-Unrequested GPUs-Relays and Actuators-",
    "<b>Gone</b>Couldn't read it: RuntimeError: Prometheus unreachable",
    "ShopKill switch offAgendawaste 3Waste10.00 USD/monthRequests20.00 USD/monthIdle GPUs1.5 of 8 GPUs idle, 100.00 USD/monthUnrequested GPUs1 on 1 nodes, 40.00 USD/monthRelays and Actuators-",
  ]);
  const totals = [...page.querySelectorAll(".fleet .total")].map((c) => c.textContent);
  expect(totals).toEqual([
    "EUR, 1 Contract: waste a month37 EURRequests a month73 EURIdle GPUs a month0 EURUnrequested GPUs a month0 EUR",
    "USD, 1 Contract: waste a month10 USDRequests a month20 USDIdle GPUs a month100 USDUnrequested GPUs a month40 USD",
  ]);
  expect(page.querySelector(".fleet section.contract")?.querySelector("span")?.textContent).toBe("Kill switch on");
  expect([...page.querySelectorAll(".fleet div.bad")].map((d) => d.textContent)).toEqual([
    "eu: Actuator in eu speaks Protocol 3, this Hub 1: upgrade the Hub",
  ]);
  expect(screen.getByText("Bank", { selector: "main a" }).getAttribute("href")).toBe("/app/?contract=bank");
  expect(page.querySelector(".fleet b")).toBeNull(); // Hub text is text
  expect(screen.queryByText("Turn on the Kill switch")).toBeNull(); // read-only, no one Contract
  expect(page.querySelector("main select")).toBeNull(); // no Contract picker
});

const HELD = {
  namespace: "shop", kind: "Deployment", container: "app", automated: true, resumes: null, stopped: false,
  reverting: false, revertable: true, outcome: "applied", cpu: 0.115, memory: 96 * 2 ** 20, pods: 3, reason: "3 of 3 pods resized", at: 1_700_000_000,
};
const AUTOMATION = {
  kill_switch: false,
  clusters: [
    {
      cluster: "kind",
      containers: [
        { ...HELD, name: "api", reason: '<img src=x onerror="alert(1)">' },
        { ...HELD, name: "db", stopped: true, reverting: true, revertable: false, outcome: "reverted" },
        { ...HELD, name: "web", automated: false, revertable: false, outcome: "rolled_back", resumes: 1_700_604_800 },
        { ...HELD, name: "cache", resumes: 1_700_604_800, outcome: "failed" },
        { ...HELD, name: "kept", outcome: "skipped" }, // a skip leaves the earlier hold: still to revert
        { ...HELD, name: "idle", outcome: "skipped", revertable: false },
        { ...HELD, name: "grown", stepped: 228 * 2 ** 20 }, // a Step-up beside its Sizing (slice 13.1c)
        { ...HELD, name: "hot", burst: 0.25 }, // a Burst (slice 13.3b)
      ],
    },
  ],
};

function automating(answers: Record<string, [number, unknown]>, sent: [string, unknown][]) {
  history.replaceState(null, "", "/app/?view=automation");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method === "POST") {
        sent.push([path, init.body ? JSON.parse(String(init.body)) : null]);
        const [status, body] = answers[path] ?? [404, {}];
        return new Response(JSON.stringify(body), { status });
      }
      const routes: Record<string, unknown> = {
        "/api/v1/contracts": [{ id: "demo-shop", name: "demo-shop" }],
        "/api/v1/contracts/demo-shop/automation": AUTOMATION,
      };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
}

test("Automation lists each container's last Actuation as text, and Revert posts that container", async () => {
  const sent: [string, unknown][] = [];
  const after = { ...AUTOMATION, clusters: [{ ...AUTOMATION.clusters[0], containers: [{ ...AUTOMATION.clusters[0].containers[0], reverting: true }] }] };
  automating({ "/api/v1/contracts/demo-shop/automation/revert": [200, after] }, sent);
  const { container: page } = render(<App />);
  expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeTruthy(); // text, never markup
  expect(page.querySelector("img")).toBeNull();
  expect(screen.getByText("stopped by a Revert, until you approve one of its Sizings")).toBeTruthy();
  expect(screen.getByText("ended by 3 rollbacks, resumes 2023-11-21 22:13 UTC")).toBeTruthy();
  expect(screen.getByText("off")).toBeTruthy(); // web: not automated, whatever its rollbacks
  expect(screen.getByText("reverting")).toBeTruthy(); // db: no second Revert
  expect(screen.queryByLabelText("Revert shop/Deployment/db/app")).toBeNull();
  expect(screen.queryByLabelText("Revert shop/Deployment/web/app")).toBeNull(); // rolled back: nothing held
  expect(screen.getByLabelText("Revert shop/Deployment/cache/app")).toBeTruthy(); // failed: pods may hold it
  expect(screen.getByLabelText("Revert shop/Deployment/kept/app")).toBeTruthy(); // by revertable, not its outcome
  expect(screen.queryByLabelText("Revert shop/Deployment/idle/app")).toBeNull();
  expect(screen.getAllByText(/cpu 115m memory 96Mi/).length).toBe(8);
  expect(screen.getByText("Step-up: memory limit 228Mi")).toBeTruthy();
  expect(screen.getByText("Burst: CPU limit 250m")).toBeTruthy();
  expect(screen.getAllByText(/Step-up/).length).toBe(1); // only where there is one
  expect(screen.getAllByText(/Burst/).length).toBe(1);
  fireEvent.click(screen.getByLabelText("Revert shop/Deployment/api/app"));
  await waitFor(() => expect(screen.queryByLabelText("Revert shop/Deployment/api/app")).toBeNull());
  expect(sent).toEqual([
    ["/api/v1/contracts/demo-shop/automation/revert", { cluster: "kind", namespace: "shop", kind: "Deployment", name: "api", container: "app" }],
  ]);
});

test("a refused Revert shows the Hub's words, and the Kill switch says Reverts are refused", async () => {
  const sent: [string, unknown][] = [];
  automating({ "/api/v1/contracts/demo-shop/automation/revert": [409, { detail: "The Contract's Kill switch is on" }] }, sent);
  AUTOMATION.kill_switch = true;
  try {
    render(<App />);
    expect(await screen.findByText("The Kill switch is on: a Revert is refused.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Revert shop/Deployment/api/app"));
    expect(await screen.findByText("Couldn't: The Contract's Kill switch is on")).toBeTruthy();
  } finally {
    AUTOMATION.kill_switch = false;
  }
});

test("idle GPUs show beside the waste, per Workload and per GPU, as text (slice 17.2b)", async () => {
  const gpu = workload("trainer", 0, 0, []);
  const idle = { idle: 1, of: 2, cost: 365, not_judged: { shared: 1 }, text: "1 of 2 GPUs idle, 365.00 EUR/month; 1 not judged (1 shared)" };
  Object.assign(gpu, { gpus: idle });
  Object.assign(gpu.containers[0], {
    gpus: {
      asked: 1,
      gpus: [
        { id: "GPU-<b>a</b>", node: "n1", pod: "trainer-0", busiest: 0.01, by: "engine", hours: 168, memory: 2 ** 30, held: 1, verdict: "idle" },
        { id: "GPU-b", node: "n1", pod: "trainer-1", busiest: null, by: null, hours: 0, memory: null, held: 1, verdict: "unread" },
      ],
    },
  });
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, currency: "EUR", cost: { waste: 1, requests: 2 }, workloads: [gpu, workload("plain", 0.5, 0, [])], gpus: idle }],
    "/api/v1/contracts/demo-shop/history": [200, { points: [] }],
  });
  const { container: page } = render(<App />);
  fireEvent.click(await screen.findByText("Deployment/trainer"));
  const tile = screen.getByText("Idle GPUs", { selector: "main p" });
  expect(tile.nextElementSibling?.textContent).toBe(idle.text); // its figure, beside the waste
  expect(screen.getByText("Idle GPUs", { selector: "th" })).toBeTruthy();
  const cells = [...page.querySelectorAll("tbody tr")].map((tr) => tr.children[7]?.textContent);
  expect(cells).toEqual(["-", idle.text]); // ranked by waste: plain first
  const listed = [...page.querySelectorAll(".one-container details li")].map((li) => li.textContent);
  expect(listed).toEqual([
    "GPU-<b>a</b> on n1 (trainer-0): idle, busiest hour 1% by engine over 168 h, held 100% of the window, memory peak 1024Mi",
    "GPU-b on n1 (trainer-1): unread over 0 h, held 100% of the window",
  ]);
  expect(page.querySelector(".one-container details b")).toBeNull();
});

test("a Contract without GPUs has no Idle GPUs line or column", async () => {
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, DATA],
  });
  render(<App />);
  await screen.findByText("Deployment/memory-heavy");
  expect(screen.queryByText(/Idle GPUs/)).toBeNull();
});

test("the Cost page says the idle GPUs beside the Savings (slice 17.2b)", async () => {
  history.replaceState(null, "", "/app/?view=cost");
  const zero = { cpu: 0, memory: 0 };
  const total = { requests: zero, waste: zero, cost: null };
  const realized = { currency: null, rate: zero, accrued: zero, week: zero, cost: null, drops: [] };
  const gpus = { idle: 0.5, of: 1, cost: null, not_judged: {}, text: "0.5 of 1 GPUs idle" };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/cost": [
      200,
      { contract: "demo-shop", by: "namespace", currency: null, groups: [], groupings: ["namespace"], warnings: [], total, realized, prices: [], gpus },
    ],
  });
  render(<App />);
  const value = await screen.findByText("0.5 of 1 GPUs idle");
  expect(value.previousElementSibling?.textContent).toBe("Idle GPUs");
  expect(screen.getByText("beside the Savings, never in them")).toBeTruthy();
});

test("Nodes shows GPU nodes, their GPUs in what could go and the Unrequested GPUs (slice 17.5b)", async () => {
  history.replaceState(null, "", "/app/?view=nodes");
  const base = { type: "", taints: [], cordoned: false, daemonsets: { cpu: 0, memory: 0 }, pods: 1, bound: 0, allocatable: { cpu: 4, memory: 16 * GI },
    requests: { cpu: 1, memory: GI }, room: { cpu: 3, memory: 15 * GI }, mine: { pods: 1, cpu: 1, memory: GI }, others: { pods: 0, cpu: 0, memory: 0 }, own: [],
    verdict: { state: "clear", why: null, blockers: [] } };
  const went = { nodes: ["g1"], cpu: 4, memory: 16 * GI, gpus: 1, cost: 1460 };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/nodes": [
      200,
      { currency: "USD", clusters: [{ cluster: "kind", at: 1_700_000_000, autoscaler: "karpenter", problem: null, largest: null,
        could_go: { now: went, after: { ...went, cost: null }, note: null, gpu_note: "GPU model not checked: 1 GPU nodes don't carry <b>it</b>" },
        unrequested_gpus: { gpus: 3, nodes: 2, cost: 1095 },
        rules: "Karpenter's rules", reported: 1_700_000_600, judged: { clear: 2, blocked: 0, unknown: 0 }, placement: null, spot: null,
        nodes: [
          { ...base, node: "g1", gpus: { allocatable: 4, capacity: 4, requests: 1, product: "Tesla-T4" } },
          { ...base, node: "n1" },
        ] }] },
    ],
  });
  const { container: page } = render(<App />);
  await screen.findByText("demo-shop: Nodes");
  const could = page.querySelector(".could-go")?.textContent ?? "";
  expect(could).toContain("Could go now: up to 1 (g1), 1,460.00 USD/month with 1 GPUs.");
  expect(could).toContain("with 1 GPUs (no GPU price: set gpu in its prices).");
  expect(could).toContain("GPU model not checked: 1 GPU nodes don't carry <b>it</b>.");
  expect(page.querySelector(".could-go b")).toBeNull();
  const unrequested = [...page.querySelectorAll(".could-go")].map((p) => p.textContent).find((t) => t?.startsWith("Unrequested"));
  expect(unrequested).toContain("Unrequested GPUs: 3 on 2 nodes, 1,095.00 USD/month.");
  const chips = [...page.querySelectorAll("tbody tr")].map((tr) => [...tr.querySelectorAll(".chip")].map((c) => c.textContent));
  expect(chips).toEqual([["1 of 4 GPUs asked, Tesla-T4"], []]);
});

test("a vLLM Workload without an HPA shows its Floor against its replica count (slice 17.4b)", async () => {
  const where = { cluster: "kind", namespace: "lab", kind: "Deployment", policy: "production", percentile: 0.1, risks: [], at: 1_700_000_000 };
  const floors = [
    { ...where, workload: "kind/lab/Deployment/llm", name: "llm", hpa: "", fixed: true, minimum: 4, maximum: 4, state: "ready",
      history_h: 168, needs_h: 24, floor: 2, replicas: 1.75, saving: { cpu: 0.2, memory: GI }, gpu_saving: 3.5, cost: null },
  ];
  const floors_total = { workloads: 1, replicas: 1.75, saving: { cpu: 0.2, memory: GI }, gpus: 3.5, fixed: 1, cost: null };
  answer({
    "/api/v1/contracts": [200, [{ id: "demo-shop", name: "demo-shop" }]],
    "/api/v1/contracts/demo-shop/workloads": [200, { ...DATA, currency: "USD", cost: { waste: 1, requests: 2 }, floors, floors_total }],
  });
  const { container: page } = render(<App />);
  await screen.findByText("Autoscaled Workloads");
  const line = page.querySelector(".floors > ul > li")?.textContent ?? "";
  expect(line).toContain("Floor 2; no HPA scales it: it runs 4 replicas");
  expect(line).toContain("1.75 replicas fewer on average, 0.20 cores, 1.00 GiB, 3.50 GPUs (with an autoscaler holding the Floor, such as KEDA)");
  expect(line).not.toContain("its HPA");
  expect(page.querySelector(".floors .total")?.textContent).toContain(
    "Floors: 1 can run fewer replicas, 1.8 replicas fewer on average: 0.20 cores, 1.00 GiB, 3.50 GPUs",
  );
});
