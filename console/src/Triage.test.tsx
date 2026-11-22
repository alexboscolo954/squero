// The triage pages restyled (slice 21.4): an Investigation's status as a coloured badge, each
// integration's Health as a tile (a failing one framed red), and the buttons that act marked
// primary or danger.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { HealthPage } from "./Checks";
import { InvestigationList } from "./Investigations";
import { TeachingsPage } from "./Teachings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function hub(routes: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      return new Response(JSON.stringify(routes[path] ?? {}), { status: path in routes ? 200 : 404 });
    }),
  );
}

const brief = (id: string, status: string, failed = false) =>
  ({ id, contract: "shop", kind: "triage", title: `t ${id}`, status, failed, started: 1_700_000_000 });

test("an Investigation's status is green when done, red when failed, grey otherwise", async () => {
  hub({ "/api/v1/contracts/shop/investigations": [brief("a", "done"), brief("b", "done", true), brief("c", "waiting_approval")] });
  render(<InvestigationList contract="shop" />);
  expect((await screen.findByText("done")).className).toContain("text-saving");
  expect(screen.getByText("failed").className).toContain("text-bad");
  expect(screen.getByText("waiting approval").className).toContain("text-muted");
  expect(screen.getByText("Ask").className).toBe("primary");
});

test("each integration is a tile; a failing one is framed red and says FAILING", async () => {
  hub({
    "/api/v1/contracts/shop/health": {
      integrations: [
        { name: "kubernetes kind", ok: true, ms: 12, detail: "" },
        { name: "jira", ok: false, ms: 5003, detail: "HTTP 401 from jira.example" },
      ],
      last: null, config: "", secrets: {}, spent_usd: 0, budget_usd: null, hub: "",
    },
  });
  render(<HealthPage contract="shop" />);
  const tile = (name: string) => screen.getByText(name, { selector: "h2" }).closest("section")!;
  expect((await screen.findByText("jira", { selector: "h2" })).closest("section")?.className).toContain("border-bad");
  expect(tile("kubernetes kind").className).not.toContain("border-bad");
  expect(tile("kubernetes kind").textContent).toBe("kubernetes kindok12 ms");
  expect(tile("jira").textContent).toBe("jiraFAILING5003 msHTTP 401 from jira.example");
});

test("a Teaching's approve and add are primary, reject and retire danger", async () => {
  const teaching = { alerts: [], source_id: null, correction: null, contradicts: null, decided_by: null, decided_at: null };
  hub({
    "/api/v1/contracts/shop/teachings": {
      limit: 500, left_out: 0,
      proposed: [{ ...teaching, id: 2, status: "proposed", text: "check the node" }],
      approved: [{ ...teaching, id: 1, status: "approved", text: "restart the cache", decided_by: "slack:U1", decided_at: 1 }],
      past: [],
    },
  });
  render(<TeachingsPage contract="shop" />);
  expect((await screen.findByText("Approve this text")).className).toBe("primary");
  expect(screen.getByText("Reject").className).toBe("danger");
  expect(screen.getByText("Retire").className).toBe("danger");
  expect(screen.getByText("Add, approved").className).toBe("primary");
});
