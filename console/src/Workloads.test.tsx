// The Workloads page restyled (slice 21.3a): a request and its Sizing per pod summed over the
// containers, Automation per Workload, and the filters; the cells' bars and the Total row (21.6c).
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type * as api from "./api";
import App, { automatedOf, perPod } from "./App";
import { kept, share } from "./format";

vi.mock("./Chart", () => ({ default: () => <div>chart</div> }));
vi.mock("./TotalsChart", () => ({ default: () => <div>totals chart</div> }));

beforeEach(() => history.replaceState(null, "", "/app/"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const res = (current: number | null, target: number) => ({ action: "lower", current, target, basis: target });
const box = (container: string, automated: boolean, cpu: [number | null, number] | null, policy = "production") =>
  ({ container, automated, policy, cpu: cpu && res(...cpu), memory: cpu && res(cpu[0] && cpu[0] * 2 ** 30, cpu[1] * 2 ** 30), risks: [] }) as unknown as api.Container;

function workload(name: string, namespace: string, containers: api.Container[], waste = 0.5): api.Workload {
  return {
    workload: `kind/${namespace}/Deployment/${name}`, cluster: "kind", namespace, kind: "Deployment", name,
    pods: 2, waste: { cpu: waste, memory: 0 }, risks: [], containers,
  } as unknown as api.Workload;
}

test("a Workload's request and Sizing are per pod, summed over the containers that have them", () => {
  const w = workload("api", "shop", [box("app", true, [1, 0.25]), box("side", false, [null, 0.1]), box("bare", false, null)]);
  expect(perPod(w, "cpu")).toEqual({ from: 1, to: 0.35, whole: false }); // an unset request counts nothing
  expect(perPod(workload("y", "shop", [box("app", true, [1, 0.25])]), "cpu")?.whole).toBe(true);
  expect(perPod(workload("x", "shop", [box("bare", false, null)]), "cpu")).toBeNull();
});

test("Automation is all, some or none of a Workload's containers", () => {
  expect(automatedOf(workload("a", "s", [box("x", true, null), box("y", true, null)]))).toBe("all");
  expect(automatedOf(workload("a", "s", [box("x", true, null), box("y", false, null)]))).toBe("some");
  expect(automatedOf(workload("a", "s", [box("x", false, null)]))).toBe("none");
});

test("the table shows request → Sizing, the policies and Automation; chips and search filter it", async () => {
  const data = {
    contract: "shop", clusters: { kind: { at: 1_700_000_000, error: null, since: null } }, total: { cpu: 1, memory: 0 },
    currency: null, cost: null, learning: [], excluded: [], prices: [],
    workloads: [
      workload("api", "shop", [box("app", true, [1, 0.25]), box("side", true, [0.5, 0.25], "batch")], 2),
      workload("web", "shop", [box("app", false, [0.5, 0.5])], 1),
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      const routes: Record<string, unknown> = { "/api/v1/contracts": [{ id: "shop", name: "shop" }], "/api/v1/contracts/shop/workloads": data };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
  render(<App />);
  await screen.findByText("Deployment/api");
  const row = (name: string) => screen.getByText(name, { selector: "tbody button" }).closest("tr")!;
  const cells = [...row("Deployment/api").children].map((td) => td.textContent);
  expect(cells.slice(1, 7)).toEqual(["2.00 cores, 0.00 GiB", "1500m → 500m", "1536Mi → 512Mi", "2", "batchproduction", "automated"]);
  expect(row("Deployment/web").children[6].textContent).toBe("no");
  fireEvent.click(screen.getByRole("button", { name: "Not automated" }));
  expect(screen.queryByText("Deployment/api", { selector: "tbody button" })).toBeNull();
  expect(new URLSearchParams(location.search).get("automated")).toBe("no");
  fireEvent.click(screen.getByRole("button", { name: "Automated" }));
  expect(screen.queryByText("Deployment/web", { selector: "tbody button" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  fireEvent.change(screen.getByLabelText("Search Workloads"), { target: { value: "WEB" } });
  expect([...document.querySelectorAll("tbody button")].map((b) => b.textContent)).toEqual(["Deployment/web"]);
  fireEvent.change(screen.getByLabelText("Search Workloads"), { target: { value: "nothing" } });
  expect(screen.getByText("No Workload matches.")).toBeTruthy();
  expect(screen.getByText(/Automation is changed by a pull request/)).toBeTruthy();
});

test("a bar's share: of the largest row shown, or of the request a Sizing keeps; none without one", () => {
  expect(share(5, 10)).toBe(0.5);
  expect(share(10, 10)).toBe(1);
  expect(share(0, 10)).toBeNull(); // nothing to save: no bar
  expect(share(5, 0)).toBeNull();
  expect(share(NaN, 10)).toBeNull();
  expect(kept(1, 0.25)).toBe(0.25);
  expect(kept(1, 1)).toBe(1);
  expect(kept(0.5, 1)).toBeNull(); // a raise: a full bar would read as "fine"
  expect(kept(null, 0.1)).toBeNull(); // an unset request
  expect(kept(0, 0)).toBeNull();
});

test("cells carry bars and the Total row adds the rows shown, money and cores apart (21.6c)", async () => {
  const priced = (w: api.Workload, waste: number) => ({ ...w, cost: { waste, requests: waste * 2 } });
  const data = {
    contract: "shop", clusters: { kind: { at: 1_700_000_000, error: null, since: null } }, total: { cpu: 0, memory: 0 },
    currency: "EUR", cost: { waste: 150, requests: 300 }, learning: [], excluded: [], prices: [],
    gpus: { idle: 0, of: 1, cost: null, text: "0 of 1 GPUs idle" },
    workloads: [
      priced(workload("api", "shop", [box("app", true, [1, 0.25])]), 100),
      priced(workload("web", "shop", [box("app", false, [0.5, 1])]), 50), // its Sizing raises
      workload("job", "batch", [box("app", false, [null, 0.1])], 0.5), // no price for its cluster, no request set
      workload("mix", "batch", [box("app", false, [1, 0.25]), box("side", false, [null, 0.1])], 0.25), // one request unset
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      const routes: Record<string, unknown> = { "/api/v1/contracts": [{ id: "shop", name: "shop" }], "/api/v1/contracts/shop/workloads": data };
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
  render(<App />);
  await screen.findByText("Deployment/api");
  const row = (name: string) => screen.getByText(name, { selector: "tbody button" }).closest("tr")!;
  const bars = (name: string) => [...row(name).querySelectorAll(".bar > div")].map((d) => (d as HTMLElement).style.width);
  expect(bars("Deployment/api")).toEqual(["100%", "25%", "25%"]); // the largest; its Sizings keep a quarter
  expect(bars("Deployment/web")).toEqual(["50%"]); // half the largest; a raise draws none
  expect(bars("Deployment/job")).toEqual(["100%"]); // cores on their own scale; an unset request draws none
  expect(bars("Deployment/mix")).toEqual(["50%"]); // 1000m → 350m is not one container's: no bar under it
  expect(row("Deployment/api").children[1].className).toContain("num");
  expect(row("Deployment/api").children[0].className).toBe("");
  const foot = () => [...document.querySelectorAll("table.workloads tfoot td")].map((td) => td.textContent);
  expect(foot()).toEqual(["Total, 4 workloads", "150.00 EUR/month + 0.75 cores, 0.00 GiB", "", "", "8", "", "", "", ""]);
  expect(document.querySelectorAll("table.workloads thead th")).toHaveLength(9); // with Idle GPUs: a cell under every head
  fireEvent.change(screen.getByLabelText("Search Workloads"), { target: { value: "web" } });
  expect(foot().slice(0, 5)).toEqual(["Total, 1 workload", "50.00 EUR/month", "", "", "2"]); // the rows shown
  expect(bars("Deployment/web")).toEqual(["100%"]); // now the largest shown
  fireEvent.change(screen.getByLabelText("Search Workloads"), { target: { value: "nothing" } });
  expect(document.querySelector("tfoot")).toBeNull();
  expect(screen.getByText(/Bars: under Savings, the row's share of the largest shown/)).toBeTruthy();
});
