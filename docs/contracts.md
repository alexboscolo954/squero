# Contracts

**In short:** a Contract is one client engagement, a directory in your private contracts repo.
The Hub runs every Contract it finds in `SQUERO_CONTRACTS_DIR`, and keeps each one's tools, data and notifications apart.
This page covers the layout, secrets, alert rules and each integration a Contract can turn on.
Fields for the cluster agents are in [client-cluster.md](client-cluster.md), optimizer fields in [optimizer.md](optimizer.md), and `model:` and `monthly_budget_usd:` in [running-the-hub.md](running-the-hub.md#model-and-cost).

- [Layout and secrets](#layout-and-secrets)
- [Related alert groups](#related-alert-groups)
- [Masking](#masking)
- [GitOps](#gitops)
- [Jira](#jira)
- [ServiceNow](#servicenow)
- [Scheduled Checks](#scheduled-checks)
- [Deploy verification](#deploy-verification)
- [Similar incidents](#similar-incidents)
- [Teachings](#teachings)
- [Vendor integrations](#vendor-integrations)
- [Azure](#azure)
- [Google Cloud](#google-cloud)
- [AWS](#aws)

## Layout and secrets

A Contract is a directory in your **private** contracts repo. It never lives in this repository ([ADR 0003](adr/0003-contract-data-in-private-git.md)).

Each Contract directory holds:

| File or folder | What it is |
|---|---|
| `contract.yaml` | The Contract's config |
| `secrets.sops.yaml` | Its secrets, SOPS-encrypted, with one age key per Contract |
| `runbooks/` | Its own Runbooks |

### Shared knowledge

- Runbooks in `shared/runbooks/` are Shared knowledge for every Contract.
- A Contract's own Runbook wins on a name clash.
- A general lesson is a Runbook with no `alerts:`. The agent finds it with `search_runbooks`.

### The Shared knowledge check

Shared knowledge must hold no client data. Two places enforce this:

- `squero contracts validate`;
- the Hub, at startup.

Both refuse any file under `shared/` that names a Contract. A name counts as a match as a whole word, ignoring case. The names checked are:

- the Contract's id or name;
- a kube context;
- the host of an integration URL;
- its Jira project;
- its Slack channels;
- its AWS key id;
- a vendor setting.

The error says which line and what it names. It never prints the value.

**Not checked:** namespaces, cluster names, terms under 4 characters, and generic hosts. Generic hosts are those with no dot (like `prometheus` or `localhost`), an IP, or a cluster-local `*.svc`. They would match every client's text.

*Why:* the check catches a copied name. It does not catch an IP address or a person.

### Before you upgrade the Hub

Before you upgrade the Hub to a version with this check:

- Run `squero contracts validate` on the contracts repo, with the new image.
- A shared file that names a Contract stops the Hub at startup, the same way an invalid Contract does.
- So the check catches a copied name, not an IP address or a person.

### What the agent can do with the repo

- The agent has no write path to the contracts repo.
- Its tools only read. The two opt-in Kubernetes actions act on the cluster, not on files.
- No tool takes a file path. A Runbook is read by name.
- Both deploys mount the contracts repo read-only.

### Credential plugins

A kubeconfig whose user runs a credential plugin is refused. This covers:

- `exec`, as EKS, AKS kubelogin and GKE use;
- an `auth-provider`.

The kubeconfig is accepted only if that cluster sets `allow_exec: true`.

*Why:* the plugin would run on the Hub, next to every other Contract's secrets ([ADR 0002](adr/0002-hub-and-relay.md)).

### Naming clusters

- Name each cluster as your alerts' `cluster` label does.
- The agent passes that label as the tools' `cluster` argument.
- A tool refuses any cluster or namespace outside its Contract.
- [`demo/contracts`](../demo/contracts) shows the layout, with two synthetic Contracts and their test-only keys.

### Secrets with SOPS

Run these from the contracts repo root. SOPS looks for `.sops.yaml` in the current directory.

```bash
brew install sops age
age-keygen -o ~/.config/sops/age/acme.agekey   # one key per Contract, kept outside the repo
# add a creation rule for acme/secrets.sops.yaml with its public key to .sops.yaml
sops edit acme/secrets.sops.yaml               # create or edit the secrets
sops updatekeys acme/secrets.sops.yaml         # after changing acme's key in .sops.yaml
cat ~/.config/sops/age/*.agekey > ~/.config/sops/age/keys.txt
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt squero contracts validate .
```

- `validate` reports every problem as `path: field: message`.
- It never prints a secret value.

### Alert tokens

- Each Contract's Alertmanager posts to `/contracts/<id>/alertmanager`, with its own token.
- `contract.yaml` holds only the token's SHA-256, in `webhook_token_sha256`.
- `squero contracts new-token` prints a token and its hash line.
- Two Contracts can't share a token.
- No Contract's token can equal the approver token.

## Related alert groups

A new Alert group can join an open Triage of the same Contract. It does not start its own Triage in that case.

### When a group joins

A new group joins when all of these hold:

- they share a match label, and
- no scope label differs.

An **open Triage** is one that:

- has an Alert group that still fires;
- started within the window (30 minutes, `SQUERO_RELATED_WINDOW_MINUTES`);
- did not fail or get refused.

### The label rules

| Rule | What it means |
|---|---|
| Scope labels | `cluster` and `namespace`. A scope label present on both must be equal. |
| Match labels | `service`, `service_name`, `app`, `deployment`. At least one must be equal. |
| Sharing a scope | Sharing a scope label never joins two groups on its own. One namespace often holds unrelated services. |

- The new group is compared with each Triage's own Alert group.
- It is never compared with the groups already related to that Triage.
- With several candidates, the most shared match labels win. Ties go to the newest Triage.
- A group never joins its own Triage. A re-sent group with a new Alert still starts a new one.

### What joining does

- The group is recorded.
- Its re-sends count as duplicates of that Triage.
- It is posted in the Triage's Slack thread, flagged when its severity is higher.
- It is listed on the console page.
- It is not in the Client report.
- Only the Triage's own Alert group makes its Proposed actions Stale.

### Triage separately

With Slack buttons on, the post has a **Triage separately** button. Only the Operator can press it.

- The group gets a Triage and a thread of its own, once.
- The budget applies. Over budget, nothing starts and the button stays.
- From then on, the group belongs to that Triage. Its re-sends, its resolution and its Stale rule follow that Triage.
- The two groups don't join each other while that Triage is within the window. A later incident may join them again.
- A group that resolved before the click is not tracked. Its Triage starts as if it still fired.

### Config

```yaml
related:
  scope_labels: [cluster, namespace]
  match_labels: [service, service_name, app, deployment]   # [] turns joining off
  window_minutes: 30
```

| Field | Example | What it does |
|---|---|---|
| `scope_labels` | `[cluster, namespace]` | Labels that must be equal on both groups, when both carry them |
| `match_labels` | `[service, service_name, app, deployment]` | At least one must be equal. `[]` turns joining off |
| `window_minutes` | `30` | How long after a Triage starts a group can still join it. Overrides `SQUERO_RELATED_WINDOW_MINUTES` |

## Masking

A Contract's Data policy decides what may reach the LLM.

- Without a Data policy, nothing is masked.
- Secrets are still redacted for every Contract.

### Config

With `data_policy.mask`, the client's identifiers become placeholders before every model call. They come back in what the model answers.

```yaml
data_policy:
  mask:
    terms: [acme.internal, Acme]   # whole words, any case; a domain covers its subdomains
    emails: true                   # the default
    ips: true                      # the default; not loopback, 0.0.0.0 or a time like 12:30:45
```

| Field | Default | What it does |
|---|---|---|
| `terms` | not set | Whole words to mask, any case. A domain also covers its subdomains |
| `emails` | `true` | Masks email addresses |
| `ips` | `true` | Masks IP addresses. Not loopback, not `0.0.0.0`, not a time like `12:30:45` |

### What the model sees

The model sees placeholders in this form:

- `<host-3f9a1c2e>`
- `<name-…>`
- `<email-…>`
- `<ip-…>`

How each placeholder is built:

- It is an HMAC of the value.
- The key is the Contract's own `mask-key` secret, in its `secrets.sops.yaml`.
- `squero contracts validate` refuses a mask without that key.

What that gives you:

- A host reads the same in every Run of that Contract.
- The same host reads differently in any other Contract.
- A key rotation changes only that Contract's placeholders.

### What is masked, and where

Only the wire is masked. One model-call middleware does this:

- It masks the messages, the system prompt (Teachings included) and the tool definitions, just before the provider gets them.
- It unmasks the reply, the tool-call arguments and the structured Report, before anything else sees them.

What stays real:

- Tools run on the real names.
- The state, checkpoints, stored tool calls, Reports, Slack, Client reports, Jira and the console hold the real values.

Also masked the same way:

- Questions;
- Checks;
- the Teaching drafter;
- the Report embeddings.

Kubernetes names are different. Namespaces, pods and Workloads are masked only when a term names them. They are the agent's best clues.

### When the model gets a placeholder wrong

- The system prompt tells the model to copy each placeholder whole.
- Haiku once shortened one to `<host>`.
- A placeholder the model shortens or invents stays as written.

### Checking it works

- The console's Investigation page says how many values each kind masked for the LLM.
- Example: "Masked for the LLM: 3 hosts, 1 email".
- So a term that never matches shows.
- A masked term also counts as one of the Contract's names for the Shared knowledge check.

### Scenarios

- Scenarios take a `data_policy:` too.
- `dependency-bad-rollout-masked` is `dependency-bad-rollout` with the host that the failing calls go to masked.

## GitOps

A cluster's `gitops_namespaces` are where `recent_changes` reads GitOps objects. An example is `[argocd, flux-system]`.

### What is read

| Kind | API version | Note |
|---|---|---|
| ArgoCD Applications | `argoproj.io/v1alpha1` | |
| Flux Kustomizations | `kustomize.toolkit.fluxcd.io/v1` | |
| Flux HelmReleases | `helm.toolkit.fluxcd.io/v2` | Flux 2.3 or later |

- An older API version shows nothing.
- A controller that isn't installed shows nothing.
- No other tool may name these namespaces.

### Which objects are shown

These namespaces are shared with other teams. So an object is shown only when every namespace it deploys into is one of the cluster's `namespaces`:

- **Application:** its destination, and every namespaced resource in its status.
- **Kustomization:** its target and inventory.
- **HelmRelease:** its release namespace, history and inventory.

Never shown:

- an object whose namespaces can't be told;
- an object that deploys to another cluster. That means an Application whose destination isn't this cluster, or a Kustomization or HelmRelease with a `kubeConfig`.

Nothing of a hidden object reaches the model. That includes its name and message.

### Revisions each controller gives

- **ArgoCD:** each sync's revision, and the one before.
- **Flux:** each applied revision.
  - On Flux before the Kustomization history field, only the last revision. It is timed by its Ready condition.
- **Each Helm release:** its chart, its app version, and whether its values changed.

### RBAC

- `deploy/gitops/rbac-gitops.yaml` grants read on those three kinds only.
- For a Relay, the chart's `relay.gitopsNamespaces` does the same job.
- Grant all three kinds in each of `argocd` and `flux-system`. Each kind is asked in each namespace.
- The grant never covers their Secrets.
- A missing grant shows in the result as a 403. It does not show as an empty timeline.

### The kind demo

- The kind demo runs this against stand-in CRDs and objects ([`demo/gitops.sh`](../demo/gitops.sh)).
- It does not run against the real controllers.

## Jira

A builtin integration gives the agent the Contract's past Jira issues as Evidence. That means an earlier issue with the same cause, and how it was resolved.

### Config

```yaml
integrations:
  - name: jira
    url: https://acme.atlassian.net        # Jira Cloud; https only (Basic auth)
    email: sre-bot@acme.example            # the API token's owner
    token_secret: jira-token               # a name in secrets.sops.yaml
    project: OPS                           # every search stays inside this project
```

| Field | Example | What it does |
|---|---|---|
| `url` | `https://acme.atlassian.net` | Jira Cloud. Must be https (Basic auth) |
| `email` | `sre-bot@acme.example` | The API token's owner |
| `token_secret` | `jira-token` | A name in `secrets.sops.yaml` |
| `project` | `OPS` | Every search stays inside this project |

### What the agent can read

The agent gets two read tools:

- `jira_search_issues`: searches by words from a service or an error, newest first.
- `jira_get_issue`: one issue's description, resolution and latest comments.

The Hub writes every JQL query itself, inside `project`. The model passes only words. So:

- a search can't reach another project the token can see;
- `jira_get_issue` refuses an issue that isn't in the project.

Make the API token for an account that can browse that project, and nothing else you'd mind the agent reading.

### Create Jira issue

A finished Report has a **Create Jira issue** button. It appears on its Slack thread and on its console page.

- The token must be allowed to create issues in `project`.
- `issue_type` names the type, as the project calls it. The default is `Task`.

What gets filed:

- The full Report, Evidence and executed actions, redacted like Slack.
- The issue is labelled `squero`.
- The text is built as plain text nodes. Markup in tool output stays text.
- Only links the Hub kept become links.

Who can file, and how often:

- Only the Operator can file.
- Once per Investigation.
- The Hub claims it first. So a double click, or a redelivered click, files nothing more.

If something goes wrong:

- **Jira refuses** (a wrong issue type, say): the message says why, and the button works again.
- **Jira doesn't answer:** it may have created the issue. The Hub keeps the claim and asks you to check the project before filing by hand.

After filing, the thread and the console show the issue's key and link.

The agent never files. Creating is Hub code behind the button, not a tool.

## ServiceNow

A builtin integration gives the agent the past incidents of **one assignment group** in the client's ServiceNow, as Evidence. That means an earlier incident with the same cause, and how it was resolved.

It reads only. A Create ServiceNow incident button is the next slice (19.5b).

### Config

```yaml
integrations:
  - name: servicenow
    url: https://acme.service-now.com            # the instance; https only (Basic auth)
    user: squero.reader                          # a ServiceNow user for the Hub
    password_secret: servicenow-password         # a name in secrets.sops.yaml
    group: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6      # the assignment group's sys_id
```

| Field | Example | What it does |
|---|---|---|
| `url` | `https://acme.service-now.com` | The instance. Must be https with no path (Basic auth) |
| `user` | `squero.reader` | The user the Hub signs in as |
| `password_secret` | `servicenow-password` | A name in `secrets.sops.yaml` |
| `group` | 32 characters, `0-9` and `a-f` | The assignment group's `sys_id`. Every read stays inside it |

A Contract can have ServiceNow and [Jira](#jira) together.

### Set it up

1. **Make a user for the Hub** in ServiceNow. Someone with the `user_admin` role does this:
   - open the Users list (in the app's menu, **User Administration**, then **Users**) and press **New**;
   - set a **User ID** (this is `user`) and a **Password**;
   - set **Identity type** to **Machine**. That ticks **Web service access only**: the user can call the API and can't sign in to the app;
   - give it a role that reads incidents of the group, and nothing else you'd mind the agent reading.
2. **Find the group's `sys_id`.** Ask the instance, signed in as that user (curl asks for the password; it is not in the command):

   ```bash
   curl -u squero.reader -H "Accept: application/json" \
     "https://acme.service-now.com/api/now/table/sys_user_group?sysparm_query=name=Payments%20SRE&sysparm_fields=name,sys_id"
   ```

   The answer holds `"sys_id": "<32 characters>"`. An empty `result` means the name is wrong, or the user can't read groups.
3. **Add the password** to the Contract's secrets: `sops edit acme/secrets.sops.yaml`, then a line `servicenow-password: <the password>`. See [Layout and secrets](#layout-and-secrets).
4. **Add the block above** to `contract.yaml` and run `squero contracts validate <contracts dir>`.
5. **Restart the Hub**, then open the console's Health page for the Contract: a `servicenow` row says ok, or shows ServiceNow's error.

**Not confirmed from ServiceNow's pages** (they did not load, and there is no instance to try yet):

- the menu path in step 1, and which role the user needs. `itil` is the usual one for reading incidents;
- the table name `sys_user_group` in step 2. In the ServiceNow app, the same id is on the group's form: its menu has **Copy sys_id**.

### What the agent can read

The agent gets two read tools:

- `servicenow_search_incidents`: incidents whose short description or description has every word given (at most 5), newest first.
- `servicenow_get_incident`: one incident's description, its resolution code and its resolution notes.

Each incident comes with its number, state, when it was opened and resolved, and a link.

### How a read stays inside the group

- **The Hub writes every query.** The model passes only words. A `^`, a `=`, a comma or `javascript:` never reaches ServiceNow.
- **Every row is checked.** ServiceNow drops a part of a query it can't read and answers with the rest. So the Hub checks the group stored on each row that comes back, and drops any other.
- **A dropped row is logged** by its count only.
- `servicenow_get_incident` also checks the number that came back is the one asked for.

These are the client's own tickets. The Hub's own past Reports are [Similar incidents](#similar-incidents), with other tools.

### If something goes wrong

| What you see | What it means |
|---|---|
| `ServiceNow error 401: ...` | The user or the password is wrong. Or the user has multi-factor sign-in enforced: the API can't pass it, so use a user without it |
| `ServiceNow error 403: ...` | The user may not read incidents through the API |
| `ServiceNow error 302: Found` | The instance sent the Hub somewhere else, usually a login page. Check the URL is the instance's own |
| `ServiceNow error: its answer was not the Table API's` | The instance answered with a page, not data. A developer instance that is asleep may do this (not confirmed) |
| `ServiceNow unreachable: ...` | No connection, or no answer in 15 seconds |
| Searches find nothing | The `group` isn't the group's `sys_id`, or the incidents are older than `days` |

An error shows ServiceNow's status and its message on one line, cut at 200 characters. It never shows the password or the reply's page.

### Known limits

- Comments and work notes are not read. The resolution notes carry how it was fixed.
- A search needs every word in the short description or the description. Fewer words find more.
- Two Contracts may name the same instance and group. Nothing refuses it, as with Jira.
- Built on ServiceNow's documented answers. A real instance has not been read yet.

## PagerDuty

A Contract's PagerDuty is an alert source, and a place the Client report can go. This is slice 19.2. It gives the agent no tools.

```yaml
pagerduty:
  webhook_secret: pagerduty-signing     # required: the webhook subscription's signing secret
  api_key_secret: pagerduty-api-key     # optional: a REST API key
  from_secret: pagerduty-from           # optional, needs the key: a user's email
```

| Field | Needed | What it adds |
|---|---|---|
| `webhook_secret` | Always | Incidents arrive as Alert groups. Every delivery is checked by its signature. |
| `api_key_secret` | Optional | Each incident's alerts: their summary and details for the agent, and a second way to join. A read-only key is enough. A row on the Health page. |
| `from_secret` | Optional, with the key | The Client report as a note on the incident. The key must then be a full-access one. |

Each value is a name in `secrets.sops.yaml`. A `from_secret` without an `api_key_secret` is refused. Two Contracts with the same signing secret are refused.

### Setting it up in PagerDuty

The webhook, as PagerDuty's [Webhooks](https://docs.pagerduty.com/integrations/webhooks) page describes it:

1. Open **Integrations**, then **Generic Webhooks (v3)**. On newer accounts the menu is **Configuration**, then **Integrations**, then **Generic Webhooks (v3)**.
   - For one service only, start from the service instead: **Services**, **Service Directory**, the service, its **Integrations** tab, the **Webhooks** section, **Add or manage webhooks for this service**.
2. Click **New Webhook**.
3. **Webhook URL**: `https://<domain>/contracts/<id>/pagerduty`.
4. **Event Subscription**: `incident.triggered`, `incident.reopened` and `incident.resolved`. The Hub ignores any other.
5. Click **Add Webhook**. A confirmation shows the signing secret. Copy it then: PagerDuty's page doesn't say it can be read again.
6. Put the secret in `secrets.sops.yaml`, under the name `webhook_secret` gives.
7. To check it: open the webhook, and under **Test** click **Send Test Event**. The Hub answers 202 and ignores it. A 401 means the secret is wrong.

Who can do this: a Manager, an Admin or the Account Owner.

The key, as PagerDuty's [API Access Keys](https://docs.pagerduty.com/account-admin/api-access-keys) page describes it:

1. Open **Integrations**, then **Developer Tools**, then **API Access Keys**.
2. Click **Create New API Key** and type a **Description**.
3. Tick **Read-only API Key**, unless you want notes: a note needs a key that can write.
4. Click **Create Key**. The key is shown in full only this once. Put it under the name `api_key_secret` gives.

Who can do this: an Admin or the Account Owner.

For notes, put the email of a PagerDuty user of that account under the name `from_secret` gives. A note is written as that user.

The Hub must be reachable from the internet for the webhook: see [What is public](running-the-hub.md#what-is-public).

### What an incident becomes

- An incident that starts (`incident.triggered`, `incident.reopened`) is a firing Alert group. One that ends (`incident.resolved`) resolves it, and makes its Proposed actions Stale.
- From there it follows Alertmanager's path: the same dedup, budget and resolve rules, and the [Related alert group](#related-alert-groups) rules. Two incidents of one PagerDuty service are related by their `service` label.
- The agent sees an Alertmanager-shaped group:

  | Where | What |
  |---|---|
  | `alertname` | The incident's title |
  | Labels | `source: pagerduty`, and its `service`, `urgency` and `priority` |
  | Each alert (10 at most, with a key) | Its `severity` as a label; its summary (500 characters) and details (2,000) as annotations |
  | `incident` annotation | The incident's link |

- With no key, or when PagerDuty doesn't answer within 3 seconds, the incident is triaged alone, without its alerts' text.

### An incident that is an Alert group you already send

Alertmanager and Grafana give PagerDuty the SHA-256 of the group key they send the Hub. So the Hub can tell that an incident is a group it already has.

- An incident whose key, or one of its alerts' keys, is the hash of an Alert group this Contract sent in the last 30 minutes joins that group's Triage. No second Triage starts.
- The Triage's Slack thread says so, with the incident's number and link.
- A joined incident resolving changes nothing. Only the Triage's own Alert group makes its Proposed actions Stale.
- If PagerDuty's delivery arrives before Alertmanager's own, there are two Triages. So are there when the incident is created more than 30 minutes after its Alert group.
- Without a Slack thread, a join shows only in the Hub's log.

### The Client report as a note

- **Send to client** also puts the Client report on the Triage's PagerDuty incidents as a note. It needs `from_secret`.
- The note has the same words as the client's Slack message, as plain text.
- Only the Operator's press sends it, once, with the sender recorded. Never on its own.
- With no Slack client channel, the button still shows for a Triage that has an incident.
- **PagerDuty refuses the note:**
  - If nothing was sent yet, the claim is freed and you can press again.
  - If the client's Slack channel already has the report, the thread names the refusal and nothing is sent twice.
- **PagerDuty doesn't answer:** it may have the note. The Hub keeps the claim and asks you to check the incident.
- A refusal shows PagerDuty's status and code only. Its message can quote the `From` email.

### What the Hub never does

- It never acknowledges or resolves an incident. That is a write to someone else's paging system.
- It calls two endpoints with the key: list an incident's alerts, and add a note. And one more for the Health page (`/abilities`).
- A signed delivery is never refused, whatever it holds. PagerDuty drops a refused delivery for good, and disables the webhook after three.

Known limits:

- Built on the shapes PagerDuty documents. Not checked against a real account yet.
- US accounts only. The EU service region has another host.
- One signing secret per Contract. While PagerDuty rotates a secret it sends two signatures, and the Hub accepts the one it knows.

## Outbound webhook

**In short:** a Contract can name one URL that gets what its Operator channel gets, as signed JSON events. It is for an Operator-side system (ticketing, a data store, automation). It sends on its own, with the full Report and its Evidence, so never point it at a client's system. This is slice 19.3.

```yaml
webhook:
  url_secret: webhook-url              # an https:// URL, in secrets.sops.yaml
  signing_secret: webhook-signing-key  # the key your receiver checks each event with
```

| Field | What it is |
|---|---|
| `url_secret` | A name in `secrets.sops.yaml` holding the URL. *Why a secret:* a receiver's URL often carries a key in its path. Only its host is ever logged. |
| `signing_secret` | A name in `secrets.sops.yaml` holding a long random string you choose. The Hub signs every event with it. |

The URL must be `https://`, with no user or password in it. A Contract whose URL isn't is refused at load, without quoting it.

### The events

Every event has the same envelope:

```json
{
  "version": 1,
  "id": "2f1c6e0a-…",
  "event": "report.finished",
  "contract": "acme",
  "at": "2026-11-23T10:15:00+00:00",
  "investigation": {"id": "…", "kind": "triage", "title": "KubePodCrashLooping shop", "status": "done", "started": 1795428900.0},
  "report": {"summary": "…", "category": "resource_limits", "probable_root_cause": "…", "confidence": "high", "severity": "high", "affected": ["shop/deployment/checkout"], "evidence": [{"source": "kubernetes_pod_logs", "finding": "…", "link": null}], "recommended_actions": ["…"], "runbook": null},
  "executed_actions": []
}
```

| `event` | When | What it adds to the envelope |
|---|---|---|
| `approval.needed` | A Triage or a Check waits for a decision | `investigation`, `proposed` (each tool and its arguments), `deadline` |
| `approval.decided` | The decision is made, by a person or by the Hub when it goes Stale | `investigation`, `approve`, `by` (`operator` for the API, `slack:<user id>`, `console:<login>`, `mcp:<login>`, or `stale`), `actions` (their names), `reason` |
| `action.executed` | A Run executed approved actions | `investigation`, `actions`: what that Run executed, from the Hub's own record |
| `report.finished` | A Triage has its Report; a Check failed, or recovered | `investigation`, `report`, `executed_actions`; for a Check also `verdict` (`passed`, `summary`, `evidence`) |
| `investigation.failed` | A Run failed, or was refused (over budget) | `investigation`, `error` |
| `rollback` | The Operator channel was told of a Rollback | `cluster`, `namespace`, `kind`, `name`, `container`, `outcome`, `reason`, `actuated_at`; `automation_ended: true` on the one that ends its Workload's Automation |
| `actuation` | The Operator channel was told of a Step-up, a Burst, or a container still at its cap | The same fields; a Step-up's also `memory` and a Burst's `cpu`, the limit Slack's line names |
| `stale_notice` | The Operator channel got a Stale notice | `cluster`, `kind` (`sizing`, `relay`, `bill`), `state` (`failing`, `still_failing`, `recovered`), `since`, `error` |

What an event never holds:

- The raw Alert group, the Slack thread, or who was sent the Client report. An event is built from a fixed list of fields.
- The Hub's instruction to the model. A Stale decision's `reason` is why it went Stale.
- A secret the Hub recognises: every string is redacted as Slack's is, and cut at 4,000 characters. A list is cut at 50 items.

What sends no event:

- A Question. It is the Operator's own conversation.
- A passing Check with nothing open. It is quiet in Slack too.
- The Client report.

### Checking an event in your receiver

Each request carries `Squero-Signature: t=<unix time>,v1=<hex>`. The `v1` value is the HMAC-SHA256 of the time, a dot and the raw body, with your signing secret.

```python
import hashlib, hmac, time


def verified(secret: str, header: str, raw_body: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    signed = parts["t"].encode() + b"." + raw_body
    want = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    fresh = abs(time.time() - int(parts["t"])) < 300  # refuse an old event
    return fresh and hmac.compare_digest(want, parts["v1"])
```

1. Check the signature over the raw bytes, before parsing the JSON.
2. Refuse a time more than a few minutes old.
3. Drop an `id` you have already seen. A retry keeps its `id` (also in the `Squero-Id` header), with a fresh time and signature.
4. Answer any 2xx within 10 seconds.
5. Treat every text field as text. A Report is the model's own prose, and Slack shows any URL in it as plain text, because a prompt injection could plant one. JSON can't do that for you: only `evidence[].link` came from a tool, so never turn any other URL into a link.

### How it is delivered

| Rule | Value |
|---|---|
| Order | One event at a time per Contract, in the order they happened |
| One attempt | 10 seconds in all |
| Retried | A connection error, a timeout, a 5xx, a 429: three more times, after 1, 5 and 25 seconds |
| Dropped at once | Any other answer, a redirect included. The Hub never follows one. |
| Given up | A log line naming the host and the status, and `squero_webhook_events_total{outcome="failed"}` |
| Waiting | 1,000 events per Contract at most; more are dropped and counted |

- A receiver that fails never slows or fails the Hub's own work.
- **It is a copy, not the record.** Events wait in memory: a Hub restart loses the ones not yet delivered, a Rollback's among them. Slack and the console keep everything.
- The Health page shows a `webhook` row with the last delivery's outcome. It makes no request to do so.

### What depends on Slack

- `rollback`, `actuation` and `stale_notice` fire only once Slack took the message. So they need the Contract's `slack:` block and the Hub's Slack token, and a Slack outage delays them.
- The hourly "N more Rollbacks" line in Slack has no event.
- A Check's recovery is tracked through its Slack thread, so its recovery event needs Slack too.
- The Investigation events (`approval.*`, `action.executed`, `report.finished`, `investigation.failed`) don't depend on Slack.

## Client channels: Teams and Google Chat

**In short:** beside Slack's client channel, a Contract can list Microsoft Teams channels and Google Chat spaces. Each gets the Client report when the Operator presses **Send to client**, and nothing else, ever. They have no buttons: approvals stay in Slack and the console. This is slice 19.4.

```yaml
client_channels:
  - kind: teams
    url_secret: teams-acme-oncall      # the workflow's webhook URL, in secrets.sops.yaml
  - kind: google_chat
    url_secret: chat-acme-oncall       # the space's webhook URL, in secrets.sops.yaml
```

| Field | What it is |
|---|---|
| `kind` | `teams` or `google_chat`. It decides the message's shape. |
| `url_secret` | A name in `secrets.sops.yaml` holding the incoming webhook's URL. *Why a secret:* the URL holds the key. The Operator sees the channel by this name, never by its URL. |

Refused at load, without quoting the URL:

| Refused | Why |
|---|---|
| A `google_chat` URL that isn't `https://chat.googleapis.com/v1/spaces/<space>/messages?key=...&token=...` | Google fixes the host and the path. A typo can't send a client's report elsewhere. |
| A `teams` URL that isn't `https://`, or that carries a user or a password | A workflow's host varies by tenant, so any https host is taken. |
| A URL with a space or a line break inside it | A long URL wrapped in the secrets file. It would fail at the first send otherwise. |
| One URL in two Contracts, or twice in one | A client would get another's report, or its own twice. |

### Setting up a Teams channel

As Microsoft's [incoming webhook](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook) page describes it:

1. In Teams, go to the team and channel that should get the report.
2. Select **More options (...)** next to the channel, then **Workflows**.
3. Search for and select the template **Send webhook alerts to a channel**.
   - Microsoft says each template has a different authentication type. The Hub sends no token, so the workflow must accept a request from anyone who has its URL. The templates named "from specific people" and "from people in an org" are likely not that; this is not confirmed.
4. Choose the **Team** and **Channel**, then **Save**.
5. Copy the webhook URL the workflow shows. Put it in `secrets.sops.yaml`, under the name `url_secret` gives. Microsoft's pages don't say it can be read again later.

Good to know:

- A workflow belongs to the person who made it, not to the channel. If that person leaves it can stop. An admin can add co-owners.
- The old Office 365 connector webhooks were retired in May 2026. Use a workflow.

### Setting up a Google Chat space

As Google's [webhooks quickstart](https://developers.google.com/workspace/chat/quickstart/webhooks) describes it. It needs a Business or Enterprise Google Workspace account that allows incoming webhooks.

1. In a browser (not the mobile app), open Chat and go to the space.
2. Click the arrow next to the space's title, then **Apps & integrations**, then **Add webhooks**.
3. Fill in the dialog and save it. Copy the webhook's URL. Put it in `secrets.sops.yaml`, under the name `url_secret` gives.

### What the client gets

- The Client report: the summary, root cause, severity, what is affected and the Recommendations. The same words as Slack's client channel, as plain text. Never Evidence, Executed actions or ids.
- **Teams:** an Adaptive Card with one text block per line, the first one bold.
- **Google Chat:** one text message.
- **No link and no mention can come from the Report.** It is the model's own prose, and neither product documents a way to switch its parsing off. So two things are rewritten:

  | In the Report | What the client sees | Why |
  |---|---|---|
  | `https://grafana.acme.example/d/1` | `https[:]//grafana[.]acme[.]example/d/1` | It can't become a clickable link. |
  | `<` | `＜` (a full-width look-alike) | Chat's link and mention syntax opens with `<`, as in `<users/all>`. |

- A long report is cut to what the product takes (Teams 28 KB, Chat 32,000 bytes), with a line saying so.

### How one press sends

- **Send to client** shows when the Contract has any place to send: Slack's client channel, a PagerDuty incident with a `From`, or a client channel here.
- Its confirm dialog names every destination, each client channel by its secret's name. The press is the consent. Slack allows that text 300 characters: with many long names it names those that fit, then "and N more".
- The order is Slack's client channel, then PagerDuty's note, then each client channel in the Contract's order. Each client channel gets 10 seconds.
- It is sent once. The Hub records one "sent" per Investigation.
- **A channel refuses** (a 4xx):
  - If nothing was sent yet, the claim is freed and you can press again.
  - If something was sent, the thread names the refusal with its status, and nothing is sent twice. The other channels still get theirs.
- **A channel doesn't answer** (a timeout, a 5xx): it may have the report. The Hub keeps the claim and asks you to check the channel.
- The thread says **Accepted by** each channel that took it. For Teams that means the workflow took the request: it posts a moment later, and the Hub can't see whether that worked.

Known limits:

- Built on the payloads the two products document. Not checked against a real Teams or Google Chat yet.
- No Health row: an incoming webhook can't be checked without posting to the client's channel.
- A bare domain with no path (`evil.example`) is not rewritten, as in Slack.
- In Teams each line is its own text block. Numbered Recommendations may each show as "1." if the card restarts its numbering; not seen yet.

## Scheduled Checks

A Check runs on a schedule, with no alert.

- Its prompt, which you write, says what healthy means.
- The agent returns a verdict: passed or failed, with Evidence.
- A failing Check also returns a full Report. That Report is investigated like an alert.
- A failing Check can propose actions for your approval.
- A Run that ends without a verdict counts as failing. This covers an error and the call limit.

### How a Check reports

**Passing is quiet.** A passing Run:

- posts nothing to Slack;
- is left out of the console's Investigations list and `GET /investigations`.

The console's Checks table still shows each Check's next time, last Run, Open failure and last skip.

**The first failure** posts a thread, like a Triage's. The thread has Report, Feedback, Jira and Send to client. That first failure becomes the Check's **Open failure**.

**Each later failing Run** replies *still failing* in that thread. The exception is a Run that proposes an action:

- it gets its own thread, with the buttons;
- the old thread says where it continued;
- the new thread is the Open failure from then on.

**The first passing Run** replies *recovered* in the thread, and ends it.

### Schedule and budget

- The schedule is a 5-field cron expression.
- It runs in the Check's `timezone`. The default is UTC. Daylight saving is included.
- It runs at most once every 15 minutes. `contracts validate` refuses a faster one.
- The schedule lives in Postgres.
  - A claim moves the next time and records the Run in one transaction.
  - So a restart never fires a Check twice.
  - Fire times missed while the Hub was down run once, at startup.
- A Check never starts while its previous Run is running or waiting for a decision.
- At 100% of the budget, a Check is skipped. It is recorded, not posted, because the budget notice already said so.
- Checks share `SQUERO_MAX_CONCURRENT_TRIAGES` with Triages. So many Checks on the same minute make alerts wait.

A changed `schedule` or `timezone` takes effect at the next start. A Check removed from `contract.yaml` stops.

A Check gets the general Teachings, not those for alert names. Its Report is not searched as a past incident. Only Triage Reports are.

### What a Check costs

- A Haiku Triage costs about $0.004.
- A Check every 15 minutes is 96 Runs a day.
- That is about $0.40 a day, or $12 a month, on Haiku.
- On Sonnet it is 20 times that.

Each Run also keeps:

- its checkpoints;
- its raw tool results;
- its usage rows.

Nothing prunes them yet. At that rate, one Check keeps 96 sets a day.

### Config

```yaml
checks:
  - name: shop-healthy
    schedule: "0 9 * * 1-5"        # weekdays at 9
    timezone: Europe/Rome
    prompt: Every deployment in namespace shop has all replicas ready, and no pod restarted in the last hour.
```

| Field | Example | What it does |
|---|---|---|
| `name` | `shop-healthy` | The Check's name |
| `schedule` | `"0 9 * * 1-5"` | A 5-field cron expression |
| `timezone` | `Europe/Rome` | Default UTC |
| `prompt` | (text) | What healthy means, in your words |

## Deploy verification

With `deploy_checks:` set, the Hub verifies each deploy it sees in the Contract's clusters. It does this with a Check, `delay_minutes` after the deploy.

### How a deploy is found

- Every 2 minutes, the Hub calls the Contract's own kubernetes `recent_changes` for the last hour.
- It reads rollouts, ArgoCD syncs and Flux releases.
- This is no model call. It is Kubernetes reads only.
- A deploy is known by its namespace, object and revision.

**The baseline.** The first successful poll of each cluster after the Hub starts is the baseline.

- Deploys from before the Hub started are never verified.
- Deploys from while the Hub was down are never verified.
- Each later deploy is verified once.

**Grouping.** Deploys in one namespace close together are verified in one Run.

- A new deploy joins the pending verification and pushes it back.
- It is never pushed back past twice the delay, counted from the first deploy.
- The pending verification is in Postgres. It fires after a restart.

### What the Check asks

The Check asks two things:

- whether what changed is healthy now;
- whether anything else in the namespace started failing after the change.

*Why the second question:* another workload's rollout can break its callers. That is a deploy failure too.

**Failures that were already there** are noted in the summary. They are never failed for. Otherwise one existing crash loop would fail every deploy next to it.

**Telling old from new.** The model can't tell this reliably from pod ages and events. So the Hub tells it:

- Each poll lists the pods of the namespaces it watches.
- Each pod's Workload comes from its owners (`list_pods`' `workload`).
- From an older Relay, the Workload is guessed from the pod's name.
- A deploy's prompt names the workloads that were not running ready at the poll just before it.
- It also names any workload where nothing was running ready, or where the Hub couldn't tell.
- Deploys that join a pending verification keep what failed before the first one.

**What the prompt holds.** Only each change's object and revision. It never holds a change cause or a sync message. Whoever deploys can write those. The agent reads them through `recent_changes`, as data.

### Results

- A passing verification is quiet.
- A failing verification gets its own thread, like a Check's, with Proposed actions.
- It has no Open failure.
- It never touches a scheduled Check's Open failure.

### Budget and overlap

- The budget and overlap rules are the Checks' rules.
- At 100% of the budget, a verification is skipped. It is shown on the console.
- While the namespace's last verification is still open, the next one waits.
- The console lists the latest verifications.

### Rollbacks

- A `kubectl rollout undo` is a deploy too.
- `recent_changes` lists it as a `rollout undo` under its new revision.
- It is timed by the Deployment's `Progressing` condition.
- The ReplicaSet it reuses keeps its first creation time.
- So it is verified like any rollout.
- An older undo, superseded since, keeps that creation time.

### Known limits

- A GitOps object that deploys into several namespaces is grouped under all of them.
- A burst of more changes than `recent_changes` returns is cut.
- Every deploy costs a Run.

### Config

```yaml
deploy_checks:
  delay_minutes: 10
  namespaces: [shop]                  # optional: only these
  prompt: Also check that checkout's /health answers.   # optional, added to the Hub's
```

| Field | Default | What it does |
|---|---|---|
| `delay_minutes` | `10` | Minutes after a deploy before its verification runs |
| `namespaces` | not set | Optional. Only these namespaces are verified |
| `prompt` | not set | Optional. Added to the Hub's own prompt |

## Similar incidents

`- name: incidents` in `integrations:` gives the agent the Contract's own past Triage Reports.

```yaml
integrations:
  - name: incidents
    embeddings: false    # keep Report text away from the embedding model
```

### The two tools

| Tool | What it does |
|---|---|
| `incidents_search` | Postgres full-text search over the title and every string in the Report. Best match first. The last 90 days by default, at most 365 |
| `incidents_read` | One Report in full, with the actions the Operator approved or rejected and why, what ran, and its Jira issue |

### What can be found

- Only finished Triages of the same Contract are found or read.
- The Hub builds the server for one Contract and puts its id in every query.
- The model passes only words and ids.

### How the agent uses it

- A Triage searches once for the alerting workload before its Report.
- It does this even when the cause looks clear. A past Report matters exactly when current evidence looks complete but isn't.
- A match is a lead. The agent confirms it with current tools. A match is never proof on its own.
- A search that found nothing is dropped from the Evidence.
- The CLI's `squero triage` has no database. It leaves these tools, and their guide, out.

### Search by meaning

With `SQUERO_EMBEDDING_MODEL` set, the search also finds Reports by meaning.

- Example: a Report that says *certificate expired* matches an alert about an *x509 handshake failure*. The two share no word.

How it works:

1. When a Triage Report finishes, the Hub embeds its title, summary, category, root cause, affected workloads and Evidence findings.
   - The store is pgvector, with 512 dimensions.
   - The model gateway does the embedding.
2. A search embeds its keywords.
3. The two rankings are merged by reciprocal rank fusion:
   - a ranking by words;
   - a ranking by meaning, using cosine distance under `SQUERO_EMBEDDING_MAX_DISTANCE`.
4. So a Report that both find comes first.

Keeping it current:

- Only vectors made by the current model are compared.
- After changing the model, or to embed Reports from before it was set, run `squero incidents embed` once.
- That command embeds what is missing. It exits non-zero if any failed.

Failures never block the work:

- A failed embedding never fails a Triage or a search.
- That Report or that search then uses words only.

Cost:

- Embedding tokens are recorded in the Contract's usage.
- They count toward its budget.
- A search's tokens count under the Contract alone, not an Investigation.

A Contract whose Report text must not reach the embedding model sets `embeddings: false`, as shown above.

## Teachings

Teachings are lessons for one Contract's future Investigations. You approve each one before it is ever used.

### How a Teaching is made

After a Triage Report, Slack asks *Was this Report right?*. The console page asks it too.

- **Right** only records it.
- **Wrong…** asks for your correction. The agent then drafts a Teaching from it, in one model call.
  - The call runs on the Contract's model.
  - It counts against the Contract's budget.
  - It is shown as the Investigation's cost.

A reason you type when rejecting a Proposed action also drafts one.

- It is drafted after the resumed Run, with its Report as context.
- A Stale rejection drafts nothing.
- A rejection without a typed reason drafts nothing.

What the drafter sees:

- the context;
- your words;
- the Contract's approved Teachings only.

It never sees a proposed, rejected or retired Teaching. It never sees another Contract's Teachings.

Duplicates and conflicts:

- A draft that says what an approved or waiting Teaching already says isn't queued. It is marked *Already taught*.
- A draft that contradicts an approved Teaching is flagged.
- Approving a flagged draft retires the old one.

### Review

Each proposal is posted in the Investigation's thread, with three buttons:

- **Approve**
- **Edit…**: approves exactly the text you submit.
- **Reject**

The console's Teachings page lists them too. There you can:

- approve an edited text;
- reject a proposal;
- retire an approved one;
- write one by hand, which is approved at once.

A Teaching is at most 500 characters. It can be limited to some alert names.

A draft that fails, or is over budget, is said in the thread. Your feedback is kept either way.

### Where approved Teachings go

Approved Teachings go into every Run's prompt. That covers Triage and Question. They belong to their own Contract only.

They appear under the heading *Teachings from the Operator*. They override the agent's defaults. They never override its Rules.

| Run | Which Teachings it gets |
|---|---|
| Triage | Those for one of its alert names first, then the general ones. None for other alerts |
| Question | Every one |

- Newest first.
- At most 30 Teachings and 6,000 characters.
- The Teachings page warns when a Contract's Teachings don't all fit.
- Each Run reads them as they are when it starts. A resume after an approval sees one retired or approved meanwhile.

### Scenarios

- A Scenario can list `teachings:`, as `{text, alerts}`. It is replayed the same way.
- Save as Scenario writes the Contract's approved Teachings as of saving.
- That is not necessarily the ones the Run saw.
- `expect.recommendations` checks keywords in the Recommendations.

## Vendor integrations

Besides the builtins, a Contract can turn on a vendor MCP server that the Hub knows.

### Config

```yaml
integrations:
  - name: grafana             # its tools appear as grafana_<tool>
    vendor: grafana           # an entry in the Hub's registry (src/squero/servers/vendors.py)
    env: {GRAFANA_URL: https://grafana.acme.example}
    env_secrets: {GRAFANA_SERVICE_ACCOUNT_TOKEN: grafana-token}   # names in secrets.sops.yaml
    tools: {search_dashboards: read, query_prometheus: read}       # only these are visible
    probe: {tool: search_dashboards, args: {query: ""}}            # optional health check
```

| Field | Example | What it does |
|---|---|---|
| `name` | `grafana` | Its tools appear as `grafana_<tool>` |
| `vendor` | `grafana` | An entry in the Hub's registry (`src/squero/servers/vendors.py`) |
| `env` | `{GRAFANA_URL: ...}` | Plain variables. Only the ones the registry entry lists are allowed |
| `env_secrets` | `{GRAFANA_SERVICE_ACCOUNT_TOKEN: grafana-token}` | Variables whose values are names in `secrets.sops.yaml` |
| `tools` | `{search_dashboards: read}` | The Hub's own list. Only these tools are visible. Each is `read` or `write` |
| `probe` | `{tool: search_dashboards, args: {query: ""}}` | Optional health check |

### Rules for every vendor

- The command comes from the Hub's registry. It is an absolute path.
- It never comes from the contracts repo.
- The binary ships in the image.
- `env` and `env_secrets` may set only the variables the registry entry lists.
  - Anything else is refused when the Contract loads.
  - Examples of refused names: `LD_PRELOAD`, `PATH`, `NODE_OPTIONS`. These could run code.
- The process gets only those variables, plus MCP's safe defaults.
- It never gets the Hub's environment.
- A tool not listed in `tools` is invisible.
- A tool needs approval if its list entry says `write`, or if the server doesn't mark it read-only.

Give each Contract its own vendor credentials, scoped to that client.

*Why:* the Hub keeps Contracts apart. But it can't see inside a vendor account that several clients share.

### Schema snapshots

Each registry vendor has a schema snapshot in `src/squero/servers/snapshots/`.

- It holds the tools' names, descriptions, schemas and annotations.
- So Scenarios with a vendor replay without its binary.

After bumping a vendor's pinned version:

1. Run `make snapshots`. It runs `squero integrations snapshot <vendor>` in the image, where the pinned binary is.
2. Commit the diff.

Drift checks:

- `--check` fails when the running vendor no longer matches its snapshot.
- hub-smoke runs it in CI.
- `gcp` isn't pinned. Google deploys its servers. So in hub-smoke its drift is a warning, not a failure.
- Drift can't make the Hub less safe there. A new tool stays hidden until a Contract lists it. A tool that stops claiming read-only needs approval. Production always uses the live schemas.
- Only a replay sees an old snapshot, until `make snapshots` runs.
- The vendor runs with no Contract and no variables for this. So no credential can end up in the file.

### The grafana vendor

The registry's `grafana` vendor is [mcp-grafana](https://github.com/grafana/mcp-grafana), pinned in the Dockerfile.

- It runs read-only: `--disable-write`, so every tool claims read-only.
- It sends no usage statistics to Grafana Labs.
- It offers only these tools: search, datasource, Prometheus, Loki, Tempo, Pyroscope, alerting, dashboard and navigation.
- It does not offer the generic API, rendering or docs tools.

It takes these variables:

- `GRAFANA_URL`
- `GRAFANA_SERVICE_ACCOUNT_TOKEN`
- `GRAFANA_ORG_ID`

Give it a service account with the **Viewer** role in that client's Grafana.

List in `tools` what the agent should use. Examples:

- `query_loki_stats`
- `query_loki_patterns`, and `query_loki_logs`. Patterns come before raw logs.
- `search_tempo_traces` and `get_tempo_trace`
- `query_prometheus`
- `search_dashboards`
- `generate_deeplink`

With `generate_deeplink` listed, Reports link their key evidence to Grafana.

`src/squero/servers/snapshots/grafana.json` has every tool's name and arguments.

## Azure

**Azure** is the second registry vendor, `azure`.

- It is the official [Azure MCP server](https://github.com/microsoft/mcp), `azmcp`.
- It is pinned in the Dockerfile, and verified against npm's integrity hash.
- It runs read-only: `--read-only`, so every tool claims read-only.
- It offers an explicit list of 62 tools.

### What it offers, and what it leaves out

Offered, in these areas:

- subscriptions and resource groups;
- resource health;
- Monitor metrics, activity and log queries;
- Advisor and quotas;
- AKS clusters and node pools;
- VMs and scale sets;
- App Service, Functions and Container Apps;
- SQL, PostgreSQL, MySQL and Cosmos DB accounts;
- Redis, Storage, Service Bus and Event Hubs.

Not offered:

- Key Vault (secrets);
- app settings, which are often connection strings;
- Cosmos DB documents, which are client data;
- the tools that deploy, or that build az CLI commands.

The registry also does this:

- It sets the telemetry off.
- It makes the server use only the Contract's service principal.
- It never uses the host's `az login`, the VS Code session, or a managed identity.

### Config

```yaml
integrations:
  - name: azure
    vendor: azure
    env: {AZURE_TENANT_ID: <tenant id>, AZURE_CLIENT_ID: <app id>, AZURE_SUBSCRIPTION_ID: <subscription id>}
    env_secrets: {AZURE_CLIENT_SECRET: azure-client-secret}    # only from secrets.sops.yaml
    tools: {resourcehealth_availability-status_get: read, monitor_metrics_query: read, quota_usage_check: read}
```

### Service principal

Make a service principal with the **Reader** role on the client's subscription, and nothing more:

```bash
az ad sp create-for-rbac --name squero-<client> --role Reader --scopes /subscriptions/<id>
```

What Reader does and doesn't cover:

- Reader reads resources and metrics.
- Its `*/read` usually covers Log Analytics queries too.
- A workspace that uses table-level (granular) access also needs **Log Analytics Data Reader** on it. See [Manage access to Log Analytics workspaces](https://learn.microsoft.com/azure/azure-monitor/logs/manage-access#azure-rbac).
- Reader can't list keys or read app settings. Those are actions, not reads.
- Reader doesn't query an Azure Monitor workspace's Prometheus metrics: that is a data action. Where a Prometheus uses `auth: azure`, also give the app **Monitoring Data Reader** on that workspace: this app takes the token for it (slice 19.1c). See [Azure Monitor and Google Managed Prometheus](optimizer.md#azure-monitor-and-google-managed-prometheus-azure-google).

Known gaps under Reader:

- **Resource health** answers 409 until the subscription has `Microsoft.ResourceHealth` registered. Reader can't do that. Ask the client to register it.
- `appservice_webapp_deployment_get` is Unauthorized under Reader.
- The App Service diagnostics detectors return empty tables under Reader. So don't list them.

`src/squero/servers/snapshots/azure.json` lists every tool with its arguments.

## Google Cloud

**Google Cloud** is the third registry vendor, `gcp`.

It uses Google's own remote MCP servers:

- Cloud Logging;
- Cloud Monitoring;
- Cloud Trace.

Their URLs are `https://{logging,monitoring,cloudtrace}.googleapis.com/mcp`. They are merged into one integration of 17 tools. Every one is annotated read-only.

### How it connects

- Nothing runs on the Hub.
- The Hub calls those URLs with a token it mints from the Contract's service account key.
- The token request goes to Google's token endpoint only. It never goes to the key's own `token_uri`.
- The scopes are `logging.read`, `monitoring.read` and `trace.readonly`. So a token can't write, whatever the roles allow.
- The key never leaves the Hub.
- Error Reporting has no remote server, so it isn't offered.

### Config

```yaml
integrations:
  - name: gcp
    vendor: gcp
    env: {GOOGLE_CLOUD_PROJECT: <project id>}
    env_secrets: {GOOGLE_SERVICE_ACCOUNT_KEY: gcp-key}    # the JSON key, only from secrets.sops.yaml
    tools: {list_log_names: read, list_log_entries: read, query_range: read, list_traces: read, get_trace: read}
```

- The prompt names the project. So the model passes `projects/<id>` where a tool wants it.
- `list_log_names` must be listed. The health page calls it on the project.
- Listing these servers' tools needs no credential. Without that call, a revoked key would show as healthy.

### Service account

Make a service account in the client's project. Give it these four roles:

| Role | Identifier |
|---|---|
| Logs Viewer | `roles/logging.viewer` |
| Monitoring Viewer | `roles/monitoring.viewer` |
| Cloud Trace User | `roles/cloudtrace.user` |
| MCP Tool User (allows calling MCP tools) | `roles/mcp.toolUser` |

Monitoring Viewer also covers a Prometheus with `auth: google` (slice 19.1c). It must then be granted on the project in that Prometheus's URL, which with a metrics scope is the scoping project. See [Azure Monitor and Google Managed Prometheus](optimizer.md#azure-monitor-and-google-managed-prometheus-azure-google).

Then create a JSON key:

```bash
gcloud iam service-accounts keys create key.json --iam-account=squero@<project>.iam.gserviceaccount.com
```

Notes:

- Cloud Trace User can also manage trace scopes. The read-only token scopes keep it from doing so.
- The Logging, Monitoring and Trace APIs must be enabled in the project.
- An organization that blocks key creation (`iam.disableServiceAccountKeyCreation`) needs an exception for this account.

## AWS

**AWS** is a builtin, `aws`. It is not a vendor.

### Why it is a builtin

AWS's official servers don't fit:

- The local CloudWatch server claims none of its tools read-only. So every call would need approval. It also adds 249 MB to the image.
- The remote AWS MCP Server reads data only through `run_script`. That tool claims to be destructive.

### The six read-only tools

| Area | Tools |
|---|---|
| Alarms | `describe_alarms` |
| Metrics | `list_metrics`, `get_metric_data` |
| Logs | `describe_log_groups`, `query_logs` (Logs Insights, stopped after 20 s) |
| What changed | `lookup_events` (CloudTrail management events) |

### How requests are signed

- The Hub signs each request itself, with SigV4.
- So the key never leaves the Hub.
- It sends the request only to one of the Contract's regions.

### Config

```yaml
integrations:
  - name: aws
    regions: [us-east-1]                        # the first is the default
    access_key_id: AKIA...                       # an IAM user's key
    secret_access_key_secret: aws-secret-key     # its secret, only from secrets.sops.yaml
```

### IAM user permissions

Make an IAM user with exactly these policies, and an access key for it:

- **CloudWatchReadOnlyAccess**
- **AWSCloudTrail_ReadOnlyAccess**

For the optimizer, add these permissions:

- `ce:GetCostAndUsage`, for its [Bill check](optimizer.md#bill-check).
- `ec2:DescribeSpotPriceHistory`, for its [spot list prices](optimizer.md#spot-nodes). This is from 0.59.0. Without it, the read is refused. A spot node is then priced at its discount, and the read waits a day.
- `aps:QueryMetrics` and `aps:ListAlerts`, only where a Prometheus uses `sigv4`: an [Amazon Managed Prometheus](optimizer.md#amazon-managed-prometheus-sigv4) workspace, whose requests this key signs (slice 19.1b).

Do **not** use ReadOnlyAccess. It also reads S3 objects, DynamoDB items and Lambda environment variables. Those are client data, and often secrets.

The health page reads the alarms in the first region.
