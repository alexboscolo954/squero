# The client's cluster: Relay and Actuator

Two optional parts run inside a client's cluster, from the Hub's own image. Each dials out to the Hub over HTTPS, so the client opens no inbound port.

- **Relay**: for a cluster the Hub can't reach. It carries the Hub's reads (and, only if the client allows it, the two gated actions) and the cluster's alerts.
- **Actuator**: applies the Sizings an Operator approved, in place, and rolls one back if it hurt the pods.

## What each one may do

| | Relay | Actuator |
|---|---|---|
| Namespace | `squero-relay` | `squero-actuator` |
| Reads | read-only ServiceAccount, bound per namespace, like the Hub's own | pods, Deployments, ReplicaSets, StatefulSets, DaemonSets, Argo Rollouts, CronJobs, Jobs, HPAs, VPAs, PodDisruptionBudgets, LimitRanges and ResourceQuotas in the Contract's namespaces; Events in `default`, where a Node's are (for `reflexes.oom`; its own Role, which a client may remove) |
| Writes | none, unless the client also applies `deploy/relay-actions`: evict pods and scale deployments only | resize pods (`pods/resize`, a RoleBinding per namespace), evict pods (`pods/eviction`, only with the Contract's eviction on), create Events, read and replace its own ConfigMap, patch its own webhook's `caBundle` |
| Token | `relay_token_secret` | `actuator_token_secret`, never a Relay's |
| Talks to | `wss://<the Hub's domain>/relay` | `GET /actuator/desired`, `POST /actuator/reports` |

Neither can change an image or a command. The full table is in [safety.md](safety.md).

## A cluster behind a Relay

A cluster in a client network the Hub can't reach sets `via: relay` and `relay_token_secret` (a secret in the Contract's SOPS file, 32 characters or more; the client puts the same value in its Relay's Secret). The Relay inside that network holds the cluster's credentials and dials the Hub's `/relay` websocket with that token, which binds it to this Contract's cluster and nothing else. Such a cluster names no `context`, kubeconfig, `in_cluster`, `allow_exec` or Prometheus URL or token here: the Hub never reaches it Direct, and refuses to start if a Relay token is another Relay's or equals the approver, MCP or a webhook token. The agent names it like any other cluster: its Kubernetes and Prometheus calls go to the Relay after the Hub's own cluster and namespace check, through the Hub's own tools (list, schemas, approval gate), so nothing else a Relay offers reaches the agent. A Run that calls it while its Relay is offline, or gets no answer within `SQUERO_RELAY_CALL_SECONDS`, ends Failed ("Relay offline"). `recent_changes` takes no namespace, so the Hub keeps only the changes in the Contract's namespaces (a GitOps notice: its GitOps namespaces), however many the Relay allows. The Hub's own reads go through it too: its hourly Sizings, node facts and nodes' Room (the one read that sums pods outside the Contract's namespaces, per node: [Nodes](optimizer.md#nodes)) and the Bill check's node-hours, as the Relay's `hub_promql_*`, when the Contract has a `prometheus` integration. Health shows each Relay connected (since when, and the version it reports) or offline (since when), with the Kubernetes and Prometheus checks made through it; the Agenda lists an offline Relay as data the Hub can't read. Its cluster's alerts come through it as well: the Hub takes them as from the Contract's own alert route, with the same webhook token, and nothing about them differs from there on.

```yaml
clusters:
  - name: edge
    namespaces: [shop]
    via: relay
    relay_token_secret: edge-relay-token
```

## Running a Relay

The client applies `deploy/relay` in its own cluster (namespace `squero-relay`): a read-only ServiceAccount bound per namespace, like the Hub's own, and one `squero relay` pod from the same image, with its token from a Secret they create (`kubectl -n squero-relay create secret generic squero-relay --from-literal=SQUERO_RELAY_TOKEN=...`). It serves the Hub's own tool names for its one cluster, so the Hub's allowlist and approval gate apply unchanged, plus `hub_promql_instant` and `hub_promql_range` for the Hub's own Sizing and Health queries, which the agent never gets. Write tools don't exist on it unless the client also applies `deploy/relay-actions`, which turns `SQUERO_RELAY_WRITES` on and adds RBAC for evicting pods and scaling deployments only; the Contract's `enable_actions` and your approval still apply on top. It dials `wss://<the Hub's domain>/relay`, a public route like the alert routes and the Actuator's two (Caddy passes only a websocket upgrade there). It dials again after any drop, waiting 1 s, doubling to a minute, and back to 1 s once a connection has held; a refused token logs "check the token" and keeps trying.

## Alerts through a Relay

The Relay's Service (`squero-relay.squero-relay:8080`) takes the cluster's own Alertmanager webhook at `POST /alertmanager`, with the Contract's webhook token as its bearer credentials, exactly as a Direct cluster's Alertmanager posts to the Hub. The Relay keeps no queue and no secret of its own for it: it sends each alert group over its connection to the Hub, which checks the token against the Relay's own Contract (never another's) and answers once the alert is stored. Alertmanager gets the Hub's answer: 202, 401 for a wrong token, 422 for something that isn't an alert group; 503 when the Relay has no connection to the Hub or no answer within 10 s, and 502 when the Hub failed to take it, so Alertmanager sends it again. A receiver for it:

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

| Relay setting | Default | What it is |
|---|---|---|
| `SQUERO_RELAY_HUB` | (required) | the Hub's `wss://<domain>/relay` |
| `SQUERO_RELAY_TOKEN` | (required) | the value of the Contract's `relay_token_secret` |
| `SQUERO_RELAY_CLUSTER` | (required) | the cluster's `name` in the Contract |
| `SQUERO_RELAY_NAMESPACES` | (required) | JSON list, as the RoleBindings grant |
| `SQUERO_RELAY_GITOPS_NAMESPACES` | `[]` | where Argo CD or Flux objects live (needs their read RBAC too: `kubectl apply -f deploy/relay/rbac-gitops.yaml`) |
| `SQUERO_RELAY_PROMETHEUS_URL` | none | the cluster's Prometheus inside the client's network |
| `SQUERO_RELAY_PROMETHEUS_TOKEN` | none | its bearer token |
| `SQUERO_RELAY_WRITES` | `false` | the client's switch for the two write tools |
| `SQUERO_RELAY_ALERTS_PORT` | `8080` | its Alertmanager receiver |

## Running an Actuator

A cluster whose Approved Sizings should be applied sets `actuator_token_secret` (a secret in the Contract's SOPS file, 32 characters or more, never a Relay's token; Direct and Relay clusters alike). The client applies `deploy/actuator` in that cluster (namespace `squero-actuator`): one `squero actuator` pod from the same image, a ServiceAccount that can read pods, Deployments, ReplicaSets, StatefulSets, DaemonSets, Argo Rollouts, CronJobs, Jobs, HPAs, VPAs, PodDisruptionBudgets, LimitRanges and ResourceQuotas and resize pods in the Contract's namespaces (a RoleBinding per namespace), create Events, read and replace its own ConfigMap, and patch its own webhook's `caBundle`, and a MutatingWebhookConfiguration whose `namespaceSelector` lists the same namespaces; its token comes from a Secret they create (`kubectl -n squero-actuator create secret generic squero-actuator --from-literal=SQUERO_ACTUATOR_TOKEN=...`). It refuses to start below Kubernetes 1.33. Every minute it pulls `GET https://<the Hub's domain>/actuator/desired`: its cluster's current Approved Sizings of Deployments in the cluster's namespaces, none while the Kill switch is on. Each one newer than what it applied to that container is applied once: every running pod of the Deployment gets the approved requests through the `resize` subresource, in place, never above the container's limit, and a container whose `resizePolicy` would restart for it is skipped. It keeps what it did in its ConfigMap `squero-actuator` (a restart applies nothing twice; beside it, the Kill switch as the Hub last said it and the reports the Hub hasn't taken yet, so a restart while the Hub is unreachable neither forgets the Kill switch nor loses a report), writes an Event on the Deployment (`SizingApplied`, `SizingSkipped`, `SizingFailed`) and reports each outcome to `POST /actuator/reports`, which the Hub keeps 90 days as the Contract's `actuations`; a report the Hub didn't take is sent again, unless it refused it (a 4xx other than 401, 403 or 429: logged and dropped). The Hub takes a report only for the namespaces of the token's own cluster, and refuses one with a number that isn't one (NaN, infinity) or is negative (422, naming the field). A report's time is the Actuator's own clock, never read as later than the Hub's when it arrived: one dated ahead can't stay a container's last Actuation, count as a Rollback or outlive its 90 days for longer than its arrival allows. What an Actuator sends (names, its reason, which can carry an API server's message) reaches Slack as text: it can't mention a channel or make a link.

