# Contracts

A Contract is one client engagement. The Hub runs every Contract it finds in `SQUERO_CONTRACTS_DIR` and keeps each one's tools, data and notifications apart. Fields for the client's own cluster agents are in [client-cluster.md](client-cluster.md), the optimizer's in [optimizer.md](optimizer.md), and `model:` and `monthly_budget_usd:` in [running-the-hub.md](running-the-hub.md#model-and-cost).

- [Layout and secrets](#layout-and-secrets)
- [Related alert groups](#related-alert-groups)
- [Masking](#masking)
- [GitOps](#gitops)
- [Jira](#jira)
- [Scheduled Checks](#scheduled-checks)
- [Deploy verification](#deploy-verification)
- [Similar incidents](#similar-incidents)
- [Teachings](#teachings)
- [Vendor integrations](#vendor-integrations)
- [Azure](#azure)
- [Google Cloud](#google-cloud)
- [AWS](#aws)

## Layout and secrets

A Contract is a directory in your **private** contracts repo, never in this one ([ADR 0003](adr/0003-contract-data-in-private-git.md)). It holds `contract.yaml`, a SOPS-encrypted `secrets.sops.yaml` with one age key per Contract, and its own `runbooks/`. Runbooks in `shared/runbooks/` are Shared knowledge for every Contract; a Contract's own Runbook wins on a name clash. A general lesson is a Runbook with no `alerts:`, found by `search_runbooks`. Shared knowledge must hold no client data: `squero contracts validate`, and the Hub at startup, refuse any file under `shared/` that names a Contract (its id or name, a kube context, the host of an integration URL, its Jira project, Slack channels, AWS key id or a vendor setting), as a whole word, ignoring case, and say which line and what it names, never the value. Namespaces, cluster names, terms under 4 characters and generic hosts (no dot, like `prometheus` or `localhost`, an IP, or a cluster-local `*.svc`) aren't checked: they would match every client's text. Before upgrading the Hub to a version with this check, run `squero contracts validate` on the contracts repo with the new image: a shared file that names a Contract stops the Hub at startup, like an invalid Contract does. So it catches a copied name, not an IP address or a person. The agent has no write path to it: its tools only read (the two opt-in Kubernetes actions act on the cluster), none takes a file path, a Runbook is read by name, and both deploys mount the contracts repo read-only.
A kubeconfig whose user runs a credential plugin (`exec`, as EKS, AKS kubelogin and GKE use, or an `auth-provider`) is refused unless that cluster sets `allow_exec: true`. The plugin would run on the Hub next to every other Contract's secrets ([ADR 0002](adr/0002-hub-and-relay.md)).

Name each cluster as your alerts' `cluster` label does: the agent passes that label as the tools' `cluster` argument, and a tool refuses any cluster or namespace outside its Contract. [`demo/contracts`](../demo/contracts) shows the layout with two synthetic Contracts and their test-only keys.

Run these from the contracts repo root, since sops looks for `.sops.yaml` in the current directory:

```bash
brew install sops age
age-keygen -o ~/.config/sops/age/acme.agekey   # one key per Contract, kept outside the repo
# add a creation rule for acme/secrets.sops.yaml with its public key to .sops.yaml
sops edit acme/secrets.sops.yaml               # create or edit the secrets
sops updatekeys acme/secrets.sops.yaml         # after changing acme's key in .sops.yaml
cat ~/.config/sops/age/*.agekey > ~/.config/sops/age/keys.txt
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt squero contracts validate .
```

`validate` reports every problem as `path: field: message`. It never prints a secret value.
Each Contract's Alertmanager posts to `/contracts/<id>/alertmanager` with its own token. `contract.yaml` holds only the token's SHA-256 (`webhook_token_sha256`); `squero contracts new-token` prints a token and its hash line. Two Contracts can't share a token, and none can equal the approver token.

## Related alert groups

A new Alert group joins an open Triage of the same Contract instead of starting its own when they share a match label and no scope label differs. An open Triage is one whose own Alert group still fires, that started within the window (30 minutes, `SQUERO_RELATED_WINDOW_MINUTES`) and that did not fail or get refused. A scope label (`cluster`, `namespace`) present on both must be equal, but sharing one never joins on its own: one namespace often holds unrelated services. At least one match label (`service`, `service_name`, `app`, `deployment`) must be equal. The new group is compared with each Triage's own Alert group, never with the groups already related to it. With several candidates, the most shared match labels win, then the newest Triage. A group never joins its own Triage: a re-sent group with a new Alert still starts a new one. Joining costs nothing: the group is recorded, its re-sends are duplicates of that Triage, it is posted in the Triage's Slack thread (flagged when its severity is higher) and listed on the console page. It is not in the Client report. Only the Triage's own Alert group makes its Proposed actions Stale. With Slack buttons on, the post has a **Triage separately** button (Operator only): the group gets a Triage and thread of its own, once, and the budget applies (over budget, nothing starts and the button stays). From then on the group belongs to that Triage (its re-sends, its resolution, its Stale rule), and the two groups don't join each other while that Triage is within the window (a later incident may join them again). A group that resolved before the click is not tracked, so its Triage starts as if it still fired. Set per Contract:

```yaml
related:
  scope_labels: [cluster, namespace]
  match_labels: [service, service_name, app, deployment]   # [] turns joining off
  window_minutes: 30
```

## Masking

A Contract's Data policy decides what may reach the LLM. Without one, nothing is masked (secrets are still redacted for every Contract). With `data_policy.mask`, the client's identifiers become placeholders before every model call and come back in what the model answers:

```yaml
data_policy:
  mask:
    terms: [acme.internal, Acme]   # whole words, any case; a domain covers its subdomains
    emails: true                   # the default
    ips: true                      # the default; not loopback, 0.0.0.0 or a time like 12:30:45
```

The model sees `<host-3f9a1c2e>`, `<name-…>`, `<email-…>` and `<ip-…>`. Each is an HMAC of the value with the Contract's own `mask-key` secret (in its `secrets.sops.yaml`; `squero contracts validate` refuses a mask without it). So a host reads the same in every Run of that Contract, and differently in any other, and a key rotation changes only that Contract's placeholders. Only the wire is masked: one model-call middleware masks the messages, the system prompt (Teachings included) and the tool definitions just before the provider gets them, and unmasks the reply, tool-call arguments and the structured Report included, before anything else sees it. Tools run on the real names, and the state, checkpoints, stored tool calls, Reports, Slack, Client reports, Jira and the console hold the real values. Questions, Checks, the Teaching drafter and the Report embeddings are masked the same way. Kubernetes names (namespaces, pods, Workloads) are only masked when a term names them: they are the agent's best clues. The system prompt tells the model to copy each placeholder whole (Haiku once shortened one to `<host>`); a placeholder it shortens or invents stays as written. The console's Investigation page says how many values each kind masked for the LLM ("Masked for the LLM: 3 hosts, 1 email"), so a term that never matches shows. A masked term also counts as one of the Contract's names for the Shared knowledge check. Scenarios take a `data_policy:` too; `dependency-bad-rollout-masked` is `dependency-bad-rollout` with the host the failing calls go to masked.

## GitOps

A cluster's `gitops_namespaces` (such as `[argocd, flux-system]`) are where `recent_changes` reads ArgoCD Applications (`argoproj.io/v1alpha1`), Flux Kustomizations (`kustomize.toolkit.fluxcd.io/v1`) and HelmReleases (`helm.toolkit.fluxcd.io/v2`, Flux 2.3 or later; an older API version, like a controller that isn't installed, shows nothing). No other tool may name these namespaces. They are shared with other teams, so an object is shown only when every namespace it deploys into is one of the cluster's `namespaces`: an Application's destination and every namespaced resource in its status, a Kustomization's target and inventory, a HelmRelease's release namespace, history and inventory. An object whose namespaces can't be told, or that deploys to another cluster (an Application whose destination isn't this cluster, a Kustomization or HelmRelease with a `kubeConfig`), is never shown, and nothing of a hidden object (name, message) reaches the model. ArgoCD gives each sync's revision and the one before; Flux gives each applied revision (on Flux before the Kustomization history field, only the last one, timed by its Ready condition) and each Helm release's chart, app version and whether its values changed. `deploy/gitops/rbac-gitops.yaml` (and `deploy/relay/rbac-gitops.yaml` for a Relay) grants read on those three kinds only, all three in each of `argocd` and `flux-system` since each kind is asked in each namespace, never their Secrets; a missing grant shows in the result as a 403 instead of an empty timeline, and a kind whose controller isn't installed shows nothing. The kind demo runs this against stand-in CRDs and objects ([`demo/gitops.sh`](../demo/gitops.sh)), not the real controllers.

## Jira

A builtin integration gives the agent the Contract's past Jira issues as Evidence: an earlier issue with the same cause, and how it was resolved.

```yaml
integrations:
  - name: jira
    url: https://acme.atlassian.net        # Jira Cloud; https only (Basic auth)
    email: sre-bot@acme.example            # the API token's owner
    token_secret: jira-token               # a name in secrets.sops.yaml
    project: OPS                           # every search stays inside this project
```

The agent gets two read tools: `jira_search_issues` (words from a service or an error, newest first) and `jira_get_issue` (one issue's description, resolution and latest comments). The Hub writes every JQL query itself, inside `project`; the model passes only words, so a search can't reach another project the token can see, and `jira_get_issue` refuses an issue that isn't in the project. Make the API token for an account that can browse that project and nothing else you'd mind the agent reading.

### Create Jira issue

A finished Report has a **Create Jira issue** button on its Slack thread and its console page (the token must be allowed to create issues in `project`; `issue_type`, default `Task`, names the type as the project calls it). It files the full Report, Evidence and executed actions included, redacted like Slack, as an issue labelled `squero`; the text is built as plain text nodes, so markup in tool output stays text, and only links the Hub kept become links. Only the Operator can file, once per Investigation: the Hub claims it first, so a double click or a redelivered click files nothing more. If Jira refuses (a wrong issue type, say), the message says why and the button works again. If Jira doesn't answer, it may have created the issue, so the Hub keeps the claim and asks you to check the project before filing by hand. The thread and the console then show the issue's key and link. The agent never files: creating is Hub code behind the button, not a tool.

## Scheduled Checks

A Check runs on a schedule with no alert: its prompt, which you write, says what healthy means, and the agent returns a verdict, passed or failed, with Evidence. A failing Check also returns a full Report, investigated like an alert, and can propose actions for your approval. A Run that ends without a verdict (an error, the call limit) counts as failing. Passing is quiet: no Slack post, and it is left out of the console's Investigations list and `GET /investigations`; the console's Checks table shows each Check's next time, last Run, Open failure and last skip. The first failure posts a thread like a Triage's (Report, Feedback, Jira, Send to client) and becomes the Check's Open failure. Each later failing Run replies *still failing* in that thread, unless it proposes an action: then it gets its own thread with the buttons, the old thread says where it continued, and the new one is the Open failure from then on. The first passing Run replies *recovered* in it and ends it. The schedule is a 5-field cron expression in the Check's `timezone` (default UTC, daylight saving included), at most once every 15 minutes (`contracts validate` refuses a faster one). It lives in Postgres: a claim moves the next time and records the Run in one transaction, so a restart never fires a Check twice, and fire times missed while the Hub was down run once at startup. A Check never starts while its previous Run is running or waiting for a decision, and at 100% of the budget it is skipped (recorded, not posted: the budget notice already said so). A changed `schedule` or `timezone` takes effect at the next start; a Check removed from `contract.yaml` stops. A Check gets the general Teachings, not those for alert names, and its Report isn't searched as a past incident (only Triage Reports are). Checks share `SQUERO_MAX_CONCURRENT_TRIAGES` with Triages, so many Checks on the same minute make alerts wait. A Haiku Triage costs about $0.004; a Check every 15 minutes is 96 Runs a day, about $0.40 a day ($12 a month) on Haiku and 20 times that on Sonnet. Each Run also keeps its checkpoints, raw tool results and usage rows, and nothing prunes them yet: 96 sets a day per Check at that rate.

```yaml
checks:
  - name: shop-healthy
    schedule: "0 9 * * 1-5"        # weekdays at 9
    timezone: Europe/Rome
    prompt: Every deployment in namespace shop has all replicas ready, and no pod restarted in the last hour.
```

## Deploy verification

With `deploy_checks:` set, the Hub verifies each deploy it sees in the Contract's clusters with a Check, `delay_minutes` after it (default 10). Every 2 minutes it calls the Contract's own kubernetes `recent_changes` for the last hour (rollouts, ArgoCD syncs, Flux releases: no model call, Kubernetes reads only). A deploy is known by its namespace, object and revision. The first successful poll of each cluster after the Hub starts is the baseline, so deploys from before it, or from while the Hub was down, are never verified, and each later one is verified once. Deploys in one namespace close together are verified in one Run: a new one joins the pending verification and pushes it back, never past twice the delay from the first. The pending verification is in Postgres and fires after a restart. The Check asks whether what changed is healthy now and whether anything else in the namespace started failing after the change, because another workload's rollout breaking its callers is a deploy failure too. Something already failing the same way before the change is noted in the summary, never failed for: otherwise one existing crash loop would fail every deploy next to it. The model can't tell that reliably from pod ages and events, so the Hub says it: each poll also lists the pods of the namespaces it watches (each pod's Workload from its owners, `list_pods`' `workload`; from an older Relay, guessed from the pod's name), and a deploy's prompt names the workloads that weren't running ready at the poll just before it (or that nothing was, or that the Hub couldn't tell). Deploys joining a pending verification keep what failed before the first one. Its prompt holds only each change's object and revision, never a change cause or sync message, which whoever deploys can write: the agent reads those through `recent_changes`, as data. A passing verification is quiet, and a failing one gets its own thread like a Check's, with Proposed actions. It has no Open failure and never touches a scheduled Check's. The budget and overlap rules are the Checks': at 100% it is skipped (shown on the console), and while the namespace's last verification is still open the next one waits. The console lists the latest verifications. Known limits: `kubectl rollout undo` reuses an old ReplicaSet, which keeps its old creation time, so the rollback itself isn't seen; a GitOps object deploying into several namespaces is grouped under all of them; a burst of more changes than `recent_changes` returns is cut; and every deploy costs a Run.

```yaml
deploy_checks:
  delay_minutes: 10
  namespaces: [shop]                  # optional: only these
  prompt: Also check that checkout's /health answers.   # optional, added to the Hub's
```

## Similar incidents

`- name: incidents` gives the agent the Contract's own past Triage Reports: `incidents_search` (Postgres full-text search over the title and every string in the Report, best match first, the last 90 days by default, at most 365) and `incidents_read` (one Report in full, with the actions the Operator approved or rejected and why, what ran, and its Jira issue). Only finished Triages of the same Contract are found or read: the Hub builds the server for one Contract and puts its id in every query, and the model passes only words and ids. A Triage searches once for the alerting workload before its Report, even when the cause looks clear: a past Report matters exactly when current evidence looks complete but isn't. A match is a lead the agent confirms with current tools, never proof on its own. A search that found nothing is dropped from the Evidence. The CLI's `squero triage` has no database, so it leaves the tools and their guide out.
With `SQUERO_EMBEDDING_MODEL` set, the search also finds Reports by meaning: a Report that says *certificate expired* for an alert about an *x509 handshake failure*, which share no word. When a Triage Report finishes, the Hub embeds its title, summary, category, root cause, affected workloads and Evidence findings (pgvector, 512 dimensions, through the model gateway), and a search embeds its keywords. The two rankings, by words and by meaning (cosine distance under `SQUERO_EMBEDDING_MAX_DISTANCE`), are merged by reciprocal rank fusion, so a Report both find comes first. Only vectors made by the current model are compared: after changing it, or to embed Reports from before it was set, run `squero incidents embed` once (it embeds what is missing and exits non-zero if any failed). A failed embedding never fails a Triage or a search: that Report or that search uses words only. Embedding tokens are recorded in the Contract's usage and count toward its budget (a search's under the Contract alone, not an Investigation). A Contract whose Report text must not reach the embedding model sets `- name: incidents` with `embeddings: false`.

## Teachings

Lessons for one Contract's future Investigations, which you approve before they are ever used. After a Triage Report, Slack asks *Was this Report right?* (the console page too): **Right** only records it; **Wrong…** asks for your correction, and the agent drafts a Teaching from it in one model call on the Contract's model, counted against its budget and shown as the Investigation's cost. A reason you type when rejecting a Proposed action drafts one too, after the resumed Run, with its Report as context (a Stale rejection, or one without a typed reason, drafts nothing). The drafter sees the context, your words and the Contract's approved Teachings only, never a proposed, rejected or retired one, or another Contract's. A draft that says what an approved or waiting Teaching already says isn't queued (*Already taught*); one that contradicts an approved Teaching is flagged, and approving it retires the old one. Each proposal is posted in the Investigation's thread with **Approve**, **Edit…** (approves exactly the text you submit) and **Reject**, and listed on the console's Teachings page, where you can also approve an edited text, reject, retire an approved one, or write one by hand (approved at once). At most 500 characters, optionally for some alert names. A draft that fails or is over budget is said in the thread; your feedback is kept either way. Approved Teachings go into every Run's prompt, Triage and Question, for their own Contract only, under *Teachings from the Operator*: they override the agent's defaults, never its Rules. A Triage gets those for one of its alert names first, then the general ones (none for other alerts); a Question gets every one; newest first, at most 30 and 6,000 characters. The Teachings page warns when a Contract's don't all fit. Each Run reads them as they are when it starts, so a resume after an approval sees one retired or approved meanwhile. A Scenario can list `teachings:` ({text, alerts}), replayed the same way, and Save as Scenario writes the Contract's approved ones as of saving (not necessarily those the Run saw); `expect.recommendations` checks keywords in the Recommendations.

## Vendor integrations

Besides the builtins, a Contract can turn on a vendor MCP server the Hub knows:

```yaml
integrations:
  - name: grafana             # its tools appear as grafana_<tool>
    vendor: grafana           # an entry in the Hub's registry (src/squero/servers/vendors.py)
    env: {GRAFANA_URL: https://grafana.acme.example}
    env_secrets: {GRAFANA_SERVICE_ACCOUNT_TOKEN: grafana-token}   # names in secrets.sops.yaml
    tools: {search_dashboards: read, query_prometheus: read}       # only these are visible
    probe: {tool: search_dashboards, args: {query: ""}}            # optional health check
```

The command comes from the Hub's registry (an absolute path), never from the contracts repo, and the binary ships in the image. `env` and `env_secrets` may set only the variables the vendor's registry entry lists: anything else, like `LD_PRELOAD`, `PATH` or `NODE_OPTIONS`, could run code and is refused when the Contract loads. The process gets only those, plus MCP's safe defaults, never the Hub's environment. `tools` is the Hub's own list: a tool not listed is invisible, and a tool needs approval if the list says `write` or the server doesn't mark it read-only. Give each Contract its own vendor credentials, scoped to that client: the Hub keeps Contracts apart, but it can't see inside a vendor account several clients share.

The registry has one vendor so far, `grafana` ([mcp-grafana](https://github.com/grafana/mcp-grafana), pinned in the Dockerfile). It runs read-only (`--disable-write`, so every tool claims read-only), sends no usage statistics to Grafana Labs, and offers only the search, datasource, Prometheus, Loki, Tempo, Pyroscope, alerting, dashboard and navigation tools: not the generic API, rendering or docs tools. It takes `GRAFANA_URL`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` and `GRAFANA_ORG_ID`. Give it a service account with the Viewer role in that client's Grafana. List in `tools` what the agent should use, for example `query_loki_stats`, `query_loki_patterns` and `query_loki_logs` (patterns before raw logs), `search_tempo_traces` and `get_tempo_trace`, `query_prometheus`, `search_dashboards` and `generate_deeplink`. With `generate_deeplink` listed, Reports link their key evidence to Grafana. `src/squero/servers/snapshots/grafana.json` has every tool's name and arguments.

## Azure

**Azure** is the second registry vendor, `azure`: the official [Azure MCP server](https://github.com/microsoft/mcp) (`azmcp`, pinned in the Dockerfile and verified against npm's integrity hash). It runs read-only (`--read-only`, every tool claims read-only) with an explicit list of 62 tools: subscriptions and resource groups, resource health, Monitor metrics, activity and log queries, Advisor, quotas, AKS clusters and node pools, VMs and scale sets, App Service, Functions, Container Apps, SQL, PostgreSQL, MySQL, Cosmos DB accounts, Redis, Storage, Service Bus and Event Hubs. Not Key Vault (secrets), app settings (often connection strings), Cosmos DB documents (client data), or the tools that deploy or build az CLI commands. The registry sets its telemetry off and makes it use only the Contract's service principal, never the host's `az login`, VS Code session or managed identity.

```yaml
integrations:
  - name: azure
    vendor: azure
    env: {AZURE_TENANT_ID: <tenant id>, AZURE_CLIENT_ID: <app id>, AZURE_SUBSCRIPTION_ID: <subscription id>}
    env_secrets: {AZURE_CLIENT_SECRET: azure-client-secret}    # only from secrets.sops.yaml
    tools: {resourcehealth_availability-status_get: read, monitor_metrics_query: read, quota_usage_check: read}
```

Make a service principal with the **Reader** role on the client's subscription, nothing more: `az ad sp create-for-rbac --name squero-<client> --role Reader --scopes /subscriptions/<id>`. Reader reads resources and metrics, and its `*/read` usually covers Log Analytics queries too; a workspace that uses table-level (granular) access also needs **Log Analytics Data Reader** on it ([Manage access to Log Analytics workspaces](https://learn.microsoft.com/azure/azure-monitor/logs/manage-access#azure-rbac)). Reader can't list keys or read app settings: those are actions, not reads. Resource health answers 409 until the subscription has `Microsoft.ResourceHealth` registered, which Reader can't do: ask the client to register it. Under Reader, `appservice_webapp_deployment_get` is Unauthorized and the App Service diagnostics detectors return empty tables, so don't list them. `src/squero/servers/snapshots/azure.json` lists every tool with its arguments.

## Google Cloud

**Google Cloud** is the third registry vendor, `gcp`: Google's own remote MCP servers for [Cloud Logging](https://docs.cloud.google.com/logging/docs/reference/v2_mcp/mcp), Cloud Monitoring and Cloud Trace (`https://{logging,monitoring,cloudtrace}.googleapis.com/mcp`), merged into one integration of 17 tools, every one annotated read-only. Nothing runs on the Hub: it calls those URLs with a token it mints from the Contract's service account key, posting to Google's token endpoint only (never the key's own `token_uri`), with the scopes `logging.read`, `monitoring.read` and `trace.readonly`, so a token can't write whatever the roles allow. The key never leaves the Hub. Error Reporting has no remote server, so it isn't offered.

```yaml
integrations:
  - name: gcp
    vendor: gcp
    env: {GOOGLE_CLOUD_PROJECT: <project id>}
    env_secrets: {GOOGLE_SERVICE_ACCOUNT_KEY: gcp-key}    # the JSON key, only from secrets.sops.yaml
    tools: {list_log_names: read, list_log_entries: read, query_range: read, list_traces: read, get_trace: read}
```

The prompt names the project, so the model passes `projects/<id>` where a tool wants it. `list_log_names` must be listed: the health page calls it on the project, since listing these servers' tools needs no credential and would show a revoked key as healthy. Make a service account in the client's project with **Logs Viewer** (`roles/logging.viewer`), **Monitoring Viewer** (`roles/monitoring.viewer`), **Cloud Trace User** (`roles/cloudtrace.user`) and **MCP Tool User** (`roles/mcp.toolUser`, which allows calling MCP tools), then a JSON key: `gcloud iam service-accounts keys create key.json --iam-account=squero@<project>.iam.gserviceaccount.com`. Cloud Trace User can also manage trace scopes; the read-only token scopes keep it from doing so. The Logging, Monitoring and Trace APIs must be enabled in the project. An organization that blocks key creation (`iam.disableServiceAccountKeyCreation`) needs an exception for this account.

## AWS

**AWS** is a builtin, `aws`, not a vendor. AWS's official servers don't fit: the local CloudWatch server claims none of its tools read-only, so every call would need approval, and it adds 249 MB to the image. The remote AWS MCP Server reads data only through `run_script`, which claims to be destructive. The builtin has six read-only tools:
- alarms: `describe_alarms`
- metrics: `list_metrics`, `get_metric_data`
- logs: `describe_log_groups`, `query_logs` (Logs Insights, stopped after 20 s)
- what changed: `lookup_events` (CloudTrail management events)

The Hub signs each request itself (SigV4), so the key never leaves it, and sends it only to one of the Contract's regions.

```yaml
integrations:
  - name: aws
    regions: [us-east-1]                        # the first is the default
    access_key_id: AKIA...                       # an IAM user's key
    secret_access_key_secret: aws-secret-key     # its secret, only from secrets.sops.yaml
```

Make an IAM user with **CloudWatchReadOnlyAccess** and **AWSCloudTrail_ReadOnlyAccess**, nothing more, and an access key for it; for the optimizer, also `ce:GetCostAndUsage` (its [Bill check](optimizer.md#bill-check)) and `ec2:DescribeSpotPriceHistory` (its [spot list prices](optimizer.md#spot-nodes), from 0.59.0: refused, a spot node is priced at its discount and the read waits a day). Not ReadOnlyAccess: it also reads S3 objects, DynamoDB items and Lambda environment variables, which are client data and often secrets. The health page reads the alarms in the first region.

Each registry vendor has a schema snapshot in `src/squero/servers/snapshots/`, its tools' names, descriptions, schemas and annotations, so Scenarios with a vendor replay without its binary. After bumping a vendor's pinned version, run `make snapshots` (it runs `squero integrations snapshot <vendor>` in the image, where the pinned binary is) and commit the diff. `--check` fails when the running vendor no longer matches its snapshot, and hub-smoke runs it in CI. `gcp` isn't pinned: Google deploys its servers, so in hub-smoke its drift is a warning, not a failure. Drift can't make the Hub less safe there: a new tool stays hidden until a Contract lists it, a tool that stops claiming read-only needs approval, and production always uses the live schemas. Only a replay sees an old one, until `make snapshots`. The vendor runs with no Contract and no variables for this, so no credential can end up in the file.
