# Phase 19: Integrations breadth

**In short:** Phase 19 adds the alert sources, paging tools and outbound channels that clients use. It starts with managed Prometheus logins, so the optimizer works for clients on managed clouds. 19.1a and 19.1b are built so far.

**Status:** grilled 2026-11-21. 2 of 8 slices built (19.1a, 2026-11-21; 19.1b, 2026-11-22).

**Goal:** integrations only, through the Phase 4 framework: managed Prometheus logins, PagerDuty, outbound webhooks, Microsoft Teams and Google Chat, ServiceNow, and Datadog and New Relic.

**Done when:** every integration has a probe and fixtures captured from a real free or dev account, through the Phase 4 framework. A recorded Scenario and a passing eval only where the agent gains tools (Datadog, New Relic, ServiceNow's past incidents). A Teams post or an outbound webhook changes nothing the model sees.

| Slice | What | Status |
|---|---|---|
| [19.1a](#191a-bearer-and-basic-one-helper) | `bearer` and `basic` through one Prometheus auth helper | ✅ built, Grafana Cloud check owed |
| [19.1b](#191b-sigv4-for-amp) | `sigv4` for Amazon Managed Prometheus | ✅ built, AWS check owed |
| 19.1c | `azure` and `google` Prometheus auth | Not built |
| 19.2 | PagerDuty | Not built |
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

**Live check (planned):** in the sandboxes, about $1 each, after a cost estimate and the owner's yes.

## 19.2 PagerDuty

**What:** PagerDuty becomes an alert source.

**Decided:**
- A V3 webhook on the Contract's own URL becomes an Alert group, as Grafana's does.
- The webhook is checked by PagerDuty's signature (`X-PagerDuty-Signature`, an HMAC with the subscription's secret in SOPS). *Why:* PagerDuty can't send the Hub's token header.
- An incident whose alerts' `dedup_key` is the SHA-256 of a known Alertmanager group key joins that Triage instead of starting a second one. This costs one API call per incident.
- The Client report goes on the incident as a note, only when the Operator sends it (the same button, the same sender recorded), never on its own.
  - *Why:* the client's on-call sees PagerDuty, and a wrong Report there costs trust.
- A REST API key and the `From` email are SOPS secrets.
- Never acknowledge or resolve an incident. *Why:* that is a write to someone else's paging system.

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