```yaml
clusters:
  - name: prod
    context: prod
    namespaces: [shop]
    actuator_token_secret: prod-actuator-token
```

It resizes the pods of Deployments, StatefulSets and DaemonSets (the last two from 0.44.0) and Argo Rollouts (from 0.45.0), and holds CronJobs' (from 0.45.0, through its webhook only). The Actuator names the kinds it resizes on each pull, and the Hub sends it only those: one of 0.43.0 or older names none and gets Deployments only, one of 0.44.0 no Rollouts or CronJobs, so such an approval waits for its upgrade (re-apply `deploy/actuator/rbac.yaml` too: it reads StatefulSets, DaemonSets, CronJobs, Jobs and, read only, `rollouts.argoproj.io`). A Job of no CronJob is never sent: it runs once. A Rollout's pods are those of its own ReplicaSets, its stable and canary ones alike, resized in place as a Deployment's, never evicted; its template is its own, or its `workloadRef` Deployment's. Argo Rollouts not installed is none.

A CronJob's pods are never resized in place: an approved CronJob is held at once (`SizingApplied`, "held for its next run's pods") after the same pre-checks on its `jobTemplate` (a ResourceQuota counted for its `parallelism`), and the webhook gives each pod of its runs (a Job named `<cronjob>-<minute>`, as the CronJob controller names them: its scheduled time in minutes, at most 30 days old, so a Job of your own named `x-123` is never taken for CronJob `x`'s run) the held requests. Its first run under them is watched until it ends, 7 days at most: the first Job created since whose pods got them (a run whose pods all started from the template, the webhook missing them, doesn't count). A pod of that run with the held requests `Failed` or OOM-killed (`restartPolicy: Never` or `OnFailure`) is a Rollback: no longer held, so its next run's pods start from the template, and the usual backoff. A Revert or a hand-back lets it go the same way; no catch-up, no eviction.

| Actuator setting | Default | What it is |
|---|---|---|
| `SQUERO_ACTUATOR_HUB` | (required) | the Hub's `https://<domain>` |
| `SQUERO_ACTUATOR_TOKEN` | (required) | the value of the Contract's `actuator_token_secret` |
| `SQUERO_ACTUATOR_NAMESPACE` | (required) | its own namespace, for its ConfigMap (the manifest sets it) |
| `SQUERO_ACTUATOR_INTERVAL` | `60` | seconds between pulls |

