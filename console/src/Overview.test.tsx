// The Overview (slice 21.2): the tiles from the Hub's own numbers, money only with prices, the
// Automation ring, the charts' columns, and the Agenda's first items.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type * as api from "./api";
import { automation, figures, OverviewPage } from "./Overview";
import { columns } from "./series";

vi.mock("./TotalsChart", () => ({ default: ({ res }: { res: string }) => <div>{res} chart</div> }));

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

test("the charts' columns are times, then requests, Sizings and usage, in cores or GiB", () => {
  expect(columns(TOTALS, "cpu")).toEqual([[1_700_000_000, 1_700_003_600], [4, 2], [1, 1], [0.5, 0.5]]);
  expect(columns(TOTALS, "memory")[1]).toEqual([8, 4]);
});

test("the page shows the tiles, the ring, both charts and the Agenda's first five", async () => {
  hub({
    "/api/v1/contracts/shop/workloads": workloads(true, { gpus: { idle: 1, of: 4, cost: 99, text: "1 of 4 GPUs idle" } }),
    "/api/v1/contracts/shop/cost": { realized: REALIZED },
    "/api/v1/contracts/shop/totals": TOTALS,
    "/api/v1/contracts/shop/agenda": { contract: "shop", items: items(7) },
  });
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("400 EUR")).toBeTruthy();
  expect(screen.getByText("Monthly cost")).toBeTruthy();
  expect(screen.getByText("1 of 4 GPUs idle")).toBeTruthy();
  expect(screen.getByText("99 EUR a month")).toBeTruthy(); // the idle GPUs' cost
  expect(screen.getByText("7", { selector: "p" }).nextElementSibling?.textContent).toBe("On the Agenda");
  expect(screen.getByLabelText("containers automated: 2 of 4")).toBeTruthy();
  expect(screen.getByText("cpu chart")).toBeTruthy();
  expect(screen.getByText("memory chart")).toBeTruthy();
  const listed = [...document.querySelectorAll(".agenda li")].map((li) => li.textContent);
  expect(listed).toEqual(["item 1", "item 2", "item 3", "item 4", "item 5"].map((t, i) => `kind/shop/Deployment/w${i}: ${t}`));
  expect(screen.getByText("See all 7").getAttribute("href")).toBe("/app/?contract=shop");
});

test("no GPUs, no GPU tile; no stored runs, no chart; a failed cost read is no error", async () => {
  hub({
    "/api/v1/contracts/shop/workloads": workloads(false),
    "/api/v1/contracts/shop/totals": { points: [] },
    "/api/v1/contracts/shop/agenda": { contract: "shop", items: [] },
  });
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText("Requests", { selector: "p" })).toBeTruthy(); // unpriced: not "Monthly cost"
  expect(screen.queryByText("Monthly cost")).toBeNull();
  expect(screen.queryByText("Idle GPUs")).toBeNull();
  expect(screen.getAllByText("No hourly run stored in the last 14 days.")).toHaveLength(2);
  expect(screen.getByText("Savings realized").previousElementSibling?.textContent).toBe("none yet");
  expect(screen.getByText("Nothing needs you now.")).toBeTruthy();
});

test("a Hub that doesn't answer says so", async () => {
  hub({});
  render(<OverviewPage contract="shop" />);
  expect(await screen.findByText(/The Hub didn't answer/)).toBeTruthy();
});
