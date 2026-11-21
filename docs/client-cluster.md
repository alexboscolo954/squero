# The client's cluster: Relay and Actuator

**In short:** two optional parts run inside a client's cluster, from the Hub's own image. The **Relay** lets the Hub reach a cluster it can't otherwise reach. It carries the Hub's reads (and, only if the client allows it, the two gated actions) and the cluster's alerts. The **Actuator** applies approved Sizings in place, and rolls one back if it hurt the pods. Both dial out to the Hub over HTTPS, so the client opens no inbound port.

## What each one may do

| | Relay | Actuator |
|---|---|---|
| Namespace | `squero-relay` | `squero-actuator` |
| Token | `relay_token_secret` | `actuator_token_secret`, never a Relay's |
| Talks to | `wss://<the Hub's domain>/relay` | `GET /actuator/desired`, `POST /actuator/reports`, `POST /actuator/blockers` |

### Reads

- **Relay:** a read-only ServiceAccount, bound per namespace, like the Hub's own.
- **Actuator:**
  - pods, Deployments, ReplicaSets, StatefulSets, DaemonSets, Argo Rollouts, CronJobs, Jobs, HPAs, VPAs, PodDisruptionBudgets, LimitRanges and ResourceQuotas in the Contract's namespaces;
  - Events in `default`, where a Node's are (for `reflexes.oom`; its own Role, which a client may remove);
  - nodes, listed for the values of the labels its pods' nodeSelector and node affinity name (its own ClusterRole, which a client may remove).

### Writes