## Holding requests: the webhook

What it applied it holds: its webhook gives a new pod of that Workload (a rollout, an Argo Rollout's canary step, a reschedule, a scale-up, a new node's DaemonSet pod) the same requests, never above the pod's limit, as long as the container still asks what its template asked when the Actuator applied. When a person changes those requests in the template, or the Deployment or its container goes, the Actuator hands the container back: a `HandedBack` Event, an Actuation `handed_back`, and new pods keep the template's requests from then on. A DaemonSet's new pod gets only the requests lower than its template's, since it must start on whatever node it lands: a raise reaches it in place once it runs (caught up as below), where its node has room. Where it has none (the resize `Infeasible`, refused by the API server, or `Deferred` for 10 minutes), that pod is left without its raise: its pending resize undone, the requests it lowers still set in place (a lowering always fits), and a `PodLeft` Event. The others keep the requests, new pods still get the lowered ones, and that pod isn't tried again until the next Sizing. The same goes for a DaemonSet's pods during a rollout (a canary left, the next pod is the canary); only when no pod's node has room does it fail. The webhook serves TLS with a certificate the Actuator makes at each start and puts in its webhook's `caBundle` (no cert-manager). It fails open: with the Actuator down, slow or scaled to zero, pods start from their template; while the Kill switch is on it changes no pod. Such a pod catches up once the Actuator may change things again (the Hub reachable, the Kill switch off): the pre-checks again, then a canary and one pod at a time, as for a new approval, ending in another `SizingApplied` (a pre-check that fails now ends the hold, `SizingSkipped`).

## Pre-checks and the rollout

Before a resize it skips, with the reason in its Event and Actuation, a change that would change the pods' QoS class (a Guaranteed container's limits would have to change too), a Workload a VPA sizes (any mode but `Off`), requests outside the namespace's LimitRange (minimum, maximum, limit-to-request ratio) and an increase that would pass a ResourceQuota; it never clamps. A resource an HPA of the Workload scales on by utilization (cpu for an HPA without metrics) is left to it, the other one still sized; the Hub shows such a Sizing as held by its HPA, with the `AverageValue` change that frees it ([Sizings held by an HPA](optimizer.md#sizings-held-by-an-hpa)). Then it resizes one canary pod and watches it 10 minutes, then the others one at a time, each once the kubelet allocated the last one and it is Ready (one per minute). A resize the kubelet calls `Infeasible`, or one not done in 10 minutes (`Deferred` included), stops it there as failed, unless the Contract turns eviction on ([Eviction](#eviction)); it never evicts another pod to make room. "Applied" is reported when every live pod has the new requests. The rollout goes on only while the Hub answers and the Kill switch is off. Only Deployments, for now. Each call to the API server is tried once and may take 15 seconds, its own minute being the retry; a failure that may pass (the network's, a 429, the API server's own 5xx) while it goes to the next pod is waited out, tried again each minute for 10 at most, before the rollout fails. An approval that meets such a failure before anything changed (reading the Workload, its checks, the canary's resize) isn't used up: it is tried again each minute, for 10 at most, and only then reported `failed`. A Workload scaled to zero mid-rollout ends it `applied` ("N pods resized in place, then none left to resize"): held, so the pods of its next scale-up start with the requests. An approval that is skipped or fails leaves what an earlier one holds as it is ("run N's requests stay held"): still given to new pods, still undone by a Revert.

## Rollback

From its first resize until an hour after the rollout ends, the Actuator watches the pods with its requests: a container that restarts (an OOM kill named as one) since it got them, or a pod it resized that stays NotReady for 2 minutes, rolls the Sizing back. Every pod with its requests goes back to the template's, in place and at once (a request the template doesn't set stays), with a `SizingRolledBack` Event and an Actuation `rolled_back` that say what went back to what ("2 pods back from cpu 115m, memory 91Mi to their template's cpu 1, memory 1Gi"), and a message in the Contract's operator channel (once, however often the report is resent). The Hub posts it after it has answered the Actuator, from what it stored, and again later until Slack takes it (5 minutes after a post Slack didn't take, then twice as long each time, an hour at most; for a week; one told more than ten minutes late says when it was reported), so Slack being down delays a Rollback's message and never loses it. At most 20 Rollbacks of one cluster are posted in an hour; past that, one line counts the rest and points to the Automation page. Then it applies no new Sizing of that container for 24 hours, 48 after a second rollback within 7 days, 96 after a third: an approval meanwhile is skipped with the date. CPU throttling is no reason (it is already a Sizing's risk). Nor is a pod whose node is gone (a node that failed, or a spot node taken back): Kubernetes then marks the pod NotReady while its containers' own readiness stays as the kubelet last wrote it, and removes it with the reason `DeletionByTaintManager` or `DeletionByPodGC`, or the kubelet ends it for a node shutdown; the Actuator tells that from the pod alone (it reads no node), counts neither its NotReady nor a CronJob run's pod that Failed that way, and never gives it a Sizing's requests: a rollout goes on without it (a canary lost inside its ten minutes was never watched, so the next pod resized is the canary), a catch-up leaves it until its node is back, and an approval that only such pods are missing is tried again for ten minutes. A container that restarted or was OOM-killed before its node went still counts, and a Rollback or a Revert still puts such a pod's requests back, for when its node returns. A CronJob run's pod that Failed because it was evicted to drain a node (`EvictionByEvictionAPI`) or by a node under pressure is not told apart from one a Sizing harmed, so it counts as before. A node that restarts and comes back starts its containers again: such a restart, which the container runtime reports as last ended with reason `Unknown` and exit code 255, is not the Sizing's either (a crash reads `Error`, an OOM kill `OOMKilled`, and both still count, then or later), and its 2 minutes of NotReady are counted from when its containers started again, not from when the node went. A CronJob run's pod that Failed in a node's restart is not told apart yet. It rolls back with the Hub unreachable too, never while the Kill switch is on. Pods that already had the requests when a rollout or a catch-up began are counted from then, so a restart from before is never the reason. A pod that refuses to go back (the API server busy) is tried again each minute, pod by pod, the Hub reachable or not, never under the Kill switch, until every pod is back, the API server refuses for good, or a person changes the template (`; N refused ...; tried again each minute` in the Actuation).

