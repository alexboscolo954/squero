# Phase 21: Console design

**In short:** the console works but looks like raw HTML: a link bar, headings and bare forms. This phase gives it a real dashboard design, in the style of ScaleOps' product UI, without changing what any page does. It is built next, before Phase 19 continues.

**Status:** grilled 2026-11-21; 21.1 and 21.2 built.

**Goal:** a console the Operator is glad to open every day: a sidebar, an Overview dashboard, and every page laid out as number tiles, charts and tables in cards.

**Done when:** every page of `/app/` uses the new shell and components, in dark and light themes, with no API change beyond the Overview's one read-only endpoint, and `make console-check` green.

| Slice | What | Status |
|---|---|---|
| [21.1](#211-the-shell) | The shell: sidebar, header, Contract switcher, Kill switch, design tokens and components | ✅ built |
| [21.2](#212-the-overview) | The Overview dashboard | ✅ built |
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
- The shared components (number tile, chart card, table card with chips and search, badge, pill, "before → after" cell) come with the first page that uses them: 21.2 and 21.3.
- Every existing page moved into the shell unchanged inside.

**Built (2026-11-21):**
- Tailwind CSS 4 (theme and utilities, without its reset, so the pages' own rules still hold) and shadcn/ui's helpers (`clsx`, `tailwind-merge`, `class-variance-authority`) and `lucide-react` icons, each pinned and at least a week old.
- `styles.css`: the colours as tokens for both themes (money, saving, waste, request, usage, the accent, the sidebar's); the pages' old rules in a `legacy` layer, so a utility always wins over them.
- `Shell.tsx`: the sidebar in its four groups, the Contract switcher at its top (a name alone with one Contract), "Collapse" to icons (remembered; a browser that refuses storage still works), the header naming the Contract and the page.
- The Kill switch in the header, on a Contract's own pages only: a red-outlined "Turn on the Kill switch", or a red line under the header while it is on.
- `ui/button.tsx`, the one component the shell uses.

**Choices made while building, for the owner to confirm:**
- The shared components arrive with the first page that uses them (21.2, 21.3), not ahead of it.
- The header is not a heading: each page keeps its own `h1` until 21.3 and 21.4 restyle it.
- Icons from `lucide-react`.
- The Contract switcher shows on every page but one Investigation's, whose address names no Contract: there the header names none either.
- The page is wider: 1280 px, from 1120.

**Tests:** 7 in `Shell.test.tsx` (links with and without the Contract, the groups, an Investigation under Investigations, the header, the switcher, the remembered collapse, refused storage) and one in `App.test.tsx` (the Kill switch on a Contract's pages only); 4 existing tests scoped to the page. 14 mutations, 13 caught, 1 equivalent (the token page returns before the Kill switch is decided; the redundant check was removed).

**Checked:** in Chrome on a local build with a stub API and the Hub's CSP: the sidebar, the collapse, the switcher, the Kill switch off and on, and the Teachings page (with the tests' Teachings) inside the frame; no CSP violation. The dark theme only: the light one was not viewed. The old `container` class of a Workload's detail renamed `one-container`, since Tailwind's utility of that name would have narrowed it.

### 21.2 The Overview

**What:** the landing page for a Contract.
- Number tiles: monthly cost, wasted spend, realized Savings, potential Savings, Idle GPUs, Agenda items, the Automated ring.
- CPU and memory over time, from the new read-only endpoint.
- The Agenda as a short list linking to its pages.

**Decided (grill, 2026-11-21, all recommended):**
- The Overview is a page of its own, `view=overview`, first in the sidebar; the logo links to it. Plain `/app/?contract=x` stays Workloads, so every link already sent (Agenda, Fleet, Slack) still works.
- The tiles: Monthly cost (the requests', indigo), Wasted spend (the waste's share of it, pink, with a bar), Savings available (the waste's cost) and realized (the monthly rate of requests lowered), Idle GPUs (only with GPUs), the Agenda's count, and the Automated ring. Without prices, cores and GiB, never an amount.
- The ring: automated containers of the ready ones. Automated: its Sizing rule says `automate: true` and its cluster runs an Actuator.
- The charts: 14 days, hourly, summed over the ready containers times their pods; requests amber, Sizings green, usage (the Sizings' basis) a blue fill, the waste pink between requests and Sizings.
- The Agenda stays on the Workloads page too, until 21.3.

**Built (2026-11-21):**
- `GET /api/v1/contracts/{id}/totals` (`webapp.totals_of`, `Store.sizing_totals`): one SQL sum per stored run over the last 14 days, of the Contract's own ready rows only; a container without CPU or memory counts nothing of it.
- Each container in the Workloads carries `automated` (its rule, and its cluster's Actuator), learning ones too.
- `Overview.tsx`: the tiles (`ui/card.tsx`: `Card`, `Tile`, `Ring`), `TotalsChart.tsx` (uPlot, its colours read from the theme's tokens, the waste a band), and the Agenda's first five with "See all N" (the Agenda's list moved to `Agenda.tsx`, read once).
- Tiles show whole amounts ("1,841 EUR"), as ScaleOps does; the pages keep the cents. Unpriced: the newest run's requests in cores and GiB, the waste as a share of the CPU requests.

**Tests:** 3 in `test_totals.py` (the sums times pods, ready only, the 14 days, another Contract's never counted; `automated` by rule and Actuator; the route behind the identity and cookie, an unknown Contract 404) and 8 in `Overview.test.tsx`, one more in `Shell.test.tsx`. 16 mutations, all caught.

**Checked:** in Chrome on a local build with the Hub's CSP and sample data: the tiles in one row at 1476 px, the ring, both charts with the waste band, the Agenda's first five; no CSP violation. Dark theme only. Not checked live (the mini PC's Hub runs 0.63.0).

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
