# Phase 11: The Hub as a source, and the whole console in the app

**In short:** Phase 11 let the Operator work from Claude Code. It added the Hub MCP server, which reads across Contracts and can make a few guarded writes. Then the whole console moved into the React app, and the old Jinja pages were deleted.

*Why it matters:* the Operator can list the top Sizings and approve one from Claude Code. Every write is audited, and a Kill switch stops all writes for one Contract.

**Status:** done on 2026-11-08. Released as v0.18.0 to v0.29.0.

**Done when:** Claude Code lists a Contract's top 5 Sizings and approves one through the Hub MCP server. An Automation change arrives as a pull request. The Jinja templates are gone, and every console test still passes.

**Live checks:** on the Mac Hub, from Claude Code, under 0.10 USD for the phase, with the Operator's standing yes.

**Glossary first:** Hub MCP server, Kill switch, Approved Sizing, Proposed change, Agenda. See [CONTEXT.md](../../CONTEXT.md).

| Slice | What | Status |
|---|---|---|
| [11.1a](#111a-the-endpoint-and-the-reads) | The Hub MCP server: the endpoint and the reads | ✅ built, checked live |
| [11.1b](#111b-the-rest-of-the-reads-and-ask) | The rest of the reads, and `ask` | ✅ built, checked live |
| [11.2a](#112a-decide-and-the-kill-switch) | `decide` and the Kill switch | ✅ built, checked live |
| [11.2b](#112b-approved-sizings) | Approved Sizings | ✅ built, checked live |
| [11.3](#113-proposed-changes-as-pull-requests) | Proposed changes as pull requests | ✅ built, checked live |
| [11.4](#114-the-agenda) | The Agenda: "what should I do now" | ✅ built, checked live |
| [11.5a](#115a-investigations-in-the-app) | Investigations in the app (read) | ✅ built, checked live |
| [11.5b](#115b-act-on-investigations-in-the-app) | Acting on Investigations in the app | ✅ built, checked live |
| [11.6a](#116a-teachings-in-the-app) | Teachings in the app | ✅ built, checked live |
| [11.6b](#116b-checks-and-health-in-the-app) | Checks and Health in the app | ✅ built, checked live |
| [11.7a](#117a-questions-in-the-app) | Questions in the app | ✅ built, checked live |
| [11.7b](#117b-the-jinja-console-deleted) | The Jinja console deleted | ✅ built, checked live |

## Decisions (grill, 2026-11-05)

Four rounds, all recommended, except that asking a Question moves into 11.1.

- **Order:** the MCP track first (11.1 to 11.4), then the console (11.5 to 11.7).
- **Releases:** one release per slice.
- **Evals:** no eval rerun, because the Investigation agent's prompt and tools do not change.
- **The Hub MCP server** is `/mcp` (streamable HTTP). It runs in the Hub process.
- It is reached only through `tailscale serve`. Caddy's public port answers 404.
- A call needs the bearer `AIOPS_MCP_TOKEN`. The Hub refuses to start if this equals another token.
- A call also needs a `Tailscale-User-Login` listed in `AIOPS_CONSOLE_OPERATORS`. The audit names that login.
- Every tool takes a required `contract`, except `list_contracts`.
- Its output follows that Contract's Masking.
- A placeholder in an argument is mapped back only if the Hub handed it out.
- Every call is an `aiops.audit` line, reads included. The line has `channel: mcp` and the login.
- The Investigation agent never gets these tools.
- **Kill switch:** a per-Contract flag in the database. While on, it refuses every write for that Contract. It is set through MCP or the console, and cleared only in the console.
- **Pull requests:** a fine-grained token for the contracts repo only, limited to opening pull requests. The Operator merges.
- **Glossary first:** Hub MCP server, Kill switch, Approved Sizing, Proposed change, Agenda.

## Part 1: the Hub MCP track (11.1 to 11.4)

### 11.1a: the endpoint and the reads

- `hubmcp.py` is FastMCP, stateless HTTP with JSON answers. It is mounted at `/mcp/` behind both doors. Its lifespan is entered in the Hub's.
- Tools: `list_contracts`, `sizings` (`top` 1 to 100, `cluster`, `namespace`, `risky`), and `sizing`.
- `sizings` and `sizing` share their code with the Workloads page (`webapp.workloads_of`) and with the agent's tool (`servers.sizing.workload`). The agent's tool definition is byte-for-byte unchanged, so no eval was rerun.

**Changed from the grilling:**

- The token must be 32 characters or more.
- The URL ends in `/mcp/`.
- The placeholders handed out are kept in memory per Contract. So after a restart, the client lists again.

**Tests:** hub-smoke turns it on. It checks the endpoint from the Hub's port: 401 without the token, and its tools with it. It also checks that Caddy answers 404.

**Checked live (2026-11-06, Mac Hub, v0.18.0, through `tailscale serve`):**

- `/mcp/` answered 401 without the token.
- With the token, `list_contracts` named demo-bank and demo-shop.
- `sizings(demo-shop, top=5)` ranked kind/lab's `api` first: 2653m and 2800Mi of waste, 69.58 USD a month.
- The kind cluster's reads had been failing since 2026-11-01. This was shown as `failing`.
- **Found:** no audit line reached the Hub's output, neither MCP's nor the agent's. `aiops serve` never set up logging, so Python dropped every `aiops` INFO line.
- **Fixed in v0.18.1:** `serve` gives the `aiops` loggers a stderr handler at INFO.

### 11.1b: the rest of the reads, and `ask`

- Tools: `cost`, `savings`, `bill`, `investigations` (`open`, `limit`), `investigation` (refused for another Contract's id), `checks`, and `ask`.
- `cost` and `savings` share code with the Cost page (`webapp.allocation_of`). `bill` is `billing.report`.
- Each tool is the same function as its `/api/v1` route.
- `ask` stores a Question like the console does. It is not posted to Slack. It returns its id at once.
- `ask` is not marked read-only. The question's placeholders are put back for the agent.

**Changed from the grilling:**

- A comparison's clusters come as a list, like `sizings`. *Why:* masking leaves dict keys alone, and a cluster's name can be the client's.
- A `by` grouping named after the client, such as a label key like `acme.io/team`, comes back from its placeholder.
- A listed Answer is masked before it is cut to 500 characters.
- *Known limit:* a title is cut at 200 characters when it is stored. So a term cut in two there goes out as a fragment.

**Checked live (2026-11-06, Mac Hub, v0.19.0, from Claude Code through `tailscale serve`):**

- `cost(demo-shop, by=team, compare_days=7)` gave storefront 103.00 USD a month of requests, and 70.34 of waste. The five teams added up to 145.79 and 75.30. Each team's change was shown.
- `savings` gave the same 75.30 available, with nothing realized.
- `bill` showed no billed cluster.
- `investigations` listed the four Triages, newest first.
- `checks` showed none.
- Claude Code asked before `ask`, because it is not read-only.
- The Question ran on claude-haiku-5.5. It made 6 model calls, for 0.0052 USD recorded. It answered that the cluster API and Prometheus were unreachable, because the kind cluster was stopped. It fell back to the stored Sizing of lab/leaky, which was OOM-killed at 256Mi and needs about 308Mi.
- `investigation` returned that Question.
- Every call was an `aiops.audit` line with the login.
- The Hub has a Slack bot, but the Question has no Slack thread.

### 11.2 split

Split 2026-11-06, the Operator's choice: 11.2a covers `decide` and the Kill switch, and 11.2b covers Approved Sizings.

### 11.2a: `decide` and the Kill switch

**Built (2026-11-06):**

- Migration 0023 adds `kill_switches`. A row exists while the switch is on. It records who and when.
- `settle` is the one decision path. It refuses an approval while the Kill switch is on. It says so as `KillSwitchOn`, in the API, the console, Slack and the Hub MCP server. It claims nothing.
- `agent.refuse_when_killed` sits inside the Executed-action recorder. It reads the switch on every call the approval gate covers. This includes vendors' tools.
- While the switch is on, the call answers an error and does not run. So a change approved just before the switch went on is refused. It is recorded as FAILED.
- Planned: `decide(contract, investigation_id, action_ids, approve, message)`, naming the exact Proposed actions seen.
- The Hub MCP server gets `decide(contract, id, pause, approve, reason)` and `turn_on_kill_switch(contract)`.
- `decide` has `destructiveHint`. A decision is recorded as "<login> via MCP".
- `list_contracts` shows each switch. `investigation` shows a `pause` token.
- The console app gets a Kill switch bar on every page.
- It also gets `GET`, `POST` and `DELETE /api/v1/contracts/{id}/kill-switch`. These are the API's first changes. They are same-origin, and need identity and the cookie.

**Changed from the grilling:** `decide` names the pause by its exact deadline, as text. The agent's Proposed actions carry no id of their own. One decision covers the whole pause.

**Decided, the Operator's choice:** while a Kill switch is on, approving is refused, and the pause is kept. This was chosen over turning the approval into a rejection.

**Checked live (2026-11-07, Mac Hub, v0.20.0, migration 0023 applied):**

- From Claude Code, `turn_on_kill_switch(demo-shop)` answered on.
- `list_contracts` showed the switch on for demo-shop only.
- The Hub logged it as on, by `mcp:<login>`, with its audit line.
- The console app's bar showed who turned it on, and since when.
- Its "Turn it off" sent `DELETE /api/v1/contracts/demo-shop/kill-switch`. The Hub logged it as off, by `console:<login>`.
- After that, `list_contracts` showed it off.
- No Investigation was paused at the time. So the refusal of an approval is proven by the tests only. The tests cover every path, and a change that would run is refused.

### 11.2b: Approved Sizings

**Grilled 2026-11-07 (the Operator's choice, the recommended one):**

- An Approved Sizing is superseded by any later stored Sizing of its container, whatever its values.
- A run that failed on its cluster, or is still going, supersedes nothing.
- Defaults: one container per approval. Requests only: limits stay `keep` until Phase 12.
- An approval is refused while the container is learning, excluded, or has no change, or while a Kill switch is on.
- There is no revoke. The next run supersedes the approval.
- It is shown through the Hub MCP server only.

**Built (2026-11-07):**

- Migration 0024 adds `approved_sizings`. It holds the run, the requests it changes, and who approved and when.
- It has no foreign key. So pruning a run keeps the record.
- An approval is stored only where the Contract has no Kill switch on, in the same statement.
- An approval is current while no later run has read its cluster.
- The Hub MCP server gets `approve_sizing(contract, cluster, namespace, name, container, cpu, memory, kind)`, with `destructiveHint`.
- The values are the ones `sizing` showed. A mismatch answers with the latest values.
- `approved` (who, and when) appears on a container in `sizing` and `sizings`.
- The agent's `sizing_for_workload` and the console's API are unchanged. So no eval was rerun, and no types were regenerated.
- `servers.sizing.allowed` is shared with the agent.

**Checked live (2026-11-07, Mac Hub, v0.21.1, migration 0024 applied), from Claude Code:**

- `sizings(demo-shop, top=5)` ranked kind/lab's `api` first, at 69.58 USD a month of waste.
- `approve_sizing` was called with its shown 116m and 91Mi. It answered approved.
- `sizing` then showed it approved by `mcp:<login>`, with the time. The row stored cpu 0.116 and memory 91Mi, against run 374.
- The call was an `aiops.audit` line with the login.
- A deliberately wrong value (db's cpu as 70m) was refused with the latest value (68m, 286Mi). Nothing was stored for it.
- The kind cluster was stopped. So the approval is of the last run that read it, 2026-11-01 11:34 UTC. It stays current until a run reads kind again.
- Phase 11's first done-when is met: list the top 5 Sizings, and approve one, through the Hub MCP server.
- Nothing changes in a cluster until the Actuator (12.2).
- Also released in v0.21.1: oauthlib 4.0.1, from Dependabot #23.
- The console's dev-only minimatch alert (#24) was dismissed as a tolerable risk. ESLint still requires minimatch 10.

### 11.3: Proposed changes as pull requests

**Changed 2026-11-07 (the Operator's choice):** GitHub Free enforces no branch protection, and no ruleset, on a private repo. So nothing on GitHub stops the token from pushing to `main`.

Instead:

- The Hub's code only ever pushes an `aiops/` branch, and opens a pull request. A test asserts that no write touches `main`. Reading its ref is how the commit is checked.
- The Hub runs only the contracts the Operator pulls onto its machine. A commit nobody reviewed shows in `git log` first.
- GitHub Pro, with enforced protection, and Operator-signed commits checked at load, were held back.
- The contracts repo is the private `aiops-contracts`. It was pushed 2026-11-07.

**Asked 2026-11-07:**

- The edits are `set_rule`, `remove_rule`, `set_default` and `set_policy`. This was the Operator's choice over the two recommended options, so a change can be undone through a pull request too.
- The Kill switch does not refuse a proposal. The recommendation was that a pull request changes nothing until it is merged.

**Built (2026-11-07):**

- `proposals.py`, and `propose_change(contract, edits, title, why)` on the Hub MCP server. Edits are typed. They cover the Sizing policy only. Automation joins in 12.1.
- At start, the Hub notes the commit that its clone's HEAD names (a loose ref, `packed-refs` or a detached HEAD). It also notes each `<id>/contract.yaml`'s git blob id.
- A proposal reads main's ref, and the file at that commit. It refuses unless both still match.
- It makes the edits with `ruamel.yaml`, so comments are kept. New rules are written in flow style, first.
- It reads the result with PyYAML, as the Hub will. It refuses if anything but `sizing` changed, if nothing changed, or if `contracts.build` rejects the Contract. All of this happens before the first write.
- It writes through GitHub's REST API, with `httpx`.
- Then it creates a branch `aiops/<id>-<UTC time>`, and the commit there. The Contents API writes the default branch when `branch` is missing. So a test checks that every write names the branch.
- Then it opens the pull request.
- `AIOPS_CONTRACTS_REPO` and `AIOPS_CONTRACTS_TOKEN` are used. The token is sent only in the `Authorization` header, to `api.github.com`. An HTTP failure is shown as its status and host.
- A branch left over after a failed write is deleted by hand.

**Checked live (2026-11-07, Mac Hub, v0.22.0, from Claude Code):**

- The private `aiops-contracts` was cloned at the Hub's contracts dir. Its `main` was the commit the Hub loaded, `1f86160`.
- `propose_change(demo-shop, set_rule lab/web -> high-availability)` opened pull request #1, from `aiops/demo-shop-20261107-205027` to `main`.
- It changed one file by one line. The new rule came first under `rules:`, in the file's own flow style. Every comment and every other line was untouched, including `policy: off`.
- `main` stayed at `1f86160`.
- The description carried the reason, the edit in words, and the login.
- The Hub logged the URL and the call's `aiops.audit` line.
- Phase 11's second done-when is met. A policy change arrived as a pull request. The change was a policy change, since the grilling.
- The pull request was closed unmerged, and its branch deleted.

### 11.4: the Agenda

**Asked 2026-11-08 (both recommended):**

- A fixed order of urgency, not a score. The order is:
  1. the Kill switch, if on;
  2. pauses, by deadline;
  3. failing Checks, newest first;
  4. stale data, oldest first;
  5. Bills outside, by the biggest gap;
  6. running Investigations, oldest first;
  7. the top 3 Workloads by waste cost.
- The panel is in the console, above the Workloads. It is not a tab of its own.
- Stale data means Sizing reads failing per cluster, and bill reads failing. A cluster no longer in the Contract is left out.

**Built (2026-11-08):**

- `agenda.py` has `of` and `counts`. It works from what the Hub stores: the Kill switch, unfinished Investigations, Check schedules, the latest Sizing runs, bill tries, `billing.report`'s rows, and the Workloads ranking. It uses no LLM.
- The Hub MCP server gets `now(contract)`, and `now()`. `now()` gives counts per Contract only. Each Contract's id and name are shown under its own Masking.
- `GET /api/v1/contracts/{id}/agenda` serves the data.
- The console app's Now panel shows it. Each item links to its Investigation, its Workload, or the Cost page.
- A Check whose Run is still going is a running Investigation, not a failing Check.
- A Check whose Run errored is failing, "could not run". This matches how the Hub's Slack thread counts it.

**Checked live (2026-11-08, Mac Hub, v0.23.0, from Claude Code):**

- `now()` gave: Demo bank 1 stale, and Demo shop 1 stale and 3 waste (17 ms).
- `now(demo-shop)` gave, first, the kind cluster's Sizing reads failing since 2026-11-01 13:56 UTC. The cluster is stopped.
- Then kind/lab's `api` (69.58 USD a month of waste), `report` (2.62) and `db` (1.41). This took 7 ms. Each call was an `aiops.audit` line.
- The console app's Now panel showed the same four items, in the same order, above the Workloads. The waste items were links.
- `api` is listed, even though its Sizing was approved in 11.2b. The Agenda ranks by waste cost alone.
- The MCP track of Phase 11 (11.1 to 11.4) is done.

## Part 2: the console moves into the app (11.5 to 11.7)

The console pages move into the app in slices 11.5 to 11.7. Each of these was split in two, as noted. The Kill switch and the Agenda go straight into the app. Each slice gets one release.

### 11.5a: Investigations in the app

**Asked 2026-11-08 (all recommended):** 11.5 is split. 11.5a covers reads: the Investigations list and page, the Scenario download, and the Token page. 11.5b covers actions: approving and rejecting, Report feedback, and Jira filing. These move together as actions on the page.

A Report, Answer or Check verdict is shown as its fields, as text. There is no Markdown rendering, and no new dependency. *Why:* the model's bold and tables are lost. The app's no-HTML rule is kept.

**Built (2026-11-08):**

- `GET /api/v1/contracts/{id}/investigations`.
- `GET /api/v1/investigations/{id}`. An Investigation of a Contract no longer loaded answers 404.
- `GET /api/v1/investigations/{id}/scenario.yaml`. It uses `console.scenario_file`, which the Jinja page shares, so the two files are the same.
- `POST /api/v1/token`. It needs identity and same origin. It uses `console.set_cookie`, which the Jinja form also uses. A 401 now names `/app/?view=token`.
- The app's `Investigations.tsx` has the list, the page (it updates every 5 s while running), and the token page.
- The header's Investigations and Approver token links, and the Agenda's items, link to the app's pages.
- The Jinja pages stay. The app's page links to the older page for deciding, until 11.7.

**Checked live (2026-11-08, Mac Hub, v0.24.0, in the console app):**

- demo-shop's Investigations were listed newest first. That is the 2026-11-06 Question, then four Triages. Each was a link.
- The Question's page showed its Answer and three Evidence lines, as text. It showed its tokens (41516 in, 2014 out), its cost (0.0052 USD), and one masked name. It had no decisions. Its nine tool calls showed their arguments. Seven failed, because the kind cluster is stopped.
- Save as Scenario downloaded `demo-shop-706cb355.yaml`. It is 7.8 KB, a Question Scenario with eight tools' recordings. The Hub logged a 200.
- The token page refused a wrong token with "that is not the approver token" (`POST /api/v1/token` gave 403). The browser's cookie was left as it was.

### 11.5b: act on Investigations in the app

**Built (2026-11-08):**

- `POST /api/v1/investigations/{id}/decision`. It takes the pause's deadline, as the page showed it, plus approve and a reason.
- `POST /api/v1/investigations/{id}/feedback`. It takes right, or wrong with 1 to 2000 characters.
- `POST /api/v1/investigations/{id}/jira`.
- Each is a change. It needs same origin, identity and the cookie.
- Each goes through the Jinja console's own function. So each records `console:<login>`.
- A Kill switch, a Stale pause, or a filed issue refuses in the same way in both consoles. The Hub's words are shown on the page.
- The page gains `jira` (the project, the issue and its link, and who is filing, since when).
- The page also gains `can_judge`. It is true for a finished Triage's or Check's Report.
- The app's page has Approve, Reject (with a reason), Right, Wrong (with what was wrong), and Create Jira issue.
- After each action, the page as it is afterwards comes back and replaces it.
- The link to the older page for deciding is gone.

**Checked live (2026-11-08, Mac Hub, v0.25.0, in the console app):**

- The 2026-10-24 03:05 Triage's page offered Right and Wrong. It had no decision panel and no Jira line. demo-shop has no Jira.
- Right added "console:<login>: right" under the buttons, without a reload.
- Wrong (it drafts a Teaching with the model), Approve, Reject and Create Jira issue were left to the tests.
- *Why:* no Investigation was paused, because the kind cluster is stopped. And no Contract has Jira.

### 11.6a: Teachings in the app

**Asked 2026-11-08:** 11.6 is split. 11.6a covers Teachings. 11.6b covers Checks and Health. The old main page's leftovers move by the end of 11.6.

**Built (2026-11-08):**

- `GET /api/v1/contracts/{id}/teachings`. It lists proposed, approved, rejected and retired Teachings, newest first. It also says how many approved ones do not fit in a prompt.
- `POST .../teachings` (under the same `/api/v1/contracts/{id}/` prefix as the GET). It creates a Teaching written by the Operator. That Teaching is approved at once.
- `POST .../teachings/{n}/decision`. It approves with the text as edited, or rejects.
- `.../retire`.
- Each is a change. It needs same origin, identity and the cookie. Each records `console:<login>`, through the Jinja console's own function.
- A Teaching of another Contract, reached through this Contract's path, answers 404.
- The app's Teachings page replaces the link to the older page.

**Checked live (2026-11-08, Mac Hub, v0.26.0, in the console app):**

- demo-shop's Teachings page showed none waiting, and none approved.
- A Teaching was written for every alert. It was listed as approved by `console:<login>` at once. The form was cleared.
- Retire moved it to Rejected and retired: "retired by console:<login>". So it never reached a prompt.
- Approving and rejecting a draft were left to the tests. A draft needs a wrong Report's correction. That costs a model call.

### 11.6b: Checks and Health in the app

**Built (2026-11-08):**

- `GET /api/v1/contracts/{id}/checks` lists the Check schedules. For each, it shows its last Run's status and verdict, its Open failure, and its last skip. It also gives its latest 30 Deploy verifications, newest first.
- `GET /api/v1/contracts/{id}/health` gives:
  - the Contract's integrations, probed now, through the Jinja console's own function;
  - its last finished Investigation;
  - its config as YAML, with secrets by name, and whether each one decrypted;
  - this month's model spend and budget;
  - the Hub's settings, masked and redacted.
- The app's Checks and Health pages replace the link to the older Health page.
- The older main page keeps the Contracts table and Ask a Question, until 11.7.
- Health checks only the Contract shown. This is the rule for every app page.

**Checked live (2026-11-08, Mac Hub, v0.27.0, in the console app):**

- demo-shop's Checks page said no Checks were scheduled, and no Deploy verifications. It has neither.
- Its Health page probed the integrations then:
  - kubernetes and prometheus on kind were FAILING. The name-resolution error was shown as text. The kind cluster is stopped. The long error text was capped.
  - runbooks, incidents and sizing were ok.
  - Each had its time.
- The last successful Investigation linked to the 2026-11-06 Question.
- The month's model spend was 0.0052 USD. That is the Question's cost. No budget is set.
- Masking of the config and the Hub's settings was left to the tests.

### 11.7a: Questions in the app

**Asked 2026-11-08:** 11.7 is split. 11.7a covers Questions in the app. 11.7b deletes the Jinja console. The old main page's Contracts table becomes an app page. This was the Operator's choice over dropping it. Old `/console` addresses redirect into the app. An Investigation's address redirects to its page (recommended).

**Built (2026-11-08):**

- `POST /api/v1/contracts/{id}/questions`.
- `POST /api/v1/investigations/{id}/questions`. This is a follow-up.
- Each is a change. It needs same origin, identity and the cookie. Each goes through the Jinja console's own `ask`. So a Question from either has no Slack thread.
- The question is 1 to 4000 characters. Otherwise the API answers 422.
- A follow-up is refused, with 409, while the Question is still answering, or when it is on another kind.
- The page gains `can_follow_up`.
- `GET /api/v1/overview` lists every Contract. Each shows its clusters, its operator channel, its model, and this month's model spend and budget.
- The app gets: "Ask a Question" on a Contract's Investigations (its page opens running), "Follow up" on a Question's page, and a Contracts page in the header.
- Health's time column no longer wraps.

**Changed after the live check (2026-11-08):**

- The Operator's follow-up ran, twice. But the page gave no sign of it. The page showed only the newest Answer. It never showed what was asked.
- **Fix:** migration 0025, `question_turns`. The follow-up's claim records its question, and the Answer it replaces, in the same statement. Only a claim that wins records one. So a Slack redelivery adds none.
- The page gains `asked` (the question that the Answer answers), and `exchange` (each earlier question with its Answer, or none when that Run failed).
- Follow-ups before 0.28.1 are not recorded.

**Checked live (2026-11-08, Mac Hub, v0.28.0, then v0.28.1 with migration 0025, in the console app):**

- The Contracts page listed demo-bank and demo-shop, with their clusters, operator channel, and model (the Hub default). It showed the month's spend.
- A Question was asked from demo-shop's Investigations. The page opened running, and then answered on Haiku. The kind cluster is stopped, so the answer came from its tools' configuration.
- On 0.28.1, one follow-up showed the earlier question with the Answer it got. Then "Follow-up: Which namespaces again?" appeared above the new Answer (demo, lab and orders).
- The Question cost 0.0062 USD in all, three follow-ups included.

### 11.7b: the Jinja console deleted

**Built (2026-11-08):**

- The Jinja console is gone. That covers its pages, `templates/`, `static/console.css`, and the `jinja2` dependency. `markdown-it-py` stays, as another package's dependency.
- `console.py` keeps the doors and helpers that the app and the Hub MCP server share:
  - identity;
  - the `aiops_actions` cookie, unchanged, so no browser pastes the token again;
  - same-origin checks;
  - the Scenario file;
  - the `console:<login>` actor.
- It gains `redirects()`:
  - a GET to `/console/investigations/<id>`, or one of its files, answers 303 to that Investigation's app page. The id is quoted, so it stays one parameter.
  - any other `/console` address answers 303 to `/app/`.
  - a POST answers 405.
  - a Hub without the console answers 404, as before.
- The tests that went through the old pages now go through `/api/v1`.
- `hub-smoke` checks the app's API, Health included, and the redirect.

**Checked live (2026-11-08, Mac Hub, v0.29.0):**

- On its local port, `/console` and `/console/health` answered 303 to `/app/`.
- `/console/investigations/<id>` answered 303 to that Question's app page.
- A POST to `/console/questions` answered 405.
- Through `tailscale serve`, the Operator's old address of the 2026-11-08 Question opened its app page, with its exchange.
- `/console` opened the Workloads page.

## Phase 11 done (2026-11-08)

- Claude Code lists a Contract's top 5 Sizings, and approves one, through the Hub MCP server (11.1a and 11.2b).
- A policy change arrived as a pull request on the contracts repo (11.3).
- The Jinja templates are gone. Every console test goes through the app's API (11.7b).
- Released as v0.18.0 to v0.29.0.