## Eviction

A resize that can never fit its node, because the node's allocatable is smaller than the new requests, is `Infeasible`: the kubelet marks it so on Kubernetes 1.33 and 1.34, and from 1.35 the API server refuses the resize at once (403, "node didn't have enough allocatable resources"); the Actuator treats both alike (a node that is merely full now makes it `Deferred`, which waits up to 10 minutes and then fails, as before). With eviction on, the Actuator evicts that pod instead, so its ReplicaSet starts a replacement that the scheduler places on a node with room and the webhook gives the new requests. It is off unless the Contract says so, in its `actuator:` block:

```yaml
actuator:
  eviction: true
  timezone: Europe/Rome          # IANA name; UTC when left out
  windows:                       # at least one when eviction is on
    - {days: [sat, sun], start: "00:00", end: "24:00"}
    - {days: [mon, tue, wed, thu, fri], start: "22:00", end: "02:00"}   # past midnight: the night is the day it starts
```

The Hub tells each of the Contract's Actuators, in `/actuator/desired`'s `eviction`, whether eviction is `off`, or on with its window `closed` or `open` now (`off` under the Kill switch). The windows gate eviction only: resizes in place go on at any time.

- **Outside a window** the rollout waits on that pod, without a time limit, and says so once in an `EvictionWaiting` Event. A newer Sizing, approved or under Automation, still replaces it; the pause annotation and the Kill switch stop it as any rollout. A Revert needs an Actuation of the container first, so a first rollout waiting for a window can't be reverted until it ends (a newer approval, the pause or the Kill switch can stop it).
- **Never evicted**: a StatefulSet's pod (it keeps its state; a zonal volume would pin its replacement), a DaemonSet's (it would start again on the same node), an Argo Rollout's (resized in place only), a pod of a Deployment with one replica (eviction would stop it), or one no PodDisruptionBudget in its namespace covers (nothing would keep enough pods up). Its rollout fails at once, window open or not, as with eviction off, and its Actuation says why. So does a failed read of the Deployment or its budgets.
- **Inside a window** it evicts through the Eviction API, so PodDisruptionBudgets hold: a budget that allows no disruption now keeps it waiting (one `EvictionWaiting` Event), any other refusal fails the rollout. A `PodEvicted` Event names the pod and the requests its replacement starts with.
- **Its Actuation** says so: the rollout's reason ends with `<pod> evicted, its resize Infeasible`. While a replacement is awaited, no new Sizing replaces the rollout (10 minutes at most).
- **One at a time**: never while another evicted pod's replacement is awaited, and at least 5 minutes after the last eviction, across the cluster. Only the pod whose resize is `Infeasible`, never another pod to make room.
- **The replacement** (a pod of the Deployment that wasn't there before the eviction), once Ready with the requests, stands for the evicted pod: watched for a Rollback like any pod it resized (an evicted canary's 10 minutes start again), and the rollout goes on. One that starts from its template (the webhook missed it) fails the rollout instead of evicting again. None Ready within 10 minutes is a Rollback (unless none is owed: the Deployment scaled down meanwhile and already has the pods it wants, and the rollout goes on), and a replacement still `Pending` (no node has room) is evicted again, holding nothing, so it starts from its template; a Pending pod's eviction ignores budgets.

Eviction needs a cluster with an `actuator_token_secret`, and the Actuator's RBAC in `deploy/actuator/rbac.yaml` grants `create pods/eviction` and reads `poddisruptionbudgets` per namespace (re-apply it when upgrading from 0.42.0: without that read, nothing is evicted). An Actuator of 0.42.0 or older never reads `eviction` and fails an `Infeasible` resize, as before.

## Reflexes: OOM kills and the Step-up

A Reflex is a bounded, expiring change the Actuator makes on its own for a Workload under Automation. The first, for OOM kills, is switched on per Contract:

```yaml
actuator:
  reflexes:
    oom:
      max: 2       # a Step-up never goes past this many times the memory limit in the Workload's template; 2 unless set, more than 1, 4 at most
      expires: 7d  # a Step-up ends this long after its last step unless it is still needed; 7d unless set, 1h to 90d
```

