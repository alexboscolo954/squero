# Phase 21: Console design

**In short:** the console works but looks like raw HTML: a link bar, headings and bare forms. This phase gives it a real dashboard design, in the style of ScaleOps' product UI, without changing what any page does. It is built next, before Phase 19 continues.

**Status:** 21.1 to 21.4 released in 0.64.0 (2026-11-22), running on the mini PC's Hub. 21.5 released in 0.65.0 (2026-11-22). 21.6 grilled (2026-11-22), not built. Owed: a look at the light theme and at every page with real data.

**Goal:** a console the Operator is glad to open every day: a sidebar, an Overview dashboard, and every page laid out as number tiles, charts and tables in cards.

**Done when:** every page of `/app/` uses the new shell and components, in dark and light themes, with no API change beyond the Overview's one read-only endpoint, and `make console-check` green.

| Slice | What | Status |
|---|---|---|
| [21.1](#211-the-shell) | The shell: sidebar, header, Contract switcher, Kill switch, design tokens and components | ✅ built |
| [21.2](#212-the-overview) | The Overview dashboard | ✅ built |
| [21.3a](#213-the-optimizer-pages) | Workloads and Cost restyled | ✅ built |
| 21.3b | Nodes, Automation and Fleet restyled | ✅ built |
| [21.4](#214-the-triage-pages) | The triage pages restyled: Investigations, Teachings, Checks, Health, Contracts | ✅ built |
| [21.5a](#215-refinement-after-two-design-skills) | Tokens, font, sidebar, header, buttons, badges, tables, focus and motion | ✅ built |
| [21.5b](#215-refinement-after-two-design-skills) | Number tiles, charts, empty and loading states; the checklist on every page | ✅ built |
| [21.6a](#216-density-and-layout-after-a-live-reference) | Denser type, a title row on every page, figures in cards, uppercase small labels | ⬜ next |
| [21.6b](#216-density-and-layout-after-a-live-reference) | The Overview as a grid of cards | ⬜ |
| [21.6c](#216-density-and-layout-after-a-live-reference) | Tables: bars in cells, a Total row, numbers in mono | ⬜ |

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

**Decided (grill, 2026-11-21, all recommended):**
- Split: 21.3a Workloads and Cost, 21.3b Nodes, Automation and Fleet.
- The Workloads table, one row per Workload: Savings (the waste's monthly cost; cores and GiB without prices), CPU and memory as "request → Sizing" per pod summed over its containers (the new value green), pods, the policies as lavender pills, Automation as a badge, risks. The CPU and memory waste and the requests' cost move into the Workload's detail.
- No "Propose Automation…" in the console yet: the pull-request flow exists only in the Hub MCP server (`propose_change`). The badge and a hint saying so; a console route for it is a write path into the contracts repo, its own slice and grill.
- Filters: chips for Automated and Not automated, the cluster and namespace selects, a search over the names.
- A Workload's detail stays a card under the table (its `workload=` address keeps working).
- The Agenda leaves the Workloads page (decided in 21.2): the Overview's "See all" now opens the rest in place.

**Built 21.3a (2026-11-21):**
- Workloads: tiles (the waste a month with its cores and GiB, the requests a month, the Workload count, Idle GPUs), the table as decided, `automated=` and `q=` in the address like the cluster and namespace, "No Workload matches." when the filters leave none; the detail opens with the Workload's waste and requests' cost; Learning and Excluded in a card.
- Cost: the grouping and comparison on top, tiles for the requests and the Savings available (the change, when compared, under each), the realized Savings in a card, the groups' table with the Savings in green.
- Each restyled page's `h1` is for screen readers only; the header names the page. `ui/badge.tsx`: `Badge` and `Change` ("before → after").

**Tests:** 3 in `Workloads.test.tsx` (request → Sizing per pod with an unset request, Automation all, some or none, the table's cells, the chips and the search with their address), and 9 existing tests moved to the new markup (the Agenda's now render its panel; the compared change, the totals and Idle GPUs read their tiles). 9 mutations, all caught.

**Checked:** in Chrome on a local build with the Hub's CSP and sample data: both pages, no CSP violation. Dark theme only.

**Built 21.3b (2026-11-21):**
- Nodes: each cluster a card; above what could go, "Nodes that could go: now 1 → 2 after the Sizings" (a "before → after"); what could go and the Unrequested GPUs in framed boxes; the verdicts as badges (yes green, blocked red, unknown grey, pinned lavender); a node's type, GPUs, spot, pinned and taints as grey badges.
- Automation: each cluster a card, the Automation state a badge (on green, off grey, stopped or ended red), Revert an outlined button.
- Fleet: totals per currency as tiles on top (whole amounts, never converted), then a card per Contract (its name linking to its Workloads, its Kill switch as a badge, each value under its name, a failing Contract's error in its card).
- Choice made while building, for the owner to confirm: a Fleet card still links to the Contract's Workloads, as 18.4 decided, not to its Overview.

**Tests:** the Fleet test reads the cards and the totals' tiles; one assertion more for "Nodes that could go". 6 mutations, all caught (one after that assertion).

**Checked:** in Chrome with the Hub's CSP and sample data: Fleet and Automation, no CSP violation. Nodes only through its tests (no sample data for it in the preview). Dark theme only.

### 21.4 The triage pages

**What:** Investigations, Teachings, Checks, Health and Contracts.
- Lists as tables with status badges; a Report's page as cards (findings, Evidence, Proposed actions with their Approval buttons).
- Forms (a Question, a Teaching) as proper form cards.
- Health as status tiles per integration and part.

**Built (2026-11-21):**
- The pages' own fields and buttons in the shell's look, once, in `styles.css`: inputs, selects and text areas with the theme's border and focus ring; buttons outlined, `primary` (the accent) for the action that goes ahead (Ask, Approve, Follow up, Approve this text, Add, Save) and `danger` (red outline) for Reject and Retire; panels, details and tables with rounded corners.
- Investigations: each status a badge (done green, failed red, the rest grey).
- Health: each integration a tile with its time and error, a failing one framed red with "FAILING"; spend, config and the Hub's settings in panels as before.
- Teachings, Checks, Contracts and the token page: their titles for screen readers only, the header naming the page.
- Choice made while building, for the owner to confirm: Contracts and Checks stay tables (in the shell's table look), not cards.

**Tests:** 3 in `Triage.test.tsx` (the status badges' colours, the Health tiles and the failing frame, the primary and danger buttons); the 67 earlier tests unchanged. 5 mutations, all caught.

**Checked:** in Chrome on a local build with the Hub's CSP and sample data: Teachings (the page of the owner's screenshot), Health and Investigations; no CSP violation. Dark theme only.

## 21.5 Refinement after two design skills

**Why:** on 0.64.0 the owner found the console "better but still too dull", and pointed at two design skills, [hallmark](https://github.com/nutlope/hallmark) and [taste-skill](https://github.com/leonxlnx/taste-skill). Both were read (nothing installed or run) and distilled into a brief. Their diagnosis: every panel the same box at the same weight, seven colours with none argued, uppercase grey table heads, the system font. Their cure: scale and mass, one accent, not more colour.

**Decided (grill, 2026-11-22, all recommended):**
- **Colour, a hybrid:** figures in ink, but the Savings in the accent (what squero is for); one accent, a teal-green (hue 175: the skills ban purple-blue and cyan on dark, so the indigo and the teal go); status colours (ok, warning, failing) only on badges and notices, never the accent; in charts one argued series, the Sizings.
- **Font:** Hanken Grotesk (variable), JetBrains Mono for ids and code, both self-hosted from npm (`@fontsource-variable/*`); the Hub serves `.woff2` from `/app/assets/`.
- **Hierarchy:** one hero figure (40 px) and the rest 32 px, separated by thin rules, no boxes; the sidebar on the page's own ground with a rule and an accent bar on the active page; tables without an outer border, 40 px rows, sentence-case heads, the selected row barred in the accent; elevation by lightness on dark, no shadows, no card in a card.
- **The Kill switch:** outlined; no confirm to turn it on (the safe direction, an emergency), a confirm to turn it off.
- **Slices:** 21.5a the tokens and the shared parts, 21.5b the tiles, charts and states, then the skills' checklist on every page.

**Built 21.5a (2026-11-22):**
- Tokens for both themes: a neutral canvas, cards a step lighter, raised surfaces, rules, ink and muted text, the accent (`#0b6b60` light, `#4fbfaf` dark), ok, warning and failing with their tints; dark text at weight 350. The ScaleOps colours retired: money and waste are ink, the Savings the accent.
- Hanken Grotesk and JetBrains Mono (5.3.0, OFL), every file served by the Hub: Vite never inlines an asset (`assetsInlineLimit: 0`), since a small font would become a `data:` URL that `default-src 'self'` refuses. The Hub serves `.woff2` as `font/woff2`.
- The sidebar on the canvas with a right rule, sentence-case group names, the active page barred in the accent; the header 56 px with a chevron breadcrumb.
- Buttons 36 px, radius 8, a 1 px press, disabled at 55%; badges in status colours (the policy pill neutral); tables with rules between rows only, sentence-case heads, the selected row barred; one focus ring everywhere; `prefers-reduced-motion` turns motion off.
- The Kill switch asks before turning off ("Changes to this Contract's systems will run again." Yes, turn it off / Keep it on), the question reset when the Contract changes.

**Tests:** the asset route serves a `.woff2` as `font/woff2`; the Kill switch test sends nothing on "Keep it on" and turns off on "Yes"; a new one: the question never follows you to another Contract. 6 mutations, all caught (one after that test).

**Checked:** in Chrome with the Hub's CSP: both fonts load from the Hub, no CSP violation. Dark theme only.

**Built 21.5b (2026-11-22):**
- Figures without boxes: a row with thin rules between, each label above its number; one hero figure, 40 px (the Savings available on the Overview and Cost, the waste on Workloads, each currency's waste on the Fleet), the rest 28 px, in ink, the Savings in the accent.
- The Automation ring became a figure with a thin meter ("60%", "3 of 5 containers"): the brief counts a donut as a dashboard tell. A thin neutral meter under Wasted spend too.
- Charts (the Overview's and a container's): the Sizings the one accent line; requests a muted line; the waste a faint neutral band between them; usage in ink with a faint fill; horizontal rules only, no ticks, muted 13 px labels; the legend as line swatches. Their colours are read from the theme when they draw, and they draw again when the system switches theme (`theme.ts`).
- Every page shows a flat block in its shape while it loads, never an empty page; an empty Workloads or chart says what is missing, why, and "Check Health".
- Colours only from tokens: the Kill switch's line and the filled buttons use `--bad-solid` and `--on-solid`, not Tailwind's own red and white.

**The brief's checklist, run on the console's source and in Chrome:**
- No uppercase or tracked labels; no `transition: all`; no shadows; no card in a card. ✅
- Colours only from tokens (none outside the token block). ✅ (after the fix above)
- One filled button per view: Teachings had one per waiting Teaching, so "Approve this text" is outlined and "Add, approved" filled. ✅
- One loud chart series; horizontal rules only; figures in ink but the Savings. ✅
- No purple-blue or cyan on dark: the accent is a teal-green. ✅
- A focus ring everywhere, shown at once; motion off on request; `overflow-x: clip`. ✅
- Every loading and empty state says something, the empty ones with an action. ✅ (the pages without data show their Hub's own sentence, as before)
- Contrast: not measured with a tool; the light theme previewed by setting its tokens by hand (the system was dark), so its charts were seen in the dark theme's colours.

**Tests:** 2 in `theme.test.ts` (a colour at an opacity, the axes), one for the loading block, the Overview's tests on the hero, the meter and the empty charts' action; 7 existing tests moved to the figure layout. 7 mutations, all caught.

**Checked:** in Chrome with the Hub's CSP: the Overview in both themes, no CSP violation.


## 21.6 Density and layout after a live reference

**Why:** on 0.65.0 the owner found the console "much better", but the text too large and some layouts "too simple". ScaleOps never answered a trial request, so the owner opened a free account on another optimizer's console, and a throwaway kind cluster was connected to it. Its pages were measured in Chrome (dark theme only; it has no light one).

**What the reference does that ours does not:**

| | Reference | Ours (21.5) |
|---|---|---|
| Body and table text | 12.5 px | 14 px |
| Table rows | 34 px, heads 10.5 px uppercase | 40 px, heads 13 px sentence case |
| Page title | 20 px/800, with the page's controls on the same row | the breadcrumb only |
| Figures | 27 px/700 in a card, with a bar and a footnote | 32 to 40 px, no box |
| Overview | a grid of cards: icon chip, title, subtitle, preview, "Explore details →" | a hero figure, charts, the Agenda |
| Tables | bars inside cells, a Total row, numbers in mono | plain numbers |

**Decided (grill, 2026-11-22, all recommended):**
- **Type scale:** between the two. Body, tables and the sidebar 13 px; table rows 36 px; card titles 15 px/600; the page title 20 px/700.
- **A title row on every page:** an icon and the title, short linked counts ("3 clusters · 41 workloads"), the page's controls on the right (Contract picker, range).
- **Figures in cards:** a small icon, the label, the figure (28 px/700), a thin bar where a share exists (waste %, automated %), a footnote ("of $297/mo spend"). The hero (the Savings available) stays first, in the accent. *Reverses 21.5's "no boxes" for figures.*
- **Uppercase small labels** on cards, table heads and sidebar groups (11 px, tracked); never on body text. *Reverses the 21.5 checklist's "no uppercase labels".*
- **The Overview as a 3-column grid of cards:** Waste, Savings, Automation, Nodes, the Agenda, Investigations; each with an icon chip, title, subtitle, a small preview and "Explore details →" to its page. Its charts move to the Cost page.
- **Tables:** bars inside cells (cost, waste, CPU and memory use), a Total row, numbers in JetBrains Mono. No filter under each column and no Columns panel: the chips and search already filter.
- **Colour unchanged:** figures in ink, the Savings in the accent, one argued chart series. The complaint was size and layout, not colour.
- **Unchanged:** the font, both themes, the teal accent.
- **Slices:** 21.6a the type scale, title row, figure cards and labels; 21.6b the Overview grid; 21.6c the tables. Each updates the 21.5 checklist and "The look" in `running-the-hub.md`, and is checked in Chrome in both themes.
