# Phase 21: Console design

**In short:** the console works but looks like raw HTML: a link bar, headings and bare forms. This phase gives it a real dashboard design, in the style of ScaleOps' product UI, without changing what any page does. It is built next, before Phase 19 continues.

**Status:** grilled 2026-11-21; nothing built yet.

**Goal:** a console the Operator is glad to open every day: a sidebar, an Overview dashboard, and every page laid out as number tiles, charts and tables in cards.

**Done when:** every page of `/app/` uses the new shell and components, in dark and light themes, with no API change beyond the Overview's one read-only endpoint, and `make console-check` green.

| Slice | What | Status |
|---|---|---|
| 21.1 | The shell: sidebar, header, Contract switcher, Kill switch, design tokens and components | ⏳ |
| 21.2 | The Overview dashboard | ⏳ |
| 21.3 | The optimizer pages restyled: Workloads, Cost, Nodes, Automation, Fleet | ⏳ |
| 21.4 | The triage pages restyled: Investigations, Teachings, Checks, Health, Contracts | ⏳ |

## Why this phase exists

- No slice ever covered how the console looks. ScaleOps was recorded only as the optimizer's inspiration for features (ADR 0004, [decisions](decisions.md)).
- So each console slice (9.5a, 11.5 to 11.7) added its page with near-default styling.

## The reference: ScaleOps' product UI

Read 2026-11-21 from the product recordings on scaleops.com (the site refuses plain fetches, so it was read in a browser). Every optimizer page there is the same three bands, white rounded cards on a light grey page:

1. **Number tiles.** A big number over a small grey label. Money in indigo ("Monthly cost"), waste in pink with a thin bar ("Wasted spend"), savings in green, counts in near-black, and a green "Automated 86%" ring.
2. **Charts.** "CPU over time" and "Memory over time" side by side. Usage is a blue fill, request an amber line, waste a pink fill between them, the optimized request a green line. Legend dots under the charts.
3. **A table.** Filter chips (automated, un-automated, namespaces, labels) and a search box. Rows read "0.9 → **0.4**" with the new value in green, the policy as a lavender pill, and an "Automated" toggle.

Other views follow the same pattern: a Workload's detail (policy, period and container selectors over one chart), Nodes (a consolidation card with "before → after" and an instance-type table), a multi-cluster savings table, and a cluster picker with search and select-all chips.

## Decisions (grill, 2026-11-21)

Two rounds, all answers the recommended ones.

- **When:** a new phase, built now, before 19.1b. *Why:* it is what the Operator looks at every day.
- **The look:** ScaleOps' layout, as above.
  - A sidebar with grouped sections (Overview, Optimization, Investigations, Settings), the Contract switcher at its top.
  - An Overview dashboard of number tiles and charts.
  - Cards for each section, tables with status badges, charts in cards.
  - The Kill switch as a clear red control in the header, not a grey button.
- **Sidebar:** icons and labels, in groups, collapsible to an icon rail. *Why:* with about 11 pages, labels matter; ScaleOps' icon-only rail suits fewer pages.
- **How it is built:** Tailwind CSS plus shadcn/ui, its components copied into the repo (not a runtime library).
  - Within the console's rules: strict CSP, nothing from a CDN, built in the Docker build, model-written text rendered as text.
- **Themes:** dark and light, following the system setting.
- **Colours:** ScaleOps' meanings with squero's own primary accent.
  - Money indigo, savings green, waste pink, request amber, usage blue.
  - The accent is a design token, tunable later.
- **Automation shown, never toggled.** Automation is a Sizing rule in the contracts repo, changed by a pull request ([ADR 0004](../adr/0004-standing-approval-for-sizing.md)).
  - The ring and each row's state are read-only badges.
  - A row's menu offers "Propose Automation…", the existing pull-request flow.
  - *Why:* a toggle that only opens a pull request would mislead.
- **Charts:** uPlot stays, styled with the series scheme above (usage fill, request line, waste fill, Sizing line).
- **The Overview's charts** need one new read-only endpoint: a Contract's CPU and memory over time, summed from the stored Workload series. Nothing else in the API changes.
- **No eval rerun:** nothing the agent sees changes.

## Slices

### 21.1 The shell

**What:** the frame every page sits in.
- Tailwind and shadcn/ui set up in `console/`, design tokens for the colours, both themes.
- The sidebar (grouped, collapsible), the header with the Contract switcher and the Kill switch.
- Shared components: number tile, chart card, table card with chips and search, badge, pill, "before → after" cell.
- Every existing page moved into the shell unchanged inside.

### 21.2 The Overview

**What:** the landing page for a Contract.
- Number tiles: monthly cost, wasted spend, realized Savings, potential Savings, Idle GPUs, Agenda items, the Automated ring.
- CPU and memory over time, from the new read-only endpoint.
- The Agenda as a short list linking to its pages.

### 21.3 The optimizer pages

**What:** Workloads, Cost, Nodes, Automation and Fleet in the three-band layout.
- Workloads: tiles, charts, and the table with "request → Sizing", the policy pill, the Automation badge and its menu.
- Nodes: the nodes that could go as a "before → after" card, Blockers as badges.
- Fleet: one card per Contract, totals per currency (never converted, as before).

### 21.4 The triage pages

**What:** Investigations, Teachings, Checks, Health and Contracts.
- Lists as tables with status badges; a Report's page as cards (findings, Evidence, Proposed actions with their Approval buttons).
- Forms (a Question, a Teaching) as proper form cards.
- Health as status tiles per integration and part.