It needs a cluster with an `actuator_token_secret`. The Actuator tells each OOM kill of an automated container apart, reports it, and answers one at a memory limit with a **Step-up** ([ADR 0005](adr/0005-reflexes-may-raise-limits.md)): with the [Startup boost](#the-startup-boost) and the [Burst](#burst-cpu-throttling), the only times it changes a limit.

The Hub lists, in `/actuator/desired`'s `oom`, the cluster's containers under Automation (a Sizing ready or not; not of a Workload whose Automation three Rollbacks ended, nor of a container reverted since its last approval; none under the Kill switch). Each minute the Hub answers, the Actuator reads the pods of those namespaces once and tells each OOM kill of the last 10 minutes of a listed container, once:

- **container-limit**: the container has a memory limit and its node's kubelet said nothing. It reached its own limit, or one above it: its pod's, or the node's allocatable memory, which the kubelet sets as a limit on all pods together. Those read alike on the pod and bring no Event, so the Actuator can't tell them apart (the container runtime marks a container `OOMKilled` whichever limit caused the kill).
- **node-level**: the node itself ran out of memory, which the kubelet says in a `SystemOOM` Event on the Node within 2 minutes of the kill; or the container has no memory limit.

The pod reads the same in both (`OOMKilled`, exit code 137), so the Event decides. A Node's Events are in `default`: `deploy/actuator/rbac.yaml` grants a Role there with `get` and `list` on Events, which covers every Event in `default`, those of anything that runs there included. A client who would not grant that removes the Role and its RoleBinding; then, as on a cluster installed before this and not re-applied, a kill of a container with a memory limit counts as container-limit and its report says the node's Events were not read, and why.

Each container's kills that get no Step-up are one Actuation `oom_killed`, with the limit it had, how many pods, and which kind each was (five pods named, the rest counted). The Hub stores it with the others and, for now, shows it nowhere: the Automation page shows what a container holds as before, nothing is posted to Slack, and no Event is put on the Workload (the page and the Slack line come with the step-up). Until then a sighting is in the Actuator's log and the Hub's `actuations` table. A container is reported every 10 minutes at most, with its pods killed since the last report; a pod shows only its last kill, so one killed three times in that time counts once. At most 50 containers are reported in a minute, the others the next. The Actuator keeps when it last reported in memory only: after its restart the kills of the last 10 minutes are reported again, the same report stored once, one with a newer kill as a second row that counts the earlier pods again. It reports none while the Hub is unreachable or has not taken its earlier reports. Not seen: a kill the container runtime reports as `Error` with exit code 137 (it sometimes misses the kernel's event), which no one can tell from a `SIGKILL`. A paused Workload's kills are reported too: reporting changes nothing.

### The Step-up

A container-limit kill of a pod is answered within the minute: the container's memory limit and its memory request are raised together on every pod of the Workload, in place and at once, killed or not, and new pods start with them.

- **How much.** The limit to the larger of 1.2 times and 100 MiB more, in whole Mi; the request by the same factor, so a pod's QoS class stays (a Guaranteed pod's two values stay the very same). A request a Sizing had lowered is not kept: the step starts from the template's, or from a Sizing's higher one.
- **How far.** Never past `max` times the limit in the Workload's template. There it stops, and each further kill is reported with "raise it in its manifest, or look for a leak": no Rollback, and Automation goes on.
- **How often.** One Step-up a container every 10 minutes, and 10 a cluster an hour. Only a kill of a pod already at the container's current step earns the next; a pod below it (started from its template since) is brought to it and no further.
- **Not made**, and said with the kill ("No Step-up: ..."): on a paused Workload; under a VPA in any mode but `Off`; where an HPA scales on its memory; where its `resizePolicy` restarts the container for memory (every replica at once); where its template sets no memory limit; while a Sizing of that container still rolls out; past a LimitRange's maximum or a ResourceQuota's `limits.memory` or `requests.memory`; and never under the Kill switch, with the Hub unreachable, or while reports the Hub hasn't taken wait.
- **Its pods.** One pod's refusal (its node has no room) leaves the others their step, and a pod whose node is gone is not touched. A step no pod took is none: not kept, nor one of the hour's ten. A pod counts as at a step by the limit the kubelet says it gave it, not the one it was asked (on Kubernetes 1.33 and 1.34 a resize the node can't fit stays asked).
- **New pods.** The webhook starts a new pod at the container's Step-up, its limit and its request both, where the pod still asks what the template asked when the step was made (a template that sets only a limit asks that much), and only if its QoS class stays. Never under the Kill switch or for a paused Workload. A DaemonSet's new pod is not raised at its creation (it must fit whatever node it starts on) but in place, the minute after. Each minute the Hub answers, a pod that asks less than its container's Step-up (the webhook was down) is brought to it in place; one whose node refuses is tried again each minute, logged and not reported. A pod is only ever raised: one above the step (from a Step-up dropped since) keeps its own. On Kubernetes 1.33 and 1.34 a resize the node can't fit is accepted and stays pending: the minute after, that pod is put back to what it was given and left as it is for good (said once, "left as they were: their node has no room"), so that a Sizing's rollout never reads the pending resize as its own Infeasible pod and evicts it.
- **CronJobs.** A run's pod is never resized in place: the Step-up is for the next runs' pods, from the webhook. A kill in the first run a Sizing is watched over is the Reflex's too, on the same terms as below.
- **The Sizing beside it.** The container's memory is the Step-up's from then on: a held Sizing keeps its cpu, and no later Sizing sets that container's memory while the Step-up lasts (its Actuation says "memory left to its Step-up"; one that asks memory only is skipped, said once).
- **Its end.** A person changing the memory limit or request in the Workload's template, or the Workload gone, ends it within the minute (an Actuation `step_ended`, an Event `HandedBack`; no Sizing's hand-back, so what the container holds of a Sizing stays held and revertable): new pods start from the template again, and running pods keep their memory until they start again. A Revert drops it the same way.
- **Rollback.** With the Reflex answering, an OOM kill is no longer a Sizing's Rollback, in its watch hour or out of it, with two exceptions: a kill under a Sizing that lowered that container's memory request, in its watch hour, is a Rollback as before and never a Step-up (a template that sets only a memory limit asks that much); and a node-level kill of a container whose memory request the Actuator holds lowered is a Rollback at any time. Any other node-level kill is only reported. Where the Reflex doesn't look (the Hub unreachable or older than this, the container not listed, reports waiting), an OOM kill in a watch hour is a Rollback as before. A crash that is no OOM kill is still the Sizing's, and so is a pod NotReady whose OOM kill dates from before it got the Sizing's requests. A kill the Reflex looks at and declines (an HPA on memory, its cap) is reported, with no Rollback and no Step-up.

