// The Workloads page restyled (slice 21.3a): a request and its Sizing per pod summed over the
// containers, Automation per Workload, and the filters.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type * as api from "./api";
import App, { automatedOf, perPod } from "./App";

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
  expect(perPod(w, "cpu")).toEqual({ from: 1, to: 0.35 }); // an unset request counts nothing
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
