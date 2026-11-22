// The Overview (slice 21.2; a grid of cards since 21.6b): each card's numbers from the Hub's own,
// money only with prices, the nodes at a glance, the charts' columns, and a failed read per card.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type * as api from "./api";
import { automation, figures, nodesAtGlance, OverviewPage } from "./Overview";
import { Shell } from "./Shell";
import { columns } from "./series";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const GI = 2 ** 30;
const box = (automated: boolean) => ({ automated }) as api.Container;
const TOTALS = {
  points: [
    { at: 1_700_000_000, cpu: { requests: 4, sizings: 1, usage: 0.5 }, memory: { requests: 8 * GI, sizings: 2 * GI, usage: GI } },
    { at: 1_700_003_600, cpu: { requests: 2, sizings: 1, usage: 0.5 }, memory: { requests: 4 * GI, sizings: 2 * GI, usage: GI } },
  ],
} as api.Totals;

function workloads(priced: boolean, extra: object = {}): api.Workloads {
  return {
    contract: "shop",
    clusters: {},
    total: { cpu: 0.5, memory: GI },
    currency: priced ? "EUR" : null,
    cost: priced ? { requests: 400, waste: 100 } : null,
    workloads: [{ containers: [box(true), box(false), box(true)] }, { containers: [box(false)] }],
    learning: [],
    excluded: [],
    ...extra,
  } as unknown as api.Workloads;
}

const REALIZED = { rate: { cpu: 0.25, memory: GI / 2 }, cost: { rate: 30, accrued: 5, week: 2 } } as api.Allocation["realized"];
const items = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ kind: "waste", title: `item ${i + 1}`, workload: `kind/shop/Deployment/w${i}` }));

function hub(routes: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      return new Response(JSON.stringify(routes[path] ?? { detail: "no" }), { status: path in routes ? 200 : 404 });
    }),
  );
}

test("priced, the tiles are money in the Contract's currency, waste a share of the cost", () => {
  const f = figures({ workloads: workloads(true), realized: REALIZED, totals: TOTALS, agenda: null });
  expect(f).toEqual({
    cost: "400 EUR",
    wasted: "25%",
    share: 0.25,
    available: "100 EUR",
    realized: "30 EUR",
  });
  const none = figures({ workloads: workloads(true), realized: { ...REALIZED, cost: null }, totals: TOTALS, agenda: null });
  expect(none.realized).toBe("none yet");
});

test("without prices, the tiles are cores and GiB from the latest run, never an amount", () => {
  const f = figures({ workloads: workloads(false), realized: REALIZED, totals: TOTALS, agenda: null });
  expect(f).toEqual({
    cost: "2.00 cores, 4.00 GiB", // the newest point's requests
    wasted: "25%", // 0.5 cores wasted of 2 requested
    share: 0.25,
    available: "0.50 cores, 1.00 GiB",
    realized: "0.25 cores, 0.50 GiB",
  });
  const unread = figures({ workloads: workloads(false), realized: REALIZED, totals: null, agenda: null });
  expect([unread.cost, unread.wasted]).toEqual(["not read", "not read"]); // a failed read is no "0%"
  const empty = figures({ workloads: workloads(false), realized: null, totals: { points: [] }, agenda: null });
  expect([empty.cost, empty.wasted, empty.realized]).toEqual(["no reads yet", "0%", "none yet"]);
});

test("a currency without a cost (a cluster without prices) shows cores and GiB, never an amount", () => {
  const f = figures({ workloads: workloads(false, { currency: "EUR" }), realized: REALIZED, totals: TOTALS, agenda: null });
  expect(f.cost).toBe("2.00 cores, 4.00 GiB");
});

test("the ring counts the ready containers a rule automates", () => {
  expect(automation(workloads(true))).toEqual({ automated: 2, ready: 4 });
});

test("in the Shell, its title row counts the clusters and links the workloads (21.6a)", async () => {
  hub({
    "/api/v1/contracts/shop/workloads": workloads(true, { clusters: { a: {}, b: {} } }),
    "/api/v1/contracts/shop/cost": { realized: REALIZED },
    "/api/v1/contracts/shop/totals": TOTALS,
    "/api/v1/contracts/shop/agenda": { contract: "shop", items: [] },
  });
  render(
    <Shell view="overview" here="contract=shop" contracts={[]} shown="shop" onContract={() => {}}>
      <OverviewPage contract="shop" />
    </Shell>,
  );
  expect(await screen.findByText("2 clusters")).toBeTruthy();
  expect(screen.getByText("2 workloads").getAttribute("href")).toBe("/app/?contract=shop");
});

