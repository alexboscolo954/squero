# Phase 21: Console design

**In short:** the console works but looks like raw HTML: a link bar, headings and bare forms. This phase gives it a real dashboard design, in the style of ScaleOps' product UI, without changing what any page does. It is built next, before Phase 19 continues.

**Status:** 21.1 to 21.4 released in 0.64.0 (2026-11-22), running on the mini PC's Hub. 21.5 released in 0.65.0 (2026-11-22). 21.6 built (2026-11-22), 21.6a to 21.6f, not released: 0.66.0 on the owner's word. Owed: a look at the light theme and at every page with real data.

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
| [21.6a](#216-density-and-layout-after-a-live-reference) | Denser type, a title row on every page, figures in cards, uppercase small labels | ✅ built |
| [21.6b](#216-density-and-layout-after-a-live-reference) | The Overview as a grid of cards | ✅ built |
| [21.6c](#216-density-and-layout-after-a-live-reference) | Tables: bars in cells, a Total row, numbers in mono | ✅ built |
| [21.6d](#216d-to-216f-the-same-pattern-on-every-page) | Nodes and Automation in the page pattern | ✅ built |
| [21.6e](#216d-to-216f-the-same-pattern-on-every-page) | Investigations and an Investigation's page | ✅ built |
| [21.6f](#216d-to-216f-the-same-pattern-on-every-page) | Checks, Health, Teachings, Contracts, Approver token, the Workload detail, Cost's lines | ✅ built |

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
- **Hierarchy:** one hero figure (40 px) and the rest 32 px, separated by thin rules, no boxes (superseded by 21.6a: figures in cards, 28 px); the sidebar on the page's own ground with a rule and an accent bar on the active page; tables without an outer border, 40 px rows, sentence-case heads, the selected row barred in the accent; elevation by lightness on dark, no shadows, no card in a card.
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
- No uppercase or tracked labels; no `transition: all`; no shadows; no card in a card. ✅ (uppercase small labels superseded by 21.6a)
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

**Built 21.6a (2026-11-22):**
- The type scale: body, tables, sidebar and Tailwind's `text-sm` 13 px; table rows 36 px; card titles 15 px; headings 20, 15 and 13 px; chart labels 12 px.
- A title row in the Shell on every page but an Investigation's (which has its own title): the page's icon and its name as the one `h1` (the Contract's name inside it for screen readers), the page's counts and controls drawn beside it through `PageTools`, and the Contract switcher at the right. The pages' hidden headings went.
- The Contract switcher moved there from the sidebar, shown only with several Contracts and never on a page across them.
- Counts where the page already holds them: Overview (clusters, workloads, linked to Workloads), Workloads (clusters, workloads), Fleet (Contracts, linked to Contracts). Cost's grouping and comparison moved into the row.
- Figures in cards: an icon, an 11 px uppercase label, the figure at 28 px, a full-width meter for a share, a footnote (Wasted spend: "of 895 EUR a month"). Every figure the same size; the first card is the hero, the Savings in the accent.
- Uppercase small labels on figures, table heads and sidebar groups (11 px, 600, 0.05em).

**Tests:** 3 new (the Shell's title row: icon, the Contract for screen readers, counts beside it, one `h1`; Workloads' counts, one cluster and two workloads; the Overview's counts and link inside the Shell); the switcher's, the Investigation's and the Fleet's tests extended; the Overview's hero and footnote; 10 tests now wait on page content instead of the old hidden headings. 77 console tests. 10 mutations, all caught.

**Checked:** in Chrome with the Hub's CSP and sample data: Overview (priced, and unpriced with GPUs), Workloads, Cost, Fleet and Teachings dark; the Overview light (its tokens set by hand: the system was dark, so its charts kept the dark colours). The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- On Workloads and Fleet the first figure is in ink, so with every figure 28 px it no longer stands out; only the Savings carry the accent.
- No counts on Nodes, Automation and the triage pages: they would need a fetch of their own. (Nodes and Automation got theirs in 21.6d, from what each page already reads.) Only Cost's controls moved into the title row; other pages keep theirs where they were.
- With GPUs the Overview has 7 figures, 6 to a row at 1,476 px, so the seventh sits alone. Unpriced values ("4.10 cores, 10.50 GiB") wrap to two lines.
- Not seen in a browser: Nodes, Automation, Checks, Health, Investigations and an Investigation's page (no sample data), and a narrow window.
- Sizes the grill did not set: chart labels, badges, hints and chips at 12 px.
- No API change, no eval rerun (nothing the agent sees changed), no isolation test (no Contract data moves).

**Decided for 21.6b (2026-11-22, all recommended):**
- The seven figures fold into the six cards: every number shows once.
- The reference is a reference, never a layout to copy: the cards are named after squero's own pages and link to them ("Open Cost"), with neutral icons and the console's own Agenda and status badges.
- The Savings card first; Idle GPUs beside the waste, never in it; the Agenda card opens the rest in place (it has no page).

**Built 21.6b (2026-11-22):**
- The Overview is a grid of six cards, three to a row on a wide window: Savings, Waste, Automation, Nodes, Agenda, Investigations. Each has an icon, its name, a line on what it shows, its numbers, and a link to its page.
- Two new reads, the nodes and the Investigations. Every read but the Workloads fails alone: its card says "Couldn't read…" and still links to its page; a failed totals read, which only an unpriced Contract's waste share needs, is never shown as "0%".
- A cluster with a read problem is listed as not read, never counted as 0 nodes; no cluster read says "No nodes read yet"; "could go" shows only where the Hub judged it.
- A Contract with nothing stored says "No Sizings stored yet." above the cards, with "Check Health" (the Workloads page's own block, now shared).
- The CPU and memory charts moved to the Cost page (`Usage.tsx`), under the groups, with their own read: a failed one says so and the page stands. They sit beside the page's content, so regrouping or comparing never reads or draws them again.
- The Investigations list is the Hub's own: newest first, its latest 100.

**Tests:** the Overview's two page tests rewritten for the cards (order, numbers, links, the Agenda's three and "See all", the latest three Investigations, a failed read per card, nothing stored, a failed totals read); 1 new for the nodes at a glance; 1 new for the Cost page's charts (drawn, kept across a comparison, no run stored, a failed read). 79 console tests. 19 mutations, all caught.

**Checked:** in Chrome with the Hub's CSP and sample data: the Overview priced and unpriced (a failed nodes read, an empty Agenda) in dark, priced in light (tokens set by hand), and the Cost page with its charts. The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- The Overview makes six reads where it made four.
- Not seen: a narrow window (the grid goes to two columns, then one).
- No API change beyond one schema's description (types regenerated), no eval rerun, no isolation test: every read is the Contract's own, by its id.

**Decided for 21.6c (2026-11-22, all recommended):**
- **Which tables:** Workloads and Cost get bars and a Total row: the two that rank things by money or resources. Every other table gets mono numbers only.
- **What a bar measures:** under a Savings, or a group's requests and Savings, the row's share of the largest row shown; under a CPU or memory "request → Sizing", the share of the request the Sizing keeps. A raise draws none: a full bar would read as "fine".
- **The Total row** adds the rows shown, so on Workloads it follows the filters; the whole Contract's totals stay in the cards above.

**Built 21.6c (2026-11-22):**
- Workloads: a bar under each Savings (in the accent) and under each CPU and memory (neutral); a Total row with the Savings and pods of the rows shown. Money and cores never share a scale or a sum: a row without a price is scaled against the other unpriced rows and totalled apart ("150.00 EUR/month + 0.50 cores, 0.00 GiB").
- Cost: a bar under each group's requests and Savings; a Total row with the Hub's own total and, when comparing, its change. Unpriced, cores and GiB scale the bars as one number, a core weighing 4 GiB, as the ranking does.
- A bar adds no text and no column. No bar where there is nothing to compare: a zero, a raise, a Workload with a container whose request is unset (its sums are over different containers), a group gone since.
- Numbers in mono (`.num`): quantities and money in the Workloads, Cost, Nodes, Automation and Contracts tables; they never break from their unit. Dates and names stay in the text face.
- The Workloads, Cost, Automation and Contracts tables scroll sideways inside their card when the window is narrow (the page itself clips overflow); Nodes already did.
- One line under each table says what its bars mean.

**Tests:** 2 new (a bar's share and the share a Sizing keeps; Workloads' bars, mixed units and a Total that follows the search), and 4 Cost tests extended (bars priced and unpriced, the Total row with and without a comparison, none without groups). 81 console tests. 17 mutations, all caught.

**Checked:** in Chrome with the Hub's CSP and sample data: Workloads priced and unpriced (with the Idle GPUs column) and Cost in dark, Workloads in light (tokens set by hand); the page squeezed to 560 px: the table scrolls inside its card and the page does not. The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- The per-pod CPU and memory columns have no Total.
- The Workloads Total is the console's sum of the rows shown; unfiltered it is the same sum the Hub makes for the cards.
- Not seen in a browser: the Cost page while comparing (tested), the Nodes, Automation and Contracts tables in mono (no sample data), a real narrow window (the window would not resize; the page was squeezed instead).
- For the owner's eye: the numbers are 12 px mono beside 13 px text, and the Savings column carries more of the accent than before.
- No API change, no eval rerun, no isolation test.

## 21.6d to 21.6f The same pattern on every page

**Why:** after 21.6c the owner asked whether the new look reaches every page. It does not: Overview, Workloads, Cost and Fleet carry it; the other nine got only the shared type and frame. "I don't want only half the Hub looking good."

**What the other pages show today** (seen in Chrome with sample data, 2026-11-22):
- The tables of Investigations, Checks, Contracts and Teachings' Approved sit on the page's ground, not in a card.
- No figures on Nodes, Automation, Investigations, Checks, Health, Teachings or Contracts.
- Nodes is mostly sentences, and since 21.6c's no-wrap numbers its table is wider than its card at a 1,500 px window: "Could be removed" needs a sideways scroll.
- An Investigation's page has no title row; its facts are one line of small text.

**Decided (grill, 2026-11-22, all recommended):**
- **One page pattern everywhere:** the title row with counts, a row of figure cards, then the content in cards, every table inside a card with a title. Statuses are badges everywhere.
- **Done already:** Overview, Workloads, Cost, Fleet. **Redone:** the nine others, plus the parts of Workloads and Cost not opened in 21.6 (a picked Workload's detail, Cost's bill and realized lines).
- **Figures**, only from what each page already reads, no new API:

  | Page | Figures |
  |---|---|
  | Nodes | Nodes read, could go now, could go after the Sizings, spot nodes, clusters not read |
  | Automation | On, off, stopped by a Revert, ended by rollbacks (the page's own states) |
  | Investigations | Waiting for you, running, failed, of the list's latest 100. No "done": the list leaves out passing Checks |
  | Checks | Checks, failing now, Deploy verifications passed and failed |
  | Health | Integrations ok with a meter, model spend this month (a meter where a budget is set), the last successful Investigation |
  | Teachings | Waiting for review, approved, left out of prompts |
  | Contracts | Contracts, model spend this month, a meter per row where a budget is set |

- **Nodes:** each cluster's facts as a label-and-value list, every caveat word for word; the table narrowed so "Could be removed" fits; no bars (21.6c's decision stands).
- **Filter chips**, kept in the address as Workloads' are: Investigations by kind and status; Automation by All, On, Not on.
- **An Investigation's page:** a header card (title, status badge, kind, Contract, start time) and figures (tokens, cost, tool calls, masked names). Approve, Reject with its reason and the Stale deadline stay exactly as they are.
- **Slices:** 21.6d Nodes and Automation; 21.6e Investigations and an Investigation's page; 21.6f Checks, Health, Teachings, Contracts, Approver token, the Workload detail and Cost's lines. Each checked in Chrome on every page it touches.
- **Release:** one, 0.66.0, after 21.6f, so the mini PC's Hub never shows half of it.

**Built 21.6d (2026-11-22):**
- **Nodes:** figures on top: nodes read, Nodes that could go now and after the Sizings, spot nodes, clusters not read (by name). They are the Overview card's own numbers (`nodesAtGlance`). No verdict stored says "not judged", never 0; spot shows only where a cluster names a spot label, with the Hub's count for it.
- Each cluster's facts as a label-and-value list: read, autoscaler, largest Room, whose rules, the last report of Blockers, Placement, spot. Every caveat kept word for word: the could-go box, "(the Hub can't read them)", both "Not part of Savings", "Its Actuator doesn't place them yet.".
- The table fits its card at 1,500 px (1,345 px wide before, 1,178 now): cores and GiB break between the two, never inside one. The cells' text is unchanged.
- **Automation:** figures of the containers' states, one each in the page's own order (off wins over its rollbacks), so they add up to the rows; they count the containers the Actuators reported on in 90 days, which is not the Overview's share of ready containers.
- Chips All, On and Not on, kept in the address (`state`); the figures stay whole under a filter; a cluster the filter empties says "No container here matches.".
- Each last outcome a badge: failed and rolled back red (the two the Actuator itself posts as warnings), applied green, every other (and any this console doesn't know) neutral.
- Shared pieces: `params.ts` (`useParam`, moved out of `App.tsx`) and `ui/chips.tsx` (Workloads' chips, now used by both pages).
- Revert, its label, and the Kill switch and "Couldn't:" notices are untouched. No bars on Nodes.

**Tests:** 1 new for Automation (figures, chips, an emptied cluster, outcome badges); the Nodes tests moved from sentences to the facts list, with the figures (judged and not judged, spot, clusters not read) added; `nodesAtGlance`'s spot count. 82 console tests. 13 mutations, all caught.

**Checked:** in Chrome with the Hub's CSP and the tests' own sample data: Nodes in dark and light (tokens set by hand), Automation in dark, unfiltered and "Not on"; the Nodes table measured, no sideways scroll at 1,500 px. The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- Not seen in a browser: a cluster with Placement and spot facts, and the spot Workloads lists (tested).
- No API change, no eval rerun, no isolation test.

**Built 21.6e (2026-11-22):**
- **The list:** three figures (awaiting approval, running, failed) over the list; the title row says how many are listed, "the latest 100" only when the list is that long. No figures of an empty list.
- Chips for the kind and the status, kept in the address (`kind`, `status`), the status in the badges' own words (awaiting approval, running, failed, done); none over an empty list; the figures stay whole under a filter; a filter that matches nothing says "No Investigation matches.", never "None yet.".
- The table sits in a card with its title and the chips. Rows keep their text.
- **One status per Investigation**, for its badge, the figures and the chips: a failed Run (`status: failed`) is now red like a failed Check, here and in the Overview's card; it was grey. A status this console doesn't know shows under no filter, in no figure, neutral.
- **An Investigation's page:** a header card with the title (the page's one `h1`), its status badge, kind, its own Contract as a link to that Contract's list, start time and id.
- Four figures: tokens (in, with out under it), cost (a dash when not known, never $0), tool calls (with how many failed), and what was masked for the LLM (only when something was). They sit under "Waiting for your decision", so what waits stays first.
- Its Decisions, Tool calls, related alert groups and the Jira and Scenario lines each sit in a card; a decision is a badge. A flat block shows while the page loads (it was empty).
- The approval panel is untouched: its words, Approve, Reject with its reason, and the Stale deadline.
- The legacy card style (`.panel`, `.detail`) took the Card's 10 px radius, so the two no longer differ side by side.

**Decided while building:** the figure is labelled "Awaiting approval", the badge's own words, not "Waiting for you": a list row carries no deadline, so the console can't tell whether an approval can still be given. The Agenda stays the place for what needs the Operator.

**Tests:** 4 new (the list's figures, chips and card; "the latest 100" and an empty list; a page's figures with an unknown cost, failed calls and nothing masked; the loading block); the page test extended (header, link, figures, the decision above them, its controls unchanged); the link checked with another Contract picked, an approved decision's badge; the status test now has a failed Run. 86 console tests. 14 mutations, all caught (one after a tighter assertion).

**Checked:** in Chrome with the Hub's CSP and the tests' own sample data: the list unfiltered and filtered in dark, a Triage awaiting approval top to bottom in dark, a followed-up Question in light (tokens set by hand), Teachings after the radius change. The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- The figures count the list, not every Investigation the Contract ever had.
- Not seen in a browser: a page with related alert groups, Report feedback or the Jira line (tested before, their text unchanged), a narrow window.
- No API change, no eval rerun, no isolation test.

**Built 21.6f (2026-11-22):**
- **Checks:** four figures (Checks, failing now, Deploy verifications passed and failed); the two tables each in a card under its title; a verdict as a badge around its link (passed green, failed red, any other word neutral). "Failing now" counts Checks with an Open failure; the Deploy figures count each row's own word, so a Run that failed by its status counts as failed.
- **Health:** three figures: how many are ok now with a meter (the Hub's list holds integrations, Relays and Actuators, so the figure says "Ok now", not "Integrations ok"), the model spend with a meter where a budget is set, the last successful Investigation. No budget draws no meter; $0 of a budget an empty one; over budget a full one, the share in words. (A Contract's budget is above 0 by its model.) The "ok" pill is the status green; it was the Savings accent. The config and the settings stay collapsed, as the Hub sends them.
- **Teachings:** three figures (waiting for review, approved, left out of prompts); the approved, the form and the past each in a card under its title. The buttons and the left-out notice are as they were.
- **Contracts:** two figures (the Contracts with how many have a model budget, the model spend over all of them, in USD); the table in a card; a meter under a spend that has a budget.
- **Approver token:** the form in a card.
- **Workloads:** the autoscaled Workloads in a card; the price lines in a card under the figures; a picked Workload's detail now has the Card's 10 px radius (a utility kept it at 12 px after 21.6e), its request and Sizing line in mono.
- **Cost:** the price, Bill and comparison lines in one card under the figures, each line and its notice style as it was; the card's title names only what it holds ("Prices, Bills", "The comparison"). A Bill is never a figure.

**Tests:** 2 new (Checks counted by the row's own word, a running one neutral, nothing at all; Health's meter without a budget, at $0, over budget) and 8 extended (Checks, Health, Teachings, Contracts, the token page, Cost's card by what it holds and absent when empty, Workloads' price card present and absent). 88 console tests. 16 mutations: 15 caught (one after a tighter assertion), 1 an equivalent mutant (a guard for a budget of 0, which a Contract can't have; removed).

**Checked:** in Chrome with the Hub's CSP and the tests' own sample data, on the final build: Checks, Health, Teachings, Contracts, the token page (not typed in), a picked Workload's detail with its charts, Workloads with its price card and the autoscaled Workloads card, Cost with its card of price and Bill lines, the Overview and the Fleet in dark; Health in light (tokens set by hand). The browser console read from the first page load: no error, no CSP refusal.

**Known limits:**
- Not seen in a browser: the Cost page while comparing (tested), a narrow window, any page but Health in light.
- Light touches, for the owner to judge before the release: a picked Workload's detail got only the radius and a mono line; Cost's realized line is as it was; Teachings and the token page have no counts in the title row; a Deploy verification that has not run shows its state as plain text.
- A container's chart legend picked up the small uppercase heads of 21.6a (it is a table).
- No API change, no eval rerun, no isolation test.

**21.6, whole:** every page of the console now follows one pattern. Not released: 0.66.0 waits for the owner's word.
