# Running the Hub

**In short:** the Hub runs every Contract from one machine you own, a VM or your laptop.
It is a compose stack with the Hub, Postgres, Caddy and nightly backups.
This page covers setup, Slack, the web console, the Hub MCP server, the in-cluster deploy, models and cost, and every `SQUERO_*` setting.

- [The compose stack](#the-compose-stack)
- [Slack](#slack)
- [Web console](#web-console)
- [Hub MCP server](#hub-mcp-server)
- [Running as a service in one cluster](#running-as-a-service-in-one-cluster)
- [Other models](#other-models)
- [Model and cost](#model-and-cost)
- [Configuration](#configuration)

## The compose stack

[`deploy/hub`](../deploy/hub) is a compose stack with four parts:

- the Hub;
- Postgres;
- Caddy;
- nightly backups.

`make hub-smoke` runs it on Linux and macOS.

### 1. Host

- Use Docker with compose.
- Copy `deploy/hub` to the host.
- For client alerts, you need:
  - a DNS name pointing at the host;
  - ports 80 and 443 open.
- Without them (a laptop behind NAT), set `SQUERO_DOMAIN=localhost` and other ports in `.env`.
  - Then only senders that reach the machine can alert.
  - Examples: a local kind cluster, or `curl -k`.

### 2. Contracts

Clone your private contracts repo into `deploy/hub/contracts`, with a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/squero-contracts      # add the .pub as a read-only deploy key
GIT_SSH_COMMAND='ssh -i ~/.ssh/squero-contracts' git clone git@github.com:<you>/contracts.git contracts
git -C contracts config core.sshCommand 'ssh -i ~/.ssh/squero-contracts'
```

### 3. Age keys

Put every Contract's age key in one file. Only the Hub's user (uid 10001) can read it.

```bash
cat acme.agekey globex.agekey > age-keys.txt && chmod 0400 age-keys.txt
sudo chown 10001 age-keys.txt   # Linux only: Docker Desktop on macOS maps file owners itself
```

### 4. Settings

- Copy `env.example` to `.env`. It holds the Postgres password and the domain.
- Copy `hub.env.example` to `hub.env`. It holds the approver token and the model key.

### 5. Start

```bash
docker compose up -d --wait
```

Caddy gets a certificate for your domain.

### 6. Operator access

```bash
tailscale serve --bg 8080
```

- This publishes the API to your tailnet over HTTPS.
- The API listens only on `127.0.0.1:8080`.
- Never publish port 8080 another way. Ports Docker publishes bypass host firewalls.

### 7. Alert sources

Each Contract has three alert sources. The first two take that Contract's token. PagerDuty's is checked by its signature:

| Source | Address |
|---|---|
| Alertmanager | `https://<domain>/contracts/<id>/alertmanager` |
| Grafana Alerting | `https://<domain>/contracts/<id>/grafana` |
| [PagerDuty](contracts.md#pagerduty) | `https://<domain>/contracts/<id>/pagerduty` |

### What is public

Only three alert routes are public:

- `POST /contracts/<id>/alertmanager`
- `POST /contracts/<id>/grafana`
- `POST /contracts/<id>/pagerduty`

Their bodies are up to 1 MB. See the [`Caddyfile`](../deploy/hub/Caddyfile).

Investigations, decisions, metrics and the API docs answer only on `127.0.0.1`.

### Updating Contracts

`./update-contracts.sh` does the following:

1. Pulls the contracts repo.
2. Validates every Contract inside the image.
3. Restarts the Hub.

If any Contract is invalid, it rolls the clone back. It leaves the running Hub alone.

*Why:* one bad Contract would otherwise stop alerts for all of them.

### Backups

- A `pg_dump` runs whenever the newest one is a day old.
- The Hub checks every 5 minutes, against the dumps' file times.
  - A failed dump is retried in 5 minutes.
  - A laptop's sleep stops the container's clock. It never pushes a backup time back.
- Dumps are kept 14 days, in `deploy/hub/backups`.
- Dumps are owner-only, because they hold Contract data: Reports and raw tool results.
- A dump on the host is lost with the host. Copy `backups/` somewhere else.

### What the smoke test covers

`make hub-smoke` runs the stack locally and in CI, with the demo Contracts. It checks that:

- the stack comes up healthy;
- every path except the alert route returns 404 through Caddy, including `../` and `%2F` tricks and oversized bodies;
- an alert is stored;
- a backup restores;
- a bad contracts update is refused.

CI does **not** cover:

- `tailscale serve`;
- real Let's Encrypt certificates;
- the deploy-key clone;
- the arm64 image.

## Slack

Each Investigation is one thread in its Contract's channel.

- The parent message shows the status: investigating, waiting for your approval, done, or failed.
- Replies carry the Proposed actions, each Approval decision, and the Report or the error.

### Setup

1. Create the app from [`deploy/slack-app.yaml`](../deploy/slack-app.yaml) (api.slack.com/apps > From a manifest).
   - Install it.
   - Set its bot token as `SQUERO_SLACK_BOT_TOKEN`.
2. For the buttons:
   - Go to Basic Information > App-Level Tokens.
   - Generate a token with `connections:write`. Set it as `SQUERO_SLACK_APP_TOKEN`.
   - Set your own member ID as `SQUERO_SLACK_OPERATOR_ID`. Find it in your profile, under ⋮ > Copy member ID.
3. Make one channel per Contract, and invite the bot: `/invite @squero`.
   - The bot can't post where it isn't a member.
4. Put the channel's ID in the Contract.
   - Find it in the channel details, the `C...` at the bottom.
   - Config: `slack: {operator_channel: C0123ABCD}`.
   - Two Contracts can't share a channel.

### Buttons

While Proposed actions wait, the parent message has two buttons:

- **Approve**, with a confirm step.
- **Reject**. A modal asks for a reason, which the agent reads.

How the clicks are handled:

- Clicks arrive over Socket Mode, so the Hub opens no Slack endpoint.
- Clicks take the same decision path as `POST /investigations/{id}/decision`.
- Only `SQUERO_SLACK_OPERATOR_ID`'s clicks count, and only in the Investigation's own Contract channel.
- Anyone else gets a private "Only the Operator can decide".
- Each button names its pause. So a second click, or a click left over from an earlier pause, decides nothing.
- Once the actions are decided (in Slack or through the API), or go Stale, the buttons disappear.
- The thread then says who decided, or why it went Stale.

### Client reports (optional, per Contract)

Add `client_channel: C...` next to `operator_channel`.

- It must be a Slack Connect channel shared with the client, with the bot invited.
- A finished Report then gets a **Send to client** button in your thread.
- Nothing reaches the client until you click it. It goes once.

What the client gets:

- plain text with the summary, root cause, severity, what is affected, and the Recommendations.

What stays with you:

- Evidence,
- Executed actions,
- the Runbook,
- the Investigation id.

The client message never has buttons.

**Read before you send.** Only the Evidence list is held back. The summary and root cause are the model's own words. They can still name a pod or quote a log line.

A channel belongs to one Contract, as operator channel or client channel, never both.

### Stale notices (audit A14)

When one of a Contract's reads has failed for 3 hours without a break, the Hub posts once to its operator channel.

- The reads covered are the ones the Now panel lists as stale: a cluster's Sizing reads, its Relay, its Bill reads.
- Example post: `:warning: *[acme]* eu: Sizing reads failing since 2026-11-15 16:16 UTC (3 h): ConnectError: Name or service not known`
- With `SQUERO_CONSOLE_URL` set, the post links to the console.

Follow-ups, one thread per cluster and kind of read:

- A reminder there, once a day, while it fails. Example: `Still failing, 2 days 3 h now`.
- A reply when it reads again. Example: `Sizing reads work again, after 5 days 8 h`.
- A cluster taken out of the Contract ends quietly.

Timing and retries:

- The Hub looks every 10 minutes.
- Each post is claimed in Postgres before it is sent. So a restart never posts one twice.
- A post that Slack refuses is tried again at the next look.
- A crash between the claim and the post delays it a day.
- A refused recovery reply is not tried again.

No notices are posted:

- for a Contract without `slack:`. Its Now panel still lists them;
- for a Hub without `SQUERO_SLACK_BOT_TOKEN`.

A Hub that is itself down posts nothing. A watchdog for that is Deferred.

### Questions

Mention the bot in a Contract's operator channel, for example `@squero what changed in checkout today?`.

- It answers in that thread, with an Answer backed by Evidence.
- It uses only that Contract's read tools.
- A Question never proposes an action.
- Mention it again in the same thread to follow up on the same Question.
- Only `SQUERO_SLACK_OPERATOR_ID` can ask.
- The bot receives only messages that mention it. It never receives other channel traffic.

A private refusal is sent when someone mentions the bot:

- in a client channel;
- in a channel no Contract owns;
- in a Triage thread;
- while the last Answer is still coming.

### Without the app token

Without `SQUERO_SLACK_APP_TOKEN`:

- there are no buttons;
- there are no Questions;
- you decide through the API;
- Client reports can't be sent.

Everything posted is escaped, redacted and capped.

- A label like `<!channel>` arrives as text, never as a ping.
- A Slack failure, including a dropped socket, is logged. The socket reconnects.
- A Slack failure never fails an Investigation.

## Web console

The console is the Operator's view of every Contract.

- It is a React single-page app.
- The Hub serves it at `/app/`.
- It is the only console. The older Jinja pages are gone.
- Their old addresses under `/console` redirect into the app. An Investigation's old address goes to its page.

It has no passwords. Instead, `tailscale serve` adds your Tailscale identity to every request it proxies. It arrives as `Tailscale-User-Login`. Any copy a client sends is stripped.

### Who can sign in

1. Set `SQUERO_CONSOLE_OPERATORS='["you@example.com"]'` in `hub.env`. It lists the Tailscale logins that may sign in.
2. Open `https://<vm>.<tailnet>.ts.net/app/` from a device on your tailnet. This is the `tailscale serve` address from step 6 above.

Anything else gets a 403. That includes a tagged device, which has no user.

### The look

- **A sidebar** groups the pages (from 0.64.0, slice 21.1):

  | Group | Pages |
  |---|---|
  | Overview | Overview, Fleet |
  | Optimization | Workloads, Cost, Nodes, Automation |
  | Investigations | Investigations, Teachings, Checks, Health |
  | Settings | Contracts, Approver token |

  - A Contract's pages link with the one picked.
  - "Collapse" at its bottom shrinks it to icons. The browser remembers it.
- **The Overview** (from 0.64.0, slice 21.2; a grid of cards since slice 21.6b) is a Contract at a glance, one card for each of its pages:

  | Card | Shows | Opens |
  |---|---|---|
  | Savings | the Savings available (in the accent) and realized | Cost |
  | Waste | the wasted share of the requests with a meter, the requests' cost, Idle GPUs beside it (with GPUs) | Workloads |
  | Automation | the share of ready containers a Sizing rule automates where an Actuator runs, with a meter | Automation |
  | Nodes | the nodes read, those that could go now and after the Sizings (where the Hub judged it), the clusters not read | Nodes |
  | Agenda | its count and first three items; "See all" opens the rest in place | (no page of its own) |
  | Investigations | the latest three, each with its status | Investigations |

  - Without prices, cores and GiB, never an amount.
  - A card whose read failed says so ("Couldn't read the nodes."); the others stand. Only the Workloads read fails the page.
  - A Contract with nothing stored yet says so above the cards, with "Check Health".
- **Workloads** (from 0.64.0, slice 21.3a): tiles for the waste, the requests' cost and the count, then a table of Workloads ranked by their waste:
  - Savings, CPU and memory as "request → Sizing" per pod (summed over the Workload's containers), pods, the policies as pills, Automation ("automated", "partly", "no"), risks;
  - chips for Automated and Not automated, the cluster and namespace, a search over the names (each kept in the address);
  - a bar under each Savings (the row's share of the largest shown) and under each CPU and memory (the share of the request its Sizing keeps; none for a raise, or where a container has no request set);
  - a Total row: the Savings and pods of the rows shown, so it follows the filters. A request and a Sizing are per pod, so they have no sum;
  - a Workload's waste in cores and GiB and its requests' cost in its detail, under the table.
  - Automation shows, never changes there: it is a pull request to the contracts repo, `propose_change` in Claude Code.
- **Cost**: the grouping and comparison on top, tiles for the requests and the Savings available (with the change when compared), the realized Savings, the groups, then the charts.
  - The groups' table (slice 21.6c): a bar under each group's requests and Savings (its share of the largest group), and a Total row, the Hub's own total and change.
  - The charts (here since slice 21.6b): CPU and memory over 14 days, summed over the ready containers times their pods: requests, Sizings, usage (the Sizings' basis), the waste between requests and Sizings. `GET /api/v1/contracts/{id}/totals` serves them, read on its own; regrouping or comparing the cost never redraws them.
- **Nodes, Automation, Fleet** (slice 21.3b): each cluster or Contract a card; verdicts, Automation states and Kill switches as coloured badges; the Fleet's totals per currency as tiles on top, never converted.
  - **Nodes** (slice 21.6d): figures on top (nodes read, Nodes that could go now and after the Sizings, spot nodes where a cluster names a spot label, clusters not read); each cluster's facts as a list (read, autoscaler, largest Room, whose rules, the last report of Blockers, Placement, spot); a table that fits its card. The figures of nodes that could go are in ink: an upper bound, not part of Savings.
  - **Automation** (slice 21.6d): figures of the containers' states, one state each (on, off, stopped by a Revert, ended by rollbacks), counting the containers its Actuators reported on in 90 days; chips All, On and Not on, kept in the address; each last outcome a badge.
- **Investigations, Teachings, Checks, Health, Contracts** (slice 21.4): statuses as coloured badges, each integration's Health a tile (a failing one framed red), the action that goes ahead a filled button and Reject or Retire a red-outlined one.
  - **Investigations** (slice 21.6e): figures and filter chips over the list, the table in a card; an Investigation's page opens with a header card and its figures. See [Investigations](#investigations).
  - **Checks, Health, Teachings, Contracts, Approver token** (slice 21.6f): figures on top, every table in a card under its title, a verdict as a badge, a meter under a model spend that has a budget, the token form in a card. See each page's section below.
  - **Workloads and Cost** (slice 21.6f): a picked Workload's detail and the autoscaled Workloads each in a card; the price lines (Workloads), and the price, Bill and comparison lines (Cost), in one card under the figures, each line as it was; the card is named for what it holds.
- **The look** (slice 21.5a): Hanken Grotesk and JetBrains Mono, served by the Hub itself; one accent (a teal-green) with the Savings in it and every other figure in ink; status colours only on badges; charts with the Sizings as their one coloured line, redrawn when the system changes theme; a flat block while a page loads; turning the Kill switch off asks first ("Yes, turn it off"), turning it on never does.
- **Density** (slice 21.6a): 13 px text, 36 px table rows, small uppercase labels on figures, table heads and sidebar groups; each figure in its own card, with a thin meter where it is a share and a footnote.
- **Numbers** (slice 21.6c): quantities and money in tables are in JetBrains Mono, so their digits line up, and never break from their unit; dates and names are not. A table too wide for its window scrolls sideways inside its card.
- **The title row** (slice 21.6a) opens every page but an Investigation's: the page's icon and name, its counts (clusters, workloads, Contracts) and controls (Cost's grouping and comparison), and the Contract switcher at the right when there are several.
- **The header** names the Contract and the page. On a Contract's own pages it has the Kill switch: "Turn on the Kill switch", or, while it is on, a red line saying since when and by whom, with "Turn it off".
- The app uses one stylesheet and the app's own scripts.
- The Hub serves them behind the same identity.
- No script, font or file comes from anywhere else. So the page's `default-src 'self'` policy stays strict.
- Light and dark follow the device.
- Reports, Answers and Check verdicts are shown as text.
  - A Report is written by a model that read tool output.
  - So nothing in it can become markup or a script.

### The approver token, once per browser

Your identity alone opens only the **approver token** page.

- Paste `SQUERO_APPROVER_TOKEN` there, at `/app/?view=token`. Do this once per browser.
- After that, every other page opens, along with approving, rejecting and asking.

How the browser remembers:

- The browser keeps a cookie that proves the token without holding it.
- The cookie is an HMAC.
- Its flags: Secure, HttpOnly, SameSite=Strict. It lasts 30 days.

Why the identity alone is not enough:

- The identity header alone shows nothing.
- Other things on the VM could send that header.

Decisions from the console:

- A decision must come from the console's own page. The check uses `Sec-Fetch-Site` or `Origin`.
- So another site can't make your browser post one.
- It takes the same path as Slack.
- The thread says who decided.
- The page names its pause. A page left open from an earlier pause decides nothing.

### Workloads

**Workloads** (`/app/`) is the console app's first page.

- It shows each Contract's Workloads from the Hub's latest stored Sizings.
- They are ranked by waste.
- Each row shows the Workload's pods, risk kinds, and each cluster's freshness.
- Pick one to see:
  - its containers' requests and Sizings;
  - their risks;
  - charts of request, Sizing and usage basis.
- Charts cover 14 days (hourly), or 400 days (daily).
- The page reads only stored runs. It never reads a client's Prometheus.

Access:

- The page needs the Operator's Tailscale identity.
- Its JSON API (`/api/v1/`) also needs the approver token's cookie.
- Without the cookie, the API links to the token page instead. The cookie is set for the whole Hub.

Security of the page:

- Its CSP allows only the Hub's own origin.
- Everything from the API is rendered as text.
- ESLint refuses raw HTML.
- A test scans the app's source for raw HTML.
- A component test renders a risk holding markup, and shows it as text.

Building the app:

- It is built in the image's Docker build, in `console/`.
- The stack is Vite, React, TypeScript and uPlot.
- Node 22 is pinned by digest.
- `npm ci` runs from the lockfile, with install scripts off.
- Only its static files ship.

Development:

- `make console-check` runs what CI runs: types in sync with the API, typecheck, lint, tests and build.
- `make console-dev` serves the app on `127.0.0.1:5173` against a local Hub.
  - The Hub is set by `SQUERO_DEV_HUB`. The default is `http://127.0.0.1:18100`.
  - `SQUERO_DEV_LOGIN` sets the Tailscale identity. It is added in development only.
- After changing the API, run `make console-types`. Then commit `console/openapi.json` and `console/src/api-types.ts`.

### Investigations

**Investigations** (`/app/?view=investigations`) lists a Contract's Investigations, newest first. Passing Checks are left out.

On top (slice 21.6e):

- three figures: awaiting approval, running, failed. They count the list, which holds the Hub's latest 100;
- chips for the kind (Triages, Questions, Checks) and the status (awaiting approval, running, failed, done: the badges' own words), kept in the address.

An Investigation has one status: a failed Check and a failed Run are both "failed", in red.

Each Investigation opens its own page. It starts with a header card (the title, its status, kind, Contract and start time), then what waits for a decision, then four figures: tokens, cost, tool calls (with how many failed) and what was masked. The page shows:

- the Report, Answer or Check verdict, as fields:
  - summary;
  - root cause, with its confidence and Category;
  - severity;
  - affected;
  - Evidence;
  - Recommendations;
  - executed actions;
- what waits for a decision, and when it goes Stale;
- Report feedback, and the Teachings drafted from it;
- related alert groups;
- decisions;
- tool calls, with each result's first 4000 characters;
- tokens, cost, and what was masked.

A running Investigation updates every 5 seconds.

A finished Investigation with its input recorded offers **Save as Scenario**.

Display rules:

- Everything is rendered as text.
- Alert titles come from client Alertmanagers.
- Reports come from a model that reads tool output.
- A model's Markdown shows as written.
- An Evidence link stays text. It never becomes a link.

Actions on the page. Each one is a change, recorded as `console:<login>`. Each needs your identity, the cookie, and the app's own page (`Sec-Fetch-Site: same-origin`).

- **Approve or reject** the pause the page shows. You can give a reason, which the agent reads. This works like Slack's buttons.
  - An older pause's deadline decides nothing.
  - Approving is refused while the Kill switch is on.
- **Mark a finished Report right or wrong.**
  - A wrong one needs what was wrong.
  - The agent drafts a Teaching from it.
- **File it into Jira** once, when the Contract has Jira.
  - After that, the page links the issue.

**Approver token** (`/app/?view=token`) earns the browser its cookie. It does this through `POST /api/v1/token`. That call works only from your identity and the app's own page. A call without the cookie links to the token page.

### Teachings

**Teachings** (`/app/?view=teachings`) lists a Contract's Teachings in three groups:

- Waiting for review. Each shows the alerts it is for, the Investigation and your words it was drafted from, and the approved one it would retire.
- Approved.
- Rejected and retired.

Actions, each recorded as `console:<login>`:

- Edit a drafted text, then approve it.
- Reject it.
- Retire an approved one.
- Write one by hand. It is approved at once.

The page warns when the approved Teachings don't all fit in a prompt.

On top (slice 21.6f), three figures: waiting for review, approved, and left out of prompts.

A drafted text is model output. The page shows and edits it as plain text.

### Checks and Health

**Checks and Health** (`/app/?view=checks` and `view=health`) shows two things.

**Checks** (`view=checks`) lists the Contract's scheduled Checks:

- schedule;
- next Run;
- the last Run's verdict and an Open failure, each linking its Investigation;
- the last skip, with its reason.

It also lists the latest Deploy verifications: where, which changes, when due, and their Run's verdict or why skipped.

On top (slice 21.6f), four figures:

| Figure | Counts |
|---|---|
| Checks | the scheduled Checks |
| Failing now | Checks with an Open failure, not those whose last Run failed |
| Deploy verifications passed | of the latest listed, those whose Run passed |
| Deploy verifications failed | those whose Run failed, by its verdict or its status |

A verdict is a badge: passed green, failed red, any other word neutral.

**Health** (`view=health`) checks the Contract shown only, now. It makes one cheap read per integration. Each read uses that integration's own tools and credentials. Examples:

- a cluster's deployments in its first namespace;
- Prometheus `vector(1)`;
- a Runbook search.

Health shows:

- each integration as ok, or FAILING with its error (redacted) and time;
- each Relay as connected (since when, its version and Protocol), or not;
- each Actuator with when it was last seen, its version and Protocol. It is FAILING only when its Protocol was refused;
- the last successful Investigation;
- this month's model spend against its budget;
- the Contract's config, with only the names of its secrets;
- the Hub's settings, with every secret masked.

Three of these are figures on top (slice 21.6f): how many of the integrations, Relays and Actuators are ok now, with a meter; the model spend, with a meter where a budget is set; the last successful Investigation. The config and the settings stay collapsed until opened.

### Questions and Contracts

**Ask a Question** sits at the top of a Contract's Investigations (`/app/?view=investigations`).

- It only reads, like a Slack Question.
- Its page opens at once.
- The page updates every 5 seconds until the Answer is in.
- A Question from the app has no Slack thread.

**Follow up.** A Question that is no longer answering offers **Follow up** on its page.

- It continues the same Question, with its earlier exchange.
- The page lists each earlier question, with the Answer it got.
- The follow-up sits above the Answer that answers it.
- This works from 0.28.1. Follow-ups before that are not recorded.
- A follow-up on a Question that started in Slack also answers in its Slack thread.

**Contracts** (`/app/?view=contracts`) lists every Contract:

- its clusters;
- its operator channel;
- its model;
- this month's model spend against its budget, with a meter where a budget is set.

Each row links to its own pages.

On top (slice 21.6f), two figures: the Contracts, with how many have a model budget, and the model spend this month over all of them. It is in USD, whatever a Contract's own prices, and is never added to a Contract's costs.

**Fleet** (`/app/?view=fleet`, `GET /api/v1/fleet`, from 0.63.0) lists every Contract at once. It is read-only, with one row per Contract. Each row links to that Contract's Workloads.

A row shows:

- its Kill switch, on or off. It can be turned on only on its own pages;
- its Agenda's counts by kind;
- a month of its waste and of its requests;
- its Idle GPUs and Unrequested GPUs, summed over its clusters, with their cost;
- each cluster's Relay and Actuator, with its version and Protocol. A refused one is shown in red, and says which side to upgrade.

Money rules:

- Money stays in each Contract's own currency.
- Totals show one line per currency, below the rows.
- Nothing is converted.
- A Contract without prices says so, and stays out of the totals.
- A GPU without a price stays out of the totals too.

Loading rules:

- It is computed on each load, one Contract at a time, as `now()` does.
- A Contract that can't be read shows its error on its own row.
- The other Contracts still show.

### Now

**Now** is on the Overview (from 0.64.0; above the Workloads before): its first five items, and "See all" for the rest. It is the Contract's Agenda: what to look at now.

The Hub ranks it without a model, in a fixed order of urgency:

1. the Kill switch, while it is on;
2. pauses waiting for an Approval decision, soonest to go Stale first;
3. failing Checks, newest first;
4. data the Hub can't read: Sizing or bill reads failing, oldest first;
5. a Relay or Actuator whose Protocol the Hub refused. It says which side to upgrade;
6. Bills outside their tolerance, biggest gap first;
7. running Investigations, oldest first;
8. the three Workloads whose waste costs most.

Leaving out approved Workloads:

- Item 8 leaves out each Workload with a current Approved Sizing.
- That applies on a cluster that runs an Actuator, whether applied or not.
- One line last counts them: "2 Workloads approved: their waste is read again within the hour".
- The next run that sizes it supersedes the approval.
- `now()` across Contracts doesn't count that line.

Each item links to where to act: its Investigation, its Workload, or the Cost page.

It is also available as `GET /api/v1/contracts/{id}/agenda`, and as `now` on the Hub MCP server.

### Automation

**Automation** (`/app/?view=automation`) is for each cluster of the Contract that runs an Actuator.

For each container the Actuator reported on in the last 90 days, the page shows:

- its last Actuation: outcome, pods, when, the requests it set, and the Actuator's reason, shown as text;
  - the outcome is a badge: failed and rolled back in red (the two the Actuator posts as warnings), applied in green, the rest neutral;
- its Automation status. That is one of: off, on, ended by 3 Rollbacks until a date, or stopped by a Revert until you approve one of its Sizings.

On top (slice 21.6d), four figures count the containers in each status, one status each, and chips show All, On or Not on. The figures count the containers the Actuators reported on, not the Overview's share of ready containers.

**Revert** on a container whose last Actuation is applied or failed asks for a Revert. This is the same as `revert_sizing`, and it is refused the same way, with the Hub's words.

- The button shows "reverting" until the Actuator reports it done.
- It is stored as `console:<login>`.
- It works from the console's own page only.
- The page says when the Kill switch is on, since a Revert is then refused.

### Kill switch

**Kill switch** is on every console app page. From there you can turn a Contract's Kill switch on, or off.

While it is on:

- approving a Proposed action is refused on every path: Slack, the console, the API and the Hub MCP server;
- the pause is kept, so you can still reject it;
- you can approve it once the switch is off.

A change approved just before the switch went on is refused where it would run. It is recorded as a FAILED Executed action.

Reads, Questions and Reports go on as usual.

Only this page turns the switch off. It works from the same origin, with your identity and the approver cookie, like approving. The Hub's log names who turned it on or off.

### Where to run it

With no operators set, the console doesn't exist:

- `/app/`, `/api/v1/` and `/console` all answer 404.

**Set it only where nothing but `tailscale serve` reaches the Hub's port.**

- That condition holds on the VM. But local processes, and other containers on the compose network, could still send the header themselves.
- Never set it on the in-cluster deploy, where any pod can reach the Service.
- Through Caddy, the console is a 404.

## Hub MCP server

The Hub MCP server is your own MCP endpoint on the Hub. Use it from Claude Code, or any MCP client, on your devices.

With it you can:

- ask what needs you now;
- ask for a Contract's top Sizings, cost, Savings, bill, Investigations and Checks;
- ask the Hub's agent a Question;
- decide a pause;
- approve a Sizing;
- propose a change to a Contract's Sizing policy, as a pull request;
- turn a Contract's Kill switch on.

The Investigation agent never gets these tools.

### Setup

1. Set `SQUERO_MCP_TOKEN` in `hub.env`. Generate it with `openssl rand -hex 32`.
   - Put it beside `SQUERO_CONSOLE_OPERATORS`.
   - Restart the Hub.
2. On a device on your tailnet, add the server to Claude Code. Use the `tailscale serve` URL and the token:

   ```bash
   claude mcp add --transport http squero-hub https://<vm>.<tailnet>.ts.net/mcp/ \
     --header "Authorization: Bearer <SQUERO_MCP_TOKEN>"
   ```

   - The URL ends in `/mcp/`.
   - `127.0.0.1` won't do. The Hub needs the identity that `tailscale serve` adds.

### Two doors

A call needs two things:

- the token; and
- a listed Operator's `Tailscale-User-Login`.

*Why:* the token alone could come from anything that read the Hub's settings. The identity header alone could come from anything else that reaches the port.

Startup refuses:

- a token shorter than 32 characters;
- a token equal to the approver token, or to a Contract's webhook token;
- a token set without `SQUERO_CONSOLE_OPERATORS`.

Other facts:

- Caddy's public port answers 404 for `/mcp/`.
- It uses stateless HTTP. A Hub restart leaves no session to lose.

### Tools

**Read tools.** All of these are marked read-only.

- **`list_contracts`**: ids, names, clusters, and whether each Kill switch is on.
- **`sizings(contract, top=10, cluster, namespace, risky)`**: the Workloads ranked by what their waste costs, as the console's Workloads page ranks them.
  - A request its HPA divides by is marked `held`, with the manifest change that frees it.
  - A DaemonSet's container is also sized per instance type (`by_instance_type`). `applied` says whether its Actuator applies those, or the one Sizing.
  - Each autoscaled Workload's Floor is shown against its HPA's minimum. A vLLM Workload without an HPA is shown against its replica count.
  - `idle_gpus` covers the Contract and each Workload with GPUs. See [Idle GPUs](optimizer.md#workload-usage).
- **`sizing(contract, cluster, namespace, name, kind)`**: one Workload's Sizing, as the agent's own tool gives it.
- **`nodes(contract, cluster, top=20)`**: each cluster's nodes, with their Room and whether each could be removed.
  - Blocked nodes come first.
  - A node's Blockers are shown one per Workload, capped at 5. The rest are counted.
  - Each Blocker comes with the change that removes it, and its manifest where there is one.
  - The nodes that could go now, and after the Sizings, with their monthly cost.
  - That cost is an upper bound. It is no part of Savings.
  - GPU nodes are shown with their GPUs, and each cluster's `unrequested_gpus`.
- **`cost(contract, by, compare_days)`** and **`savings(contract, compare_days)`**: as the Cost page shows them. `cost` also shows the Contract's idle GPUs, beside the Savings.
- **`bill(contract, days)`**: as `sizing bill` does.
- **`investigations(contract, open, limit)`**: newest first. Each has its Category, severity and summary, Answer or verdict.
- **`investigation(contract, id)`**: one Investigation in full, with its Evidence, pending Proposed actions and Approval decisions. Another Contract's id answers "no such Investigation".
- **`checks(contract)`**: each Check's schedule and last Run.
- **`now(contract)`**: the Contract's Agenda, as the console's Now panel shows it.
  - `now()` without a Contract shows only how many items of each kind every Contract has.

**`ask`.** This one is not read-only.

- `ask(contract, question)` starts a Question, like the console's.
- It has the same 4000-character limit and budget.
- It is never posted to Slack.
- It returns its id at once, so your client asks before it runs.
- Read the Answer with `investigation`.

### Deciding, and the Kill switch

`investigation` shows a pause's Proposed actions, and its `pause` token. The token is the pause's exact deadline, as text.

`decide(contract, id, pause, approve, reason)` approves or rejects every action of that pause. It works like Slack's buttons.

- An older token never decides a newer pause.
- Another Contract's id is "no such Investigation".
- The thread and the decisions table say `<login> via MCP`, recorded as `mcp:<login>`.
- A rejection's reason reaches the agent with its placeholders put back.

`turn_on_kill_switch(contract)` stops every change to that Contract's systems at once.

- Nothing on the Hub MCP server turns the Kill switch off. Only the console does.
- `decide` is marked destructive, so your client asks first.
- `turn_on_kill_switch` is marked not read-only.

### Approving a Sizing

`approve_sizing(contract, cluster, namespace, name, container, cpu, memory, kind)` approves one container's latest Sizing. The Actuator then applies it. Nothing changes in a cluster before that.

- `cpu` and `memory` must be the Sizing exactly as `sizing` or `sizings` showed it, for example `115m`, `96Mi`, or null where none was shown.
- So a newer Sizing is never approved unseen.
- A mismatch answers with the latest values.

It is refused:

- for a container that is learning or excluded;
- for a container whose Sizing keeps its requests;
- for a pod's name. Name the Workload instead;
- while the Contract's Kill switch is on.

What it stores:

- the requests it changes. One kept stays as it is.
- They come from that run.
- They are stored as `mcp:<login>`.
- `sizing` and `sizings` show who approved it, and when.

When it stops applying:

- The next Sizing of that container supersedes it, whatever its values. That is for good.
- A run failing there does not supersede it.
- Nor does a run that read the cluster without that container, such as one whose Workload was deleted.

Timing:

- The cluster's Actuator applies it within a minute.
- One superseded before then is never applied.

DaemonSets:

- A DaemonSet's Sizings per instance type are approved with it. The answer lists them as `by_instance_type`.
- This also holds where its pooled Sizing keeps its requests, and only a type's changes them.
- A DaemonSet with more types than the ten `sizings` shows is refused.
- On a cluster whose Actuator applies types (0.54.0 and later), the types alone decide.
  - A container whose types keep their requests, or are all still learning, is refused.
  - That holds whatever its pooled Sizing says.

`approve_sizing` is marked destructive, so your client asks first.

### Reverting a Sizing

`revert_sizing(contract, cluster, namespace, name, container, kind)` undoes what the cluster's Actuator holds for one container. `kind` defaults to `Deployment`.

What happens:

- Within a minute, the container's pods go back to their template's requests, in place.
- The Actuator stops holding the container.
- A `SizingReverted` Event is written, and a `reverted` Actuation is recorded.
- If nothing was held, the answer says so.
- A Step-up or a Burst of that container is dropped with it.
- Automation of that container stops, until you approve one of its Sizings.

It is refused:

- for a cluster without an Actuator;
- for a namespace that is not the cluster's;
- for a container the Actuator never reported on;
- while the Contract's Kill switch is on.

Other facts:

- It is stored as `mcp:<login>`.
- An Actuator of 0.40.0 or older never reads Reverts. The tool still answers `asked`, and nothing happens.
- The tool is marked destructive, so your client asks first.
- A Revert also drops the container's Step-up, if it has one. See [how a Step-up ends](client-cluster.md#how-a-step-up-ends-and-where-it-shows).
- A Revert can be asked of a container that has only a Step-up.

### Proposing a change

`propose_change(contract, edits, title, why)` opens a pull request on the contracts repo. It is not marked read-only.

- The repo is set by `SQUERO_CONTRACTS_REPO`, with the token `SQUERO_CONTRACTS_TOKEN`.
- The pull request edits that Contract's Sizing policy and nothing else.
- You review it and merge it.

What the Hub checks before it writes anything:

1. GitHub's `main` is the commit the Hub loaded.
2. `<id>/contract.yaml` there is the file the Hub loaded.
   - If not, it refuses. Pull and restart the Hub after a merge.

Then it makes the edit, with comments and layout kept. It refuses if:

- anything but `sizing` changed;
- nothing changed;
- the Contract would not load.

Then it:

1. creates a branch named `squero/<id>-<UTC time>`;
2. commits there;
3. opens the pull request.

It never writes `main`. Nothing changes until you merge it and restart the Hub on it.

#### The `edits` list

The edits are applied in order. Each edit is one of:

| Edit | What it does |
|---|---|
| `set_rule` | Sets a rule. The match can use any of `cluster`, `namespace`, `kind`, `name`, `container`. A rule with exactly that match gets the `policy`. Otherwise a new rule goes first, since the first match wins. `automate: true` or `false` turns [Automation](client-cluster.md#automation) on or off for it. Left out, it keeps what the rule has. |
| `remove_rule` | Removes the rule with exactly that match. |
| `set_default` | Sets the default policy. |
| `set_policy` | Changes one of the Contract's own policies: `name`, `base`, and the settings to change, written as in `contract.yaml`. |

#### Refusals

- A placeholder the Hub never handed out is refused.
- The Kill switch does not stop a proposal.

#### Setup

- Use a private repo that holds the contracts.
- Clone it at `SQUERO_CONTRACTS_DIR`. It must be the root, so a Contract's file is `<id>/contract.yaml`.
- Make a fine-grained token for that repo alone.
  - Permissions: Contents, read and write; Pull requests, read and write.

#### A caveat about branch protection

GitHub Free enforces no branch protection on a private repo. So the token *could* write `main`.

- The Hub's code doesn't write `main`.
- The Hub runs only what you pull.

### One Contract per call, under its Data policy

Every tool except `list_contracts` takes `contract`. It answers only within that Contract.

What a tool returns goes to your client's model:

- If the Contract masks, the client gets the same placeholders its own agent sends.
- Secrets are redacted.
- The text of an error is redacted too. An unexpected error shows no text at all.

Placeholders you pass back:

- A placeholder you pass back turns back into its value only if the Hub handed it out for that same Contract.
- This includes the `contract` itself.
- It must have been handed out since the Hub's last restart. After a restart, list again.

### Audit

Every call is written as an `squero.audit` line on the Hub's stderr. Read it with `docker compose logs hub`.

Each line has:

- `"channel": "mcp"`;
- your login;
- the Contract;
- the arguments, as the client wrote them.

## Running as a service in one cluster

The Hub can also run inside a cluster. This serves one Contract.

### Install

```bash
uv run squero contracts new-token    # give the token to Alertmanager; the hash goes in
                                    # deploy/base/contract.yaml (webhook_token_sha256)
kubectl -n squero create secret generic squero-secrets \
  --from-literal=SQUERO_APPROVER_TOKEN=... --from-literal=SQUERO_MODEL_API_KEY=... \
  --from-literal=SQUERO_DATABASE_URL=postgresql://user:pass@host:5432/squero   # a Postgres you run
kubectl apply -k deploy/base        # read-only
kubectl apply -k deploy/actions     # optional: evict / scale, still behind approval
kubectl apply -f deploy/gitops/rbac-gitops.yaml   # optional: ArgoCD / Flux objects for
                                    # recent_changes; also set the cluster's gitops_namespaces
```

- The image is published to GHCR on version tags.
- To use your own build, run: `cd deploy/base && kustomize edit set image ghcr.io/alexboscolo954/squero=<your-image>`.
- This in-cluster deploy serves one Contract, `in-cluster`. See [`deploy/base/contract.yaml`](../deploy/base/contract.yaml). It is mounted from a ConfigMap.
- For several Contracts, run the Hub on a VM instead. See [Running the Hub](running-the-hub.md).
- Point Alertmanager at it with [`deploy/alertmanager-receiver.yaml`](../deploy/alertmanager-receiver.yaml).

### Grafana Alerting

Grafana Alerting is a second alert source.

1. In the client's Grafana, add a contact point:
   - type: Webhook;
   - URL: `https://<domain>/contracts/<id>/grafana`;
   - Authorization header scheme: `Bearer`;
   - credentials: the Contract's webhook token.
2. Leave "Disable resolved message" **off**. Then a resolved alert makes its proposed actions stale.
3. Route the alerts you want triaged to it, with a notification policy.

How the Hub handles the payload:

- Grafana's notification is Alertmanager's payload, plus each rule's query values.
- The Hub keeps those query values as the alert's `query_values` annotation.
- It drops Grafana's rendered title and message. They repeat the labels and annotations.
- From there, it handles the alert like an Alertmanager group. The same token, dedup and resolve rules apply.

The test payloads were captured from Grafana 13.2. See [`tests/fixtures/grafana-firing.json`](../tests/fixtures/grafana-firing.json).

### Endpoints

| Endpoint | Token | What it does |
|---|---|---|
| `POST /contracts/{id}/alertmanager` | that Contract's webhook token | Starts a triage when an alert not seen before is firing in the group. An unknown Contract gets the same 401 as a wrong token. Duplicates are checked per Contract, so two Contracts can send the same group key. Anything else within 30 min (a repeat, or a group that shrank) returns the existing triage. A resolved group makes its proposed actions stale. |
| `POST /contracts/{id}/grafana` | that Contract's webhook token | Grafana Alerting's webhook contact point, normalized into the same Alert group, then exactly as above |
| `POST /contracts/{id}/pagerduty` | PagerDuty's signature, with that Contract's signing secret | [PagerDuty](contracts.md#pagerduty)'s V3 webhook: an incident starting or ending as an Alert group, then as above. A wrong signature gets 401; a signed delivery is never refused |
| `GET /investigations`, `GET /investigations/{id}` | approver | Status, kind (`triage` for now), Contract, report, proposed and executed actions |
| `POST /investigations/{id}/decision` | approver | `{"approve": true}` or `{"approve": false, "message": "..."}` |
| `GET /metrics`, `GET /healthz` | none | `squero_triages_total`, `squero_triage_seconds`, `squero_tool_calls_total`, `squero_pending_approvals` |

### The container

- The container runs as non-root.
- Its root filesystem is read-only.
- All capabilities are dropped.
- It uses the RuntimeDefault seccomp profile.

CI deploys the real image with these manifests on kind. It checks health, auth and metrics. See [`demo/deploy-smoke.sh`](../demo/deploy-smoke.sh).

### State and restarts

State lives in Postgres. It holds:

- Investigations;
- the duplicate memory;
- every Approval decision: who, what, and when;
- every tool call, with its raw result. The raw result is redacted, and kept before the size cap. It is kept so an Investigation can become an eval Scenario;
- the LangGraph checkpoints.

Restart behaviour:

- A pending approval survives a restart.
- After a restart, a deadline that passed while the Hub was down goes Stale.
- A Run that was cut off becomes a Failed investigation.
- It is never replayed. So an approved write can't run twice.

### Postgres

- The in-cluster deploy needs a Postgres you provide.
- Version 12 or later, with the [pgvector](https://github.com/pgvector/pgvector) extension.
- The Report search is a generated column.
- Migration 0013 runs `create extension vector`. That needs a role allowed to create it.
- The smoke test brings a throwaway one: [`demo/postgres.yaml`](../demo/postgres.yaml).
- The Hub's compose stack ships one.

### Replicas and concurrency

- Timers and the concurrency limit are per process. So run one replica.
- `SQUERO_MAX_CONCURRENT_TRIAGES` is shared by every Contract.
- On a Hub with two Contracts or more, each Contract takes at most half of it, and at least one slot.
  - Its other work waits in its own queue, without holding a slot.
  - So one Contract's alert storm can't hold every slot.
- A Hub with one Contract gives it every slot.

## Other models

Any `init_chat_model` string works.

For an OpenAI-compatible gateway, such as LiteLLM or vLLM, see [llm-serving-k8s](https://github.com/alexboscolo954/llm-serving-k8s):

```bash
SQUERO_MODEL=openai:<model-name> SQUERO_MODEL_BASE_URL=http://litellm:4000/v1 SQUERO_MODEL_API_KEY=...
```

## Model and cost

### Which model a Contract runs

- A Contract runs on the Hub's `SQUERO_MODEL`.
- Unless it sets `model:`, for example a cheaper one.
- It runs through the Hub's gateway and key, so its prefix must match.
  - Example: `openai:` behind OpenRouter.

### What is recorded

- Every model call's tokens are recorded.
- Each call is priced from `SQUERO_MODEL_PRICES`, when the model has a price.
- The console shows each Investigation's tokens and cost.
- It also shows each Contract's spend this month.

### Monthly budget

`monthly_budget_usd:` sets a Contract's monthly budget. The month is the UTC calendar month.

- A Contract with a budget needs its model priced. Otherwise the Hub won't start.
- At 80% of the budget, and again at 100%, the Contract's operator channel gets a notice. Each is sent once per month.

At 100%:

- A new alert, Question or follow-up is refused.
- It is refused without calling the model.
- It becomes a Failed investigation that names the spend and the budget.
- Slack still tells you. So no alert is silently dropped.

Exceptions and caveats:

- A Run that resumes after your approval always runs. So an approved action is always reported.
- Several Runs that start together can overshoot the budget a little.

## Configuration

Hub settings are `SQUERO_*` environment variables, or set in `.env`. See [`config.py`](../src/squero/config.py).

Everything about a cluster, an integration or a webhook token lives in the Contract, not in these settings.

If one of the old variables is still set, `serve`, `triage` and `mcp` refuse to start. The old variables include `SQUERO_KUBE_CONTEXT`, `SQUERO_NAMESPACES`, and others. The error says where each one moved.

### Model

| Variable | Default | What it does |
|---|---|---|
| `SQUERO_MODEL` | `anthropic:claude-opus-5-5` | Any `init_chat_model` id. Opus 5.5 runs at its own default effort, medium (audit A12: Sonnet 5.5 sometimes kept calling tools on a Question long after it could answer). Up to v0.59 the default was `anthropic:claude-sonnet-5-5`. A Hub that relied on that default now runs Opus, at twice the price per token. It refuses to start while a Contract with `monthly_budget_usd` has no price for it in `SQUERO_MODEL_PRICES`. Set `SQUERO_MODEL` to keep Sonnet. |
| `SQUERO_MODEL_BASE_URL` / `_API_KEY` | | OpenAI-compatible gateways |
| `SQUERO_MODEL_EFFORT` | the model's | How much the model reasons: `low`, `medium`, `high`, `xhigh` or `max`. This is Anthropic's `output_config.effort`, or `reasoning_effort` through an OpenAI-compatible gateway. Claude Opus 5.5 defaults to `medium` |
| `SQUERO_MODEL_PRICES` | | JSON: USD per million tokens per model string. Example: `{"openai:anthropic/claude-haiku-5.5": {"input": 0.10, "output": 0.50, "long": {"over": 100000, "input": 0.50, "output": 2.50}}}`. Priced per model call, as providers bill: a call whose prompt is over `long.over` pays the `long` prices. Cache discounts are ignored, so costs read a little high |
| `SQUERO_MAX_TOOL_CALLS` / `SQUERO_MAX_MODEL_CALLS` | 25 / 30 | Per run. Each `read_more` counts as a tool call |
| `SQUERO_TOOL_OUTPUT_LIMIT` | 8000 | Characters of one tool result the model sees at once. The rest stays readable through `read_more` |

### Contracts, Slack and console

| Variable | Default | What it does |
|---|---|---|
| `SQUERO_CONTRACTS_DIR` | | One directory per Contract. Required for `serve`, `triage`, `mcp` |
| `SQUERO_APPROVER_TOKEN` | | The Operator's token. Required for `serve` |
| `SQUERO_DATABASE_URL` | | Postgres. Required for `serve`. Migrations run at startup |
| `SQUERO_SLACK_BOT_TOKEN` | | The Hub's Slack bot (`xoxb-...`). Each Contract names its own channel |
| `SQUERO_SLACK_APP_TOKEN` / `_OPERATOR_ID` | | Approve and reject buttons: an app-level token (`xapp-...`, `connections:write`) and the Operator's Slack member ID (`U...`). The app token needs both others |
| `SQUERO_CONSOLE_OPERATORS` | | Web console: JSON list of the Tailscale logins that may sign in. Empty means off. Only behind `tailscale serve` |
| `SQUERO_CONSOLE_URL` | | The console's address (the `tailscale serve` URL). The weekly Sizing digest links its Workloads page. Unset, no link |
| `SQUERO_MCP_TOKEN` | | The Hub MCP server at `/mcp/`: its bearer token, 32 characters or more, its own. It is refused if it is the approver or a webhook token. Needs `SQUERO_CONSOLE_OPERATORS`. Unset means off |
| `SQUERO_CONTRACTS_REPO` | | The contracts repo on GitHub (`owner/name`), cloned at `SQUERO_CONTRACTS_DIR`. This is where `propose_change` opens its pull requests. Unset means the Hub proposes nothing |
| `SQUERO_CONTRACTS_TOKEN` | | A fine-grained token for that repo alone: Contents and Pull requests, read and write. Sent only to `api.github.com`, and never shown in an error |

### Similar incidents and embeddings

| Variable | Default | What it does |
|---|---|---|
| `SQUERO_EMBEDDING_MODEL` | | Similar incidents by meaning: an embedding model on a gateway (`SQUERO_EMBEDDING_BASE_URL`, else the model's `SQUERO_MODEL_BASE_URL`; one is needed), for example `openai/text-embedding-3-small` on OpenRouter. Price it in `SQUERO_MODEL_PRICES` (`{"input": 0.02, "output": 0}`) to count it in budgets. Unset: words only |
| `SQUERO_EMBEDDING_BASE_URL` / `SQUERO_EMBEDDING_API_KEY` | | The embeddings' own gateway and key, for a model called directly. Anthropic has no embeddings API, so with `SQUERO_MODEL=anthropic:...` the embeddings stay on, for example, OpenRouter. Unset: the model's gateway, key and `SQUERO_MODEL_HEADERS`. Set: the model's key and headers never go there |
| `SQUERO_EMBEDDING_MAX_DISTANCE` | 0.6 | Cosine distance under which a past Report counts as similar |

### Investigations and alerts

| Variable | Default | What it does |
|---|---|---|
| `SQUERO_RELATED_WINDOW_MINUTES` | 30 | How long after a Triage starts a new Alert group can still join it. A Contract's `related.window_minutes` overrides it |
| `SQUERO_RELAY_CALL_SECONDS` | 30 | How long one tool call through a Relay may take. A Relay that hasn't answered by then is offline, and the Run fails |
| `SQUERO_MAX_CONCURRENT_TRIAGES` | 3 | Alert storms queue instead of fanning out. With two Contracts or more, each gets at most half (at least one) |
| `SQUERO_DECISION_TIMEOUT_SECONDS` | 1800 | Proposed actions with no decision by then are rejected as stale. So are those whose alert group resolves, which needs `send_resolved: true` ([`deploy/alertmanager-receiver.yaml`](../deploy/alertmanager-receiver.yaml)) |