test("the charts' columns are times, then requests, Sizings and usage, in cores or GiB", () => {
  expect(columns(TOTALS, "cpu")).toEqual([[1_700_000_000, 1_700_003_600], [4, 2], [1, 1], [0.5, 0.5]]);
  expect(columns(TOTALS, "memory")[1]).toEqual([8, 4]);
});

const NODES = {
  currency: "EUR",
  clusters: [
    { cluster: "kind", problem: null, nodes: [{}, {}], could_go: { now: { nodes: ["a"] }, after: { nodes: ["a", "b"] } } },
    { cluster: "lab", problem: null, nodes: [{}], could_go: null }, // read, not judged
    { cluster: "edge", problem: "its store is shared", nodes: [], could_go: null },
  ],
} as unknown as api.Nodes;
const BRIEFS = ["one", "two", "three", "four"].map((title, i) => ({
  id: `i${i}`, contract: "shop", kind: "triage", title, started: 1_700_000_000 - i * 60, status: i ? "done" : "running", failed: i === 1,
})) as api.Brief[];
const card = (title: string) => screen.getByText(title, { selector: "h2" }).closest("section")!;
const open = (title: string) => [...card(title).querySelectorAll("a")].at(-1);

test("the nodes at a glance: those read, the clusters not read, what could go where judged", () => {
  expect(nodesAtGlance(NODES)).toEqual({ nodes: 3, clusters: 2, unread: ["edge"], could: { now: 1, after: 2 }, spot: null });
  const unjudged = { ...NODES, clusters: NODES.clusters.slice(1) } as api.Nodes;
  expect(nodesAtGlance(unjudged)).toEqual({ nodes: 1, clusters: 1, unread: ["edge"], could: null, spot: null }); // never "0 could go"
  const spot = { ...NODES, clusters: [{ ...NODES.clusters[0], spot: { nodes: 2 } }, { ...NODES.clusters[1], spot: { nodes: 1 } }] } as unknown as api.Nodes;
  expect(nodesAtGlance(spot).spot).toBe(3); // the Hub's count, where a cluster names a spot label
});

test("the page is six cards, each with its numbers and a link to its page (21.6b)", async () => {
  hub({
    "/api/v1/contracts/shop/workloads": workloads(true, { clusters: { kind: {} }, gpus: { idle: 1, of: 4, cost: 99, text: "1 of 4 GPUs idle" } }),
    "/api/v1/contracts/shop/cost": { realized: REALIZED },
    "/api/v1/contracts/shop/agenda": { contract: "shop", items: items(7) }, // no totals: a priced Contract needs none
    "/api/v1/contracts/shop/nodes": NODES,
    "/api/v1/contracts/shop/investigations": BRIEFS,
  });
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("100 EUR")).toBeTruthy();
  const titles = [...document.querySelectorAll("h2")].map((h) => h.textContent);
  expect(titles).toEqual(["Savings", "Waste", "Automation", "Nodes", "Agenda", "Investigations"]); // the Savings first
  expect(screen.getByText("100 EUR").className).toContain("text-saving"); // the one figure in the accent
  expect(card("Savings").textContent).toContain("available a monthRealized: 30 EUR a month, at today's prices");
  expect(open("Savings")?.textContent).toBe("Open Cost");
  expect(open("Savings")?.getAttribute("href")).toBe("/app/?contract=shop&view=cost");
  expect(card("Waste").textContent).toContain("25%of 400 EUR a month");
  expect(screen.getByText("25%").className).not.toContain("text-saving"); // in ink
  expect(card("Waste").textContent).toContain("Idle GPUs: 1 of 4 GPUs idle, 99 EUR a month; beside the waste, never in it");
  expect(open("Waste")?.getAttribute("href")).toBe("/app/?contract=shop");
  expect(card("Automation").textContent).toContain("50%2 of 4 containers"); // a meter, not a ring (21.5b)
  expect(open("Automation")?.getAttribute("href")).toBe("/app/?contract=shop&view=automation");
  expect(card("Nodes").textContent).toContain("3nodes in 2 clustersCould go: 1 now, 2 after the SizingsNot read: edge");
  expect(open("Nodes")?.getAttribute("href")).toBe("/app/?contract=shop&view=nodes");
  expect(card("Agenda").textContent).toContain("What needs you now7");
  const listed = () => [...document.querySelectorAll(".agenda li")].map((li) => li.textContent);
  expect(listed()).toEqual(["item 1", "item 2", "item 3"].map((t, i) => `kind/shop/Deployment/w${i}: ${t}`));
  expect(card("Agenda").textContent).not.toContain("Open"); // the Agenda has no page: all of it opens here
  fireEvent.click(screen.getByText("See all 7"));
  expect(listed()).toHaveLength(7);
  const latest = [...card("Investigations").querySelectorAll(".latest li")];
  expect(latest.map((li) => li.querySelector("a")?.textContent)).toEqual(["one", "two", "three"]);
  expect(latest.map((li) => li.querySelector("span")?.textContent)).toEqual(["running", "failed", "done"]);
  expect(latest[1].querySelector("a")?.getAttribute("href")).toBe("/app/?view=investigation&id=i1");
  expect(open("Investigations")?.getAttribute("href")).toBe("/app/?contract=shop&view=investigations");
  expect(screen.queryByText("CPU over time")).toBeNull(); // the charts are on the Cost page
  expect(screen.queryByText("No Sizings stored yet.")).toBeNull(); // it has clusters
});

