// The console's frame (slice 21.1): a sidebar of grouped pages with the Contract switcher at its
// top, a header naming the page with the Kill switch beside it, and the page itself. Laid out after
// ScaleOps' product UI; every page inside is still its own.
import {
  Bot,
  Boxes,
  Coins,
  GraduationCap,
  HeartPulse,
  KeyRound,
  LayoutGrid,
  ListChecks,
  type LucideIcon,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  Search,
  Server,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import type * as api from "./api";
import { cn } from "./ui/cn";

type Page = { view: string; label: string; icon: LucideIcon; across?: boolean };

/** The sidebar's groups. `across`: a page over every Contract, so its link names none. */
export const GROUPS: { title: string; pages: Page[] }[] = [
  { title: "Overview", pages: [{ view: "fleet", label: "Fleet", icon: LayoutGrid, across: true }] },
  {
    title: "Optimization",
    pages: [
      { view: "", label: "Workloads", icon: Boxes },
      { view: "cost", label: "Cost", icon: Coins },
      { view: "nodes", label: "Nodes", icon: Server },
      { view: "automation", label: "Automation", icon: Bot },
    ],
  },
  {
    title: "Investigations",
    pages: [
      { view: "investigations", label: "Investigations", icon: Search },
      { view: "teachings", label: "Teachings", icon: GraduationCap },
      { view: "checks", label: "Checks", icon: ListChecks },
      { view: "health", label: "Health", icon: HeartPulse },
    ],
  },
  {
    title: "Settings",
    pages: [
      { view: "contracts", label: "Contracts", icon: ScrollText, across: true },
      { view: "token", label: "Approver token", icon: KeyRound, across: true },
    ],
  },
];

/** The page's name for the header: an Investigation is under Investigations. */
export function title(view: string): string {
  const shown = view === "investigation" ? "investigations" : view;
  return GROUPS.flatMap((g) => g.pages).find((p) => p.view === shown)?.label ?? "Workloads";
}

const KEY = "squero.sidebar";

function remembered(): boolean {
  try {
    return localStorage.getItem(KEY) === "collapsed";
  } catch {
    return false; // storage refused (a private window): the sidebar opens
  }
}

function remember(collapsed: boolean) {
  try {
    localStorage.setItem(KEY, collapsed ? "collapsed" : "open");
  } catch {
    // a convenience only
  }
}

/** squero's ferro, the logo, in the sidebar's colours. */
function Ferro() {
  return (
    <svg viewBox="-3 0 64 64" className="size-7 shrink-0" aria-hidden="true">
      <g fill="currentColor">
        <path d="M28 50 V30 C26 20 29 9 43 1 C42 8 42.5 15 42.5 20 V30 H40.5 A2.5 2.5 0 0 0 35.5 30 H35 V50 Z" />
        <path d="M28 49 C28 55 23 60 12 63 C25 62 33 58 35 49 Z" />
        <rect x="19" y="26" width="10" height="3" rx="1.5" />
      </g>
      <g fill="#B23A2E">
        {[29.5, 33.5, 37.5, 41.5, 45.5, 49.5].map((y, i) => (
          <rect key={y} x="34" y={y} width={i < 4 ? 12 : 15 - i} height="2.8" rx="1.4" />
        ))}
      </g>
    </svg>
  );
}

function ContractSwitcher({
  contracts,
  shown,
  onContract,
  collapsed,
}: {
  contracts: api.ContractSummary[];
  shown: string;
  onContract: (id: string) => void;
  collapsed: boolean;
}) {
  if (collapsed || !shown) return null;
  if (contracts.length < 2) {
    const one = contracts.find((c) => c.id === shown);
    return <p className="m-0 truncate px-3 text-sm text-rail-text">{one?.name ?? shown}</p>;
  }
  return (
    <label className="flex flex-col gap-1 px-3 text-xs font-medium tracking-wide text-rail-muted uppercase">
      Contract
      <select
        value={shown}
        onChange={(e) => onContract(e.target.value)}
        className="rounded-md border border-rail-hover bg-rail-hover px-2 py-1.5 text-sm font-normal tracking-normal text-rail-text normal-case"
      >
        {contracts.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Shell({
  view,
  here,
  contracts,
  shown,
  onContract,
  actions,
  children,
}: {
  view: string;
  /** `contract=<id>` for the links of a Contract's pages, or nothing. */
  here: string;
  contracts: api.ContractSummary[];
  shown: string;
  onContract: (id: string) => void;
  /** The header's right side: the Kill switch, on a Contract's page. */
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(remembered);
  const toggle = () => {
    remember(!collapsed);
    setCollapsed(!collapsed);
  };
  const current = view === "investigation" ? "investigations" : view;
  // One Investigation's address names no Contract (it may be another's than the one picked):
  // like a page across Contracts, its header names none and the switcher hides.
  const across = view === "investigation" || GROUPS.flatMap((g) => g.pages).some((p) => p.across && p.view === view);
  const name = across ? "" : (contracts.find((c) => c.id === shown)?.name ?? "");
  const href = (p: Page) =>
    p.across ? `/app/?view=${p.view}` : `/app/?${here}${p.view ? `&view=${p.view}` : ""}`;
  return (
    <div className="flex min-h-screen bg-page text-ink">
      <aside
        className={cn(
          "sticky top-0 flex h-screen shrink-0 flex-col gap-5 overflow-y-auto bg-rail py-4 text-rail-text",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <a href="/app/" className="flex items-center gap-2 px-4 text-lg font-semibold text-white hover:no-underline">
          <Ferro />
          <span className={cn(collapsed && "sr-only")}>squero</span>
        </a>
        {view !== "investigation" && (
          <ContractSwitcher contracts={contracts} shown={shown} onContract={onContract} collapsed={collapsed} />
        )}
        <nav aria-label="Pages" className="flex flex-1 flex-col gap-4">
          {GROUPS.map((g) => (
            <div key={g.title} className="flex flex-col gap-0.5">
              <p className={cn("m-0 px-4 pb-1 text-xs font-medium tracking-wide text-rail-muted uppercase", collapsed && "sr-only")}>
                {g.title}
              </p>
              {g.pages.map((p) => {
                const here = p.view === current;
                return (
                  <a
                    key={p.label}
                    href={href(p)}
                    aria-current={here ? "page" : undefined}
                    title={collapsed ? p.label : undefined}
                    className={cn(
                      "mx-2 flex items-center gap-3 rounded-md px-2 py-1.5 text-sm text-rail-text hover:bg-rail-hover hover:no-underline",
                      here && "bg-rail-hover font-semibold text-white",
                      collapsed && "justify-center",
                    )}
                  >
                    <p.icon className="size-4 shrink-0" aria-hidden="true" />
                    <span className={cn(collapsed && "sr-only")}>{p.label}</span>
                  </a>
                );
              })}
            </div>
          ))}
        </nav>
        <button
          type="button"
          onClick={toggle}
          aria-label={collapsed ? "Open the sidebar" : "Collapse the sidebar"}
          className="mx-2 flex cursor-pointer items-center gap-3 rounded-md border-0 bg-transparent px-2 py-1.5 text-sm text-rail-muted hover:bg-rail-hover"
        >
          {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          <span className={cn(collapsed && "sr-only")}>Collapse</span>
        </button>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex flex-wrap items-center gap-3 border-b border-line bg-panel/90 px-6 py-3 backdrop-blur">
          {/* Not a heading: each page keeps its own h1 until 21.3 and 21.4 restyle it. */}
          <p className="m-0 mr-auto flex items-center gap-2 text-sm text-muted">
            {name && <span>{name}</span>}
            {name && <span aria-hidden="true">/</span>}
            <span className="font-semibold text-ink">{title(view)}</span>
          </p>
          {actions}
        </header>
        <main className="mx-auto w-full max-w-7xl px-6 pt-5 pb-12">{children}</main>
      </div>
    </div>
  );
}
