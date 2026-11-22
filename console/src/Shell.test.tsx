// The console's frame (slice 21.1): the sidebar's links, the Contract switcher, the collapse
// that is remembered, the header naming the page, and the title row (slice 21.6a).
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PageTools, Shell, title } from "./Shell";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

const TWO = [
  { id: "shop", name: "Demo shop" },
  { id: "bank", name: "Demo bank" },
] as never[];

function shell(view: string, contracts = TWO, onContract = vi.fn()) {
  render(
    <Shell view={view} here="contract=bank" contracts={contracts} shown="bank" onContract={onContract}>
      <p>the page</p>
      <PageTools>
        <span>2 clusters</span>
      </PageTools>
    </Shell>,
  );
  return onContract;
}

const link = (label: string) => screen.getByText(label, { selector: "nav a span" }).closest("a")!;

test("a Contract's pages link with it, the pages across Contracts without", () => {
  shell("cost");
  expect(link("Workloads").getAttribute("href")).toBe("/app/?contract=bank");
  expect(link("Cost").getAttribute("href")).toBe("/app/?contract=bank&view=cost");
  expect(link("Health").getAttribute("href")).toBe("/app/?contract=bank&view=health");
  expect(link("Overview").getAttribute("href")).toBe("/app/?contract=bank&view=overview");
  expect(screen.getByText("squero").closest("a")?.getAttribute("href")).toBe("/app/?contract=bank&view=overview");
  expect(link("Fleet").getAttribute("href")).toBe("/app/?view=fleet");
  expect(link("Contracts").getAttribute("href")).toBe("/app/?view=contracts");
  expect(link("Approver token").getAttribute("href")).toBe("/app/?view=token");
  const current = [...document.querySelectorAll("nav a[aria-current='page']")];
  expect(current.map((a) => a.textContent)).toEqual(["Cost"]);
  expect(screen.getByText("the page", { selector: "main p" })).toBeTruthy();
});

test("the groups are Overview, Optimization, Investigations and Settings", () => {
  shell("");
  const groups = [...document.querySelectorAll("nav > div > p")].map((p) => p.textContent);
  expect(groups).toEqual(["Overview", "Optimization", "Investigations", "Settings"]);
  expect(link("Workloads").getAttribute("aria-current")).toBe("page");
});

test("an Investigation is under Investigations, names no Contract and hides the switcher", () => {
  shell("investigation"); // its address has no Contract: "bank" is only the first one
  expect(link("Investigations").getAttribute("aria-current")).toBe("page");
  expect(document.querySelector("header")?.textContent).toBe("Investigations");
  expect(screen.queryByLabelText("Contract")).toBeNull();
  expect(document.querySelector("h1")).toBeNull(); // its own page holds its title
  expect(screen.queryByText("2 clusters")).toBeNull();
  expect(title("investigation")).toBe("Investigations");
  expect(title("nonsense")).toBe("Workloads");
});

test("the header names the Contract on its pages, and none on a page across Contracts", () => {
  shell("teachings");
  expect(document.querySelector("header")?.textContent).toBe("Demo bankTeachings"); // a chevron between
  cleanup();
  shell("fleet");
  expect(document.querySelector("header")?.textContent).toBe("Fleet");
});

test("the switcher picks another Contract in the title row; with one Contract the header names it", () => {
  const picked = shell("");
  fireEvent.change(screen.getByLabelText("Contract"), { target: { value: "shop" } });
  expect(picked).toHaveBeenCalledWith("shop");
  expect(screen.getByLabelText("Contract").closest("main")).toBeTruthy();
  cleanup();
  shell("", [{ id: "bank", name: "Demo bank" }] as never[]);
  expect(screen.queryByLabelText("Contract")).toBeNull();
  expect(document.querySelector("header")?.textContent).toBe("Demo bankWorkloads");
  cleanup();
  shell("fleet"); // a page across Contracts picks none
  expect(screen.queryByLabelText("Contract")).toBeNull();
});

test("the title row names the page with its icon, the Contract for screen readers, and the page's counts", () => {
  shell("nodes");
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(h1.textContent).toBe("Demo bank: Nodes");
  expect(h1.querySelector("span")?.className).toBe("sr-only");
  expect(h1.querySelector("svg")).toBeTruthy();
  expect(screen.getByText("2 clusters").parentElement?.parentElement).toBe(h1.parentElement); // drawn beside it
  expect(document.querySelectorAll("h1")).toHaveLength(1);
  cleanup();
  shell("fleet");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Fleet");
});

test("collapsed, the sidebar keeps its links' names for screen readers and remembers it", () => {
  shell("");
  fireEvent.click(screen.getByLabelText("Collapse the sidebar"));
  expect(link("Cost").getAttribute("title")).toBe("Cost");
  expect(screen.getByText("Cost", { selector: "nav a span" }).className).toContain("sr-only");
  expect(screen.getByLabelText("Contract")).toBeTruthy(); // in the title row, not the sidebar
  expect(localStorage.getItem("squero.sidebar")).toBe("collapsed");
  cleanup();
  shell("");
  expect(screen.getByLabelText("Open the sidebar")).toBeTruthy(); // still collapsed
  fireEvent.click(screen.getByLabelText("Open the sidebar"));
  expect(link("Cost").getAttribute("title")).toBeNull();
  expect(localStorage.getItem("squero.sidebar")).toBe("open");
});

test("a browser that refuses storage still opens the sidebar", () => {
  const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("denied");
  });
  const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("denied");
  });
  shell("");
  expect(screen.getByLabelText("Collapse the sidebar")).toBeTruthy();
  fireEvent.click(screen.getByLabelText("Collapse the sidebar")); // never throws
  expect(screen.getByLabelText("Open the sidebar")).toBeTruthy();
  get.mockRestore();
  set.mockRestore();
});
