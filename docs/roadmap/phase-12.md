# Phase 12: Actuator

**In short:** The Actuator is a small process in each client cluster. It resizes pods in place to the Sizings the Hub approved, and hands them back when a person changes a template. It never changes images or commands, and a Kill switch stops it.

**Status:** built. 12.1 to 12.11 are built. Their live checks on throwaway kind clusters cover the done-when.

**Goal:** resize pods in place to approved Sizings, with rollbacks, pauses and a fail-open webhook, and never widen what the Actuator can change.

**Done when:** on kind 1.33 or later, an approved policy resizes a Deployment in place without restarts, a PodDisruptionBudget blocks an eviction, a bad Sizing is rolled back, and pods still start with the Actuator scaled to zero.

| Slice | What | Status |
|---|---|---|
| [12.1](#121-thin-slice-resize-in-place) | Thin slice: the Actuator resizes pods in place | built 2026-11-09, checked live 2026-11-10 |
| [12.2](#122-the-pod-creation-webhook) | Pod-creation webhook keeps the held values | built 2026-11-09, checked live 2026-11-10 |
| [12.3](#123-pre-checks-canary-and-rollout) | Pre-checks, canary, one pod at a time | built 2026-11-10, checked live 2026-11-10 |
| [12.4](#124-rollback) | Automatic rollback, Slack | built 2026-11-10, checked live 2026-11-10 |
| [12.4b](#124b-catch-up) | Catch-up for pods started from the template | built 2026-11-10 |
| [12.5](#125-automation) | Automation | built 2026-11-10 |
| [12.6a](#126a-revert-through-the-hub-mcp-server) | Revert through the Hub MCP server | built 2026-11-10 |
| [12.6b](#126b-the-automation-page) | Automation page with Revert | built 2026-11-10 |
| [12.7](#127-eviction-behind-the-flag) | PDB-safe eviction behind the flag | built 2026-11-11, checked live 2026-11-11 |
| [12.7b](#127b-never-evict-some-pods) | Never evict some pods | built 2026-11-11 |
| [12.8a](#128a-statefulsets-and-daemonsets) | StatefulSets and DaemonSets | built 2026-11-11, checked live 2026-11-11 |
| [12.8b](#128b-argo-rollouts) | Argo Rollouts | built 2026-11-11, checked live 2026-11-11 |
| [12.8c](#128c-cronjobs) | CronJobs, through the webhook only | built 2026-11-11, checked live 2026-11-11 |
| [12.9a](#129a-the-audits-workload-findings) | Audit findings that can harm a Workload | built 2026-11-11, checked live with 12.9b |
| [12.9b](#129b-the-audits-narrower-findings) | Narrower audit findings | built 2026-11-11, checked live 2026-11-11 |
| [12.9c](#129c-the-hubs-side-of-the-reports) | Hub-side findings on the Actuator's reports | built 2026-11-11, not checked live |
| [12.10](#1210-a-lost-node-is-not-a-sizings-fault) | A lost node is never a Sizing's fault | built 2026-11-12, checked live 2026-11-12 |
| [12.11](#1211-a-nodes-restart-is-not-a-rollback) | A node's restart is not a Rollback | built 2026-11-12, checked live 2026-11-12 |

## Decisions (grill, 2026-11-09)

Three rounds, all answers the recommended ones.

- **Shape.** Its own Deployment in each client cluster, using the Contract's `actuator_token_secret`. Its own ServiceAccount and token, bound to the Contract and cluster, so the Relay keeps its read-only identity. Works for Direct clusters too.
  - Refuses to start below Kubernetes 1.33.
  - Pulls `GET /actuator/desired` every minute and reports to `POST /actuator/reports` over HTTPS through the Hub's public boundary. The Hub never pushes.
  - Amends [ADR 0004](../adr/0004-standing-approval-for-sizing.md)'s "over the Relay connection".
- **No CRDs.** The Hub is the only source of truth. A client's GitOps cannot read the private contracts repo, and Approved Sizings never pass through git. CRDs would be a second copy our own code keeps in sync.
  - One ConfigMap in its own namespace holds what it applied. Events go on the Workloads; the Hub keeps the audit trail.
  - RBAC: `patch pods/resize`, `create pods/eviction` (12.7), its ConfigMap, `create events`, its webhook's `caBundle`. Never `patch deployments`, which could change images.
- **What it changes.** Requests only; limits stay `keep`. Never above the limit, never a QoS class change (skipped, with an Event). In place first; the fail-open webhook gives new pods the held values.
  - Eviction only behind the Contract's flag (off), inside a window, only where an in-place resize is `Infeasible`, one pod at a time, at least 5 minutes apart. It uses the Eviction API so PodDisruptionBudgets hold. It never evicts to make room.
- **How long values last.**
  - An applied Sizing stays held after it stops being current, until a later approval, Automation or a Revert (which resizes the container back to the template's values in place).
  - "Current" means waiting to be applied. A Sizing superseded before it was applied never is.
  - A person changing the template's requests wins: the container is handed back (`HandedBack`).
  - While the Hub is unreachable it keeps what it holds and changes nothing new. An event is raised after 15 minutes.
  - The Kill switch stops resizes and evictions; the webhook lets pods through unchanged. Nothing is reverted at once.
  - Local stop: scale the Actuator to zero (the webhook fails open), or annotate a Workload `aiops-actuator/pause: "true"` (`squero-actuator/pause` since the rename in 0.37.0).
- **Safety.**
  - An HPA scaling on a resource's utilization: that request is left alone. A VPA in any mode but `Off`: the Workload is skipped.
  - Over a ResourceQuota or outside a LimitRange: skipped, never clamped.
  - A canary runs 10 minutes, then the rest one at a time. `Infeasible` stops that Workload; `Deferred` waits 10 minutes.
  - A rollback follows a resize within an hour on an OOM kill, a restart, or 2 minutes NotReady. The container goes back in place, and no new Sizing of it follows for 24 hours, doubling each time. Three rollbacks in 7 days end Automation of the Workload.
  - "Adoption threshold" is dropped: the canary covers it.
- **Automation.** `automate: true` on a Sizing policy rule, under the Contract's `actuator:` block (`eviction`, `timezone`, `windows`, which gate eviction only). Changes under 10% are ignored. Increases apply at once; decreases at most once a day per container.
- **Seen.** The Hub's `actuations` table (90 days), the console's Automation view with Revert, and `revert_sizing` on the Hub MCP server. Slack gets only a rollback or the end of Automation. Resizes were to go in the daily digest; the Sizing digest is weekly (see 12.4).
- **Dropped.** CRDs and their rendering from the contracts repo. KWOK went to Deferred. The kind live check came after 12.4.

## 12.1 Thin slice: resize in place

**What:** `aiops actuator` with its own token. It pulls over HTTPS, honours the Kill switch, and resizes an Approved Sizing on a Deployment's pods in place. It writes its ConfigMap, Events and the Hub's `actuations` trail.

**Built (2026-11-09):**
- `actuator_token_secret` is 32+ characters and never another token; the Hub refuses to start otherwise.
- `GET /actuator/desired` returns the cluster's Approved Sizings of Deployments in its namespaces, none under the Kill switch. `POST /actuator/reports` stores Actuations (migration 0026, kept 90 days; a resent report is kept once; a foreign namespace is refused whole, 422). Both are public through Caddy.
- Every live pod of the Deployment's ReplicaSets is resized in place, never above the limit. Pods whose `resizePolicy` would restart them are skipped, as is a container with no live pod. Pods that cannot be listed fail that Sizing alone.
- Each approval newer than what it applied to that container is applied once, whatever the outcome, and reported until the Hub takes it. A 4xx other than 401, 403 or 429 is dropped with an error, so later reports are not stuck behind it.
- Thin on purpose: "applied" means the API server said yes; all pods at once; Deployments only; a refused approval waits for the next one; new pods start from the template until 12.2. No template requests are kept yet, because 12.2's hand-back needs them.

**Checked:** an integration test on kind resizes `inventory` and sees the kubelet allocate the request without a restart. It skips below 1.33 and passed in CI on kind v1.35.0.

## 12.2 The pod-creation webhook

**What:** the pod-creation webhook (`deploy/actuator/webhook.yaml`, `failurePolicy: Ignore`, 2 s timeout) for CREATE of pods in the Contract's namespaces.

**Built (2026-11-09):**
- A new pod of a Deployment whose container is held gets the applied requests (a JSON Patch, never above the limit), only while the pod still asks what the template asked when the hold was applied.
- Never touches `kube-*` or its own namespace. Does nothing under the Kill switch. The pod is created whatever goes wrong.
- Finds the Deployment from the ReplicaSet and `pod-template-hash`, with no API call. Self-signed TLS, made at each start and patched into its own `caBundle` (RBAC on that object only).
- Each minute, Hub reachable or not, a held container is handed back when its template's requests changed or its Deployment or container is gone: Event `HandedBack`, Actuation `handed_back` (migration 0027), never held again. A hold with no recorded template (from 12.1) is handed back.

**Found by the smoke test:** the API server calls `/mutate?timeout=2s`, which the webhook first answered with 404, so it failed open.

**Tests:** `demo/actuator-smoke.sh` in `make deploy-smoke`, on kind with the real image and manifests and no Hub.

**Checked:** a replaced pod of a held Deployment came up at the held 37m, and at the template's 10m with the Actuator at zero.

## 12.3 Pre-checks, canary and rollout

**What:** checks before any resize, each a skip with its reason and never a clamp. Then a rollout, held as `rolling`.

**Built (2026-11-10):**
- Pre-checks: QoS class would change (a Guaranteed container needs its limits changed too); a VPA in any mode but `Off` (none installed means none); the LimitRange's container minimum, maximum and limit-to-request ratio; an increase, times the live pods, passing a ResourceQuota (`requests.<r>` or `<r>`; a decrease never does); an HPA on a resource's utilization (`Resource`, or `ContainerResource` for this container; cpu assumed for a resource with no metrics).
- Rollout: one canary watched 10 minutes, then the others one at a time, each once the kubelet allocated the last (no `PodResizePending` or `PodResizeInProgress`, container status at the new requests, pod Ready).
  - `Infeasible`, or 10 minutes without that (`Deferred` included), fails the rollout. Resized pods stay as they are; rollback is 12.4.
  - A pod replaced meanwhile is done; its replacement is still to do.
  - Goes on only while the Hub answers and the Kill switch is off. A template change mid-rollout hands the container back.
- Applied is reported once every live pod has the requests. The Actuator reads RBAC for HPAs, VPAs, LimitRanges and ResourceQuotas; the webhook keeps a pod's QoS class too.

**Known limits:** one pod a minute after the canary, so a 50-pod Deployment takes about an hour (ponytail). Init containers are left out of the QoS class.

**Checked:** on the kind lab (v1.37), the integration test waited for the kubelet's verdict and passed.

## 12.4 Rollback

**What:** each resized pod's restart count is kept when it is resized, and its pods are watched from the canary until an hour after the rollout ends (applied or failed).

**Built (2026-11-10):**
- Rollback when a container with the Actuator's requests restarted since the resize (OOM kill included; a new webhook pod counts from zero), or a pod it resized is NotReady for 2 minutes.
- Every pod goes back to the template's requests, in place, at once. A request the template does not set is left, and the reason says so.
- Posts `SizingRolledBack` (Warning) and Actuation `rolled_back` (migration 0028). Backoff: no new Sizing of that container for 24 hours, doubling per rollback within 7 days; an approval in the backoff is skipped with the date. Rollbacks survive new approvals.
- Works with the Hub unreachable; never under the Kill switch, because a resize is a change. The Hub posts each new `rolled_back` once to the operator channel, however often it is resent.
- The integration test rolls `inventory` back to its template's 10m on kind, with no restart.

**Not built from the grill:** "3 rollbacks in 7 days end Automation" waits for 12.5. "Resizes in the daily digest": decided 2026-11-10 as one line in the weekly digest, slice A13, since the Sizing digest is weekly.

**Checked live:** 12.1 to 12.4 on 2026-11-10 (v0.38.0, a Mac Hub and the kind lab, no model calls).
- `app` approved at 115m and 91Mi, from 1 core and 1Gi, on 3 pods: the canary went first, the others after its 10 minutes and one minute apart, with the same pod UIDs and no restarts.
- A deleted pod came back at 115m from the webhook, where the template said 1 core. With the Kill switch on, a new pod started from the template.
- A person's `kubectl set resources` (CPU 900m) was handed back; new pods kept 900m.
- A canary whose container was stopped on the node was rolled back in place to 900m and 1Gi, with the Warning, the Actuation, no new Sizing for 24 hours, and one operator message.
- Found, and decided the same day: (1) a pod started from its template while the Kill switch was on or the Actuator down stayed there, since an approval is applied once. Decided: such pods catch up (12.4b). (2) A rollback's message began with the template's values beside "Approved Sizing, run N", which reads as if they were approved. Decided: it says what went back to what.

## 12.4b Catch-up

**What:** a pod started from its template is brought back to a held Sizing. Decided by the Operator after the 12.4 live check.

**Built (2026-11-10):**
- Each minute, while it may change things (Hub reachable, Kill switch off), a Sizing it applied is applied again where a Deployment has pods without its requests (started from the template while the Kill switch was on or the webhook failed open).
- Same path as an approval: pre-checks, canary, one pod at a time, `SizingApplied`. Watched pods keep their hour and rollbacks are kept.
- A pre-check that now fails ends the hold with `SizingSkipped`. A Sizing rolled back, skipped or handed back never catches up.
- The rollback reason says what went back to what, for example "2 pods back from cpu 115m to their template's cpu 1".

**Tests:** 10 mutations caught.

## 12.5 Automation

**What:** `automate: true` on a Sizing rule makes it Automation. The rule that picks the policy decides it. It is refused with policy `off`, or without a cluster that has an `actuator_token_secret` (the named one, if the rule names one).

**Built (2026-11-10):**
- `/actuator/desired` gains `automated`: the newest ready Sizing of each automated container on the token's cluster that changes something (`keep` is sent as none). Deployments in its namespaces only; nothing under the Kill switch. Held back where an Approved Sizing of that container is due, and for a Workload with 3 rollbacks in 7 days.
- Its own list, so an older Actuator never applies these as approvals.
- The third rollback in 7 days of an automated Workload is posted once to the operator channel, with the date Automation resumes (the oldest of the three plus 7 days).
- Churn rule: a change under 10% of what its pods ask now is ignored; an increase applies at once; a decrease at most once a day per container; an increase beside a waiting decrease goes alone.
- A new Automation Sizing waits, unsaid, while its container rolls or backs off. A skip is said once per reason (its numbers aside: a quota's skip at next hour's values is the same skip) and keeps what it holds. An approval of the run Automation applied is taken as approved in place; only what the churn rule left out is applied, after a rollout under way.
- A Sizing that leaves a resource alone keeps the value held for it (found while building: it used to drop that resource, so new pods got the template's value).
- Pause annotation `squero-actuator/pause: "true"`, read each minute: stops resizes, catch-ups and rollouts on that Deployment, and the webhook leaves its new pods unpatched. Rollbacks still happen; an approval waits.
- Hub unanswered for 15 minutes: one `HubUnreachable` Warning Event on its ConfigMap (Events in its own namespace, added to its Role). Events say `(Automation, run N)`.
- A `propose_change` pull request keeps a rule's `automate` but cannot set it yet.

**Decided while building (2026-11-10, the Operator's choice, all recommended):** the `actuator:` block waits for 12.7, and the cluster's Actuator token is the per-Contract opt-in; an ended Automation resumes by itself once its rollbacks are 7 days old; under Automation a hand-back is followed by the next Sizing, measured from the person's values; the pause is the Kill switch for one Deployment.

**Tests:** 46 mutations caught.

## 12.6a Revert through the Hub MCP server

**Grilled 2026-11-10 (all recommended):** a Revert stops Automation of that container until the Operator's next approval of it. Refused while the Kill switch is on, as approving is. `propose_change`'s `set_rule` gains `automate`.

**Built (2026-11-10):**
- Migration 0029, `reverts` (who and when); `reverted` is added to the Actuation outcomes.
- `revert_sizing(contract, cluster, namespace, name, container, kind)` on the Hub MCP server, marked destructive. Refused for a cluster without an Actuator, a foreign namespace, a container the Actuator never reported on, and under the Kill switch.
- `/actuator/desired` gains `reverts`, until a `reverted` Actuation of that container arrives after the ask; none under the Kill switch. Automation leaves a container alone while a Revert of it is newer than every approval of it.
- The Actuator does each Revert newer than its last for that container: pods back to the template's requests, as a rollback does but without the backoff. The hold is dropped (`SizingReverted`, Actuation `reverted`); with nothing held it says so. A paused Deployment waits.
- An approval older than the container's last Revert is not sent: a Revert is the last word until a new approval. The last Revert is kept across new holds, so a listed one again changes nothing. An approval of the run a Revert undid applies again.
- `set_rule` takes `automate`: true sets it, false removes it, leaving it out keeps it.

**Tests:** 26 mutations caught.

**Known limit:** a container whose first rollout is still under way has no Actuation, so `revert_sizing` refuses it ("reported nothing") until the rollout ends. Kept so by the Operator's choice, 2026-11-10.

## 12.6b The Automation page

**Built (2026-11-10):**
- The console's page at `/app/?view=automation`, served by `GET /api/v1/contracts/{id}/automation`.
- Per cluster with an Actuator, each container reported in the last 90 days, with its last Actuation and state: off, on, ended by Rollbacks until a date, or stopped by a Revert until an approval; plus a Revert under way and the Kill switch.
- Revert is offered on a container whose last Actuation is applied or failed. It goes through `POST .../automation/revert` with the `revert_sizing` check (moved to `webapp.revert`), stored as `console:<login>`.
- One helper (`actuator.rollbacks_of`, `resumes`) counts the Rollbacks behind "ended" for the Hub's desired state, its Slack line and this page.

**Tests:** 14 mutations caught.

## 12.7 Eviction behind the flag

**What:** a pod whose resize is `Infeasible` can be evicted. Eviction is off by default and runs only inside the Contract's windows.

**Built (2026-11-11):**
- The `actuator:` block: `eviction` (off by default); `timezone` (IANA, UTC when left out); `windows` with `days`, `start` and `end` as HH:MM (`24:00` allowed). An end at or before its start runs past midnight, and the night belongs to the day it starts.
- Refused: eviction without a window or without an `actuator_token_secret` cluster; an unknown timezone; a window from a time to itself.
- `/actuator/desired` gains `eviction`: `off`, or `on` with the window `closed` or `open` now; `off` under the Kill switch.
- For an `Infeasible` pod: eviction off, the rollout fails as before. Window closed, it waits with no time limit (`EvictionWaiting`); a newer Automation Sizing still replaces that rollout. Window open, it is evicted via `create pods/eviction` (added to the per-namespace ClusterRole). A 429 from a PDB keeps it waiting; any other refusal fails it. The eviction raises `PodEvicted`.
- One eviction at a time across the cluster, never while a replacement is awaited, and at least 5 minutes apart. The last eviction's time is kept in the ConfigMap, through new holds and catch-ups.
- The webhook gives new pods the rollout's requests while a replacement is awaited. A replacement (a pod not there before the eviction) that is Ready with the requests stands for the evicted pod and is watched for a Rollback; an evicted canary's 10 minutes start again. A replacement from its template fails the rollout and never causes a second eviction.
- A replacement not Ready in 10 minutes is a Rollback. This runs in `trouble()`, so it happens paused or with the Hub down too.
- On any put back, a replacement still `Pending` is evicted again, after the hold is let go, so it starts from its template. A Pending pod's eviction ignores budgets.
- The Actuation says `<pod> evicted, its resize Infeasible`. No new Sizing replaces a hold while its replacement is awaited. A Deployment scaled down meanwhile owes no replacement, and its rollout goes on.
- A Revert needs an Actuation of the container, so a first rollout waiting for a window cannot be reverted until it ends. A newer approval, the pause or the Kill switch stops it.
- From Kubernetes 1.35 the API server refuses at once a resize its node can never fit: 403 "node didn't have enough allocatable resources", not `Infeasible` (found by the live check). The Actuator treats it as `Infeasible` (evicted in a window; with eviction off, failed at once as before), and keeps the Hub's last word on eviction for new rollouts.

**Decided while building (2026-11-11, the Operator's choice, all recommended):** an `Infeasible` pod outside a window waits for one; Automation evicts too; eviction without windows is refused; a replacement not Ready in 10 minutes is a Rollback, not a pre-check of node room (which would need cluster-wide reads); `Deferred` (a node full now) is not evicted and still fails after 10 minutes.

**Tests:** 60 mutations, 59 caught. One equivalent: a window from a time to itself is refused.

**Checked live 2026-11-11** on kind (Kubernetes 1.37; workers with 3 and 10 CPUs allocatable), a working-tree image, a stand-in Hub on the Mac. No model calls, no cloud.
- The 403 above: the canary's resize on the small worker was refused, not marked `Infeasible`, so the rollout failed. Fixed before the commit.
- Window closed: waited. Window open with PDB `minAvailable: 2`: the eviction was refused and pods were untouched. PDB at 1: the canary was evicted.
- The replacement started from its template because the check's webhook selected the manifests' namespace `demo`, a setup mistake. The rollout failed once and evicted nothing more.
- With the webhook on `shop`, the canary was evicted after the 5-minute gap and its replacement started Ready at 4 cores on the big worker. The other pod was then resized in place: "2 of 2 pods resized in place, one at a time; <pod> evicted, its resize Infeasible". No restarts.
- With the big worker filled, a canary's replacement stayed Pending. Ten minutes on, the Actuator rolled back ("1 Pending replacement evicted to start from its template"), evicted it past the PDB, and it came back at the template's 499m, with no new Sizing for 24 hours.

## 12.7b Never evict some pods

**Decided 2026-11-11, the Operator's choice:** a pod is never evicted when its Deployment has one replica (none set counts as one), or when no PodDisruptionBudget in its namespace selects it.

**Built (2026-11-11):**
- Selectors use `matchLabels` and `matchExpressions`. A null selector selects nothing; an empty one selects everything.
- Such a rollout fails at once, window open or closed, with `it has one replica` or `no PodDisruptionBudget covers it`, then `so it is never evicted`.
- A failed read of the Deployment or its budgets fails the rollout too. An Actuator without the new `get, list poddisruptionbudgets` RBAC therefore evicts nothing.

**Tests:** 21 mutations, 20 caught. One equivalent: `In` on a missing key.

**Not checked live:** the eviction path is unchanged from 12.7.

## 12.8a StatefulSets and DaemonSets

**Grilled 2026-11-11 (the Operator's choice, all recommended):** three slices, 12.8a StatefulSets and DaemonSets, 12.8b Argo Rollouts, 12.8c CronJobs.
- A StatefulSet's pod is never evicted: it keeps its state, and a zonal volume could pin its replacement to a zone with no room.
- A DaemonSet's new pod gets from the webhook only requests lower than its template's. A raise reaches it in place where its node has room, so no node is left without its daemon.

**Built (2026-11-11):**
- Resized as Deployments are: pre-checks, canary, one at a time, rollback, hand-back, catch-up, Revert. Pods are found by owner UID; VPAs and HPAs count only when they target that kind; the pause annotation is read per kind (`ns/kind/name`).
- The webhook reads the owner from `ownerReferences`: a StatefulSet or DaemonSet directly, otherwise a ReplicaSet with a `pod-template-hash` suffix means a Deployment.
- The Actuator names its kinds on each pull (`kinds=Deployment,StatefulSet,DaemonSet`), and `/actuator/desired` sends only kinds it knows (`actuator.KINDS`). One naming none gets Deployments only, since an Actuator of 0.43.0 or older reads every Sizing as a Deployment's and would resize a Deployment of the same name (found in review: the first cut sent them to every Actuator). RBAC reads `statefulsets` and `daemonsets`.
- A DaemonSet pod whose node has no room is left without its raise: resize `Infeasible` (refused at once on 1.35+) or `Deferred` for 10 minutes; the pending resize is undone, except lowered requests, which stay set in place (found in the live check: the first cut left the lowering out too, the API server refusing the whole resize); a `PodLeft` Event; the reason `<pod> left without its raise: its node has no room`; never tried again until a new Sizing (`left` in its hold, kept through catch-ups). A left canary passes its 10 minutes on. The rollout fails only when every pod is left: `no node of its N pods has room for them`.
- Decided 2026-11-11 (the Operator's choice, B, recommended): on a DaemonSet a mix of node sizes is normal, so one small node must not end the Sizing for all.

**Tests:** 59 mutations, 58 caught. One equivalent: a DaemonSet request equal to its template's, which the webhook leaves alone anyway. A Hub filter it made redundant was removed.

**Checked live 2026-11-11** on kind (Kubernetes 1.37; a worker with 3 CPUs and one with 10), a working-tree image, a stand-in Hub. No model calls, no cloud.
- Every pull named `kinds=Deployment,StatefulSet,DaemonSet`. StatefulSet `db` resized in place, canary first; the same-named Deployment was untouched.
- DaemonSet `agent` at 4 cores: the big-node pod went to 4 cores; the small-node pod was refused by the API server ("requested: 4000, allocatable: 3000") and left: "1 of 2 pods resized in place ... left".
- Replacements: `db-1` started at 200m/96Mi; the big node's new DaemonSet pod started at the template's 500m with the lowered 32Mi, then was raised in place to 4 cores.
- The small node's pod created during that catch-up started from its template and was refused with the hold still applied: the finding fixed above. After the fix, a second run left it at 500m with memory lowered in place to 32Mi.
- No pod restarted.

## 12.8b Argo Rollouts

**Built (2026-11-11):**
- Resized as a Deployment is, including Revert, the pause annotation and hand-back. Read through the custom objects API (`rollouts.argoproj.io`, `get` and `list` added read-only), shaped as a Deployment: its template is its own or, with `workloadRef`, its Deployment's; its own selector comes first; its status is left out.
- Pods are those of the ReplicaSets it owns, stable and canary alike. HPAs and VPAs count when they target a Rollout. Events go on the Rollout (`argoproj.io/v1alpha1`).
- Never evicted: in place only, as grilled.
- The webhook tells a Rollout's pod by Argo's `rollouts-pod-template-hash` label on a ReplicaSet named `<rollout>-<hash>`, so a Deployment and a Rollout of one name stay apart.
- The Actuator names `Rollout` on each pull; one of 0.44.0 names three kinds and gets no Rollout Sizings.
- Argo Rollouts not installed: skipped (`no such Rollout`). A `workloadRef` of another kind fails: `reading the Rollout: 422: not read as a Deployment`.
- The pause check lists Rollouts by metadata only, so such a Rollout never hides another's pause (found in review).

**Tests:** 20 mutations, 19 caught. The survivor is `main`'s wiring (its CustomObjectsApi), which the unit tests don't run; the live check runs it.

**Checked live 2026-11-11** on kind (Kubernetes 1.37, Argo Rollouts v1.8.3), a working-tree image, a stand-in Hub.
- Argo's layout matched the code: ReplicaSets `<rollout>-<hash>` owned by the Rollout, pods labelled `rollouts-pod-template-hash` and no `pod-template-hash`.
- Rollout `web` (canary) and Rollout `ref` (`workloadRef` to a Deployment scaled to zero) were resized in place, canary first, with `SizingApplied` Events beside Argo's own. The same-named Deployment was untouched.
- Replacements started with the held requests (`ref`'s at 150m, where its template asked 300m). An Argo canary update started its pod at the held 200m, with no hand-back after promotion.
- Paused: a replaced pod started from its template and was caught up in place after unpausing.
- Person's template changes on `web`, and through `workloadRef` on `base`, were both handed back; Argo's new pods kept the person's values.
- Nothing found. Eviction not exercised (a Rollout's pod is never evicted, which is unit-tested).

## 12.8c CronJobs

**Built (2026-11-11):**
- A CronJob's Sizing gets the same pre-checks on its `jobTemplate`; a ResourceQuota counts its `parallelism`. It is held at once: `SizingApplied` "held for its next run's pods, given them as they start", and an Actuation `applied` with 0 pods.
- None of its pods is resized in place, caught up or evicted.
- The webhook recognises a run's pod by its Job's name (`<cronjob>-<digits>`, as the CronJob controller names it), with no API call. A lone Job's pod is refused.
- The first run under the hold is watched (`first_run`, saved each minute): the first Job it owns, created since the hold, whose pods got the requests. Its pods are listed by `batch.kubernetes.io/job-name`, Succeeded and Failed included.
- A held pod that `Failed`, or was OOM-killed (`state` with `restartPolicy: Never`, `last_state` with `OnFailure`), is a Rollback. The hold is dropped, so the next run starts from its template, with the usual backoff.
- The watch ends when the Job finishes or disappears, after 7 days without a run, or on Revert or hand-back. The churn rule still holds.
- Events go on the CronJob (`batch/v1`); `get, list` of `cronjobs` and `jobs` is added to RBAC. A Job with no CronJob is never sent: "a Job is not resized" (it runs once).

**Decided while building (the advisor's review, recommended defaults):** `applied` is reported when held, not after the run; the watched run is the first whose pods got the requests; the Job-name match.

**Tests:** 34 mutations, 33 caught. One equivalent: a pod owned by a StatefulSet and a Job.

**Checked live 2026-11-11** on kind (Kubernetes 1.37), a working-tree image, a stand-in Hub. Two CronJobs ran every minute with `restartPolicy: Never` and `backoffLimit: 0`. The controller named its Jobs `<cronjob>-<minute>`, as the webhook assumes.
- Both were held at once; a lone Job's Sizing was skipped ("a Job is not resized").
- `nightly` (`sleep 5`, approved at 200m/96Mi): its next run started at 200m, completed, and the watch ended with the hold kept.
- `hungry` (`tail /dev/zero`, limit 32Mi, approved at 150m): its run started at 150m and was OOM-killed. `SizingRolledBack` said "its next run's pods start from its template, not cpu 150m; no new Sizing of it until ...", and the next run started at 100m.
- A person's template change to `nightly` was handed back; its next runs were at 120m/64Mi. No error in the Actuator's log.

## 12.9a The audit's Workload findings

**Built 2026-11-11.** The audit found five defects that can harm a Workload. Each is fixed here.

1. **Old restarts counted as new.** A catch-up or new rollout counts restarts from when it begins, for every pod already holding the requests (kept in the hold's `pods`). Those pods get no NotReady rule, which stays for a pod it resized. Before, once a hold's hour was over, a catch-up read an old restart as new and rolled a healthy Sizing back, with 24 hours of backoff.
2. **Retries that never ran.** A Rollback or Revert that some pod refused is `unfinished` and retried each minute, pod by pod, whether the Hub is reachable or not, never under the Kill switch, until no pod refuses with a failure that may pass (another refusal is the API server's verdict, said once in its log). It survives a skipped approval and a later Sizing that is skipped, and ends with one that rolls, when the Workload is gone, or when its template no longer asks what the Actuator did. Before, that pod kept the requests for good.
3. **Calls that could kill the process.** API clients are wrapped (`Calm`): a network failure is an `ApiException` with status 0. Each call has a 15-second timeout, tried once. A failing minute saves what it changed and reports, and the process goes on. Before, the process died before saving, so a Sizing just rolled back could be applied again, its backoff lost.
4. **Rollouts failing on a blip.** Moving to a rollout's next pod, a failure that may pass (status 0, 429, 5xx) is waited out for 10 minutes (`stalled`).
5. **A refused approval erased the hold.** A skipped or failed approval leaves an earlier applied, rolling or failed hold in place. Before, the skip replaced the hold, so new pods lost its requests and a Revert found nothing held while the pods kept them. `refused` marks the run, said once: "run N's requests stay held".

**Found in review and fixed before release:** a finished put back was not saved; the retry stopped at the first refusing pod; a failed minute was never reported; the restart fix had widened the NotReady rule.

**Tests:** 47 mutations, all caught. `Calm` was checked over a real client against a closed port (urllib3's MaxRetryError becomes status 0).

**Not checked live** otherwise; failure paths were tested with refused and failing API calls.

*Why not in one slice:* until 12.9b, a network failure at approval time used up the approval (`failed`). 12.9a and 12.9b are released together.

## 12.9b The audit's narrower findings

**Built 2026-11-11:**

1. **Memory survives restarts.** The ConfigMap gains `state.json`: the Kill switch as the Hub last said it, and unsent reports (newest 200 kept). Read at start (what it cannot read, or another version's, is dropped, never its holds with it), saved when either changes. Before, a restart with the Hub unreachable took the Kill switch for off, and lost unsent reports, including a `reverted`.
2. **Approvals not used up by a passing failure.** An approval or catch-up that a passing failure meets before anything changed is tried again each minute (`trying`) for 10 minutes, then becomes `failed`. A catch-up waits with its hold still applied. This closes what 12.9a opened.
3. **Re-approving after a Revert.** Approving again a Sizing whose container was Reverted moves the approval past the Revert. Before, it was a no-op answered "already". `/actuator/desired` marks it `again`, and the Actuator applies it, even if it is the run the Revert undid. An approval from before the Revert still never applies.
   - A Sizing whose container has a Revert in the same pull waits: the Hub reads them in separate queries, so a new Revert could otherwise be undone in the same minute (found in review).
   - An older Actuator ignores the flag.
4. **Empty rollouts.** A rollout whose Workload has no pods left ends `applied` and held, not "no node of its 0 pods".
5. **Jobs taken for CronJob runs.** A Job is taken for a CronJob's run only if the digits ending its name are a recent minute (5 minutes ahead to 30 days back). A lone Job `x-123` no longer gets CronJob `x`'s hold.
6. **Revert offered on the wrong row.** The Automation page offers Revert by `revertable`: the last Actuation that is applied or failed. A skipped approval is not one.

Also: the integration test `test_finds_all_three_failures` raced the demo's first OOM kill in CI; it now waits for that kill.

**Tests:** 48 mutations, all caught, after one redundant condition was removed. Another Contract's or cluster's Revert never marks an approval `again`, nor lets it be approved again.

**Decided while building (recommended defaults):** the `again` field; a 30-day window on a run's minute; zero pods ending `applied`; two ConfigMap writes in a minute when there are reports.

**Checked live 2026-11-11 for 12.9a and 12.9b** on kind (Kubernetes 1.37), a stand-in Hub on the Mac that could refuse reports. No model calls, no cloud.
- Unsent reports survived a deleted Actuator pod in `state.json` and reached the Hub once it took them.
- Kill switch on, then the Hub stopped and the Actuator's pod deleted: the next run's pod started at the template's 100m. With the Hub back, "the Kill switch is off".
- A lone Job started at 100m beside runs at 200m.
- Scaled to zero mid-rollout, the Sizing ended "1 pods resized in place, then none left to resize", and its pods came back at 200m from the webhook.
- A refused approval (run 2 at 500m under a 300m LimitRange) was skipped ("run 1's requests stay held"); the next run stayed at 200m.
- A run approved before a Revert did nothing; marked `again` beside the Revert, the Revert still listed it, so it waited.
- Catch-up after an old restart: the old pod was kept as `{restarts: 1}`, and no Rollback followed.
- API server frozen (`docker pause` of the control plane) with a Revert asked: each call failed once after 15 seconds, the process did not restart, and the Revert said "tried again each minute". Unfrozen, the next minute put both pods back at 100m/64Mi.
- Found and fixed before release:
  1. The Kubernetes client retried a timed-out read three times, so the Actuator made no pull for 3 minutes 48 seconds while frozen. Clients now try once, 15 seconds a call. (ponytail: a frozen minute of some twenty calls still took 3 minutes 45 seconds; stopping at the first failure is the next step if that matters.)
  2. The `refused` mark skipped every run up to the refused one. It now skips only that run.
  - Both re-checked on the rebuilt image. 5 more mutations caught.
- Not exercised: a rollout waiting out a failure at its next pod (`stalled`); a frozen API server fails the canary's own read first.

## 12.9c The Hub's side of the reports

**Built 2026-11-11.** This covers what a compromised Actuator's token could do with its reports. Every change stays inside its own Contract and cluster.

1. **Numbers checked.** `/actuator/reports` refuses the whole report (422, naming the field, never the value) when a number is not a number or is negative (`actuator.unreal`: `cpu`, `memory`, `at`). Before, a NaN broke the Automation page for 90 days. The check sits in the route, not the shared model, since the Actuator reads stored Actuations back; nothing an honest Actuator sends is refused. Found while building: FastAPI's own 422 echoed the value, and a NaN cannot be said in JSON, so it was a 500 that an Actuator would resend forever. Every route's 422 now says where and why, never the value.
2. **Dates capped.** An Actuation is dated `least(at, received_at)`, which sets its listing, the 7 days of Rollbacks and its 90 days. `at` stays the key that tells a report from one sent again. Before, a report dated years ahead was never pruned.
3. **Rollback posts that survive Slack downtime.** Rollbacks are marked `told` (migration 0030, true for existing rows). A teller runs after the Actuator is answered and each 30-second scheduler tick, beside the tick and never inside it, one at a time. It posts, then marks, so a Rollback Slack refused is told later, for up to 7 days (more than 10 minutes late, it says when it was reported). A Contract whose post was refused rests 5 minutes, doubling to an hour at most. One Contract's failure never stops another's. A pass reads no more rows than it may post. The "Automation ended" line is part of the Rollback's message, so a retry says it too.
   - Found in review of the first cut: the teller ran inside the scheduler's tick, so Slack being down held every Contract's Checks and deploy verification.
4. **Flood limit.** At most 20 Rollbacks of one cluster are posted an hour. The rest are counted in one line ("N more Rollbacks reported by <cluster>'s Actuator are not posted here ...: see the Automation page"), itself at most once an hour. Another Contract's or cluster's posts are never spent. Tested with two Contracts in their own channels.
5. **Strings escaped.** Every string an Actuator sent goes through `slack.clean`, so `<!channel>`, `<@U…>` and `<url|text>` arrive as text.

**Decided while building (recommended defaults):** 20 posts per cluster an hour, the 7 days, the "reported" suffix after 10 minutes, the ended line inside the Rollback's message, and any negative number refused.

**Tests:** 61 mutations, all caught. One showed the number rule could be one condition.

**Not built, offered:**
- a cap on how many Actuations a cluster may store an hour (a compromised Actuator can still fill its 90 days);
- checking `kind` against the kinds it resizes (done in [audit A7a](audit.md#a7a-the-reports));
- the reviewer's note that a later run reading a cluster supersedes an approval, where the glossary says a later Sizing of that container.

**Not checked live:** forged Rollbacks on the Mac Hub would post to a real channel. The route, the scheduler tick and Slack's failures are tested through the app.

## 12.10 A lost node is not a Sizing's fault

**Grilled 2026-11-12 (found while grilling Phase 16, built before [Phase 13](phase-13.md)):** a pod NotReady because its node is gone is never a Sizing's fault.

*Why:* a resized pod NotReady for 2 minutes was a Rollback, and three end Automation. So any node failure, frequent with spot, could end a Workload's Automation.

The tell is on the pod. While a node is NotReady, the node-lifecycle controller sets only the pod's `Ready` to False and leaves `ContainersReady` as the kubelet last wrote it (Kubernetes v1.34, `MarkPodsNotReady`; a failing readiness probe sets both). A pod removed for this carries a `DisruptionTarget` condition (`DeletionByTaintManager`, or `DeletionByPodGC` once the Node is deleted), which also covers a Failed pod in a CronJob's watched run. How a graceful node shutdown reads (reason `Terminated`, "Pod was terminated in response to imminent node shutdown") was to be confirmed on kind.

**Built 2026-11-12:** `lost()` tells such a pod from the pod alone: `Ready` False with `ContainersReady` and every readiness gate True; a `DisruptionTarget` of `DeletionByTaintManager` or `DeletionByPodGC`; or reason `Terminated` with the kubelet's shutdown message.
- Its NotReady is not a Rollback, nor is a CronJob's watched pod that Failed that way. Its OOM kill still is, and so is a restart from before its node went.
- An evicted pod's replacement, lost with its node, holds the 10-minute verdict.
- A lost pod is never given a Sizing's requests and is not chosen as a canary. A rollout whose pod is lost goes on with the others; if every pod is lost it counts as scaled to zero. A canary lost inside its ten minutes was never watched, so the next pod resized becomes the canary.
- A catch-up leaves a lost pod until its node is back. An approval that only lost pods are missing is tried each minute for ten minutes (`passing`).
- Putting requests back (Rollback or Revert) still reaches a lost pod, so a returning node's pod is not left with requests nobody holds.

**Left as before, not decided:**
- A CronJob run's pod that Failed because it was evicted to drain a node (`EvictionByEvictionAPI`), or ended by a node under pressure (`TerminationByKubelet` without the shutdown message), still rolls back. A lowered request can bring the second on.
- A Deployment's pod removed either way is no longer listed, so it never counted.
- A node that restarts restarts its containers, which read as a restart. Found by the live check; fixed in 12.11.

**Tests:** no isolation test, since nothing here stores, reads or sends Contract data. Mutation-checked: 24 of 24 killed; a 25th showed a condition that could never differ, and was removed.

**Checked live 2026-11-12** on kind (Kubernetes 1.37, three workers, the Actuator on the control plane), a stand-in Hub. No model calls, no cloud. Deployments `web` and `api` and CronJob `nightly` (a 15-minute run) were approved at 200m.
- **A worker stopped** during `web`'s canary's ten minutes. Its pods read `Ready` False with `ContainersReady` True, each with a `NodeNotReady` Event, as the source said. Within the minute another `web` pod was resized and made the canary, with its own ten minutes ahead. No Rollback in the 3 minutes 22 seconds watched (the canary's NotReady was one after 2 minutes before).
- **Five minutes on**, the taint manager marked them `DeletionByTaintManager`; their replacements started, `nightly`'s at the held 200m.
- **The Node was deleted.** The pods turned `Failed` with `DeletionByPodGC`, including `nightly`'s watched run. No Rollback; the hold was kept, and that run ended Complete (1 succeeded, 1 failed). Both rollouts ended "3 of 3 pods resized in place", the replacements included.
- **A second worker stopped** with two resized pods in their watch hour: NotReady for 3 minutes 30 seconds, no Rollback. This is the bug this slice fixes.
- **That worker started again** before its pods were removed. Its containers came back restarted (count 1, last end `Unknown`, exit code 255). That read as "app restarted" and rolled both Deployments back, each with its 24-hour pause. Telling a node's restart apart is 12.11.
- Not produced on kind: a graceful node shutdown.
- Also seen: once a CronJob's first run ends, `first_run` stays in the ConfigMap until its next save; a restart finds the run over and drops it.

## 12.11 A node's restart is not a Rollback

**Decided and built 2026-11-12**, the Operator's yes to 12.10's live finding. A container its node's own restart started again is not a Rollback.

- `rebooted()` reads the container's last end: reason `Unknown` with exit code 255, as kind's containerd reported once the worker was back. Both are needed: a crash can end with 255 but reads `Error`, and an OOM kill reads `OOMKilled`.
- Such a restart is not counted while it is the last end. A crash or OOM kill after it still is. A container with no last end shown is counted as before.
- The NotReady timer starts when the containers stopped being ready, not when the node went down. `Ready` keeps the older time after the node returns, so an app slow to become ready would otherwise have hit the 2 minutes at once (found in review).

**Not covered:** a CronJob run's pod that Failed in a node's restart (how it reads was not seen), and other runtimes' wording (CRI-O's was not looked up).

**Tests:** no isolation test, as nothing here stores, reads or sends Contract data. Mutation-checked: 8 of 8 killed; a ninth could not change behaviour.

**Checked live 2026-11-12** on kind (Kubernetes 1.37, two workers), a stand-in Hub. No model calls, no cloud. Deployment `slow` (a readiness probe that passes 60 seconds after each start) was approved at 200m, with a pod on a worker.
- **The worker stopped** for 3 minutes 56 seconds. The pod read `Ready` False with `ContainersReady` True, as in 12.10.
- **The worker started again.** The container had restarted (count 1, last end `Unknown`, exit code 255) and was not ready for 64 seconds. `Ready` stayed dated from the outage and `ContainersReady` from the return: the two times this slice tells apart. The Actuator's minute fell inside those 64 seconds: no Rollback, and the hold stayed at 200m (the restart and the 2 minutes of NotReady were each one before).
- **The app then crashed** in that pod (exit code 1, last end `Error`, its second restart). The rollback came 18 seconds later: "app restarted: 2 pods back from cpu 200m to their template's cpu 100m".