Each Step-up is an Actuation `stepped_up` (its `memory` the new limit) and an Event `MemorySteppedUp` on the Workload; pods brought to one later are a `stepped_up` too, which says so. Its values and the hour's count are kept in the Actuator's ConfigMap, so a restart forgets neither.

### How a Step-up ends, and where it shows

A Step-up is not the Workload's new size: it ends by itself.

- **Its term.** `expires` after its last step (7 days unless the Contract says; an hour to 90 days), the Actuator asks whether it is still needed. The Hub sends, with each listed container, its memory peak over its Sizing's window. A peak above the limit in the Workload's template: the Step-up is kept for another term (said in an Actuation, so the Automation page still knows a Step-up that outlives the 90 days the Hub keeps Actuations). A peak the Hub doesn't know: kept too. A peak within that limit, or a container the Hub no longer lists (its Automation ended, or the Reflex was switched off): it ends, with an Actuation `step_ended` and an Event `StepUpExpired`. The term is kept with each Step-up, so one still ends once the Reflex is off and the Hub sends no term (a Step-up that never heard a term, from a Hub older than this, never expires).
- **Its pods, then.** New pods start from the template again. Running pods are put back to the template's memory limit and request in place only where the Hub's peak says they fit under it, so that no resize is left hanging above what a pod uses and no pod is killed for it; otherwise they keep theirs until they start again. The peak is the Hub's last hourly reading, sampled, and the lowering is tried once: a pod that grew since keeps its resize in progress (Kubernetes 1.34 on) until it fits, and a pod that refused is not asked again. Kubernetes 1.33 refuses to lower a memory limit in place: said pod by pod, and those pods keep theirs. A Revert does the same with the Step-up it drops. A person's change to the template rolls the pods anyway.
- **In Slack.** Each Step-up made is one line in the Contract's operator channel, with what to write in the manifest to keep it ("set its memory limit to 228Mi"); a container still OOM-killed at its cap is one line a day. Both go through the same teller as Rollbacks: after the Actuator is answered, again until Slack takes them, and under the same 20 posts a cluster an hour. Pods only brought to a Step-up, an OOM kill only seen, and a Step-up's end are stored and not posted.
- **On the Automation page.** A container's row shows its Step-up's memory limit beside what it holds of a Sizing, and a container with only a Step-up is a row too. Revert is offered for either, and drops both.
- **In a Triage's thread.** When a Triage ends, the Hub adds one line above its Report for each Step-up made, from an hour before the Triage began, on a Workload its Alerts name (by a `deployment`, `statefulset`, `daemonset`, `rollout` or `cronjob` label, or a `pod` label that begins with the Workload's name, in the same namespace). The Hub writes it from what it stored; the model never sees it (the Hub never reads a thread's messages back). A Triage that ends twice says it twice.

### Burst: CPU throttling

The third Reflex, a **Burst**, raises the CPU limit of a container that is being throttled ([ADR 0005](adr/0005-reflexes-may-raise-limits.md)). The Actuator can't see throttling (it is in Prometheus, or behind a kubelet permission too wide to grant), so the Hub decides from its hourly read and the Actuator makes the change.

```yaml
actuator:
  reflexes:
    burst:
      max: 2         # a Burst never goes past this many times the CPU limit in the Workload's template; 2 unless set, more than 1, 4 at most
      expires: 24h   # a Burst ends this long after its last step unless it is still needed; 24h unless set, 1h to 7d
```

It needs a cluster with an `actuator_token_secret`.

**What the Hub reads and tells**

- **The trigger.** With each hourly Sizing run the Hub also reads every container's share of CPU periods throttled over the last hour (two more Prometheus reads a cluster for each policy window in use). More than a quarter means a Burst is due. A Workload's pods are pooled, so one hot pod among ten quiet ones reads low. A container that ran fewer than 600 periods in the hour, its pods together (a minute's worth at the default 100 ms), is not told at all: one throttled in the only second it ran is no Burst. A container without a CPU limit has no such periods and is never told.
- **The peak.** Its highest CPU use over the last `expires` (or its policy's window, when that is shorter), from the samples the Sizing already reads. Those are 5-minute rates of one pod (coarser under a policy window of more than a month), so short spikes read well under the limit: the peak says a container is under sustained load, not that it spiked.
- **What the Actuator is told.** `/actuator/desired`'s `burst` lists the cluster's containers under Automation, as `oom` does but never a CronJob's: each with `throttled` (its share, only when over a quarter) and `peak`; `burst_run`, the Sizing run they were read in; and `burst_max` and `burst_expires`, the Contract's. A read more than two hours old (Prometheus down, the Hub restarted late) gives neither share nor peak: an hour long past is no reason to raise a limit.
- **What it needs.** cAdvisor's `container_cpu_cfs_throttled_periods_total` and `container_cpu_cfs_periods_total` in the cluster's Prometheus. They are optional for Sizings (`squero sizing show` names them when missing); without them no Burst is ever due, and nothing else says so.
- **Where it is kept.** In the container's stored Sizing row, beside its Sizing, only while the Contract sets the Reflex. The console, the digest and the model never read it.

**What the Actuator does**

- **The step.** For each listed container the read says is throttled, once a read: its CPU limit goes up by a quarter of its template's limit (200m: 250m, 300m, 350m, 400m), on every live pod in place. Requests are untouched, so nothing is rescheduled and an HPA or the Cluster Autoscaler reads the same requests. Never past `max` times the template's limit.
- **How often.** One Burst a container each read, and never two within 45 minutes (a `squero sizing refresh` minutes after the hourly run shares its throttled minutes). Bursts and Step-ups together are 10 a cluster an hour at most, Bursts 5 of them: an OOM crash loop's Step-up is looked at first each minute and never waits behind throttling.
- **Not made**, and said as an Actuation `throttled` ("No Burst: ..."), a reason that stands said once a day: a paused Workload; a Guaranteed pod (its limit can't leave its request); a template without a CPU limit; a `resizePolicy` that restarts the container for cpu; a VPA in any mode but `Off`; while a Sizing of that container still rolls out (tried again each minute, so the Burst follows the rollout by a minute, not an hour); past a LimitRange's cpu maximum or limit-to-request ratio; in a namespace whose ResourceQuota counts `limits.cpu`, whatever room it leaves (an HPA answering the same load makes new pods, each asking the Burst's limit); at its cap; the hour's 10, or its 5, used; no live pod, or every pod refused it. Never under the Kill switch, with the Hub unreachable, while reports the Hub hasn't taken wait, or for a Hub older than this.
- **New pods.** The webhook starts a new pod at the container's Burst, while the pod's own limit is still what the template's was when the Burst was made, and only if its QoS class stays; never a pod with pod-level resources. A [Startup boost](#the-startup-boost) multiplies the Burst's limit and gives that back, not the template's. Each minute the Hub answers, a pod below the Burst (the webhook was down) is brought to it in place; a pod above it is never lowered.
- **How it ends.** `expires` after its last step the Actuator asks whether it is still needed: a CPU peak above 90% of the template's limit, or one the Hub doesn't know, keeps it for another term (said in an Actuation); a peak within that, or a container the Hub no longer lists (its Automation ended, or the Reflex was switched off), ends it, and every pod above the template's limit is put back to it in place, which needs no room and restarts nothing. The term is kept with each Burst, so it still ends once the Reflex is off. A person changing the CPU limit in the template (to the value Slack named, or any other) ends it at once and touches no pod: the rollout is theirs. A [Revert](#revert) drops it and lowers its pods. The read that was handled when it ended makes no new one, nor does a Revert followed by an approval within that read.
- **What it reports.** `burst` for one made (its `cpu` the new limit), for pods brought to one and for a term renewed; `burst_ended`; `throttled`. An Event `CpuBurst` on the Workload, `BurstExpired` at its term's end, `HandedBack` when the template changed. Its values and the last read handled for each container are kept in the Actuator's ConfigMap.
- **In Slack.** Each Burst made is one line in the Contract's operator channel with what to write in the manifest to keep it ("set its CPU limit to 250m"); a container still throttled at its cap is one line a day. Pods only brought to a Burst, a renewal, its end and any other "No Burst" are stored and not posted. Same teller and same 20 posts a cluster an hour as Rollbacks and Step-ups.
- **On the Automation page.** A container's row shows its Burst's CPU limit beside what it holds of a Sizing and its Step-up; a container with only a Burst is a row too. Revert is offered for it and drops everything the Actuator holds of that container.
- **In a Triage's thread.** A Burst made on a Workload the Triage's Alerts name, from an hour before it began, is a line above the Report, as a Step-up is.

Know before you set it: a Burst raises what a container may use, not what it is given, so on a full node it changes little; and the peak is of 5-minute rates, so a container throttled only in short spikes is burst, ends at its term, and is burst again, with a Slack line each time. Upgrade the Hub before its Actuators: an Actuator's `burst` report to a Hub older than this is refused whole.

### The Startup boost

A container that needs more CPU to start than to run (a JVM, a cache to warm) can have a higher CPU limit for its start only. It is a factor on a Sizing rule, beside `automate`, so it needs no `reflexes` block:

```yaml
sizing:
  rules:
    - {namespace: shop, name: "checkout-*", policy: production, automate: true, startup_boost: 2}   # 1.5 to 4
```

- **What it does.** The webhook starts each new pod of the rule's containers with its CPU limit times the factor and writes the container's own limit in the pod's annotation `squero-actuator/startup-boost`. Each minute the Actuator gives a boosted container its own limit back in place, once its pod is Ready, the container has run for 5 minutes, or it restarted, whichever is first (so up to a minute after), with an Event `StartupBoostEnded` on the pod. The 5 minutes count from the container's own start, so pulling its image and its init containers take none of them. A restart never gets a boost: a container that restarts while boosted loses it, and one that restarts later starts from its own limit.
- **Requests untouched.** Only the CPU limit changes, so the pod is scheduled as without the boost and an HPA or the Cluster Autoscaler reads the same requests. A container that sets a CPU limit and no request gets that limit written as its request, which is what Kubernetes makes of it anyway.
- **Which containers.** The Hub lists, in `/actuator/desired`'s `boosts`, the cluster's containers under Automation whose rule sets the factor: those it has stored a Sizing for, ready or still learning; not of a Workload whose Automation three Rollbacks ended, nor of a container reverted since its last approval. The Actuator keeps the list in its ConfigMap, so boosts go on while the Hub is unreachable and after its restart. Remove the factor from the rule and they stop the minute the Hub answers.
- **Not boosted.** A Guaranteed pod (its limit can't leave its request); a container without a CPU limit (it needs none); a CronJob's run; a container whose `resizePolicy` restarts it for cpu (giving the limit back would restart it); a paused Workload; a Workload under a VPA in any mode but `Off`; a pod that already carries the annotation; a pod with pod-level resources (`spec.resources`), whose limit a container's may not pass. And every pod of a namespace whose LimitRange sets a cpu maximum or limit-to-request ratio, or whose ResourceQuota counts `limits.cpu`: either could refuse the boosted pod's creation, so the whole namespace gets none, whatever room they leave. A VPA or such a namespace is said once in the Actuator's log (`no startup boost in ...`).
- **The Kill switch and a pause.** No pod is boosted, and a pod boosted before keeps its higher limit until the switch is off or the pause lifted: the Actuator resizes nothing meanwhile.
- **Where it shows.** On the pod only: the annotation and the Event. It happens at every start, so it is no Actuation, is not posted to Slack, is not on the Automation page, and does not count in the 10 Step-ups a cluster an hour.
- **The annotation is read as anyone's word.** The Actuator only ever lowers a CPU limit to the value it names, never under the container's request. It can't remove the annotation (it may not edit pods), so it stays on the pod.
- **Know before you set it.** A JVM, and any runtime that sizes its thread pools from the CPU count it sees at start, sizes them from the boosted limit and keeps them once the limit is back. And a policy engine that caps CPU limits or the limit-to-request ratio (Kyverno, Gatekeeper, a ValidatingAdmissionPolicy) can refuse a boosted pod's creation: the Actuator reads LimitRanges and ResourceQuotas, not those.

An Actuator older than this ignores `boosts` and `burst`; a Hub older than this sends neither.

Upgrade the Hub before its Actuators, and roll Actuators back before the Hub: an Actuator's report with an outcome its Hub doesn't know is refused whole and dropped, the reports beside it too.

## Automation

A Sizing rule with `automate: true` ([Sizing policies](optimizer.md#sizing-policies)) is the Operator's standing approval for the containers it picks (ADR 0004): `/actuator/desired` then also lists, apart from the Approved Sizings, the newest Sizing of each such container on the token's cluster that is ready and changes something, and the Actuator applies it unasked, through the same pre-checks, canary and rollback. A Sizing that is learning again after a gap in Prometheus ([Sizings](optimizer.md#sizings)) is not listed: nothing new is applied, and the Actuator keeps what it holds. An Approved Sizing of the same container goes first. The churn rule: a change under 10% of what its pods ask now is ignored, an increase applies at once, and a decrease at most once a day per container (an increase beside a waiting decrease goes alone). A new one waits while that container's rollout is under way or it backs off after a Rollback, without a word; one the pre-checks skip (a VPA, a quota) is said once, in its Event and Actuation, until its reason changes, and what the Actuator holds stays held. Its Events say `(Automation, run N)`. A Sizing that leaves one resource alone, approved or not, keeps the value held for it.

Three Rollbacks of one Workload in 7 days end its Automation: the Hub lists none of its Sizings until the oldest of them is 7 days old, and the operator channel hears it once, with that date, in the message of the Rollback that ended it. To end it for good, remove `automate` from its rule. An Actuator of 0.40.0 or older never reads Automation's list, so it applies only approvals.

## Revert

The Operator's Revert, from the console's Automation page or `revert_sizing` on the Hub MCP server ([Reverting a Sizing](running-the-hub.md#reverting-a-sizing)), reaches the Actuator in `/actuator/desired`'s `reverts` until it reports it done. It puts every pod with the requests it holds for that container back to the template's, in place and at once (a request the template doesn't set stays), as a rollback does but with no backoff, drops its Step-up and its Burst, and holds that container no more: its webhook leaves new pods alone and nothing catches up. A `SizingReverted` Event and a `reverted` Actuation say what went back to what; with nothing held, the Actuation says so. A paused Deployment's Revert waits for the pause to lift. An approval applied before stays reverted; a later approval applies, and resumes Automation of that container.

## Stopping it: pause, Hub down

A person in the client's cluster stops the Actuator on one Workload (a Deployment, StatefulSet, DaemonSet, Argo Rollout or CronJob) with an annotation:

```bash
kubectl -n shop annotate deployment api squero-actuator/pause=true   # --overwrite ... =false, or remove it, to lift it
```

The Actuator reads it each minute: nothing is resized on that Workload (no approval, no Automation, no catch-up, a rollout stops where it is), and its webhook lets its new pods start from their template, as under the Kill switch. A rollback still happens, and nothing is reverted at once. An approval meanwhile waits, and applies once the pause is lifted if it is still current. To stop it everywhere, scale the Actuator to zero (the webhook fails open).

While the Hub doesn't answer, the Actuator keeps what it holds and changes nothing new (rollbacks and hand-backs go on). After 15 minutes it says so once, a `HubUnreachable` Warning Event on its ConfigMap in its own namespace (`kubectl -n squero-actuator get events`).
