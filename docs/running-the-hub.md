# Running the Hub

- [The compose stack](#the-compose-stack)
- [Slack](#slack)
- [Web console](#web-console)
- [Hub MCP server](#hub-mcp-server)
- [Running as a service in one cluster](#running-as-a-service-in-one-cluster)
- [Other models](#other-models)
- [Model and cost](#model-and-cost)
- [Configuration](#configuration)

## The compose stack

The Hub runs every Contract from one machine you own, a VM or your laptop: [`deploy/hub`](../deploy/hub) is a compose stack with the Hub, Postgres, Caddy and nightly backups. `make hub-smoke` runs it on Linux and macOS.

1. **Host:** Docker with compose. Copy `deploy/hub` there. For client alerts, a DNS name pointing at it and ports 80 and 443 open. Without them (a laptop behind NAT), set `SQUERO_DOMAIN=localhost` and other ports in `.env`: only senders that reach the machine can alert, such as a local kind cluster or `curl -k`.
2. **Contracts:** clone your private contracts repo into `deploy/hub/contracts` with a read-only deploy key:
   ```bash
   ssh-keygen -t ed25519 -N '' -f ~/.ssh/squero-contracts      # add the .pub as a read-only deploy key
   GIT_SSH_COMMAND='ssh -i ~/.ssh/squero-contracts' git clone git@github.com:<you>/contracts.git contracts
   git -C contracts config core.sshCommand 'ssh -i ~/.ssh/squero-contracts'
   ```
3. **Age keys:** put every Contract's age key in one file that only the Hub's user (uid 10001) can read:
   ```bash
   cat acme.agekey globex.agekey > age-keys.txt && chmod 0400 age-keys.txt
   sudo chown 10001 age-keys.txt   # Linux only: Docker Desktop on macOS maps file owners itself
   ```
4. **Settings:** copy `env.example` to `.env` (Postgres password, domain) and `hub.env.example` to `hub.env` (approver token, model key).
5. **Start:** `docker compose up -d --wait`. Caddy gets a certificate for your domain.
6. **Operator access:** `tailscale serve --bg 8080` publishes the API, which listens only on `127.0.0.1:8080`, to your tailnet over HTTPS. Never publish port 8080 another way: ports Docker publishes bypass host firewalls.
7. **Alert sources:** each Contract's Alertmanager posts to `https://<domain>/contracts/<id>/alertmanager`, and its Grafana Alerting to `https://<domain>/contracts/<id>/grafana`, both with that Contract's token.

**What is public:** only `POST /contracts/<id>/alertmanager` and `POST /contracts/<id>/grafana`, with bodies up to 1 MB ([`Caddyfile`](../deploy/hub/Caddyfile)). Investigations, decisions, metrics and the API docs answer only on `127.0.0.1`.

**Updating Contracts:** `./update-contracts.sh` pulls the contracts repo and validates every Contract inside the image before restarting the Hub. If any Contract is invalid, it rolls the clone back and leaves the running Hub alone, because one bad Contract would otherwise stop alerts for all of them.

**Backups:** a `pg_dump` whenever the newest one is a day old, checked every 5 minutes against the dumps' file times (so a failed dump is retried in 5 minutes, and a laptop's sleep, which stops the container's clock, never pushes one back), kept 14 days, in `deploy/hub/backups`. Dumps are owner-only because they hold Contract data (Reports, raw tool results). A dump on the host is lost with the host: copy `backups/` somewhere else.

`make hub-smoke` runs the stack locally and in CI with the demo Contracts. It checks that the stack comes up healthy, that every path except the alert route returns 404 through Caddy (including `../` and `%2F` tricks and oversized bodies), that an alert is stored, that a backup restores, and that a bad contracts update is refused. CI does not cover `tailscale serve`, real Let's Encrypt certificates, the deploy-key clone or the arm64 image.

## Slack

Each Investigation is one thread in its Contract's channel. The parent message shows the status (investigating, waiting for your approval, done, failed), and replies carry the Proposed actions, each Approval decision, the Report or the error.

1. Create the app from [`deploy/slack-app.yaml`](../deploy/slack-app.yaml) (api.slack.com/apps > From a manifest), install it, and set its bot token as `SQUERO_SLACK_BOT_TOKEN`.
2. For the buttons: Basic Information > App-Level Tokens > generate a token with `connections:write`, set it as `SQUERO_SLACK_APP_TOKEN`, and set your own member ID (your profile > ⋮ > Copy member ID) as `SQUERO_SLACK_OPERATOR_ID`.
3. Make one channel per Contract and invite the bot: `/invite @squero`. The bot can't post where it isn't a member.
4. Put the channel's ID (channel details > the `C...` at the bottom) in the Contract: `slack: {operator_channel: C0123ABCD}`. Two Contracts can't share a channel.

While Proposed actions wait, the parent message has **Approve** (with a confirm step) and **Reject** (a modal asks for a reason, which the agent reads). Clicks arrive over Socket Mode, so the Hub opens no Slack endpoint. They take the same decision path as `POST /investigations/{id}/decision`:
- only `SQUERO_SLACK_OPERATOR_ID`'s clicks count, and only in the Investigation's own Contract channel; anyone else gets a private "Only the Operator can decide";
- each button names its pause, so a second click, or a click left over from an earlier pause, decides nothing;
- once the actions are decided (in Slack or through the API) or go Stale, the buttons disappear and the thread says who decided, or why it went Stale.

**Client reports (optional, per Contract).** Add `client_channel: C...` next to `operator_channel`: a Slack Connect channel shared with the client, with the bot invited. A finished Report then gets a **Send to client** button in your thread. Nothing reaches the client until you click it, and it goes once. The client gets plain text with the summary, root cause, severity, what is affected and the Recommendations. Evidence, Executed actions, the Runbook and the Investigation id stay with you, and the client message never has buttons. Only the Evidence list is held back, though: the summary and root cause are the model's own words and can still name a pod or quote a log line, so read before you send. A channel belongs to one Contract, as operator or client channel, never both.

**Questions.** Mention the bot in a Contract's operator channel: `@squero what changed in checkout today?`. It answers in that thread with an Answer backed by Evidence, using only that Contract's read tools: a Question never proposes an action. Mention it again in the same thread to follow up on the same Question. Only `SQUERO_SLACK_OPERATOR_ID` can ask. A mention in a client channel, in a channel no Contract owns, in a Triage thread, or while the last Answer is still coming gets a private refusal. The bot receives only messages that mention it, never other channel traffic.

Without `SQUERO_SLACK_APP_TOKEN` there are no buttons and no Questions: you decide through the API, and Client reports can't be sent. Everything posted is escaped (a label like `<!channel>` arrives as text, never as a ping), redacted and capped. A Slack failure, including a dropped socket (it reconnects), is logged and never fails an Investigation.

## Web console

The console is the Operator's view of every Contract, a React single-page app the Hub serves at `/app/` (the only console: the older Jinja pages are gone, and their addresses under `/console` redirect into the app, an Investigation's to its page). It has no passwords: `tailscale serve` adds your Tailscale identity to every request it proxies, as `Tailscale-User-Login`, and strips any copy a client sends.

1. Set `SQUERO_CONSOLE_OPERATORS='["you@example.com"]'` in `hub.env`: the Tailscale logins that may sign in.
2. Open `https://<vm>.<tailnet>.ts.net/app/` from a device on your tailnet (the `tailscale serve` of step 6 above).

Anything else gets a 403, including a tagged device, which has no user.

**The look.** One stylesheet and the app's own scripts, served by the Hub behind the same identity: no script, font or file from anywhere else, so the page's `default-src 'self'` policy stays strict. Light and dark follow the device. Reports, Answers and Check verdicts are fields shown as text: a Report is written by a model that read tool output, so nothing in it can become markup or a script.

**The approver token, once per browser.** Your identity alone opens only the **approver token** page: paste `SQUERO_APPROVER_TOKEN` there (`/app/?view=token`) once per browser, and every other page, approving, rejecting and asking opens up. The browser keeps a cookie that proves the token without holding it (an HMAC; Secure, HttpOnly, SameSite=Strict, 30 days). The identity header alone shows nothing, because other things on the VM could send it. A decision must also come from the console's own page (`Sec-Fetch-Site` or `Origin`), so another site can't make your browser post one. It takes the same path as Slack: the thread says who decided, and the page names its pause, so a page left open from an earlier pause decides nothing.

### Workloads

**Workloads** (`/app/`) is the console app's first page. It shows each Contract's Workloads from the Hub's latest stored Sizings, ranked by waste, with their pods, risk kinds and each cluster's freshness; pick one for its containers' requests and Sizings, their risks, and charts of request, Sizing and usage basis over 14 days (hourly) or 400 (daily). It reads only stored runs, never a client's Prometheus. The page needs the Operator's Tailscale identity; its JSON API (`/api/v1/`) also needs the approver token's cookie, and links the token page without it (the cookie is set for the whole Hub). Its CSP allows only the Hub's own origin, and everything from the API is rendered as text: ESLint refuses raw HTML, a test scans the app's source for it, and a component test renders a risk holding markup as text. The app is built in the image's Docker build (`console/`: Vite, React, TypeScript, uPlot; Node 22 pinned by digest, `npm ci` from the lockfile with install scripts off), and only its static files ship. `make console-check` runs what CI runs (types in sync with the API, typecheck, lint, tests, build); `make console-dev` serves it on 127.0.0.1:5173 against a local Hub (`SQUERO_DEV_HUB`, default `http://127.0.0.1:18100`), adding `SQUERO_DEV_LOGIN` as the Tailscale identity in development only. After changing the API, `make console-types` and commit `console/openapi.json` and `console/src/api-types.ts`.

### Investigations

**Investigations** (`/app/?view=investigations`): a Contract's Investigations, newest first (passing Checks left out), each opening its own page: the Report, Answer or Check verdict as fields (summary, root cause with its confidence and Category, severity, affected, Evidence, Recommendations, executed actions), what waits for a decision and when it goes Stale, Report feedback and the Teachings drafted from it, related alert groups, decisions, tool calls (each result's first 4000 characters), tokens, cost and what was masked. A running one updates every 5 seconds. A finished one with its input recorded offers **Save as Scenario**. Everything is rendered as text: alert titles come from client Alertmanagers and Reports from a model that reads tool output, so a model's Markdown shows as written, and an Evidence link stays text, never a link. From the page you **approve or reject** the pause it shows (with a reason the agent reads), as Slack's buttons do: an older pause's deadline decides nothing, and approving is refused while the Kill switch is on; mark a finished Report **right or wrong** (a wrong one needs what was wrong, and the agent drafts a Teaching from it); and **file it into Jira** once, when the Contract has Jira, after which the page links the issue. Each is a change: your identity, the cookie and the app's own page (`Sec-Fetch-Site: same-origin`), recorded as `console:<login>`. **Approver token** (`/app/?view=token`) earns the browser its cookie through `POST /api/v1/token` (your identity and the app's own page only); a call without the cookie links there.

### Teachings

**Teachings** (`/app/?view=teachings`): a Contract's Teachings waiting for review (each with the alerts it is for, the Investigation and your words it was drafted from, and the approved one it would retire), its approved ones and its rejected and retired ones. Edit a drafted text and approve it, reject it, retire an approved one, or write one by hand (approved at once), each as `console:<login>`. It warns when the approved ones don't all fit in a prompt. A drafted text is model output: the page shows and edits it as plain text.

### Checks and Health

**Checks and Health** (`/app/?view=checks` and `view=health`): the Contract's scheduled Checks (schedule, next Run, the last Run's verdict and an Open failure, each linking its Investigation, and the last skip with its reason) and its latest Deploy verifications (where, which changes, when due, and their Run's verdict or why skipped). Health checks the Contract shown only, now, with one cheap read per integration through its own tools and credentials (a cluster's deployments in its first namespace, Prometheus `vector(1)`, a Runbook search): each integration ok or FAILING with its error (redacted) and time, the last successful Investigation, this month's model spend against its budget, the Contract's config with only the names of its secrets, and the Hub's settings with every secret masked.

### Questions and Contracts

**Questions and Contracts**: **Ask a Question** sits at the top of a Contract's Investigations (`/app/?view=investigations`): it only reads, like a Slack Question, and its page opens at once and updates every 5 seconds until the Answer is in. A Question no longer answering offers **Follow up** on its page, which continues the same Question with its earlier exchange; the page then lists each earlier question with the Answer it got, and the follow-up above the Answer that answers it (from 0.28.1; follow-ups before it aren't recorded). A Question from the app has no Slack thread. **Contracts** (`/app/?view=contracts`) lists every Contract with its clusters, operator channel, model and this month's model spend against its budget, each linking to its own pages.

### Now

**Now** (above the Workloads): the Contract's Agenda, what to look at now, ranked by the Hub without a model in a fixed order of urgency: the Kill switch while it is on, pauses waiting for an Approval decision (soonest to go Stale first), failing Checks (newest first), data the Hub can't read (Sizing or bill reads failing, oldest first), Bills outside their tolerance (biggest gap first), running Investigations (oldest first), and the three Workloads whose waste costs most. Each links where to act: its Investigation, its Workload, or the Cost page. `GET /api/v1/contracts/{id}/agenda`, and `now` on the Hub MCP server.

### Automation

**Automation** (`/app/?view=automation`): for each cluster of the Contract that runs an Actuator, each container it reported on in the last 90 days, with its last Actuation (outcome, pods, when, the requests it set, and the Actuator's reason, shown as text) and its Automation: off, on, ended by 3 Rollbacks until a date, or stopped by a Revert until you approve one of its Sizings. **Revert** on a container whose last Actuation is applied or failed asks a Revert as `revert_sizing` does, refused the same way (with the Hub's words); it shows "reverting" until the Actuator reports it done. Stored as `console:<login>`, and from the console's own page only. The page says when the Kill switch is on, since a Revert is then refused.

### Kill switch

**Kill switch** (every console app page): turn a Contract's Kill switch on, or off. While it is on, approving a Proposed action is refused on every path (Slack, the console, the API, the Hub MCP server) and the pause is kept, so you can still reject it, or approve it once the switch is off; a change approved just before it went on is refused where it would run, recorded as a FAILED Executed action. Reads, Questions and Reports go on as usual. Only this page turns it off (same-origin, your identity and the approver cookie, like approving); the Hub's log names who turned it on or off.

### Where to run it

A follow-up on a Question that started in Slack also answers in its thread. With no operators set, the console doesn't exist (`/app/`, `/api/v1/` and `/console` answer 404). **Set it only where nothing but `tailscale serve` reaches the Hub's port**: on the VM that holds, but local processes and other containers on the compose network could still send the header themselves. Never set it on the in-cluster deploy, where any pod can reach the Service. Through Caddy, the console is a 404.

## Hub MCP server

Your own MCP endpoint on the Hub, for Claude Code (or any MCP client) on your devices: ask it what needs you now, or for a Contract's top Sizings, cost, Savings, bill, Investigations and Checks while you work, ask the Hub's agent a Question, decide a pause, approve a Sizing, propose a change to a Contract's Sizing policy as a pull request, or turn a Contract's Kill switch on. The Investigation agent never gets these tools.

1. Set `SQUERO_MCP_TOKEN` in `hub.env` (`openssl rand -hex 32`) beside `SQUERO_CONSOLE_OPERATORS`, and restart the Hub.
2. On a device on your tailnet, add the server to Claude Code with the `tailscale serve` URL and the token:
   ```bash
   claude mcp add --transport http squero-hub https://<vm>.<tailnet>.ts.net/mcp/ \
     --header "Authorization: Bearer <SQUERO_MCP_TOKEN>"
   ```
   The URL ends in `/mcp/`. `127.0.0.1` won't do: the Hub needs the identity `tailscale serve` adds.

### Two doors

A call needs the token *and* a listed Operator's `Tailscale-User-Login`: the token alone could come from anything that read the Hub's settings, the identity header alone from anything else that reaches the port. Startup refuses a token shorter than 32 characters, one equal to the approver token or a Contract's webhook token, and a token without `SQUERO_CONSOLE_OPERATORS`. Caddy's public port answers 404 for `/mcp/`. Stateless HTTP: a Hub restart leaves no session to lose.

### Tools

`list_contracts` (ids, names, clusters, whether each Kill switch is on); `sizings(contract, top=10, cluster, namespace, risky)`, the Workloads ranked by what their waste costs, as the console's Workloads page ranks them, a request its HPA divides by marked `held` with the manifest change that frees it, a DaemonSet's container also sized per instance type (`by_instance_type`, with `applied` saying whether its Actuator applies those or the one Sizing), and each autoscaled Workload's Floor against its HPA's minimum; `sizing(contract, cluster, namespace, name, kind)`, one Workload's Sizing, as the agent's own tool gives it; `nodes(contract, cluster, top=20)`, each cluster's nodes with their Room and whether each could be removed (blocked ones first; a node's Blockers one per Workload, capped at 5 and the rest counted, each with the change that removes it and its manifest where there is one), and the nodes that could go now and after the Sizings with their monthly cost, an upper bound that is no part of Savings; `cost(contract, by, compare_days)` and `savings(contract, compare_days)`, as the Cost page shows them; `bill(contract, days)`, as `sizing bill` does; `investigations(contract, open, limit)`, newest first with each one's Category, severity and summary, Answer or verdict; `investigation(contract, id)`, one in full with its Evidence, pending Proposed actions and Approval decisions (another Contract's id answers "no such Investigation"); `checks(contract)`, each Check's schedule and last Run; `now(contract)`, the Contract's Agenda as the console's Now panel shows it, and `now()` without a Contract, only how many items of each kind every Contract has. All of those are marked read-only. `ask(contract, question)` is not: it starts a Question like the console's (same 4000-character limit and budget, never posted to Slack) and returns its id at once, so your client asks before it runs; read the Answer with `investigation`.

### Deciding, and the Kill switch

`investigation` shows a pause's Proposed actions and its `pause` token (the pause's exact deadline, as text). `decide(contract, id, pause, approve, reason)` approves or rejects every action of that pause, as Slack's buttons do: an older token never decides a newer pause, another Contract's id is "no such Investigation", and the thread and the decisions table say `<login> via MCP` (`mcp:<login>`). A rejection's reason reaches the agent with its placeholders put back. `turn_on_kill_switch(contract)` stops every change to that Contract's systems at once; nothing on the Hub MCP server turns it off, only the console does. `decide` is marked destructive, so your client asks first; `turn_on_kill_switch` is marked not read-only.

### Approving a Sizing

`approve_sizing(contract, cluster, namespace, name, container, cpu, memory, kind)` approves one container's latest Sizing, for the Actuator to apply: nothing changes in a cluster before then. `cpu` and `memory` are its Sizing exactly as `sizing` or `sizings` showed it (`115m`, `96Mi`, null where none was shown), so a newer Sizing is never approved unseen: a mismatch answers with the latest values. It is refused for a container that is learning or excluded, one whose Sizing keeps its requests, a pod's name (name the Workload), and while the Contract's Kill switch is on. It stores the requests it changes (one kept stays as it is), from that run, as `mcp:<login>`; `sizing` and `sizings` show who approved it and when. The next Sizing run that reads its cluster supersedes it, whatever its values; a run failing there does not. The cluster's Actuator applies it within a minute; one superseded before then never is. A DaemonSet's Sizings per instance type are approved with it (its answer lists them as `by_instance_type`), also where its pooled Sizing keeps its requests and only a type's changes them; one with more types than the ten `sizings` shows is refused. Where the cluster's Actuator applies types (0.54.0 and later), the types alone decide: a container whose types keep their requests, or are all still learning, is refused whatever its pooled Sizing says. Marked destructive, so your client asks first.

### Reverting a Sizing

`revert_sizing(contract, cluster, namespace, name, container, kind)` (kind defaults to `Deployment`) undoes what the cluster's Actuator holds for one container: within a minute its pods go back to their template's requests, in place, and the Actuator stops holding it (a `SizingReverted` Event and a `reverted` Actuation; one with nothing held says so). A Step-up or a Burst of that container is dropped with it. Automation of that container stops until you approve one of its Sizings. It is refused for a cluster without an Actuator, a namespace not the cluster's, a container the Actuator never reported on, and while the Contract's Kill switch is on. Stored as `mcp:<login>`. An Actuator of 0.40.0 or older never reads Reverts: the tool still answers `asked`, and nothing happens. Marked destructive, so your client asks first. A Revert also drops the container's Step-up, if it has one ([how a Step-up ends](client-cluster.md#how-a-step-up-ends-and-where-it-shows)), and can be asked of a container that has only a Step-up.

### Proposing a change

`propose_change(contract, edits, title, why)` opens a pull request on the contracts repo (`SQUERO_CONTRACTS_REPO`, with `SQUERO_CONTRACTS_TOKEN`) that edits that Contract's Sizing policy and nothing else, for you to review and merge. `edits`, applied in order: `set_rule` (any of `cluster`, `namespace`, `kind`, `name`, `container`, and a `policy`: the rule with exactly that match gets it, else a new rule goes first, since the first match wins; `automate: true` or `false` turns [Automation](client-cluster.md#automation) on or off for it, left out keeps what the rule has), `remove_rule` (the rule with exactly that match), `set_default` (a policy), `set_policy` (one of the Contract's own: `name`, `base` and the settings to change, written as in `contract.yaml`). Before it writes anything, the Hub checks that GitHub's `main` is the commit it loaded and `<id>/contract.yaml` there is the file it loaded (pull and restart the Hub after a merge, or it refuses), makes the edit with comments and layout kept, and refuses if anything but `sizing` changed, if nothing did, or if the Contract would not load. Then it creates a branch `squero/<id>-<UTC time>`, commits there and opens the pull request; it never writes `main`. Nothing changes until you merge it and restart the Hub on it. A placeholder the Hub never handed out is refused, and the Kill switch does not stop a proposal. GitHub Free enforces no branch protection on a private repo, so the token *could* write `main`: the Hub's code doesn't, and the Hub runs only what you pull. Marked not read-only.

To set it up: a private repo holding the contracts, cloned at `SQUERO_CONTRACTS_DIR` (its root, so a Contract's file is `<id>/contract.yaml`), and a fine-grained token for that repo alone with Contents and Pull requests read and write.

### One Contract per call, under its Data policy

Every tool except `list_contracts` takes `contract`, and answers only within it. What it returns goes to your client's model, so a Contract that masks gets the same placeholders its own agent sends, secrets are redacted, and so is the text of an error (an unexpected error shows no text at all). A placeholder you pass back, the `contract` itself included, turns back into its value only if the Hub handed it out for that same Contract since its last restart; after a restart, list again.

### Audit

Every call is an `squero.audit` line on the Hub's stderr (`docker compose logs hub`) with `"channel": "mcp"`, your login, the Contract and the arguments as the client wrote them.

## Running as a service in one cluster

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

The image is published to GHCR on version tags. To use your own build, `cd deploy/base && kustomize edit set image ghcr.io/alexboscolo954/squero=<your-image>`.

This in-cluster deploy serves one Contract, `in-cluster` ([`deploy/base/contract.yaml`](../deploy/base/contract.yaml)), mounted from a ConfigMap. For several Contracts, run the Hub on a VM instead ([Running the Hub](running-the-hub.md)).
Point Alertmanager at it with [`deploy/alertmanager-receiver.yaml`](../deploy/alertmanager-receiver.yaml).

**Grafana Alerting** is a second alert source. In the client's Grafana, add a contact point of type Webhook with URL `https://<domain>/contracts/<id>/grafana`, Authorization header scheme `Bearer` and the Contract's webhook token as its credentials, and leave "Disable resolved message" off, so a resolved alert makes its proposed actions stale. Route the alerts you want triaged to it with a notification policy. Grafana's notification is Alertmanager's payload plus each rule's query values; the Hub keeps those as the alert's `query_values` annotation, drops Grafana's rendered title and message (they repeat the labels and annotations), and from there handles it like an Alertmanager group: same token, dedup and resolve rules. The test payloads were captured from Grafana 13.2 ([`tests/fixtures/grafana-firing.json`](../tests/fixtures/grafana-firing.json)).

| Endpoint | Token | |
|---|---|---|
| `POST /contracts/{id}/alertmanager` | that Contract's webhook token | Starts a triage when an alert not seen before is firing in the group. An unknown Contract gets the same 401 as a wrong token. Duplicates are checked per Contract, so two Contracts can send the same group key. Anything else within 30 min (a repeat, or a group that shrank) returns the existing triage. A resolved group makes its proposed actions stale. |
| `POST /contracts/{id}/grafana` | that Contract's webhook token | Grafana Alerting's webhook contact point, normalized into the same Alert group, then exactly as above |
| `GET /investigations`, `GET /investigations/{id}` | approver | Status, kind (`triage` for now), Contract, report, proposed and executed actions |
| `POST /investigations/{id}/decision` | approver | `{"approve": true}` or `{"approve": false, "message": "..."}` |
| `GET /metrics`, `GET /healthz` | none | `squero_triages_total`, `squero_triage_seconds`, `squero_tool_calls_total`, `squero_pending_approvals` |

The container runs as non-root on a read-only root filesystem, with all capabilities dropped and the RuntimeDefault seccomp profile.
CI deploys the real image with these manifests on kind and checks health, auth and metrics ([`demo/deploy-smoke.sh`](../demo/deploy-smoke.sh)).

State lives in Postgres: Investigations, the duplicate memory, every Approval decision (who, what, when), every tool call with its raw result (redacted, before the size cap, kept for turning an Investigation into an eval Scenario) and the LangGraph checkpoints. A pending approval survives a restart. After a restart, a deadline that passed while the Hub was down goes Stale, and a Run that was cut off becomes a Failed investigation. It is never replayed, so an approved write can't run twice. The in-cluster deploy needs a Postgres you provide, version 12 or later with the [pgvector](https://github.com/pgvector/pgvector) extension (the Report search is a generated column; migration 0013 runs `create extension vector`, which needs a role allowed to create it) (the smoke test brings a throwaway one, [`demo/postgres.yaml`](../demo/postgres.yaml)); the Hub's compose stack ships one. Timers and the concurrency limit are per process, so run one replica. `SQUERO_MAX_CONCURRENT_TRIAGES` is shared by every Contract, so one Contract's alert storm can hold every slot.

## Other models

Any `init_chat_model` string works. For an OpenAI-compatible gateway (LiteLLM, vLLM; see [llm-serving-k8s](https://github.com/alexboscolo954/llm-serving-k8s)):

```bash
SQUERO_MODEL=openai:<model-name> SQUERO_MODEL_BASE_URL=http://litellm:4000/v1 SQUERO_MODEL_API_KEY=...
```

## Model and cost

A Contract runs on the Hub's `SQUERO_MODEL` unless it sets `model:` (for example a cheaper one), through the Hub's gateway and key, so its prefix must match (`openai:` behind OpenRouter). Every model call's tokens are recorded, and priced from `SQUERO_MODEL_PRICES` when the model has a price: the console shows each Investigation's tokens and cost, and each Contract's spend this month. `monthly_budget_usd:` sets a Contract's monthly budget (UTC calendar month); a Contract with a budget needs its model priced, or the Hub won't start. At 80% and at 100% of it, the Contract's operator channel gets a notice, once each per month. At 100%, a new alert, Question or follow-up is refused without calling the model: it becomes a Failed investigation that names the spend and the budget, and Slack still tells you, so no alert is silently dropped. A Run that resumes after your approval always runs, so an approved action is always reported. Several Runs that start together can overshoot the budget a little.

## Configuration

Hub settings are `SQUERO_*` environment variables (or `.env`). See [`config.py`](../src/squero/config.py). Everything about a cluster, an integration or a webhook token lives in the Contract. `serve`, `triage` and `mcp` refuse to start if one of the old variables (`SQUERO_KUBE_CONTEXT`, `SQUERO_NAMESPACES`, ...) is still set, and say where it moved.

| Variable | Default | |
|---|---|---|
| `SQUERO_MODEL` | `anthropic:claude-sonnet-5-5` | Any `init_chat_model` id |
| `SQUERO_MODEL_BASE_URL` / `_API_KEY` | | OpenAI-compatible gateways |
| `SQUERO_CONTRACTS_DIR` | | One directory per Contract (required for `serve`, `triage`, `mcp`) |
| `SQUERO_APPROVER_TOKEN` | | The Operator's token, required for `serve` |
| `SQUERO_DATABASE_URL` | | Postgres, required for `serve`. Migrations run at startup |
| `SQUERO_SLACK_BOT_TOKEN` | | The Hub's Slack bot (`xoxb-...`). Each Contract names its own channel |
| `SQUERO_SLACK_APP_TOKEN` / `_OPERATOR_ID` | | Approve and reject buttons: an app-level token (`xapp-...`, `connections:write`) and the Operator's Slack member ID (`U...`). The app token needs both others |
| `SQUERO_CONSOLE_OPERATORS` | | Web console: JSON list of the Tailscale logins that may sign in. Empty means off. Only behind `tailscale serve` |
| `SQUERO_CONSOLE_URL` | | The console's address (the `tailscale serve` URL). The weekly Sizing digest links its Workloads page; unset, no link |
| `SQUERO_MCP_TOKEN` | | The Hub MCP server at `/mcp/`: its bearer token, 32 characters or more, its own (refused if it is the approver or a webhook token). Needs `SQUERO_CONSOLE_OPERATORS`. Unset means off |
| `SQUERO_CONTRACTS_REPO` | | The contracts repo on GitHub (`owner/name`), cloned at `SQUERO_CONTRACTS_DIR`: where `propose_change` opens its pull requests. Unset means the Hub proposes nothing |
| `SQUERO_CONTRACTS_TOKEN` | | A fine-grained token for that repo alone: Contents and Pull requests, read and write. Sent only to `api.github.com`, and never shown in an error |
| `SQUERO_MODEL_EFFORT` | the model's | How much the model reasons: `low`, `medium`, `high`, `xhigh` or `max` (Anthropic's `output_config.effort`; `reasoning_effort` through an OpenAI-compatible gateway). Claude Opus 5.5 defaults to `medium` |
| `SQUERO_MODEL_PRICES` | | JSON: USD per million tokens per model string, e.g. `{"openai:anthropic/claude-haiku-5.5": {"input": 0.10, "output": 0.50, "long": {"over": 100000, "input": 0.50, "output": 2.50}}}`. Priced per model call, as providers bill: a call whose prompt is over `long.over` pays the `long` prices. Cache discounts are ignored, so costs read a little high |
| `SQUERO_MAX_TOOL_CALLS` / `SQUERO_MAX_MODEL_CALLS` | 25 / 30 | Per run; each `read_more` counts as a tool call |
| `SQUERO_RELATED_WINDOW_MINUTES` | 30 | How long after a Triage starts a new Alert group can still join it (a Contract's `related.window_minutes` overrides it) |
| `SQUERO_EMBEDDING_MODEL` | | Similar incidents by meaning: an embedding model on a gateway (`SQUERO_EMBEDDING_BASE_URL`, else the model's `SQUERO_MODEL_BASE_URL`; one is needed), e.g. `openai/text-embedding-3-small` on OpenRouter. Price it in `SQUERO_MODEL_PRICES` (`{"input": 0.02, "output": 0}`) to count it in budgets. Unset: words only |
| `SQUERO_EMBEDDING_BASE_URL` / `SQUERO_EMBEDDING_API_KEY` | | The embeddings' own gateway and key, for a model called directly: Anthropic has no embeddings API, so with `SQUERO_MODEL=anthropic:...` the embeddings stay on, say, OpenRouter. Unset: the model's gateway, key and `SQUERO_MODEL_HEADERS`; set, the model's key and headers never go there |
| `SQUERO_EMBEDDING_MAX_DISTANCE` | 0.6 | Cosine distance under which a past Report counts as similar |
| `SQUERO_TOOL_OUTPUT_LIMIT` | 8000 | Characters of one tool result the model sees at once; the rest stays readable through `read_more` |
| `SQUERO_RELAY_CALL_SECONDS` | 30 | How long one tool call through a Relay may take; a Relay that hasn't answered by then is offline and the Run fails |
| `SQUERO_MAX_CONCURRENT_TRIAGES` | 3 | Alert storms queue instead of fanning out |
| `SQUERO_DECISION_TIMEOUT_SECONDS` | 1800 | Proposed actions with no decision by then are rejected as stale. So are those whose alert group resolves, which needs `send_resolved: true` ([`deploy/alertmanager-receiver.yaml`](../deploy/alertmanager-receiver.yaml)) |
