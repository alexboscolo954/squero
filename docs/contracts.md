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
