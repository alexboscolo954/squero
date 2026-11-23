# Phase 19: Integrations breadth

**In short:** Phase 19 adds the alert sources, paging tools and outbound channels that clients use. It starts with managed Prometheus logins, so the optimizer works for clients on managed clouds. 19.1a, 19.1b, 19.1c and 19.2 are built so far.

**Status:** grilled 2026-11-21. 4 of 8 slices built (19.1a, 2026-11-21; 19.1b, 19.1c and 19.2, 2026-11-22).

**Goal:** integrations only, through the Phase 4 framework: managed Prometheus logins, PagerDuty, outbound webhooks, Microsoft Teams and Google Chat, ServiceNow, and Datadog and New Relic.

**Done when:** every integration has a probe and fixtures captured from a real free or dev account, through the Phase 4 framework. A recorded Scenario and a passing eval only where the agent gains tools (Datadog, New Relic, ServiceNow's past incidents). A Teams post or an outbound webhook changes nothing the model sees.

| Slice | What | Status |
|---|---|---|
| [19.1a](#191a-bearer-and-basic-one-helper) | `bearer` and `basic` through one Prometheus auth helper | ✅ built, Grafana Cloud check owed |
| [19.1b](#191b-sigv4-for-amp) | `sigv4` for Amazon Managed Prometheus | ✅ built, AWS check owed |
| [19.1c](#191c-azure-and-google) | `azure` and `google` Prometheus auth | ✅ built, Azure and Google checks owed |
| [19.2](#192-pagerduty) | PagerDuty | ✅ built, live check owed |
| 19.3 | Outbound webhooks | Not built |
| 19.4 | Microsoft Teams and Google Chat | Not built |
| 19.5 | ServiceNow | Not built |
| 19.6 | Datadog and New Relic | Not built |

## Decisions (grill, 2026-11-21)

Two rounds, all answers the recommended ones.

**Starting point:**
- Slack is the only outbound channel. The client's channel gets the Client report only when the Operator presses its button, with its sender recorded.
- Alerts come from Alertmanager and Grafana (4.3d).
- A Prometheus takes a Bearer token only, and its header is built in three places (`usage.py` twice, `servers/prometheus.py`).
- Vendor servers run as pinned commands or as remote https servers with a minted token (Azure, GCP).
- Jira is a builtin.

**Decisions:**
- **Integrations only.** JVM heap Sizing, and Spark and batch sizing, are new Sizing logic, not integrations. They moved to [Phase 20](phase-20.md).
- **Opsgenie goes to Deferred.** Atlassian stopped selling it in June 2025 and ends its support in April 2027.
- **Order:** managed Prometheus auth first, then PagerDuty, outbound webhooks, Teams and Google Chat, ServiceNow, and Datadog and New Relic.
  - *Why:* managed Prometheus auth unblocks the optimizer for every client on a managed cloud.
- **Auth on Prometheus (19.1).** One `auth` on a cluster's Prometheus and on the `prometheus` integration:

| `auth` | For | Secret |
|---|---|---|
| `bearer` | Today's setup | Token |
| `basic` | Grafana Cloud: a user id and a token secret | Token secret (the user id is plain text) |
| `sigv4` | Amazon Managed Prometheus (AMP) | The Contract's `aws` key and region |
| `azure` | Azure Monitor | The `azure` integration's Entra app |
| `google` | Google Managed Prometheus (GMP) | The `gcp` integration's service account |

  - *Why:* no new secrets for the clouds; one helper replaces the three copies of the header.
- **Accounts:** the owner signs up for each free tier (PagerDuty, ServiceNow's developer instance, Grafana Cloud, New Relic, Datadog's 14-day trial) when its slice needs it.

## 19.1a Bearer and basic, one helper

**What:** `bearer` and `basic` Prometheus auth through one helper.

**Decided while building (2026-11-21):**
- `basic` only over `https://`, as Jira does, so the password never crosses in clear.
- A Relay's own Prometheus stays Bearer or none. A new Relay setting and chart value come when a client needs Basic there.
- The fields are `prometheus_auth` and `prometheus_user` on a cluster, and `auth` and `user` on the `prometheus` integration.

**Built (2026-11-21):**
- `usage.auth` (an `httpx.Auth`) replaces the three copies of the header. It sends `httpx.BasicAuth` with the user id, else a `Bearer` whose header is byte for byte the old one, else nothing without a token.
- It serves `Prom` (Sizings, node facts, the Bill check's node-hours), `ask_stores` (per request, on the client shared by every Contract) and the agent's `prometheus` tools.
- The Relay passes its token through it.
- `usage.targets` takes the token, auth and user from where the URL is.
- `contracts.auth_rules` refuses `basic` without a user and a token, or with a URL that isn't `https://`. It also refuses a user under `bearer`. A Relay's cluster may set neither (`prometheus_user` in `RELAY_HOLDS`, `prometheus_auth` beside it).

**Tests:** 15 tests in `test_prometheus_auth.py`. They cover the exact header for each, a URL with a path (Grafana Cloud's `/api/prom`) reaching `/api/prom/api/v1/query`, a cluster with its own URL never taking the integration's user, two Contracts asked through one client each sending only its own credentials, and a refusal without the token. 14 mutations, all caught. No eval rerun: the tools' names, descriptions and schemas are unchanged.

**Known limits:**
- Not checked live yet: Grafana Cloud's free tier, once the owner has an account.

## 19.1b `sigv4` for AMP

**What:** `sigv4` auth for Amazon Managed Prometheus (AMP), with the Contract's `aws` key.

**Decided (grill, 2026-11-22, all recommended):**
- **The region** comes from the workspace URL's host. No new field: a second one could only disagree with the URL.
- **That region must be one the `aws` integration lists.** *Why:* every AWS call already goes to a listed region; the fix is one line in the Contract.
- **Refused at load**, each naming the cluster or the integration: no `aws` integration; a URL that isn't an AMP workspace URL; a token or a user beside it; a Relay's cluster.
- The key is the `aws` integration's IAM user key. No assumed role or session token yet.

**Built (2026-11-22):**
- `usage.SigV4`, an `httpx.Auth` over the Hub's own signer (`servers/aws.py`). It signs `host` and `x-amz-date`, in a header and never in the URL, and dates each request when it is sent: one signer serves a long-lived client, and AWS refuses an old date.
- What is sent is what is signed: the query is rewritten as AWS reads it back (sorted, a space as `%20`, only `A-Za-z0-9-_.~` unescaped). httpx alone would send a space as `+`.
- The signer comes from where the URL is, like the token and the user: a cluster's own Bearer URL under a signed integration is never signed, and the reverse.
- It serves the same three readers as 19.1a: `Prom`, the agent's `prometheus` tools (with and without clusters), and `ask_stores`, which never asks a workspace who it is (AMP has no such page). Two workspaces in one region stay two stores, by their path.
- `contracts.auth_rules` holds the shape rules; the `aws` integration and its regions are checked in `Contract._consistent`.
- A signed store's refusal is shown by its type only (`x-amzn-errortype`), or as "refused" when AWS names none, to the Operator and to the agent: AWS's message can quote the signed request.
- A Target's signer is left out of its comparison, so two reads of a Contract's targets stay equal.
- **Permissions:** the Hub calls query, query range and alerts, so the key needs `aps:QueryMetrics` and `aps:ListAlerts`. (The grill's note named four; the Hub doesn't read labels, series or metadata.)

**Tests:** 25 in `test_prometheus_sigv4.py`: the 13 refusals, the sent query and the signature over a canonical request written by hand, the date per request, repeated names, a URL with a trailing slash, the signer from where the URL is, the agent's tools, two Contracts each signed with its own key, a workspace never asked who it is, and a refusal by its type or with none. 22 mutations, all caught. No eval rerun: the tools' names, descriptions and schemas are unchanged. No API change.

**Known limits:**
- Not proven against AMP or against another signer. The signing is the Hub's own (`servers/aws.py`), whose tests cover requests to `/` with no query; a path and a query are checked here only against AWS's rule written by hand.
- Not checked live yet.

**Live check (planned):** on AWS, about $1, after a cost estimate and the owner's yes.

## 19.1c `azure` and `google`

**What:** `azure` for Azure Monitor (the `azure` integration's Entra app) and `google` for Google Managed Prometheus (the `gcp` integration's service account).

**Decided (grill, 2026-11-22, all recommended):**
- **Only each cloud's own URL shape.** Azure: `https://<workspace>.<region>.prometheus.monitor.azure.com`. Google: `https://monitoring.googleapis.com/v1/projects/<project>/location/global/prometheus`. *Why:* a token taken with a client's app or key can then go to no other host. Sovereign clouds come when a client needs one.
- **Google's project is the URL's**, not the integration's: with a metrics scope it is the scoping project.
- **Azure's 32-day limit:** Needs and HPA bounds are read 31 days back at most, and a policy in use with a longer `window` is refused at load. *Why:* the first read of Needs spans 8 weeks and repeats until it succeeds, so on Azure it would never succeed.
- **`active_alerts` stays** and answers that the store serves no alerts. *Why:* hiding it changes what the model sees and costs an eval run.
- **A refused token** shows the host and the cloud's code, never its description (Entra's quotes the tenant and the app). **A refused query** shows the cloud's code and message, cut at 300 characters.
- **Two isolation rules for Azure:** a workspace is told apart by its hostname, never by its addresses; label values that differ only by case count as the same.

**Built (2026-11-22):**
- `usage.Minted`, an `httpx.Auth` that asks the cloud for a token through the reader's own client before its first query, and keeps it until a minute before it expires. Each reader has its own; nothing is stored or shared between Contracts.
- Azure: Entra's client credentials, for `https://prometheus.monitor.azure.com/.default`. The tenant is escaped into the login path. Google: the JWT bearer grant signed by the existing `GoogleKey`, for `monitoring.read` only, sent to Google's token URL and never the key file's own.
- The key is parsed when a token is first asked for, never at load: one Contract's bad key stops only its own reads.
- It serves the same three readers as 19.1a and 19.1b: `Prom`, the agent's `prometheus` tools (with and without clusters) and `ask_stores`, which never asks these stores who they are.
- The token request carries the reader's own timeout: httpx gives a request made inside an auth flow none.
- The health check of these stores asks `count(up)`, not `vector(1)`: both clouds answer only a query that names a metric.
- `contracts.auth_rules` holds the shape rules for all three clouds in one table; `Contract._consistent` checks the integration, the app's three variables and the windows.
- `Target.longest` carries Azure's 31 days to the hourly read. `_shared_metrics` folds label values on an Azure workspace.
- `usage.refusal` reads Azure's and Google's `{"error": {...}}` as code and message.

**Tests:** 57 in `test_prometheus_minted.py`: the 24 refusals, the exact token request for each cloud (with its timeout) and the assertion checked against the key, a token reused and renewed, a reply read whole, 7 refused tokens, 5 keys that aren't one, the token from where the URL is, two Contracts each with its own app and its own key, the agent's tools, the health check's query, a refused query in each cloud's shape, stores never asked and told apart, labels by case, the window rule, and the 31-day reads. 71 mutations: 70 caught, 1 equivalent (`^` under `re.match`). No eval rerun: the tools' names, descriptions and schemas are unchanged. No API change.

**Known limits:**
- Not proven against Entra, Azure Monitor or Google. The v2.0 token endpoint with `.default` stands in for the v1 `resource=` form Microsoft's page shows.
- A token per reader: each read of a cluster asks for its own. One per store if a cloud ever throttles them.
- Azure throttles queries by the hours they span (30,000 query-hours in 30 seconds for one app; a 31-day query counts 744). Not measured against a real cluster's hourly read.
- Azure's other limits aren't checked at load: one metric name per selector (the Hub's own queries name one; the agent's PromQL may not) and a 60-second step over 48 hours.
- Google answers PromQL from its own backend: no staleness, and `rate()` on a gauge is refused.
- A Google project is taken by its id, not its number.

**Live check (planned):** in the sandboxes, about $1 each, after a cost estimate and the owner's yes.

## 19.2 PagerDuty

**What:** PagerDuty becomes an alert source.

**Decided:**
- A V3 webhook on the Contract's own URL becomes an Alert group, as Grafana's does.
- The webhook is checked by PagerDuty's signature (`X-PagerDuty-Signature`, an HMAC with the subscription's secret in SOPS). *Why:* the signature covers the body, which a token header doesn't. (The grill's reason, that PagerDuty can't send a header, was wrong: a subscription takes custom headers.)
- An incident whose alerts' `dedup_key` is the SHA-256 of a known Alertmanager group key joins that Triage instead of starting a second one. This costs one API call per incident.
- The Client report goes on the incident as a note, only when the Operator sends it (the same button, the same sender recorded), never on its own.
  - *Why:* the client's on-call sees PagerDuty, and a wrong Report there costs trust.
- A REST API key and the `From` email are SOPS secrets.
- Never acknowledge or resolve an incident. *Why:* that is a write to someone else's paging system.

**Decided while building (grill, 2026-11-22, all recommended):**
- **A top-level `pagerduty:` block**, beside `slack:`. *Why:* everything under `integrations:` starts a tool server; this has none.
- **Only the signing secret is required.** The API key adds the alerts' details and a second way to join; the `From` email, with the key, adds the note. *Why:* a note needs a full-access key, which some clients won't hand over.
- **The agent sees an Alertmanager-shaped group.** No prompt or tool change, so no eval rerun.
- **A note that fails:** Slack first. A refusal frees the claim only while nothing was sent; after that the thread names it and nothing is sent twice. No answer keeps the claim, as with Jira.
- **Built before the owner has an account**, on the documented shapes; a free live check follows.

**Built (2026-11-22):**
- `POST /contracts/<id>/pagerduty`, public through Caddy. It takes the raw body, checks the signature (any of its `v1=` values, in constant time, never with an empty secret), then reads the event leniently.
- **A signed delivery is never refused**, whatever it holds: PagerDuty drops a 4xx for good and disables the subscription after three. An unknown event or shape answers 202 "ignored".
- **It answers after the work**, not before: a restart can't lose an incident, and a resolve can't overtake its start. The alerts call has 3 seconds, inside PagerDuty's 5; when it fails the incident is triaged alone.
- `incident.triggered` and `incident.reopened` start; `incident.resolved` ends; the rest are ignored. The group key is `pagerduty:<incident id>`, so a repeat is a duplicate before any API call.
- **The join:** the incident's own `incident_key` and its alerts' `alert_key` values against the SHA-256 of each Alert group the Contract sent in the last 30 minutes (Alertmanager's `Key.Hash`; Grafana's key without the Hub's prefix). A joined incident is remembered under that Triage, so its end resolves nothing.
- **No migration:** an Investigation's incidents are its remembered groups with the `pagerduty:` prefix.
- **The note:** `Send to client` shows when the Contract has a client channel or the Triage has an incident with a `From`. Both get the same words; the note as plain text (`slack.client_note`), since Slack's escaping would show there as `&gt;`.
- **The alerts read has 3 seconds in all** (`asyncio.timeout`), not httpx's per-phase limit.
- **Health:** a `pagerduty` row when a key is set (`GET /abilities`).
- Two Contracts with one signing secret are refused at load. PagerDuty's link is shown in Slack as text, never as a link.
- Only an id of PagerDuty's own shape reaches a URL; a refusal shows its status and code, never its message.

**Tests:** 93 in two files. `test_pagerduty.py` (65): the signature, the events read and the 12 ignored, 17 unknown shapes, the group and its caps, the dedup key against a known SHA-256, the exact alerts, note and Health requests, 18 refusals by status and code, the 3 seconds in all, the note as plain text, and the Contract rules. `test_pagerduty_hub.py` (28): the signature gate (another Contract's secret on this URL, and the reverse), 8 signed deliveries never refused, one Triage per incident, an API that fails, the join (by the incident's key, by an alert's, Grafana's), the window, a joined incident resolving, the note in each failure case, and Health. Isolation: the same group key and the same incident id in two Contracts, each read with its own key; one Contract's note never on another's incident. 109 mutations, all caught; one more was equivalent and its code was removed. No eval rerun: nothing the model is told changes. No API change for the console.

**Known limits:**
- Built on documented shapes: the docs show no `incident.triggered` example, and don't state that `incident_key` equals the alert's dedup key. The alerts' keys are the second way in.
- If PagerDuty's delivery arrives before Alertmanager's own, or the incident is created more than 30 minutes after its Alert group, there are two Triages.
- Without a Slack thread, a join shows only in the Hub's log.
- A delivery replayed after 30 minutes starts a new Triage: PagerDuty signs no timestamp.
- US accounts only; one signing secret per Contract; the first 10 alerts of an incident.
- An Investigation whose incident later reopens into a new Triage no longer has it for a note.
- PagerDuty's page doesn't say a read-only key may call `/abilities` (the Health row). It is a GET, which is what a read-only key is limited to.
- Caddy's route was checked by `make hub-smoke` only for a refused delivery; a signed one through Caddy waits for the live check.

**Live check (planned, free):** in two halves, once the owner has an account. The API calls need only a key. The webhook needs the Hub reachable from the internet: Caddy on the mini PC, with the owner's OK.

## 19.3 Outbound webhooks

**What:** the Hub sends events to a URL the Contract names.

**Decided:**
- Events: Report finished, Approval needed and decided, Executed action, Actuation and Rollback, and Stale notice.
- A versioned JSON payload with what Slack gets, and nothing more.
- Signed with HMAC-SHA256, using a per-Contract SOPS secret, in a `Squero-Signature` header with a timestamp.
- `https://` only.
- Three retries with backoff, then a log line and a metric.

## 19.4 Microsoft Teams and Google Chat

**What:** client channels for Teams and Google Chat. These are not the Operator's channels.

**Decided:**
- A Contract's `client_channels` lists `{kind: teams | google_chat, url_secret}`. Each channel gets the Client report when the Operator sends it.
- Each goes through the product's incoming webhook (Teams Workflows, or a Chat space's webhook). Its URL is a SOPS secret, because it embeds a key. `https://` only.
- No buttons. *Why:* a Teams bot would need an Azure Bot registration and a public endpoint, and the Hub is reachable only through `tailscale serve`. Approvals stay in Slack and the console.
- Built on the payloads the products document.

**Checked:** when the owner has a Microsoft 365 account with Workflows, or a Google Workspace one. Unknown at the grill.

## 19.5 ServiceNow

**What:** ServiceNow, as Jira is.

**Decided:**
- Past incidents in one assignment group are Evidence.
- A Create ServiceNow incident button, which is a write behind approval.
- Basic auth. `https://` only.
- A Personal Developer Instance (free) for the fixtures and the Scenario.

## 19.6 Datadog and New Relic

**What:** triage only, through their official MCP servers, as remote vendor servers in the registry.

**Decided:**
- Sizings from their metrics would be a second metrics engine beside PromQL (`usage.py` is PromQL). That is Deferred until a client without Prometheus appears.

## Live checks and owed items

- Grafana Cloud with `basic` auth (19.1a): owed, needs the owner's free account.
- Teams and Google Chat (19.4): owed, needs a Microsoft 365 or Google Workspace account.