- **Relay:** none, unless the client sets the chart's `relay.writes: true`, which allows evicting pods and scaling deployments only.
- **Actuator:**
  - resize pods (`pods/resize`, a RoleBinding per namespace);
  - evict pods (`pods/eviction`, only with the Contract's eviction on);
  - create Events;
  - read and replace its own ConfigMap;
  - patch its own webhook's `caBundle`.

Neither can change an image or a command. The full table is in [safety.md](safety.md).

## A cluster behind a Relay

A cluster in a client network the Hub can't reach names its Relay in the Contract:

```yaml
clusters:
  - name: edge
    namespaces: [shop]
    via: relay
    relay_token_secret: edge-relay-token
```

### Setting it up in the Contract

- Set `via: relay` and `relay_token_secret`.
- The secret lives in the Contract's SOPS file and is 32 characters or more.
- The client puts the same value in its Relay's Secret.

### What the Relay does

- The Relay sits inside that network and holds the cluster's credentials.
- It dials the Hub's `/relay` websocket with the token.
- The token binds it to this Contract's cluster and nothing else.

### What such a cluster does not name

- It names no `context`, kubeconfig, `in_cluster`, `allow_exec` or Prometheus URL or token.
- The Hub never reaches it Direct.
- The Hub refuses to start if a Relay token is another Relay's, or equals the approver, MCP or a webhook token.

### How the agent reaches it

- The agent names it like any other cluster.
- Its Kubernetes and Prometheus calls go to the Relay after the Hub's own cluster and namespace check.
- The calls go through the Hub's own tools (list, schemas, approval gate), so nothing else a Relay offers reaches the agent.
- A Run that calls it ends Failed ("Relay offline") when:
  - its Relay is offline, or
  - it gets no answer within `SQUERO_RELAY_CALL_SECONDS`.

### Namespaces and the Hub's own reads

- `recent_changes` takes no namespace. The Hub keeps only the changes in the Contract's namespaces (a GitOps notice: its GitOps namespaces), however many the Relay allows.
- The Hub's own reads go through the Relay too, when the Contract has a `prometheus` integration:
  - its hourly Sizings;
  - node facts;
  - nodes' Room (the one read that sums pods outside the Contract's namespaces, per node: [Nodes](optimizer.md#nodes));
  - the Bill check's node-hours.
- These arrive as the Relay's `hub_promql_*` calls.

### Health and alerts

- Health shows each Relay as connected (since when, and the version it reports) or offline (since when).
- Health also shows the Kubernetes and Prometheus checks made through the Relay.
- The Agenda lists an offline Relay as data the Hub can't read.
- Its cluster's alerts come through the Relay as well. The Hub takes them as from the Contract's own alert route, with the same webhook token. Nothing about them differs from there on.

## Installing the chart

The Relay and the Actuator install from one Helm chart, `squero` (Phase 18.1).

- It is published with each release after 0.61.0 at `oci://ghcr.io/alexboscolo954/charts/squero`.
- Its version is the release's.
- Until that first release, use `charts/squero` from a checkout of `main`.

### Create the namespaces and Secrets first

The chart creates neither the namespace nor the token Secret. So `helm uninstall` never deletes a token or the Actuator's ConfigMap (what it holds).

```bash
kubectl create namespace squero-relay
kubectl -n squero-relay create secret generic squero-relay \
  --from-literal=SQUERO_RELAY_TOKEN=<the Contract's relay_token_secret value>
helm install squero oci://ghcr.io/alexboscolo954/charts/squero --version <X.Y.Z> -n squero-relay \
  --set relay.hub=wss://hub.example.com/relay --set relay.cluster=eu \
  --set 'namespaces={shop,orders}'
```

### Verify the signatures before installing

The image and the chart are signed with cosign keyless by the release's own GitHub Actions run. The signature carries that run's OIDC identity, recorded in Sigstore's public log. The image also carries its SBOM and build provenance.

Check both with cosign 3 or later:

```bash
for ref in ghcr.io/alexboscolo954/squero:<X.Y.Z> ghcr.io/alexboscolo954/charts/squero:<X.Y.Z>; do
  cosign verify "$ref" \
    --certificate-identity-regexp '^https://github.com/alexboscolo954/squero/\.github/workflows/ci\.yaml@refs/tags/v' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
done
docker buildx imagetools inspect ghcr.io/alexboscolo954/squero:<X.Y.Z> --format '{{json .SBOM}}'        # per platform
docker buildx imagetools inspect ghcr.io/alexboscolo954/squero:<X.Y.Z> --format '{{json .Provenance}}'
```

### Chart settings

`values.yaml` lists every setting. `values.schema.json` refuses a key it doesn't know, so a typo fails the install, as it does in a Contract.

One list feeds several places. `namespaces` (the Contract's namespaces on this cluster) feeds every RoleBinding, the Relay's `SQUERO_RELAY_NAMESPACES` and the Actuator's webhook.

| Setting | Part | What it does |
|---|---|---|
| `relay.hub` | Relay | the Hub's `wss://` address |
| `relay.cluster` | Relay | the cluster's name |
| `relay.prometheusUrl` | Relay | the cluster's Prometheus |
| `relay.writes` | Relay | evict pods and scale deployments; each still approved on the Hub |
| `relay.gitopsNamespaces` | Relay | read Argo CD and Flux objects there |
| `relay.alertsPort` | Relay | its Alertmanager receiver's port |
| `relay.callSeconds` | Relay | how long a call waits for an answer |
| `relay.existingSecret` | Relay | its token and, if it has one, `SQUERO_RELAY_PROMETHEUS_TOKEN` |
| `actuator.enabled` | Actuator | turns the Actuator on |
| `actuator.hub` | Actuator | the Hub's `https://<domain>` |
| `actuator.interval` | Actuator | seconds between pulls |
| `actuator.existingSecret` | Actuator | its own token, never the Relay's |
| `actuator.nodes` | Actuator | list nodes (audit A3); on by default |
| `actuator.nodeEvents` | Actuator | Events in `default` (13.1a); on by default |
| `actuator.self` | Actuator | its own pod (audit A4); on by default |

### Notes on the values

- **No token is ever a value.** Helm keeps values in the cluster, and they end up in shell history.
- **The image** is `ghcr.io/alexboscolo954/squero` at the chart's version, unless `image.tag` says otherwise.
- **Fixed names.** The objects have fixed names. The Actuator's webhook, Service and ConfigMap are all `squero-actuator`. So run one release per cluster.
- **The webhook's `caBundle`** is the Actuator's own, set at its start. The chart never sets one, so a `helm upgrade` keeps it.

## Protocol: Hub and cluster parts at different versions

The Relay and the Actuator each speak a **Protocol**. A Protocol is the numbered version of what they exchange with the Hub (Phase 18.3). Numbering starts at 1.

### Why it exists

- A Protocol number rises only for a change an older part would misread. It does not rise with each release.
- So the Hub and its cluster parts can be upgraded at different times.
- The Hub accepts a part's Protocol and the one before it.

### How a part says its Protocol

- A part sends its number in a `Squero-Protocol` header.
- The Relay sends it on its connection.
- The Actuator sends it on every call, with its release version in `Squero-Version`.
- A part that sends no header (0.62.0 and older) speaks Protocol 1.

### What happens on a mismatch

- A Protocol the Hub doesn't accept is answered 426, with the Hub's own Protocol.
- The token is checked first.
- The part logs which side to upgrade, for example "the Hub speaks Protocol 4 or 5, this Actuator 6: upgrade the Hub".
- The part then tries again.
- The Actuator keeps its reports and Blockers, and sends them once one side is upgraded.
- The Hub's Agenda says it too, until the part's next accepted connection or call. For example: "Actuator in eu speaks Protocol 3, the Hub needs 4 or 5: upgrade it".
- Health shows each Relay's and Actuator's version and Protocol.
- The Hub keeps this in memory. After a restart it is back at the part's next try.

## Running a Relay

The client installs the `squero` chart in its own cluster ([Installing the chart](#installing-the-chart)). The Relay lives in namespace `squero-relay`.

The Relay has:

- a read-only ServiceAccount, bound per namespace, like the Hub's own;
- one `squero relay` pod, from the same image;
- its token, from a Secret the client creates.

```bash
kubectl -n squero-relay create secret generic squero-relay --from-literal=SQUERO_RELAY_TOKEN=...
```

### What tools it serves

- It serves the Hub's own tool names for its one cluster. So the Hub's allowlist and approval gate apply unchanged.
- It also serves `hub_promql_instant` and `hub_promql_range`. The Hub uses these for its own Sizing and Health queries. The agent never gets them.

### Write tools

- No write tools exist on the Relay unless the client sets the chart's `relay.writes: true`.
- That turns `SQUERO_RELAY_WRITES` on, and adds RBAC for evicting pods and scaling deployments only.
- The Contract's `enable_actions` and your approval still apply on top.

### Connection

- It dials `wss://<the Hub's domain>/relay`.
- That route is public, like the alert routes and the Actuator's two. Caddy passes only a websocket upgrade there.
- After any drop it dials again. The wait starts at 1 s and doubles up to a minute. Once a connection has held, the wait goes back to 1 s.
- A refused token logs "check the token" and the Relay keeps trying.

### Relay settings

| Relay setting | Default | What it is |
|---|---|---|
| `SQUERO_RELAY_HUB` | (required) | the Hub's `wss://<domain>/relay` |
| `SQUERO_RELAY_TOKEN` | (required) | the value of the Contract's `relay_token_secret` |
| `SQUERO_RELAY_CLUSTER` | (required) | the cluster's `name` in the Contract |
| `SQUERO_RELAY_NAMESPACES` | (required) | JSON list, as the RoleBindings grant |
| `SQUERO_RELAY_GITOPS_NAMESPACES` | `[]` | where Argo CD or Flux objects live (the chart's `relay.gitopsNamespaces` sets it and their read RBAC together) |
| `SQUERO_RELAY_PROMETHEUS_URL` | none | the cluster's Prometheus inside the client's network |
| `SQUERO_RELAY_PROMETHEUS_TOKEN` | none | its bearer token |
| `SQUERO_RELAY_WRITES` | `false` | the client's switch for the two write tools |
| `SQUERO_RELAY_ALERTS_PORT` | `8080` | its Alertmanager receiver |

## Alerts through a Relay

The Relay's Service (`squero-relay.squero-relay:8080`) takes the cluster's own Alertmanager webhook at `POST /alertmanager`. A receiver for it looks like this:

```yaml
receivers:
  - name: squero
    webhook_configs:
      - url: http://squero-relay.squero-relay:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            credentials_file: /etc/alertmanager/secrets/squero-webhook/token   # the Contract's webhook token
```

### How an alert moves

- The client uses the Contract's webhook token as the bearer credentials. This is exactly how a Direct cluster's Alertmanager posts to the Hub.
- The Relay keeps no queue and no secret of its own for alerts.
- It sends each alert group over its connection to the Hub.
- The Hub checks the token against the Relay's own Contract, never another's.
- The Hub answers once the alert is stored.

### What Alertmanager gets back

| Answer | Meaning |
|---|---|
| 202 | stored |
| 401 | wrong token |
| 422 | not an alert group |
| 503 | the Relay has no connection to the Hub, or no answer within 10 s |
| 502 | the Hub failed to take it |

Alertmanager sends the alert again on 503 and 502.

## Running an Actuator

A cluster whose Approved Sizings should be applied sets `actuator_token_secret`. The secret lives in the Contract's SOPS file and is 32 characters or more. It must never be a Relay's token. Direct and Relay clusters alike can set it.

```yaml
clusters:
  - name: prod
    context: prod
    namespaces: [shop]
    actuator_token_secret: prod-actuator-token
```

### What the client installs

The client installs the `squero` chart with `actuator.enabled: true` in that cluster ([Installing the chart](#installing-the-chart)). The Actuator lives in namespace `squero-actuator`.

- One `squero actuator` pod, from the same image.
- A ServiceAccount that can:
  - read the kinds listed in [Which kinds it resizes](#which-kinds-it-resizes);
  - resize pods in the Contract's namespaces (a RoleBinding per namespace);
  - create Events;
  - read and replace its own ConfigMap;
  - patch its own webhook's `caBundle`.
- A MutatingWebhookConfiguration whose `namespaceSelector` lists the same namespaces.
- Its token, from a Secret the client creates.

```bash
kubectl -n squero-actuator create secret generic squero-actuator --from-literal=SQUERO_ACTUATOR_TOKEN=...
```

It refuses to start below Kubernetes 1.33.

### What it does every minute

- It pulls `GET https://<the Hub's domain>/actuator/desired` every minute.
- That gives the cluster's current Approved Sizings of Deployments in the cluster's namespaces.
- While the Kill switch is on, the list is empty.
- Each Sizing newer than what the Actuator applied to that container is applied once.

### How a Sizing is applied

- Every running pod of the Deployment gets the approved requests through the `resize` subresource, in place.
- The requests never go above the container's limit.
- A container whose `resizePolicy` would restart it for the change is skipped.
- The Actuator writes an Event on the Deployment: `SizingApplied`, `SizingSkipped` or `SizingFailed`.
- It reports each outcome to `POST /actuator/reports`. The Hub keeps reports 90 days, as the Contract's `actuations`.

### What it keeps across restarts

The Actuator keeps its state in its ConfigMap `squero-actuator`. That covers:

- what it applied, so a restart applies nothing twice;
- the Kill switch as the Hub last said it;
- the reports the Hub hasn't taken yet.

So a restart while the Hub is unreachable neither forgets the Kill switch nor loses a report.

### Retrying reports

- A report the Hub didn't take is sent again.
- Unless the Hub refused it. A 4xx other than 401, 403, 426 or 429 is refused: the Actuator logs it and drops it.

### How the Hub takes its reports

- The Hub takes a report only for the namespaces of the token's own cluster.
- It takes only the kinds an Actuator resizes.
- It refuses a number that isn't one (NaN, infinity) or is negative. The answer is 422, naming the field.
- It stores at most 1,000 reports from a cluster an hour, by when it received them.
- Past that limit, it stores the first ones of a POST up to the 1,000. It answers 429 and logs a warning.
- The Actuator then sends them all again. The reports already stored are kept once, and a POST of only those is taken.

### Report times

- A report's time is the Actuator's own clock.
- The Hub never reads it as later than the Hub's clock when the report arrived.
- Why: one dated ahead could stay a container's last Actuation, count as a Rollback, or outlive its 90 days for longer than its arrival allows.

### What Slack shows

- What an Actuator sends (names, its reason, which can carry an API server's message) reaches Slack as text.
- It can't mention a channel or make a link.

## Which kinds it resizes

- The Actuator resizes the pods of:
  - Deployments;
  - StatefulSets and DaemonSets (from 0.44.0);
  - Argo Rollouts (from 0.45.0).
- It holds CronJobs' requests (from 0.45.0, through its webhook only).
- The Actuator names the kinds it resizes on each pull. The Hub sends it only those kinds.
- An Actuator of 0.43.0 or older names none, and gets Deployments only.
- An Actuator of 0.44.0 names no Rollouts or CronJobs. So an approval for those waits for its upgrade.
- Upgrade the chart too. It reads StatefulSets, DaemonSets, CronJobs, Jobs and, read only, `rollouts.argoproj.io`.
- A Job of no CronJob is never sent: it runs once.

### Argo Rollouts

- A Rollout's pods are those of its own ReplicaSets, its stable and canary ones alike.
- They are resized in place, as a Deployment's are. They are never evicted.
- The Rollout's template is its own, or its `workloadRef` Deployment's.
- If Argo Rollouts isn't installed, there are none.

### CronJobs

A CronJob's pods are never resized in place. Its requests are held at once instead:

- The approved CronJob is held right away, with `SizingApplied` and "held for its next run's pods".
- The same pre-checks run on its `jobTemplate`. A ResourceQuota is counted for its `parallelism`.
- The webhook then gives each pod of its runs the held requests.
- The runs are Jobs named `<cronjob>-<minute>`, as the CronJob controller names them. The minute is the scheduled time in minutes.
- The webhook takes a Job only if it is at most 30 days old. So a Job of your own named `x-123` is never taken for CronJob `x`'s run.

### The first run under the held requests

- The first run under the held requests is watched until it ends, 7 days at most.
- The watched run is the first Job created since the hold whose pods got the requests.
- A run whose pods all started from the template (the webhook missed them) doesn't count.
- A pod of that run that `Failed` or was OOM-killed with the held requests is a Rollback.
- This applies with `restartPolicy: Never` or `OnFailure`.
- A Rollback stops the hold. The next run's pods start from the template, with the usual backoff.
- A Revert or a hand-back lets the hold go the same way. There is no catch-up and no eviction.

### Actuator settings

| Actuator setting | Default | What it is |
|---|---|---|
| `SQUERO_ACTUATOR_HUB` | (required) | the Hub's `https://<domain>` |
| `SQUERO_ACTUATOR_TOKEN` | (required) | the value of the Contract's `actuator_token_secret` |
| `SQUERO_ACTUATOR_NAMESPACE` | (required) | its own namespace, for its ConfigMap (the manifest sets it) |
| `SQUERO_ACTUATOR_POD` | (none) | its own pod's name, so it reports that pod with its Blockers' facts (audit A4; the manifest sets it) |
| `SQUERO_ACTUATOR_INTERVAL` | `60` | seconds between pulls |

## Holding requests: the webhook

The Actuator's webhook changes the requests of new pods, so a pod starts with the approved requests. The sections below cover each case.

### A DaemonSet per instance type

This is from 0.54.0. Where the Hub sized a DaemonSet's container per instance type ([the optimizer's page](optimizer.md#a-daemonsets-sizing-per-instance-type)):

- the Actuator gets each type's requests with the Sizing;
- it gets each node's type with the desired state;
- it gives each pod its node's type's requests.

The Actuator has no access to Nodes. The types come from the Hub's hourly read of `kube_node_labels`, the same read the Sizings were made of.

- A pod on a type with no Sizing, or on a node the Hub didn't name, gets its template's requests. A request the template doesn't set is left as it is.
- Until the Actuator has been told the nodes' types, it resizes no pod of such a DaemonSet. A rollout of one waits.
- That holds after a restart, or when the Hub's last read of the types failed. Nothing is resized on a guess.
- Every pre-check holds for every type's requests.
- If one check fails for any type, the container is skipped. It is never clamped.
- A ResourceQuota is held to the most any type asks, on every pod.
- A Rollback or a Revert puts back the pods of every type.
- The Actuation carries no single `cpu` or `memory`. Its reason names each type's requests, for example three types, with the rest counted.
- The webhook gives a new pod its node's type's requests where they are lower than its template's.
- It reads the node from the pod's node affinity, since a DaemonSet's pod has no `nodeName` when it is admitted.
- It gives nothing where the node or its type isn't known.
- A new node of a type already applied gets that type's requests.
- A Step-up takes the memory of every type, as it takes a pooled Sizing's.

### Holding what it applied

What the Actuator applied, it holds:

- its webhook gives a new pod of that Workload the same requests;
- this covers a rollout, an Argo Rollout's canary step, a reschedule, a scale-up and a new node's DaemonSet pod;
- the requests never go above the pod's limit;
- this lasts as long as the container still asks what its template asked when the Actuator applied.

### When the hold ends

When a person changes those requests in the template, or the Deployment or its container goes, the Actuator hands the container back:

- a `HandedBack` Event;
- an Actuation `handed_back`;
- new pods keep the template's requests from then on.

### DaemonSet pods and raises

A DaemonSet's new pod gets only the requests lower than its template's. It must start on whatever node it lands on.

- A raise reaches the pod in place once it runs, if its node has room. This is the catch-up below.
- If the node has no room, the resize is `Infeasible`, refused by the API server, or `Deferred` for 10 minutes.
- Then that pod is left without its raise. Its pending resize is undone, and the requests it lowers stay set in place (a lowering always fits). A `PodLeft` Event is written.
- The other pods keep the requests. New pods still get the lowered ones.
- That pod isn't tried again until the next Sizing.
- The same goes for a DaemonSet's pods during a rollout, such as a canary left behind. The next pod is the canary.
- Only when no pod's node has room does the rollout fail.

### TLS and failing open

- The webhook serves TLS with a certificate the Actuator makes at each start. It puts that certificate in its webhook's `caBundle`. There is no cert-manager.
- The webhook fails open. With the Actuator down, slow or scaled to zero, pods start from their template.
- While the Kill switch is on, the webhook changes no pod.

### Catch-up

- A pod that missed its change catches up once the Actuator may change things again. That means the Hub is reachable and the Kill switch is off.
- The catch-up runs the pre-checks again, then a canary, then one pod at a time, as for a new approval.
- It ends in another `SizingApplied`.
- A pre-check that now fails ends the hold, with `SizingSkipped`.

## Pre-checks and the rollout

### Pre-checks

Before a resize, the Actuator checks for changes it skips. It writes the reason in the Event and the Actuation. It skips:

- a change that would change the pods' QoS class. A Guaranteed container's limits would have to change too;
- a Workload a VPA sizes (any mode but `Off`);
- requests outside the namespace's LimitRange: minimum, maximum, or limit-to-request ratio;
- an increase that would pass a ResourceQuota.

It never clamps a request to fit.

### HPAs

- A resource an HPA of the Workload scales on by utilization is left to the HPA. For an HPA without metrics, that is cpu.
- The other resource is still sized.
- The Hub shows such a Sizing as held by its HPA. It also shows the `AverageValue` change that frees it ([Sizings held by an HPA](optimizer.md#sizings-held-by-an-hpa)).

### The rollout steps

1. The Actuator resizes one canary pod and watches it for 10 minutes.
2. Then it resizes the others one at a time.
3. Each pod waits until the kubelet has allocated the last one and it is Ready. This takes one per minute.

### When a resize fails

- A resize the kubelet calls `Infeasible` stops the rollout there, as failed.
- So does a resize not done in 10 minutes. `Deferred` counts as not done.
- The exception is when the Contract turns eviction on ([Eviction](#eviction)).
- The Actuator never evicts another pod to make room.
- "Applied" is reported when every live pod has the new requests.

### Retries and the Hub

- Each call to the API server is tried once and may take 15 seconds. Its own minute is the retry.
- A failure that may pass goes on to the next pod, and is waited out. Examples: a network failure, a 429, or the API server's own 5xx.
- Such a failure is tried again each minute, for 10 minutes at most, before the rollout fails.
- An approval that meets such a failure before anything changed is not used up. This covers reading the Workload, its checks, and the canary's resize.
- It is tried again each minute, for 10 minutes at most. Only then is it reported `failed`.
- The rollout goes on only while the Hub answers and the Kill switch is off.
- The rollout covers every kind in [Which kinds it resizes](#which-kinds-it-resizes).

### A scaled-down Workload

A Workload scaled to zero mid-rollout ends it `applied`, with "N pods resized in place, then none left to resize". The Workload is held, so the pods of its next scale-up start with the requests.

### Skipped or failed approvals

An approval that is skipped or fails leaves what an earlier one holds as it is. The message reads "run N's requests stay held". Those requests:

- are still given to new pods;
- are still undone by a Revert.

## Rollback

### When a Sizing rolls back

- From its first resize until an hour after the rollout ends, the Actuator watches the pods with its requests.
- A container that restarts since it got them rolls the Sizing back. An OOM kill named as one counts.
- So does a pod it resized that stays NotReady for 2 minutes.

### What a Rollback does

- Every pod with its requests goes back to the template's, in place and at once.
- A request the template doesn't set stays.
- The Actuator writes a `SizingRolledBack` Event, and an Actuation `rolled_back`.
- Both say what went back to what, for example "2 pods back from cpu 115m, memory 91Mi to their template's cpu 1, memory 1Gi".
- A message goes to the Contract's operator channel, once, however often the report is resent.

### How Slack is told

- The Hub posts the Rollback after it has answered the Actuator, from what it stored.
- If Slack doesn't take a post, the Hub tries again 5 minutes after that failed post.
- Each later wait is twice as long, an hour at most.
- It keeps trying for a week.
- So Slack being down delays a Rollback's message and never loses it.
- One told more than ten minutes late says when it was reported.
- At most 20 Rollbacks of one cluster are posted in an hour.
- Past that, one line counts the rest and points to the Automation page.

### After a Rollback

- The Actuator applies no new Sizing of that container for 24 hours.
- After a second Rollback within 7 days, the wait is 48 hours.
- After a third, it is 96 hours.
- An approval in the meantime is skipped, with the date.

### What is not a Rollback

- **CPU throttling.** It is no reason, since it is already a Sizing's risk.
- **A pod whose node is gone.** That covers a node that failed, or a spot node taken back. Kubernetes then marks the pod NotReady, while its containers' own readiness stays as the kubelet last wrote it. The pod is removed with the reason `DeletionByTaintManager` or `DeletionByPodGC`. Or the kubelet ends it for a node shutdown.
  - The Actuator tells this from the pod alone. It reads no node.
  - It counts neither the pod's NotReady, nor a CronJob run's pod that Failed that way.
  - It never gives such a pod a Sizing's requests.
  - A rollout goes on without it. A canary lost inside its ten minutes was never watched, so the next pod resized is the canary.
  - A catch-up leaves the pod until its node is back.
  - An approval that only such pods are missing is tried again for ten minutes.
- **A pod killed before its node went.** A container that restarted or was OOM-killed before its node went still counts. A Rollback or a Revert still puts such a pod's requests back, for when its node returns.
- **A CronJob run evicted to drain its node.** A run's pod that Failed because it was evicted to drain its node (`EvictionByEvictionAPI`, how Karpenter empties a spot node; from 0.58.0) is the Job's retry. It is not a Rollback.
- **A pod evicted under node pressure.** This is not told apart from one a Sizing harmed, so it counts as before.
- **A node restart.** A node that restarts and comes back starts its containers again. The container runtime reports such a restart as last ended with reason `Unknown` and exit code 255. That is not the Sizing's either.
  - A crash reads `Error`, and an OOM kill reads `OOMKilled`. Both still count, then or later.
  - Its 2 minutes of NotReady are counted from when its containers started again, not from when the node went.
  - A CronJob run's pod that Failed in a node's restart is not told apart yet.

### Other conditions

- A Rollback happens with the Hub unreachable too.
- It never happens while the Kill switch is on.
- Pods that already had the requests when a rollout or a catch-up began are counted from then. So a restart from before is never the reason.
- A pod that refuses to go back (the API server busy) is tried again each minute, pod by pod.
- It is tried again whether the Hub is reachable or not, but never under the Kill switch.
- This goes on until every pod is back, the API server refuses for good, or a person changes the template.
- The Actuation shows it as `; N refused ...; tried again each minute`.

## Eviction

A resize can never fit its node when the node's allocatable is smaller than the new requests. That resize is `Infeasible`.

- On Kubernetes 1.33 and 1.34, the kubelet marks it so.
- From 1.35, the API server refuses the resize at once (403, "node didn't have enough allocatable resources").
- The Actuator treats both alike.
- A node that is merely full now makes the resize `Deferred`. That waits up to 10 minutes and then fails, as before.

With eviction on, the Actuator evicts that pod instead. Its ReplicaSet starts a replacement. The scheduler places the replacement on a node with room, and the webhook gives it the new requests.

Eviction is off unless the Contract says so, in its `actuator:` block:

```yaml
actuator:
  eviction: true
  timezone: Europe/Rome          # IANA name; UTC when left out
  windows:                       # at least one when eviction is on
    - {days: [sat, sun], start: "00:00", end: "24:00"}
    - {days: [mon, tue, wed, thu, fri], start: "22:00", end: "02:00"}   # past midnight: the night is the day it starts
```

### Windows

- The Hub tells each of the Contract's Actuators, in `/actuator/desired`'s `eviction`, whether eviction is `off`, or on with its window `closed` or `open` now.
- Under the Kill switch, eviction is `off`.
- The windows gate eviction only. Resizes in place go on at any time.

### Outside a window

- The rollout waits on that pod, with no time limit.
- The Actuator says so once, in an `EvictionWaiting` Event.
- A newer Sizing, approved or under Automation, still replaces the waiting rollout.
- The pause annotation and the Kill switch stop it, as they stop any rollout.
- A Revert needs an Actuation of the container first. So a first rollout waiting for a window can't be reverted until it ends.
- A newer approval, the pause or the Kill switch can stop it.

### Never evicted

The Actuator never evicts:

- a StatefulSet's pod, since it keeps its state. A zonal volume would pin its replacement;
- a DaemonSet's pod, since it would start again on the same node;
- an Argo Rollout's pod. Those are resized in place only;
- a pod of a Deployment with one replica, since eviction would stop it;
- a pod no PodDisruptionBudget in its namespace covers, since nothing would keep enough pods up.

Its rollout fails at once, window open or not, as with eviction off. The Actuation says why. A failed read of the Deployment or its budgets fails it the same way.

### Inside a window

- The Actuator evicts through the Eviction API, so PodDisruptionBudgets hold.
- A budget that allows no disruption now keeps the pod waiting. The Actuator writes one `EvictionWaiting` Event.
- Any other refusal fails the rollout.
- A `PodEvicted` Event names the pod and the requests its replacement starts with.

### Its Actuation

- The rollout's reason ends with `<pod> evicted, its resize Infeasible`.
- While a replacement is awaited, no new Sizing replaces the rollout. That lasts 10 minutes at most.

### One at a time

- The Actuator evicts one pod at a time.
- It never evicts while another evicted pod's replacement is awaited.
- It waits at least 5 minutes after the last eviction, across the cluster.
- It evicts only the pod whose resize is `Infeasible`, never another pod to make room.

### The replacement

The replacement is a pod of the Deployment that wasn't there before the eviction. Once it is Ready with the requests, it stands for the evicted pod.

- It is watched for a Rollback like any pod the Actuator resized. An evicted canary's 10 minutes start again.
- The rollout then goes on.
- A replacement that starts from its template (the webhook missed it) fails the rollout. It is not evicted again.
- If none is Ready within 10 minutes, that is a Rollback. The exception is when none is owed: the Deployment scaled down meanwhile and already has the pods it wants. Then the rollout goes on.
- A replacement still `Pending`, because no node has room, is evicted again. It holds nothing, so it starts from its template.
- A Pending pod's eviction ignores budgets.

### Requirements

- Eviction needs a cluster with an `actuator_token_secret`.
- The Actuator's RBAC in the chart grants `create pods/eviction`.
- It reads `poddisruptionbudgets` per namespace.
- When upgrading from 0.42.0, re-apply the chart. Without that read, nothing is evicted.
- An Actuator of 0.42.0 or older never reads `eviction`. It fails an `Infeasible` resize, as before.

## Reflexes: OOM kills and the Step-up

A **Reflex** is a bounded, expiring change the Actuator makes on its own, for a Workload under Automation. The first Reflex is for OOM kills. It is switched on per Contract:

```yaml
actuator:
  reflexes:
    oom:
      max: 2       # a Step-up never goes past this many times the memory limit in the Workload's template; 2 unless set, more than 1, 4 at most
      expires: 7d  # a Step-up ends this long after its last step unless it is still needed; 7d unless set, 1h to 90d
```

It needs a cluster with an `actuator_token_secret`.

The Actuator tells each OOM kill of an automated container apart and reports it. For a container at its memory limit, it answers with a **Step-up** ([ADR 0005](adr/0005-reflexes-may-raise-limits.md)).

The Step-up, the [Startup boost](#the-startup-boost) and the [Burst](#burst-cpu-throttling) are the only times the Actuator changes a limit.

### Which containers it looks at

The Hub lists the cluster's containers in `/actuator/desired`'s `oom`:

- containers under Automation (a Sizing ready or not);
- not a Workload whose Automation three Rollbacks ended;
- not a container reverted since its last approval;
- none while the Kill switch is on.

### What counts as an OOM kill

- Each minute the Hub answers, the Actuator reads the pods of those namespaces once.
- It tells each OOM kill of the last 10 minutes of a listed container, once.
- A kill is one of two kinds.

| Kind | When it is |
|---|---|
| **container-limit** | the container has a memory limit, and its node's kubelet said nothing. It reached its own limit, or one above it: its pod's, or the node's allocatable memory, which the kubelet sets as a limit on all pods together. |
| **node-level** | the node itself ran out of memory. The kubelet says so in a `SystemOOM` Event on the Node within 2 minutes of the kill. Or the container has no memory limit. |

- Both read alike on the pod: `OOMKilled`, exit code 137. So the Event decides.
- The container runtime marks a container `OOMKilled` whichever limit caused the kill.
- A container-limit kill brings no Event. So the Actuator can't tell the two apart from the pod alone.

### Reading the Node's Events

- A Node's Events are in `default`.
- The chart grants a Role there, with `get` and `list` on Events. That is `actuator.nodeEvents`, on by default.
- The Role covers every Event in `default`, including those of anything that runs there.
- A client who would not grant that removes the Role and its RoleBinding.
- Then, on a cluster installed before this and not re-applied, the Actuator reads no node Events.
  - A kill of a container with a memory limit counts as container-limit.
  - Its report says the node's Events were not read, and why.

### Reporting kills

- Each container's kills that get no Step-up are one Actuation `oom_killed`.
- It shows the limit the container had, how many pods, and which kind each kill was. Five pods are named, and the rest are counted.
- The Hub stores it with the others. For now it shows it nowhere.
- The Automation page shows what a container holds, as before. Nothing is posted to Slack.
- No Event goes on the Workload. The page and the Slack line come with the Step-up.
- Until then, a sighting is in the Actuator's log and the Hub's `actuations` table.
- A container is reported every 10 minutes at most, with its pods killed since the last report.
- A pod shows only its last kill. So a pod killed three times in that time counts once.
- At most 50 containers are reported in a minute. The others wait for the next minute.
- The Actuator keeps the time of its last report in memory only.
- After a restart, the kills of the last 10 minutes are reported again. The same report is stored once. A report with a newer kill is a second row, which counts the earlier pods again.
- It reports nothing while the Hub is unreachable, or has not taken its earlier reports.
- A paused Workload's kills are reported too. Reporting changes nothing.

### Kills not seen

- A kill the container runtime reports as `Error` with exit code 137 is not seen. The runtime sometimes misses the kernel's event.
- That kill can't be told from a `SIGKILL`.

### The Step-up

A container-limit kill of a pod is answered within the minute. The container's memory limit and its memory request are raised together, on every pod of the Workload, in place and at once. That is so whether or not the pod was killed. New pods start with them too.

- **How much.** The limit goes to the larger of 1.2 times the old limit, and 100 MiB more. The result is in whole Mi.
  - The request goes up by the same factor. So a pod's QoS class stays. A Guaranteed pod's two values stay the very same.
  - A request a Sizing had lowered is not kept. The step starts from the template's request, or from a Sizing's higher one.
- **How far.** A Step-up never goes past `max` times the limit in the Workload's template.
  - There it stops.
  - Each further kill is reported with "raise it in its manifest, or look for a leak".
  - No Rollback happens there, and Automation goes on.
- **How often.** One Step-up a container every 10 minutes. And 10 a cluster an hour.
  - Only a kill of a pod already at the container's current step earns the next Step-up.
  - A pod below that step, started from its template since, is brought to the step and no further.
- **Not made.** The Actuator makes no Step-up, and says so with the kill ("No Step-up: ..."):
  - on a paused Workload;
  - under a VPA in any mode but `Off`;
  - where an HPA scales on its memory;
  - where its `resizePolicy` restarts the container for memory, which would restart every replica at once;
  - where its template sets no memory limit;
  - while a Sizing of that container still rolls out;
  - past a LimitRange's maximum, or a ResourceQuota's `limits.memory` or `requests.memory`;
  - never under the Kill switch;
  - never with the Hub unreachable, or while reports the Hub hasn't taken wait.
- **Its pods.**
  - One pod's refusal (its node has no room) leaves the others their step.
  - A pod whose node is gone is not touched.
  - A pod brought to a step whose node has no room for it has its resize left pending. That pod is put back to what it was given, and left.
  - It is tried again every 10 minutes while the Step-up lasts. The "left" message is said once, when first left.
  - A step no pod took is none. It is not kept, nor counted among the hour's ten.
  - A pod counts as at a step by the limit the kubelet says it gave it, not the one it was asked. On Kubernetes 1.33 and 1.34, a resize the node can't fit stays asked.
- **New pods.**
  - The webhook starts a new pod at the container's Step-up, its limit and its request both.
  - That applies where the pod still asks what the template asked when the step was made. A template that sets only a limit asks that much.
  - It applies only if the pod's QoS class stays.
  - Never under the Kill switch, or for a paused Workload.
  - A DaemonSet's new pod is not raised at its creation, since it must fit whatever node it starts on. It is raised in place, the minute after.
- **Catching up.**
  - Each minute the Hub answers, a pod that asks less than its container's Step-up is brought to it in place. This covers a pod the webhook missed.
  - A pod whose node refuses is tried again each minute. It is logged, and not reported.
  - A pod is only ever raised. A pod above the step, from a Step-up dropped since, keeps its own.
- **Kubernetes 1.33 and 1.34.** A resize the node can't fit is accepted, and stays pending.
  - The minute after, that pod is put back to what it was given, and left as it is for good.
  - It is said once: "left as they were: their node has no room".
  - This keeps a Sizing's rollout from reading the pending resize as its own Infeasible pod, which would evict it.
- **CronJobs.**
  - A run's pod is never resized in place.
  - The Step-up is for the next runs' pods, from the webhook.
  - A kill in the first run a Sizing is watched over is the Reflex's too, on the same terms as below.
- **The Sizing beside it.**
  - The container's memory is the Step-up's from then on.
  - A held Sizing keeps its cpu.
  - No later Sizing sets that container's memory while the Step-up lasts.
  - Its Actuation says "memory left to its Step-up". A Sizing that asks memory only is skipped, and that is said once.
- **Its end.** A person changing the memory limit or request in the Workload's template ends the Step-up within the minute. So does the Workload going away.
  - The Actuator writes an Actuation `step_ended` and an Event `HandedBack`.
  - No Sizing's hand-back happens. So what the container holds of a Sizing stays held, and revertable.
  - New pods start from the template again.
  - Running pods keep their memory until they start again.
  - A Revert drops the Step-up the same way.
- **Rollback.** With the Reflex answering, an OOM kill is no longer a Sizing's Rollback. That holds in its watch hour and out of it. There are two exceptions:
  - A kill under a Sizing that lowered that container's memory request, in its watch hour, is a Rollback as before. It is never a Step-up. A template that sets only a memory limit asks that much.
  - A node-level kill of a container whose memory request the Actuator holds lowered is a Rollback at any time.
  - Any other node-level kill is only reported.
- **Where the Reflex doesn't look.** When the Reflex isn't looking, an OOM kill in a watch hour is a Rollback as before. This covers the Hub unreachable, or older than this, the container not listed, and reports waiting.
- **What stays the Sizing's.** A crash that is no OOM kill is still the Sizing's Rollback. So is a pod NotReady whose OOM kill dates from before it got the Sizing's requests.
- **Declined kills.** A kill the Reflex looks at and declines (an HPA on memory, or its cap) is reported. It gets no Rollback and no Step-up.

Each Step-up is an Actuation `stepped_up`, with its `memory` set to the new limit. It also writes an Event `MemorySteppedUp` on the Workload. Pods brought to a Step-up later are also a `stepped_up`, which says so.

The Step-up's values and the hour's count are kept in the Actuator's ConfigMap. So a restart forgets neither.

### How a Step-up ends, and where it shows

A Step-up is not the Workload's new size. It ends by itself.

- **Its term.**
  - `expires` runs after its last step. The default is 7 days, unless the Contract says otherwise. It can be an hour to 90 days.
  - Once it runs out, the Actuator asks whether the Step-up is still needed.
  - The Hub sends, with each listed container, its memory peak over its Sizing's window.
  - A peak above the limit in the Workload's template keeps the Step-up for another term. This is said in an Actuation, so the Automation page still knows a Step-up that outlives the 90 days the Hub keeps Actuations.
  - A peak the Hub doesn't know keeps it too.
  - A peak within that limit ends it. So does a container the Hub no longer lists, because its Automation ended or the Reflex was switched off.
  - An ended Step-up writes an Actuation `step_ended` and an Event `StepUpExpired`.
  - The term is kept with each Step-up. So one still ends once the Reflex is off and the Hub sends no term.
  - A Step-up that never heard a term, from a Hub older than this, never expires.
- **Its pods, then.**
  - New pods start from the template again.
  - Running pods go back to the template's memory limit and request in place. This happens only where the Hub's peak says they fit under it. That way no resize is left hanging above what a pod uses, and no pod is killed for it.
  - Otherwise they keep theirs until they start again.
  - The peak is the Hub's last hourly reading, which is sampled. The lowering is tried once.
  - A pod that grew since keeps its resize in progress (on Kubernetes 1.34) until it fits.
  - A pod that refused is not asked again.
  - Kubernetes 1.33 refuses to lower a memory limit in place. This is said pod by pod, and those pods keep theirs.
  - A Revert does the same with the Step-up it drops.
  - A person's change to the template rolls the pods anyway.
- **In Slack.**
  - Each Step-up made is one line in the Contract's operator channel. It says what to write in the manifest to keep it, for example "set its memory limit to 228Mi".
  - A container still OOM-killed at its cap is one line a day.
  - Both go through the same teller as Rollbacks. That means they are posted after the Actuator is answered, and again until Slack takes them. The same 20 posts a cluster an hour apply.
  - Pods only brought to a Step-up, an OOM kill only seen, and a Step-up's end are stored and not posted.
- **On the Automation page.** A container's row shows its Step-up's memory limit, beside what it holds of a Sizing. A container with only a Step-up is a row too. Revert is offered for either, and drops both.
- **In a Triage's thread.**
  - When a Triage ends, the Hub adds one line above its Report for each Step-up made.
  - It covers Step-ups from an hour before the Triage began, on a Workload its Alerts name.
  - A Workload is named by a `deployment`, `statefulset`, `daemonset`, `rollout` or `cronjob` label. Or by a `pod` label that begins with the Workload's name, in the same namespace.
  - The Hub writes the line from what it stored. The model never sees it, since the Hub never reads a thread's messages back.
  - A Triage that ends twice says it twice.

### Burst: CPU throttling

The third Reflex is a **Burst**. It raises the CPU limit of a container that is being throttled ([ADR 0005](adr/0005-reflexes-may-raise-limits.md)).

The Actuator can't see throttling. It is in Prometheus, or behind a kubelet permission too wide to grant. So the Hub decides from its hourly read, and the Actuator makes the change.

```yaml
actuator:
  reflexes:
    burst:
      max: 2         # a Burst never goes past this many times the CPU limit in the Workload's template; 2 unless set, more than 1, 4 at most
      expires: 24h   # a Burst ends this long after its last step unless it is still needed; 24h unless set, 1h to 7d
```

It needs a cluster with an `actuator_token_secret`.

#### What the Hub reads and tells

- **The trigger.**
  - With each hourly Sizing run, the Hub also reads every container's share of CPU periods throttled over the last hour.
  - That is two more Prometheus reads a cluster, for each policy window in use.
  - More than a quarter means a Burst is due.
  - A Workload's pods are pooled. So one hot pod among ten quiet ones reads low.
  - A container that ran fewer than 600 periods in the hour, its pods together, is not told at all. That is a minute's worth at the default 100 ms. One throttled in the only second it ran is no Burst.
  - A container without a CPU limit has no such periods, and is never told.
- **The peak.**
  - The peak is the container's highest CPU use over the last `expires`. If its policy's window is shorter, the window is used.
  - It comes from the samples the Sizing already reads.
  - Those samples are 5-minute rates of one pod. Under a policy window of more than a month they are coarser.
  - So short spikes read well under the limit. The peak says a container is under sustained load, not that it spiked.
- **What the Actuator is told.**
  - `/actuator/desired`'s `burst` lists the cluster's containers under Automation, as `oom` does. A CronJob's containers are never listed.
  - Each has `throttled` (its share, only when over a quarter) and `peak`.
  - `burst_run` is the Sizing run they were read in.
  - `burst_max` and `burst_expires` are the Contract's.
  - A read more than two hours old gives neither share nor peak. This happens when Prometheus is down or the Hub restarted late. An hour long past is no reason to raise a limit.
- **What it needs.** cAdvisor's `container_cpu_cfs_throttled_periods_total` and `container_cpu_cfs_periods_total` in the cluster's Prometheus.
  - They are optional for Sizings. `squero sizing show` names them when missing.
  - Without them, no Burst is ever due. Nothing else says so.
- **Where it is kept.** In the container's stored Sizing row, beside its Sizing. It is kept only while the Contract sets the Reflex. The console, the digest and the model never read it.

#### What the Actuator does

- **The step.**
  - For each listed container the read says is throttled, the Actuator acts once per read.
  - Its CPU limit goes up by a quarter of its template's limit. For example, 200m goes to 250m, 300m, 350m, 400m.
  - The change applies to every live pod, in place.
  - Requests are untouched. So nothing is rescheduled, and an HPA or the Cluster Autoscaler reads the same requests.
  - The limit never goes past `max` times the template's limit.
- **How often.**
  - One Burst a container each read. Never two within 45 minutes. A `squero sizing refresh` minutes after the hourly run shares its throttled minutes.
  - Bursts and Step-ups together are 10 a cluster an hour at most. Of those, Bursts are 5 at most.
  - An OOM crash loop's Step-up is looked at first each minute. It never waits behind throttling.
- **Not made.** The Actuator makes no Burst in these cases. It says so as an Actuation `throttled` ("No Burst: ..."). Such a reason stands, and is said once a day.
  - a paused Workload;
  - a Guaranteed pod, since its limit can't leave its request;
  - a template without a CPU limit;
  - a `resizePolicy` that restarts the container for cpu;
  - a VPA in any mode but `Off`;
  - while a Sizing of that container still rolls out. This is tried again each minute, so the Burst follows the rollout by a minute, not an hour;
  - past a LimitRange's cpu maximum, or its limit-to-request ratio;
  - in a namespace whose ResourceQuota counts `limits.cpu`, whatever room it leaves. An HPA answering the same load makes new pods, each asking the Burst's limit;
  - at its cap;
  - once the hour's 10, or its 5 Bursts, are used;
  - with no live pod, or where every pod refused it.
  - Never under the Kill switch.
  - Never with the Hub unreachable.
  - Never while reports the Hub hasn't taken wait.
  - Never for a Hub older than this.
- **New pods.**
  - The webhook starts a new pod at the container's Burst.
  - That applies while the pod's own limit is still what the template's was when the Burst was made.
  - It applies only if the pod's QoS class stays.
  - Never a pod with pod-level resources.
  - A [Startup boost](#the-startup-boost) multiplies the Burst's limit and gives that back. It does not use the template's limit.
  - Each minute the Hub answers, a pod below the Burst is brought to it in place. This covers a webhook that was down.
  - A pod above the Burst is never lowered.
- **How it ends.**
  - `expires` runs after its last step. Then the Actuator asks whether the Burst is still needed.
  - A CPU peak above 90% of the template's limit keeps it for another term. So does a peak the Hub doesn't know. This is said in an Actuation.
  - A peak within that, or a container the Hub no longer lists, ends it. That includes a container whose Automation ended, or whose Reflex was switched off.
  - An ended Burst puts every pod above the template's limit back to it, in place. That needs no room and restarts nothing.
  - The term is kept with each Burst, so it still ends once the Reflex is off.
  - A person changing the CPU limit in the template ends the Burst at once. That is so whether the change is the value Slack named, or any other. It touches no pod: the rollout is theirs.
  - A [Revert](#revert) drops the Burst and lowers its pods.
  - The read that was handled when the Burst ended makes no new one. Nor does a Revert followed by an approval within that read.
- **What it reports.**
  - `burst` for one made, with its `cpu` as the new limit. It is also sent for pods brought to one, and for a term renewed.
  - `burst_ended`.
  - `throttled`.
  - An Event `CpuBurst` on the Workload. An Event `BurstExpired` at its term's end. An Event `HandedBack` when the template changed.
  - Its values, and the last read handled for each container, are kept in the Actuator's ConfigMap.
- **In Slack.**
  - Each Burst made is one line in the Contract's operator channel. It says what to write in the manifest to keep it, for example "set its CPU limit to 250m".
  - A container still throttled at its cap is one line a day.
  - Pods only brought to a Burst, a renewal, its end, and any other "No Burst" are stored and not posted.
  - It uses the same teller and the same 20 posts a cluster an hour as Rollbacks and Step-ups.
- **On the Automation page.** A container's row shows its Burst's CPU limit, beside what it holds of a Sizing and its Step-up. A container with only a Burst is a row too. Revert is offered for it, and drops everything the Actuator holds of that container.
- **In a Triage's thread.** A Burst made on a Workload the Triage's Alerts name, from an hour before it began, is a line above the Report. A Step-up is shown the same way.

#### Know before you set it

- A Burst raises what a container may use, not what it is given. So on a full node it changes little.
- The peak is of 5-minute rates. So a container throttled only in short spikes is burst, ends at its term, and is burst again. Each time there is a Slack line.
- Upgrade the Hub before its Actuators. An Actuator's `burst` report to a Hub older than this is refused whole.

## The Startup boost

A container that needs more CPU to start than to run can have a higher CPU limit for its start only. Examples are a JVM, or a cache to warm.

The Startup boost is a factor on a Sizing rule, beside `automate`. So it needs no `reflexes` block:

```yaml
sizing:
  rules:
    - {namespace: shop, name: "checkout-*", policy: production, automate: true, startup_boost: 2}   # 1.5 to 4
```

### What it does

- The webhook starts each new pod of the rule's containers with its CPU limit times the factor.
- It writes the container's own limit in the pod's annotation `squero-actuator/startup-boost`.
- Each minute, the Actuator gives a boosted container its own limit back, in place.
- That happens once its pod is Ready, or the container has run for 5 minutes, or it restarted. Whichever comes first, so up to a minute after.
- An Event `StartupBoostEnded` is written on the pod.
- The 5 minutes count from the container's own start. So pulling its image and its init containers take none of them.
- A restart never gets a boost. A container that restarts while boosted loses the boost. One that restarts later starts from its own limit.

### Requests are untouched

- Only the CPU limit changes.
- So the pod is scheduled as it would be without the boost.
- An HPA or the Cluster Autoscaler reads the same requests.
- A container that sets a CPU limit and no request gets that limit written as its request. This is what Kubernetes makes of it anyway.

### Which containers

The Hub lists, in `/actuator/desired`'s `boosts`, the cluster's containers that are:

- under Automation;
- in a rule that sets the factor.

Those are containers it has stored a Sizing for, ready or still learning. A Workload whose Automation three Rollbacks ended is not listed. Nor is a container reverted since its last approval.

- The Actuator keeps the list in its ConfigMap. So boosts go on while the Hub is unreachable, and after its restart.
- Remove the factor from the rule and the boosts stop the minute the Hub answers.

### Not boosted

- A Guaranteed pod, since its limit can't leave its request.
- A container without a CPU limit, since it needs none.
- A CronJob's run.
- A container whose `resizePolicy` restarts it for cpu, since giving the limit back would restart it.
- A paused Workload.
- A Workload under a VPA in any mode but `Off`.
- A pod that already carries the annotation.
- A pod with pod-level resources (`spec.resources`), whose limit a container's may not pass.
- Every pod of a namespace whose LimitRange sets a cpu maximum or a limit-to-request ratio. The same goes for a namespace whose ResourceQuota counts `limits.cpu`.
  - Either could refuse the boosted pod's creation.
  - So the whole namespace gets no boost, whatever room they leave.
- A VPA, or such a namespace, is said once in the Actuator's log (`no startup boost in ...`).

### Kill switch and pause

- No pod is boosted while the Kill switch is on.
- A pod boosted before keeps its higher limit until the switch is off, or the pause is lifted.
- The Actuator resizes nothing meanwhile.

### Where it shows

- It shows on the pod only: the annotation and the Event.
- It happens at every start. So it is no Actuation, is not posted to Slack, is not on the Automation page, and does not count in the 10 Step-ups a cluster an hour.

### Reading the annotation

- The annotation is read as anyone's word.
- The Actuator only ever lowers a CPU limit to the value the annotation names. It never goes under the container's request.
- The Actuator can't remove the annotation, since it may not edit pods. So the annotation stays on the pod.

### Know before you set it

- A JVM, and any runtime that sizes its thread pools from the CPU count it sees at start, sizes them from the boosted limit. It keeps them once the limit is back.
- A policy engine that caps CPU limits or the limit-to-request ratio can refuse a boosted pod's creation. Examples are Kyverno, Gatekeeper, and a ValidatingAdmissionPolicy.
- The Actuator reads LimitRanges and ResourceQuotas, not those engines' policies.

An Actuator older than this ignores `boosts` and `burst`. A Hub older than this sends neither.

Upgrade the Hub before its Actuators. Roll Actuators back before the Hub. An Actuator's report with an outcome its Hub doesn't know is refused whole and dropped, and the reports beside it too.

## Automation

A Sizing rule with `automate: true` is the Operator's standing approval for the containers it picks (ADR 0004). See [Sizing policies](optimizer.md#sizing-policies).

### What the Actuator gets

- `/actuator/desired` then also lists, apart from the Approved Sizings, the newest Sizing of each such container on the token's cluster.
- That Sizing must be ready and change something.
- The Actuator applies it unasked, through the same pre-checks, canary and rollback.
- A Sizing that is learning again after a gap in Prometheus is not listed. Nothing new is applied, and the Actuator keeps what it holds. See [Sizings](optimizer.md#sizings).
- An Approved Sizing of the same container goes first.

### The churn rule

- A change under 10% of what its pods ask now is ignored.
- An increase applies at once.
- A decrease applies at most once a day per container.
- An increase beside a waiting decrease goes alone.
- A new one waits while that container's rollout is under way. It also waits while the container backs off after a Rollback. It waits without a word.
- A Sizing the pre-checks skip (a VPA, or a quota) is said once, in its Event and Actuation. It is not said again until its reason changes. What the Actuator holds stays held.
- Its Events say `(Automation, run N)`.
- A Sizing that leaves one resource alone, approved or not, keeps the value held for it.

### Ending Automation

- Three Rollbacks of one Workload in 7 days end its Automation.
- The Hub then lists none of its Sizings until the oldest of those Rollbacks is 7 days old.
- The operator channel hears it once, with that date. It is in the message of the Rollback that ended it.
- To end Automation for good, remove `automate` from its rule.
- An Actuator of 0.40.0 or older never reads Automation's list. It applies only approvals.

## Revert

The Operator can revert a Sizing in two ways: from the console's Automation page, or with `revert_sizing` on the Hub MCP server ([Reverting a Sizing](running-the-hub.md#reverting-a-sizing)).

The Revert reaches the Actuator in `/actuator/desired`'s `reverts`. It stays there until the Actuator reports it done.

When the Actuator carries out a Revert:

- It puts every pod with the requests it holds for that container back to the template's, in place and at once.
- A request the template doesn't set stays.
- It works like a rollback, but with no backoff.
- It drops the container's Step-up and its Burst.
- It holds that container no more. Its webhook leaves new pods alone, and nothing catches up.
- A `SizingReverted` Event and a `reverted` Actuation say what went back to what.
- With nothing held, the Actuation says so.

Other cases:

- A paused Deployment's Revert waits for the pause to lift.
- An approval applied before stays reverted.
- A later approval applies, and resumes Automation of that container.

## Blockers: what the Actuator reports

Every five minutes, the Actuator reads which pods in the Contract's namespaces may keep a node from being removed. It tells the Hub when that changed, and at least once an hour. This is slice 15.1b, audit A4.

- The Hub names the namespaces in `/actuator/desired`.
- This runs under the Kill switch too, since a read changes nothing.
- The read lists pods and PodDisruptionBudgets, as the Actuator already may.
- It reads pages of 200 pods at a time.
- For what its pods need of a node, it reads the nodes too ([below](#what-its-pods-need-of-a-node)).

### What it sends

The report goes to `POST /actuator/blockers`, with the Actuator's own token. For each pod it sends:

- its namespace, name and node;
- its controller's kind and name;
- the names of its emptyDir volumes, and which are in memory;
- the names of its hostPath volumes;
- the annotations `cluster-autoscaler.kubernetes.io/safe-to-evict`, `.../safe-to-evict-local-volumes` and `karpenter.sh/do-not-disrupt`, as they are;
- the PodDisruptionBudget covering it that allows the fewest disruptions. A budget whose status isn't written yet counts as allowing none;
- when the pod started.

These are facts only. [The Hub applies the rules](optimizer.md#blockers-could-a-node-be-removed).

No other annotation, no label, no environment and no volume contents leave the cluster.

### Which pods are sent

Only pods with such a fact are sent:

- no controller, or a custom one;
- a local volume;
- one of the annotations;
- a covering budget at 0;
- a kube-system pod no budget covers.

Never sent:

- a DaemonSet's pod;
- a node's static pod;
- a pod not yet on a node;
- a pod finished or on its way out.

### Names are checked

The Hub refuses a report whose namespace, pod, node or PodDisruptionBudget name is not shaped like a Kubernetes name. That is what Kubernetes itself would refuse. These strings reach the console and the Hub MCP's model.

### What it could not read

- A namespace the Actuator is not bound in, or whose read failed, is left out of the namespaces it lists.
- The Hub then gives that namespace's nodes no verdict.
- Over 1000 pods, or over the megabyte the Hub's proxy lets through, the report is cut there. The report says so.

### When it is sent

- The read happens every five minutes. It runs after everything else in that minute, on its own.
- A failure there costs no resize and no Rollback.
- A report is sent when it differs from the last report the Hub answered. The time doesn't count.
- It is also sent once an hour whatever it holds. So a pod a rollout replaced is gone from the Hub's view within five minutes (audit A4).
- Each read lists the namespaces' pods and PodDisruptionBudgets, and the nodes where a pod needs something of one. That is twelve times an hour, a page of 200 at a time.
- A report that didn't reach the Hub goes again five minutes on.
- A report the Hub refused counts as answered, and waits the hour. An older Hub has no such route.
- The Hub stores a cluster's latest only, dated by its own clock.
- It refuses a report that names a namespace outside the cluster's.

### With Placement

- From 0.55.0, the report also says that the Actuator places.
- Per pod it says whether the pod carries Placement's preference, and its Workload as the webhook names it (`Deployment/api`). The Workload comes from the pod's owners and labels.
- So the list doesn't wait for the Hub's next read of a new pod.
- This is sent only once the Hub has given it a Placement. An older Hub refuses a field it doesn't know.

### With spot nodes

- From 16.2b, the report also says that its webhook steers replicas to spot nodes (`spotting`).
- This is sent only once the Hub has given the cluster's spot nodes.
- The Hub sends, from 16.2a, the Workloads under a [Spot ratio](optimizer.md#spot-ratios-and-candidates). It also sends the spot label and taint, and the nodes carrying the label.
- An older Actuator ignores them.

### What its pods need of a node

This is audit A3. The Hub asks for it with `constraints` in `/actuator/desired`. An older Actuator ignores it.

- For each pod that would move with its node, the Actuator sends what the pod needs of a node.
- Pods that stay with their node, of the kinds above, are skipped.
- It sends the `nodeSelector` and the node affinity as written.
- That covers the required terms, and the preferred ones with their weights.
- The webhook's own terms are among them.
- Each distinct need is sent once, as `needs`.
- Each pod's needs are sent as `needed`, keyed `namespace/pod`.
- With them, the Actuator reads every node's values of the label keys those needs name. These are sent as `labels`.
- The nodes are read through the ClusterRole `squero-actuator-nodes` in the chart (`actuator.nodes`, on by default). That grants `list nodes` and nothing else.
- A Node object holds more than labels, such as addresses, capacity and conditions. Only those label values leave the cluster.
- A client who would not grant it deletes that ClusterRole and its ClusterRoleBinding.
  - The Actuator then says `nodes_refused`.
  - A read that failed says `nodes_unread`.
  - A report holding more than these limits says `cut`. The limits are 10,000 pods' needs, 1000 distinct needs, 5000 nodes, 100 label keys, or the megabyte.
  - Each is sent as a code, with no needs sent.
  - The Hub then counts no node of that cluster as one that could go, and says why.
- No need at all reads no node.
- The Hub refuses a key, value or name of no shape Kubernetes allows.
- See [How the Hub uses them](optimizer.md#nodes-that-could-go).

### The Actuator's own pod

Its own pod runs in its own namespace. That is outside the Contract's namespaces. So the Hub's read of the nodes counts it as a pod it can't see. Its node could then never be clear.

- The Actuator reads that pod by name (`SQUERO_ACTUATOR_POD`, from the downward API).
- It reads through the Role `squero-actuator-self` in its own namespace. That grants `get pods` and nothing else. Nothing else runs there.
- It sends the same facts as any pod's. That includes its containers' requests, and what it needs of a node, as an index into `needs`. The key is `itself`.
- The Hub judges it by the same rules.
- The shipped manifest names its `tmp` emptyDir in `safe-to-evict-local-volumes`. So Cluster Autoscaler may remove its node. `kubectl drain` needs `--delete-emptydir-data`.
- The Hub counts it only on the node its read of the nodes put it on. That read must be made at least 5 minutes after it started.
- It moves with that node's other pods.
- A client who would not grant the Role deletes it and its RoleBinding. Then its node stays unknown, and one pod is not visible.
- A new image alone doesn't do it. The manifests must be applied again: the env, the Role and its RoleBinding, and the annotation.
- Where it blocks, its namespace and pod name show as a Blocker's. That is in the console and the Hub MCP's `nodes`.
- It is the one pod outside the Contract's namespaces ever named.

### CronJobs on spot

From 16.3b, with CronJobs on spot:

- The report also says that the Actuator steers CronJobs' runs (`spot_runs`).
- It lists the ones it doesn't steer, and why, as a code (`spot_skipped`).
- The codes are `namespace/name` mapped to `no_retry` or `ignores_disruptions`.
- The Hub writes the sentence, so no text of the Actuator's reaches a model.
- Each is sent only when true, or not empty.
- The Hub sends, from 16.3a, the CronJobs under `spot: true` (`cronjobs`). That is a list of their own, which an older Actuator ignores.

## Placement: gathering the pods that can't move

A pod that blocks its node's removal keeps that node, whatever it asks.

Where the Contract names a cluster's [Placement](optimizer.md#placement) label, the Actuator's webhook helps gather such pods. It applies to each new pod of a Workload the Hub lists. Those are Workloads whose pods block a node for a reason set in their spec. The webhook gives such a pod:

- a preference for the nodes carrying the label, the **Pinned nodes**;
- a toleration of their taint, where the Contract names one.

See [ADR 0006](adr/0006-placement-is-a-preference.md).

- **A preference, never a requirement.**
  - One preferred node affinity term of weight 100 is added. It goes after the pod's own terms.
  - Nothing the pod has is replaced.
  - Nothing is added where the pod already prefers that label.
  - With no room on a Pinned node, the pod starts elsewhere.
- **The toleration.**
  - It goes on the placed pods only.
  - It is for the taint the Contract names only.
  - `key=value:Effect` is tolerated `Equal`.
  - `key:Effect` is tolerated `Exists`.
  - Tainting the Pinned nodes keeps other pods off them. That leaves room there for the ones that can't move.
- **Never a running pod.**
  - A pod's affinity can't change once it exists.
  - The Actuator never evicts or restarts a pod for Placement.
  - A running pod moves at its Workload's next rollout or restart.
  - A person does that when the Nodes page says "Restart shop/Deployment/api to free it".
- **Said on the Workload.**
  - An Event `Placed` is written when a Workload joins the list. For example: "Placement: its new pods prefer the nodes labelled squero.io/pinned=yes".
  - An Event `PlacementEnded` is written when it leaves.
  - Its running pods keep what they have.
  - After the Actuator restarts, its first list says them again.
- **Stopped** by the Kill switch and the pause annotation, as everything the webhook does.
- **Lists the Hub can't make.** A list the Hub can't make now, with no fresh report or no read of the nodes, leaves the last one in place.
- **Ending it.** Removing the Contract's Placement ends it.
- **No new permission.** It is a JSON Patch on a pod's creation, and Events, which the Actuator already writes.
- A DaemonSet's pods are never placed.

## Spot: steering replicas

Where the Contract names a cluster's [spot nodes](optimizer.md#spot-nodes), and a Sizing rule sets a [Spot ratio](optimizer.md#spot-ratios-and-candidates), the Actuator's webhook steers new replicas. It puts each new replica of the listed Workloads on a side. The side is set per ReplicaSet. This is from 0.57.0, slice 16.2b. See [ADR 0006](adr/0006-placement-is-a-preference.md).

### On-demand first

- While fewer of the ReplicaSet's pods are on-demand than its ratio keeps there, a new pod is **required off the spot nodes**.
- For example, `{ratio: 0.7, on_demand: 2}` keeps 3 of 10, or 2 of 4.
- A ReplicaSet of one pod that blocks a node is all on-demand.
- The requirement is a `NotIn` on the spot label. A node without the label meets it.
- It is added to each of the pod's own required terms. They are ORed, so one beside them would loosen them.
- This is the one requirement the webhook ever adds.
- It can leave a pod Pending only where no on-demand node has room. The Workload's own manifest would do the same.
- The pod also gets `controller.kubernetes.io/pod-deletion-cost: "1"`. So a scale-down takes spot replicas first, unless the pod sets its own.

### Then spot

- The pod gets a preferred node affinity of weight 100 for the spot label. It goes after the pod's own terms.
- It also gets a toleration of the spot `taint`, where the Contract names one. AKS taints its spot nodes.
- With no spot room, the pod starts on-demand by itself. Nothing needs to fall back.

### Counted by a label

- Each pod the webhook steers carries `squero.io/capacity: on-demand` or `spot`.
- Each minute, the Actuator lists the steered Workloads' pods. It lists one namespace at a time.
- It counts each ReplicaSet's pods, by that label. Failing the label, it counts by the node. The spot nodes are those the Hub sent.
- A Pending pod without the label counts as neither.
- Terminating and finished pods are not counted at all.
- It reads a ReplicaSet's `spec.replicas` at its first admission in the minute.
- It reads it again once the pods counted reach it. A rollout grows a ReplicaSet in seconds.
- A ReplicaSet counted full is listed again at admission. That covers a replacement for a pod lost since, such as a spot interruption.
- Both reads have one second. A read that fails or is slow places nothing.
- A dry run is decided, and never counted.

### What is left alone

- A pod whose own spec settles its side. Examples are a `nodeSelector`, or a required affinity on the spot label's key.
- A paused Workload.
- Everything under the Kill switch.
- Its running pods are never changed, evicted or restarted.
- A Workload below its minimum (the Nodes page's risk) is repaired at its next rollout or restart.

### Said on the Workload

- An Event `SpotPlaced` is written when it joins the list. For example: "Spot: 70% of its new replicas prefer the nodes labelled karpenter.sh/capacity-type=spot, at least 2 kept off them".
- An Event `SpotEnded` is written when it leaves.
- Its report then says it steers (`spotting`). So the Hub drops "its Actuator doesn't place them yet".

### No new permission

The Actuator needs nothing new for this. It uses:

- the ReplicaSet `get` and pod `list` it already has;
- a JSON Patch on a pod's creation;
- Events.

### CronJobs' runs

This is from 0.58.0, slice 16.3b. It covers the CronJobs the Hub lists under `spot: true`.

- Each minute, the Actuator reads each one.
- It steers its runs only where its Jobs allow a retry, and count an interrupted pod as failed. Both must hold:
  - `backoffLimit` is above 0. Unset is 6. For an Indexed Job, `backoffLimitPerIndex` must be above 0 where set;
  - no `podFailurePolicy` rule ignores `DisruptionTarget`, or fails the Job or its index on it.
- Otherwise an Event `SpotSkipped` says why, once. Its report lists it, as `no_retry` or `ignores_disruptions`.
- A change in what the report tells sends the next report at once, not within the hour. That covers a change to "it steers", or a skip.

How a run's pods are placed:

- A run's pod prefers spot nodes, and tolerates their taint, on the run's first tries.
- At its admission, the Actuator reads the run's Job, within the same 1.2 s.
- Once the Job has a failed pod, the retry is required off spot. This is a `NotIn`, as an On-demand minimum is.
- So a run is interrupted once at most. With `parallelism` above 1, each pod of a run is interrupted at most once.
- Once the run has a failed pod, every pod it starts after goes off spot. Not only the retry.
- A CronJob's runs get no deletion cost, since a Job scales by none. Nothing is counted for them.
- A run's pod Failed because it was evicted to drain its node (`EvictionByEvictionAPI`, how Karpenter empties a spot node) is the Job's retry. It is no Rollback.

Events on the CronJob:

- `SpotPlaced` or `SpotSkipped`, from the minute's read. Never both: one skipped is never said placed.
- Again on a change.
- `SpotEnded` when it leaves the list.
- Its pause annotation is read.
- Nothing is done under the Kill switch.

No new permission: the CronJob and Job `get` it already has.

### Autoscalers

- Karpenter launches on-demand where a NodePool allowing both has no spot.
- On Cluster Autoscaler, a preference isn't counted when it picks a node group to grow. So whether new nodes are spot is its expander's setting.

## Stopping it: pause, Hub down

### Pausing one Workload

A person in the client's cluster can stop the Actuator on one Workload. That covers a Deployment, StatefulSet, DaemonSet, Argo Rollout or CronJob. They do it with an annotation:

```bash
kubectl -n shop annotate deployment api squero-actuator/pause=true   # --overwrite ... =false, or remove it, to lift it
```

The Actuator reads the annotation each minute. While it is set:

- nothing is resized on that Workload;
- no approval is applied, no Automation runs, and nothing catches up;
- a rollout stops where it is;
- the webhook lets its new pods start from their template, as under the Kill switch. There is no Placement either.
- A rollback still happens. Nothing is reverted at once.
- An approval meanwhile waits. It applies once the pause is lifted, if it is still current.

To stop the Actuator everywhere, scale the Actuator to zero. The webhook fails open.

### When the Hub doesn't answer

- While the Hub doesn't answer, the Actuator keeps what it holds.
- It changes nothing new.
- Rollbacks and hand-backs go on.
- After 15 minutes, it says so once. It writes a `HubUnreachable` Warning Event on its ConfigMap, in its own namespace.
- To see it:

```bash
kubectl -n squero-actuator get events
```