test("unpriced, cores and GiB; a card whose read failed says so, the rest stand", async () => {
  hub({
    "/api/v1/contracts/shop/workloads": workloads(false),
    "/api/v1/contracts/shop/totals": TOTALS,
    "/api/v1/contracts/shop/agenda": { contract: "shop", items: [] },
    "/api/v1/contracts/shop/investigations": [],
  });
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("0.50 cores, 1.00 GiB")).toBeTruthy(); // the Savings available, no money
  expect(card("Savings").textContent).toContain("availableRealized: none yetOpen Cost"); // the cost read failed: no error
  expect(card("Waste").textContent).toContain("25%of CPU requests (2.00 cores, 4.00 GiB)");
  expect(card("Waste").textContent).not.toContain("Idle GPUs");
  expect(card("Nodes").textContent).toContain("Couldn't read the nodes.");
  expect(open("Nodes")?.textContent).toBe("Open Nodes"); // its page may say why
  expect(screen.getByText("Nothing needs you now.")).toBeTruthy();
  expect(card("Investigations").textContent).toContain("None yet.");
  cleanup();
  hub({ "/api/v1/contracts/shop/workloads": workloads(false), "/api/v1/contracts/shop/totals": TOTALS, "/api/v1/contracts/shop/nodes": { currency: null, clusters: [] } });
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("Couldn't read the Agenda.")).toBeTruthy();
  expect(screen.getByText("Couldn't read the Investigations.")).toBeTruthy();
  expect(card("Nodes").textContent).toContain("No nodes read yet: the hourly run reads them."); // never a 0
  expect(screen.getByText("No Sizings stored yet.")).toBeTruthy(); // no cluster stored: why, and where to look
  expect(screen.getByText("Check Health").getAttribute("href")).toBe("/app/?contract=shop&view=health");
  cleanup();
  hub({ "/api/v1/contracts/shop/workloads": workloads(false) }); // unpriced, and the totals read failed
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("Couldn't read the hourly totals.")).toBeTruthy(); // never "0%"
  expect(card("Waste").textContent).not.toContain("%");
  expect(card("Savings").textContent).toContain("0.50 cores, 1.00 GiB"); // the rest stands
});

test("while it loads, a flat block in its shape, never an empty page", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // the Hub never answers
  render(<OverviewPage contract="shop" />);
  expect(screen.getByLabelText("Loading").getAttribute("aria-busy")).toBe("true");
});

test("a Hub that doesn't answer says so", async () => {
  hub({});
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText(/The Hub didn't answer/)).toBeTruthy();
});
