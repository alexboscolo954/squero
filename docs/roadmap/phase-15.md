# Phase 15: Nodes

**In short:** The Hub works out what keeps each node from being removed (its *Blockers*), which nodes could go, and what change would free each one. The Actuator reports the pods that block a node. One write is added at the end: the webhook can *place* pods that can move on the nodes the client names, as a preference only. Compaction by the descheduler is Deferred.

**Status:** done. The done-when was checked on a multi-node kind cluster on 2026-11-15. Karpenter's rules are tested from its documented behaviour and checked live in [Phase 16](phase-16.md)'s sandbox. 15.5 is Deferred.

**Goal:** show what blocks each node's removal, which nodes could go and what they would save, and let the client's pinned nodes take the pods that cannot move.

**Done when:** on multi-node kind, placement empties a node the report named as blocked.

| Slice | What | Status |
|---|---|---|
| [15.1a](#151a-the-rooms-and-the-node-capacity-cap) | Each pod's node, each node's room and taints, the node-capacity cap, the Nodes page | built 2026-11-14, checked live 2026-11-14 (with 15.1b) |
| [15.1b](#151b-the-blockers-from-the-actuator) | The Actuator's Blockers, stored and shown per node | built 2026-11-14, checked live 2026-11-14 |
| [15.1c](#151c-the-fallback-without-an-actuator) | Fallback from Prometheus for a cluster without an Actuator | built 2026-11-14, checked live 2026-11-14 (with 15.4) |
| [15.2a](#152a-the-hubs-side-of-placement) | Placement: the Hub's side | built 2026-11-15, checked live with 15.2b |
| [15.2b](#152b-the-webhook-places-pods) | Placement: the webhook, and the ADR | built 2026-11-15, checked live 2026-11-15 |
| [15.3a](#153a-sizings-per-instance-type) | DaemonSet Sizings per instance type: the Hub's side | built 2026-11-14, checked live 2026-11-14 (the read) |
| [15.3b](#153b-the-actuator-applies-each-type) | The Actuator applies each type's Sizing | built 2026-11-14, checked live 2026-11-15 |
| [15.3c](#153c-the-money-by-type) | The money by instance type | built 2026-11-15, checked live 2026-11-15 |
| [15.4](#154-nodes-that-could-go) | Nodes that could go, now and after the Sizings | built 2026-11-14, checked live 2026-11-14 |
| 15.5 | Compaction with the descheduler | Deferred |
| [15.6](#156-the-change-that-removes-a-blocker) | The change that removes a Blocker | built 2026-11-14, checked live 2026-11-14 |

## Decisions (grill, 2026-11-12)

Three rounds, all recommended. Compaction by the Actuator was first chosen as a last slice, then Deferred once the facts were in.

- **Scope.**
  - Read-only first, with one write last: Placement, through the webhook.
  - Cluster Autoscaler and Karpenter both, named per cluster in the Contract (`cluster-autoscaler`, `karpenter`, or none). *Why:* their rules differ, and guessing from which metrics exist is a guess.
  - A cluster with neither still gets the report ("a person could remove this node").
  - The Hub computes it each hour and stores it, as it does Sizings. A new Nodes page in the console, and one line in the weekly digest.
  - The Triage agent's tools are unchanged, so no eval rerun. No new permission in a client's cluster, and no spend.

- **Blockers (15.1).**
  - From Prometheus: each pod's node, each node's room and taints, PodDisruptionBudgets' allowed disruptions, and pods with no owner. From the Actuator, in its namespaces: pods and PodDisruptionBudgets.
  - The Actuator sends the Hub, hourly, only the pods that have a Blocker. The list is capped in size and checked as its reports are (12.9c).
  - kube-state-metrics has no series for emptyDir or hostPath volumes, or for which pods a budget covers, and pod annotations only when allowlisted. *Why:* a cluster without an Actuator gets what Prometheus has, and "unknown" for the rest.
  - Pods outside the Actuator's namespaces (kube-system included) are counted per node as "not visible". The report never names a pod outside the Contract.
  - Cluster Autoscaler's Blockers are its documented list: a PodDisruptionBudget allowing no disruption; kube-system pods without one; a pod with no controller; local storage; `safe-to-evict: "false"`; a pod that fits nowhere else.
  - Karpenter's are its `karpenter.sh/do-not-disrupt` annotation and PodDisruptionBudgets only. Its docs do not say a pod with no controller blocks it.
  - The verdict is the Hub's own. Beside it sits the autoscaler's own count of unremovable nodes, where Prometheus has it. Its `unremovable_nodes_count` labels reasons with numbers, translated in 1.30.7. "Unknown reason" covers one the Hub doesn't know. That count was dropped in 15.1c, once its source showed what the gauge is.

- **Nodes that could go (15.4).**
  - A requests-fit: a node with no Blocker whose pods fit in the free room of the others. Tainted nodes only fit among themselves. A DaemonSet's pods are not moved.
  - An upper bound, shown as one ("up to 3 nodes"). *Why:* affinity and spread rules cannot be seen from Prometheus.
  - Computed twice: with today's requests, and after the Sizings. *Why:* a Sizing frees capacity, but money is saved only when a node leaves.
  - Its own figure, with its monthly cost. Never summed with Savings, since after the Sizings it is partly the same money.

- **Proposals (15.6).**
  - Per Blocker, the change that removes it, ready to copy: a PodDisruptionBudget for a Workload that has none, or the annotation that lets a local volume be dropped.
  - The autoscaler's setting to check is named, not compared. The Hub cannot read it (it lives in kube-system, or in objects of no namespace).
  - The Operator was to turn one into a pull request with the Hub MCP server's existing tool. Changed in 15.6: that tool edits only a Contract's Sizing policy, so the change is to copy.
  - The descheduler is not proposed. Its `HighNodeUtilization` must be paired with a scheduler scoring `MostAllocated`, and the default scores `LeastAllocated`.

- **DaemonSet Sizing per node (15.3).**
  - One Sizing per group of nodes of the same instance type, using the label the Hub already reads for list prices. Today one pooled Sizing fits the busiest node and is too large on the rest.
  - The Actuator cannot read a node, so the Hub sends each group's node names with its Sizing. The Actuator applies each pod's Sizing by its node, in place.
  - A new pod carries its target node from its creation (the DaemonSet controller's node affinity), so the webhook gives it its group's requests, when lower than its template's.
  - A pod on a node the Hub has not seen yet keeps the template's requests until the next run names its node.
  - One approval covers all of a DaemonSet's groups. A group with too little history is still learning.

- **The node-capacity cap** (parked here since 9.1). A Sizing's request is never above the largest node's room after its DaemonSets. The risk is "needs a larger node".

- **Placement (15.2).**
  - The webhook adds a *preference*, never a requirement. *Why:* a required rule can leave a pod Pending. The preference is for nodes with a label the client names in the Contract (absent: off), such as a small pool kept for pods that can't move.
  - The report treats those nodes as where Blockers belong, and never proposes removing them.
  - No pod-to-pod affinity. The Kubernetes docs advise against it above a few hundred nodes.
  - Placed Workloads are those the report names as blocking, in the Actuator's namespaces. The Hub lists them for the Actuator, as it does Sizings. The pause annotation and the Kill switch stop it.
  - A running pod's affinity cannot be changed, and an unevictable pod is one nothing may evict. So pods already running move only at their next rollout or restart. The report says which Workload to restart to free which node. The Actuator never evicts for it.
  - With Karpenter, a preference is treated as required first, and relaxed only when it can't be met. So the label should be one a NodePool carries.
  - [ADR 0006](../adr/0006-placement-is-a-preference.md) is written with it: the first time the Actuator influences where a pod runs.

- **Grilled further 2026-11-15 (two rounds, all recommended).**
  - Placement is per cluster: `placement: "pool=pinned"` on a cluster with an `actuator_token_secret`, one `key=value`. *Why:* node labels differ per cluster, as `autoscaler` is set.
  - An optional taint can name the pool. The webhook adds its toleration to placed pods only. A toleration permits a node and requires nothing. *Why:* an untainted pool fills with pods that can move.
  - A Workload is placed for a Blocker fixed in its pods' spec only: `safe-to-evict: "false"`, `do-not-disrupt: "true"`, local storage or emptyDir, or a custom controller.
  - Never for a PodDisruptionBudget that allows no disruption, nor a timed `do-not-disrupt`. *Why:* both come and go, and would place a Workload mid-rollout. 15.6 says how to loosen the budget.
  - Never for a pod with no controller (no Workload to restart), nor for a DaemonSet.
  - The Contract's field is the consent, as for Reflexes. No approval per Workload. Each placed pod gets an Event.
  - The webhook appends one preferred node affinity term of weight 100 at CREATE. It never replaces the pod's own terms, and adds nothing where the pod already prefers that label.
  - A Pinned node is never one that could go, nor a destination in 15.4's requests-fit. Its Blockers are shown as expected, with no 15.6 proposal, and its verdict is `pinned`.
  - The report names "restart X to free node N" only when every Blocker on N is a placed Workload's pod and the pinned nodes' free room fits them all. Otherwise it says what still holds N: "pod Y", or "the pinned nodes lack room: about 1.2 cores and 3 GiB short".
  - A placed pod that started elsewhere is marked "placed, on N: the pinned nodes had no room when it started".
  - Shown on the Nodes page and by the Hub MCP's `nodes`. The digest's Nodes line is unchanged.

- **Compaction (15.5): Deferred.**
  - *Why:* the default scheduler spreads pods, so a pod evicted to empty a node tends to land back, unless it is steered away.
  - Karpenter consolidates by itself. Cluster Autoscaler drains a node under its threshold (0.5 by default, set by the client), which the proposals name.
  - It would be the first time the Actuator evicts healthy pods to save money.

- **Terms.** *Blocker*: what keeps a node from being removed. *Placement*: the webhook's preference for the nodes the client named.

**Looked up before the grill (2026-11-12):**
- Cluster Autoscaler's `skip-nodes-with-local-storage`, `-system-pods` and `-custom-controller-pods` all default to true.
- Karpenter's `consolidationPolicy` defaults to `WhenEmptyOrUnderutilized`, `consolidateAfter` to 0s, and its budgets allow 10% of nodes. Its `Unconsolidatable` Event sits on the Node.
- A pod's updatable fields do not include `affinity` or `nodeSelector`. A preferred affinity still schedules the pod when no node matches.
- kind takes worker nodes in its config.
- 12.8a's live check had left two pods of one DaemonSet at different sizes, with nothing undoing it.

## 15.1a The rooms and the node-capacity cap

**Grilled 2026-11-14, all recommended:**
- A node's Room counts every pod on it. The Contract's own pods are counted by name; the rest are one sum and one count per node, never named or split by namespace. On a cluster two Contracts share, each sees the total the other asks, per node.
- The cap is per container. A pod whose containers together ask more gets the risk, with nothing cut in proportion.
- The largest Room is over every node not cordoned, tainted ones included. *Why:* too generous changes nothing; too strict shrinks a correct Sizing.
- No Hub MCP tool until 15.4 gives something to act on.

**Built 2026-11-14:**
- `usage.rooms` reads each node (allocatable, taints, cordoned, instance type) and its Running or Pending pods' requests, joined on the pod's `uid`. Read-only on the lab's Prometheus: of 36 pods, 34 were live. Grouping by an absent label gives no error.
- Pods outside the Contract's namespaces are summed and counted by Prometheus, so none reaches the Hub by name.
- `sizing.largest_room` and a `room` in `sizing.size` cap each Sizing, with the risk `needs_larger_node`. `sizing.rows` adds the pod-level risk.
- Stored per cluster, latest only (`cluster_rooms`, migration 0037). Served by `GET /api/v1/contracts/{id}/nodes` and the console's Nodes page. `clusters[].autoscaler` is shown, nothing more.
- The Triage agent's tools are unchanged; the risk is text in the `sizing` output, not a schema change. No eval rerun.

**Chosen beyond the grill:**
- The guard for list prices is one check for every read of node series. A store shared with another Contract without cluster labels reads none of this, so the lab on the mini PC shows the refusal and not its node.
- The cap never lowers a request. A node cordoned, or missing from the read, would otherwise turn it into a cut that Automation applies. A cluster read in part has no cap. A DaemonSet's own Sizing is exempt.
- CPU's and memory's largest Room may come from different nodes.
- The Contract's own pods come from `kube_pod_info`, so a pod asking nothing is not counted as not visible. Init containers are left out.
- A failed read of the nodes is stored as the cluster's problem, and stops nothing.
- A container kept above the largest Room says so: "kept at 2000m, though no node has room for more than 1000m". It does not say it was capped.
- The pod-level risk goes on the Workload's containers that have a Sizing, not on one still learning.
- A cluster behind a Relay is read through its Relay, which passes the Hub's queries on as before. The Relay test's rule "its namespaces only" now names the exception: node series, and per-node sums and counts.

**Tests:** 64 mutations, all killed after five tests were sharpened.

**Not checked live** at build time. Its live check is 15.1b's.

## 15.1b The Blockers from the Actuator

**Grilled 2026-11-14, all recommended:**
- 15.1b holds only the Actuator's path: its hourly report, stored, and the Hub's verdict per node. 15.1c adds the fallback.
- A cluster that names no autoscaler is judged by what stops `kubectl drain` without force flags: a budget at 0, a pod with no controller, or emptyDir data.
- A node with no known Blocker but pods the Hub can't see (outside the Contract's namespaces, and not a DaemonSet's) is "unknown: N pods not visible". So 15.4 never counts it as one that could go.

**Built 2026-11-14:**
- `/actuator/desired` gains `blockers` (the cluster's namespaces). It is sent under the Kill switch too, since a read is no change. An older Hub does not send it, and then nothing is reported.
- The Actuator runs hourly, last in its minute, in its own try, so a failure costs no Rollback. It lists pods and PodDisruptionBudgets in those namespaces, a page of 200 at a time (its memory limit is 256Mi), and posts `blockers.Report` to `POST /actuator/blockers`. The report holds facts per pod and no verdict. It names the namespaces read in full, and whether it was cut at 1000 pods.
- No new permission: `deploy/actuator/rbac.yaml` is untouched.
- The Hub refuses the whole report if it is over the cap (413), names a namespace outside the cluster's, or carries a value no Actuator sends (422). It stores the latest report, dated by its own clock (`cluster_blockers`, migration 0038).
- `blockers.reasons` holds the rules; `blockers.verdicts` gives each node blocked, clear or unknown.
- `usage.rooms` counts, per node, the pods that stay with it, DaemonSet and static pods included.
- Shown on the Nodes page; the API's node gains `verdict`. The Triage agent's tools are unchanged, so no eval rerun.

**Looked up before the build (2026-11-14, primary sources):**
- Cluster Autoscaler's FAQ: local storage is a hostPath or an emptyDir not in memory. `safe-to-evict: "true"` lifts the kube-system, no-controller and local-storage rules, but is not said to lift a budget. `safe-to-evict-local-volumes` must name every local volume.
- `kubectl drain` refuses pods with no controller, and emptyDir pods, without its force flags. hostPath is not mentioned. Evictions respect budgets.
- Karpenter's disruption page: the pod annotation is "true", or a Go duration like "30m" for that long after the pod starts running. An invalid value is ignored. Blocking budgets apply, and every budget must allow. The same annotation on a Node counts. Nothing on pods without a controller, or with local storage.
- Cluster Autoscaler's drainability source could not be reached. Its rules follow the FAQ alone.

**Chosen beyond the grill:**
- Facts, not conclusions, so the rules can change without redeploying an Actuator.
- A custom controller's pod blocks under Cluster Autoscaler (its flag's default), and under `kubectl drain`.
- Karpenter's annotation blocks as "true". As a duration, it blocks until that long after the pod started. The start is sent as a fact; a pod whose start isn't known still blocks. Any other value is ignored, as Karpenter ignores it.
- A report is also cut by size, below the megabyte the Hub's proxy lets through. The proxy's list of the Actuator's routes gained this one.
- A budget whose status isn't written counts as allowing none.
- Clear needs positive evidence. A node is unknown after two hours without a fresh report, by the Hub's clock; also when a report was cut, a namespace was unread, pods are hidden, or the node was read by a 15.1a run.
- A pod of a namespace the Contract has since left is not shown.
- A report that did not reach the Hub is sent again in 5 minutes. One the Hub refused waits the hour.

**Known limits:**
- Karpenter's annotation on a Node is not read.
- Cluster Autoscaler's flags are taken at their defaults.
- No verdict without an Actuator until 15.1c.

**Tests:** 108 mutations, 107 killed after six tests were sharpened. One equivalent: a blocked node has no reason for being unknown, so which is asked first can't matter. An integration test reads two namespaces with a pod on a page, through the real client, in CI's kind job.

**Checked live 2026-11-14, 18:30 to 18:55 UTC** (15.1a and 15.1b together): a throwaway kind cluster on the Mac (Kubernetes 1.37, a control plane and three workers, one cordoned), with kube-prometheus-stack on the lab's values. Namespace `shop` was the Contract's; `other` sat outside it. No Hub process, no model calls. Deleted after.
- **Room:** for all four nodes, the requests, pod counts and pods that stay with the node equalled the API server's, including 8 of the control plane's 11 static pods. The Contract's 18 live pods were named. A finished Job's pod (500m) was in neither. No pod of `other` or `monitoring` was named.
- The cordoned node was left out of the largest Room (9.88 cores, 31.2 GiB after 120m of DaemonSets). A synthetic container using more was capped there, with the risk. The 8 real Sizings were not capped, so the cap is only shown on synthetic usage.
- **The Actuator's read,** through a ServiceAccount bound in `shop` only: 19 pods in four pages of 5. `other` was refused with a 403 and left out. It sent 5 pods with their facts.
- **The Hub,** in process after one stored run: `/actuator/desired` named `shop`; the report was taken (204; 401 with a wrong token). Verdicts followed the rules under all three rule sets: the two workers with a `web` pod blocked by its budget; `solo`, the pod with no controller, blocking under `kubectl drain`'s rules and by its annotation under Cluster Autoscaler's; the emptyDir pod blocking under those two (the in-memory volume only under `kubectl drain`'s); the `do-not-disrupt` pod under Karpenter's alone; the control plane unknown ("3 pods not visible": CoreDNS and the storage provisioner); the cordoned worker clear. All four went unknown two hours and a minute later.
- CI's kind job also ran the paged read.
- **Not checked live:** a deployed Actuator on its own hourly clock, the Nodes page in a browser against a running Hub, the route through a running Hub's Caddy beyond the smoke test, a report cut at 1000 pods, a cluster behind a Relay, and what Cluster Autoscaler or Karpenter do with these pods. The rules come from their documentation.

## 15.1c The fallback without an Actuator

**Grilled 2026-11-14, all recommended:**
- Cluster Autoscaler's own count shows as its total, and its per-reason counts untranslated. This changed from the phase's grill, which had them translated. *Why:* its source labels each reason with the number of a list that grew between releases, so the Hub cannot tell which version it is reading.
- A pod with no controller seen only through Prometheus is blocked under `kubectl drain`'s and Cluster Autoscaler's rules. The latter says "unless annotated safe-to-evict: true, which only an Actuator can see".
- The fallback applies whenever there is no usable report: no Actuator, none yet, or one older than two hours.

**Built 2026-11-14:**
- `usage.rooms` reads each pod's controller kind from `kube_pod_owner`, which says `<none>` outright. The live check showed a pod with no controller has no `created_by_kind` label, so the fallback never fired on a real cluster.
- It reads the PodDisruptionBudgets of its namespaces that allow no disruption. Both are stored with the cluster's nodes. No migration.
- `blockers.seen` gives a node's verdict without a usable report: blocked by a pod with no controller, or a custom one (not under Karpenter's rules). Otherwise unknown, never clear. A budget at 0 in the pods' namespaces is named, since Prometheus does not say which pods it covers.
- A fresh report is believed over Prometheus, since it sees the annotation that lifts a rule.

**Chosen beyond the grill:**
- Without a usable report, a node is never clear.
- A pod whose controller kind kube-state-metrics doesn't give is unknown, not taken for one with none.
- A pod read before 15.1c, whose kind isn't stored, blocks nothing.

**Looked up (2026-11-14, Cluster Autoscaler's source at tags 1.30.7 and 1.34.1):**
- The gauge's `reason` label is the reason's number, since it is formatted from an int with no String method. 1.34.1 has two reasons more, inserted after the seventh, so every later number is 2 higher ("blocked by a pod" is 13, then 15).
- The gauge is set only for reasons with nodes in that loop. A reason that fell to zero keeps its last value. It is not set during scale-down cooldown, and in 1.34.1 not without candidates.
- Its reasons include nodes simply in use, so it is no cross-check of the Hub's blocked nodes, as the grill's answer had assumed.
- Put back to the Operator, who dropped it (2026-11-14). The read, stored field and page line were built and then removed. The guide names why under its limits.

**Tests:** 22 mutations, all killed. Checked live with 15.4, below. Not checked on a real Prometheus: the budget series, since the lab has no PodDisruptionBudget.

## 15.2a The Hub's side of Placement

**Built 2026-11-15.**

- `Cluster.placement`: `label` (`key=value`) and `taint` (`key[=value]:effect`). It is refused without the cluster's `actuator_token_secret`, and it names the cluster.
- `usage.rooms` marks each node `pinned` by the label, as kube-state-metrics names it: `label_` and the key with every other character replaced by `_`.
- `blockers.verdicts`: a Pinned node's verdict is `pinned`, and its Blockers are kept unchanged. Each blocking pod gets its `placement` (the Workload it is placed by) and a `note`. A blocked node gets `restart` (`_restart`: the Workloads to restart) or `why_not`. `could_go` lands nothing on a Pinned node.
- `blockers.placing` comes from the same verdicts as the page, so the two can't disagree.
- `Desired.placement` (label, taint, Workloads) is read by no Actuator yet. It is None for a cluster without Placement, without a read of its nodes or a usable report, and under the Kill switch.
- Shown on the Nodes page and in the Hub MCP's `nodes` (`placement`, a blocker's `placed_by`, a node's `restart`). `judged` counts `pinned` only where the cluster has Placement. No migration: the read and report are stored as JSON.

**Chosen beyond the grill:**
- A custom controller's pod is not placed, though the grill listed it. The webhook names a pod's Workload from its owners and labels, and returns none for a custom controller (`actuator.workload_of`). So only a Deployment, StatefulSet, Argo Rollout or CronJob is placed.
- The restart advice needs the Actuator to say it places, and which pods carry the preference. So the report gains `placing` and a pod's `placed`, filled by 15.2b. Until then no node says "restart". A Hub of 15.2a takes them first, since the report refuses unknown fields.
- The restart's room counts every pod of those Workloads not already on a Pinned node, since a restart moves them all, whatever the Pinned nodes' taints (a placed pod tolerates the Placement's). Each node is judged on its own.
- Where no node shows the label, the page says to allowlist it in kube-state-metrics.
- A read says which label it was made with (`placement`). One made with another label pins no node until the next hourly run, as when the label was set or changed since, or with a read from before 15.2a. The page says so.
- An Actuator posts `placing` and `placed` only when true (`blockers.sent`), so a Hub from before 15.2a still takes the report of an Actuator that doesn't place.
- The Hub MCP caps the lists: 20 placed Workloads, five to restart, three pods in a `why_not`. The rest are counted.

**Open, for 15.2b:** check how kube-state-metrics names a capital-letter label key (`eks.amazonaws.com/capacityType`) against the substitution, in the live check. And the list joins two hourly snapshots, so a Workload just rolled out may drop off it for an hour. 15.2b closes this.

**Tests:** two Contracts' lists kept apart. 50 mutations, all caught. The Hub MCP's `nodes` description changed; no Scenario uses it, so no eval rerun.

## 15.2b The webhook places pods

**Grilled 2026-11-15, all recommended:**
- The Actuator reports each blocking pod's own Workload, as its webhook names it. The Hub's list then comes from one snapshot, which closes 15.2a's gap after a rollout. Not a day's grace on the Actuator, and not accepted.
- One Event on a Workload when it joins the list, and one when it leaves. Not one per pod: the webhook has no pod name or UID to put it on, and the minute loop would post one per pod at every rollout.
- A pod carries the preference when its own affinity has a preferred term with the label's key and value, whoever put it there. No annotation of the Actuator's is used.
- Not asked, each with one sensible answer: `key=value:Effect` is tolerated with `Equal`, and `key:Effect` with `Exists`. A Workload leaving the list leaves its running pods as they are. The Kill switch and the pause annotation stop it. No new permission.

**Built 2026-11-15:**
- ADR 0006 came first.
- The webhook (`actuator.placed`) adds one preferred node affinity term of weight 100 for the label, at the end of the pod's own affinity list. None is added where a term already prefers that key and value. The named taint's toleration is added where the pod has no matching one.
- It applies only to a Workload in the list, never a DaemonSet's pod, and under every rule the webhook keeps: CREATE only, no `kube-*` or its own namespace, not a paused Workload, nothing under the Kill switch.
- The Actuator keeps the Placement in memory (`place`): None turns it off; a list the Hub can't make now (`workloads` None) keeps the last one; under the Kill switch the list is kept and nothing is placed.
- An Event `Placed` or `PlacementEnded` is raised on each Workload that joins or leaves, and said again after a restart.
- The report carries `placing` (with a Placement), and per pod `placed` and `workload` (`Kind/name`, by `workload_of`). `workload` is sent only with a Placement. The Hub refuses one not shaped `Kind/name`.
- A placed Workload's pause annotation is read each minute, as a held one's is. The Events go at the end of the minute, on their own, before the rollback watch.
- Under the Kill switch the report does not say it places, so the Hub names no Workload to restart while nothing is placed.

**Chosen beyond the grill:** the Hub sends a Placement with no list (`workloads` None), where 15.2a sent none. So None means off, and the Actuator stops placing when the Contract's Placement is removed. 15.2a was not released.

**Tests:** each branch, each patch applied to the pod with `jsonpatch`. 33 mutations, all caught. One equivalent: a pod's `placed` without a label, since no key is empty. No model-visible change, so no eval rerun.

**Checked live 2026-11-15** on a throwaway kind cluster (one control plane, three workers). `worker3` was labelled `squero.io/pinned=yes` and tainted with it, and also labelled `example.com/PinnedPool=Yes`. kube-prometheus-stack 92.11.0 allowlisted every node label. A Deployment `keep` of 3 pods, annotated `safe-to-evict: "false"`, ran on the two other workers, with `web` beside it. This is the phase's done-when:
- Before a read the Hub sent the label with no list. After it, `worker3` was pinned and `keep` listed; both nodes it held read "Restart shop/Deployment/keep".
- A `Placed` Event appeared on `keep` within the minute.
- `keep` was restarted by hand. All three new pods landed on `worker3`, with weight 100 and the `Equal` toleration. Once its report came in, both nodes it held had no Blocker (unknown, not clear, since they hold pods of other namespaces), and its pods read `placed` with their own Workload.
- After an Actuator restart, `Placed` was said again. With the pause annotation or the Kill switch on, the new pod was left alone.
- With room on `worker3`, every placed pod went there: 10 of 10, at 21% CPU. None landed elsewhere while it had room, so "had no room when it started" stands.
- With `worker3` filled by an outside pod, the next two placed pods started on the other workers. Nothing was Pending; each got the note, and the two started under the pause and the Kill switch without it. Both nodes read "the pinned nodes lack room: about 0.7 cores short" (4 × 200m off it, 100m free).
- With Placement removed, the Hub sent none, `PlacementEnded` appeared on `keep`, and its next pod was not placed.
- **Found:** kube-state-metrics names `example.com/PinnedPool` as `label_example_com_pinned_pool`, so a capital-letter key was never found. `contracts.ksm_label` now does both, and is shared with 10.2b's team labels on pods, which had the same flaw.
- Everything was deleted. The Mac's `squero` cluster was not touched.

## 15.3a Sizings per instance type

**Built 2026-11-14.**

- `usage.pod_types` reads each of the Contract's pods' node and that node's instance type over the Sizing's window. It is behind the node-read guard and runs only for a cluster with an `actuator_token_secret`.
- `usage.usage` keeps a DaemonSet's container per type (`ContainerUsage.groups`): its own pods' samples, requests and limits, counted from the Running phase, with throttling and OOM kills carried over. Groups are dropped when there are fewer than two types.
- `sizing.rows` sizes each group by `size` under the container's policy. The row keeps its identity and its pooled Sizing, so approvals, the Actuator, Waste, Cost and Reverts are untouched.
- Shown by `sizing show`, the API (`Container.groups`), the console, and the Hub MCP's `sizings` (ten types, the rest counted). The Triage agent's renderer does not carry them: no eval rerun. No migration.

**Changed from the grill:** Waste stays on the pooled Sizing until 15.3b, when an Actuator can apply the types. Before that nothing could reach that money, and a type still learning would count nothing. A type's Waste must be against its own pods' requests.

**Found while building:** the pooled Sizing "fits the busiest" only for memory, a peak. For CPU, a percentile over every pod's samples, it does not: with one busy node among thirty, the pooled CPU target was under 100m while that node's pod used a core. So today's DaemonSet Sizing asks too little CPU for a busy minority.

The Operator's decision (2026-11-14): a risk on the row, `type_needs_more_cpu`, where a ready type needs more CPU than the one Sizing by more than a change the policy would make. It flags, changes nothing applied, and shows only where types are read.
- Not chosen: leaving it until 15.3b, or never lowering the row below its highest type (that would change what Automation applies).
- [Audit A9](audit.md) later removed this risk, since the pooled CPU target became the largest of each node's own percentile.

**Chosen beyond the grill:**
- A pod whose node isn't found is in no type. It would make one that is always learning, so it stays in the pooled Sizing.
- A type's requests are its own pods'. No cap by the largest Room, as for the row.
- A failed read of the types means none that hour, and never fails the cluster.

**Tests:** 38 mutations, all killed after three tests were sharpened.

**Checked live 2026-11-14, 22:55 to 23:10 UTC** (the read only): a throwaway kind cluster on the Mac, two workers given `m5.large` and one `m5.4xlarge` by hand. A DaemonSet's pod worked only on the large node. No Hub process, no model calls. `usage.pod_types` equalled the API server's. The container had three groups, each with its own pods' samples (0.17 cores at most on the large node, none on the others) and requests, and the run's row carried them, all still learning. None for the same cluster without an Actuator.
- **Not checked live:** the Sizing of each type and the risk, which need an hour of usage at the shortest policy (left for 15.3b's check). The page in a browser.

## 15.3b The Actuator applies each type

**Grilled 2026-11-14, all recommended:**
- Three slices in all: 15.3b is the Actuator applying each type's Sizing by a pod's node; 15.3c is the money by type.
- A pod whose type has no Sizing yet (still learning, or its node's type unknown) gets its template's requests, not the pooled Sizing.
- Only on clusters that run an Actuator. There, Waste counts each type against its own Sizing, since one template cannot be sized per type by hand.
- Nodes without an instance-type label are one type: today's pooled Sizing.

**Built 2026-11-14:**
- The Hub: `Wanted.types` (`sizing.by_type`, each ready type's requests, none where it keeps one). `Desired.node_types` (node to type, `usage.node_types`, over the longest window), stored with the rooms behind the node-read guard, and only for a cluster with an Actuator. Both go only to an Actuator that says `types=1` on its pull. An approval stores its run's types (`approved_sizings.types`, migration 0039). A row goes to such an Actuator when some type changes a request; to an older one, when the pooled Sizing does.
- The Actuator: a hold can carry `types`. Every look at a pod goes through `wants`: the pod's node's type's requests, its template's for a type with none, and nothing while it knows no node's type. `at_hold` covers every type, so the canary, rollout, catch-ups, watch, Rollback and Revert all follow.
- `apply` builds one set per type, with the limit's cap and Automation's churn rule each type's own. A type not in the new Sizing keeps what it holds; a type first sized starts from the pooled hold. The QoS and LimitRange checks run per type, and the quota for the most any type asks. It skips, and tries again, while it has no node's type.
- The webhook reads a new pod's node from its node affinity (`node_of`), a shape read from a real DaemonSet pod on the lab's kind cluster (v1.37).
- A Step-up takes every type's memory, and the lowered-memory Rollback rule looks at every type.

**Chosen beyond the grill:**
- The Hub sends the nodes' types, because the Actuator has no access to Nodes.
- An Actuation of a hold with types carries no `cpu` or `memory`; it names the types in its reason, three at most.
- `approve_sizing` refuses a container with more types than the ten `sizings` shows. A container is never approved unseen. Automation still applies it.
- The quota is checked conservatively, every pod at the most any type asks, so it can skip a container an exact sum would pass.
- Nodes' types are not kept across an Actuator's restart. The Hub sends them with every answer to an Actuator that knows types, so a node that joins later and a restarted Actuator are caught up.
- An Event of such an Actuation says "requests by instance type".

**Tests:** every branch. 57 mutations, all caught. Two Contracts' nodes kept apart. The only model-visible change is `approve_sizing`'s description and answer: no eval rerun.

**Open, for 15.3c:** once types are applied, the pooled row can still say "lower", so its Waste shows money no approval reaches. And `approve_sizing` answers "approved" for a container whose types change nothing. An Actuator that knows types is never sent such a container.

**Checked live 2026-11-15, 00:58 to 02:43 UTC** (15.3a and 15.3b together): a throwaway kind cluster on the Mac, workers given `m5.large`, `m5.4xlarge` and `c5.large` by hand. The working tree's Actuator image ran, and the Hub app on localhost with a throwaway database, a synthetic Contract and a scripted model. No model calls, no cloud. The DaemonSet asked for cpu 500m and memory 64Mi. It reached the `c5.large` node only after 57 minutes.
- **First run** (1.1 hours of usage under a policy asking one): pooled cpu 349m, memory 32Mi. `m5.large` and the untyped node cpu 10m; `m5.4xlarge` cpu 349m; each memory 32Mi; `c5.large` still learning (0.2 h). Before approval, a types-aware Actuator was sent the four nodes' types; an older one was not.
- **Approved** through the store call `approve_sizing` makes (the tool was not driven), with its three types: the Actuator that knows types got them, the other the pooled 349m. It resized a canary first (the untyped node's pod to 10m and 32Mi), then the other two after ten minutes, each to its type's requests. The `c5.large` pod kept its template's. "3 of 4 pods resized"; the Actuation carried no `cpu` or `memory`.
- A deleted pod's replacement was caught up within a minute. New pods were given their type's requests by the webhook and never resized (generation 1), the `c5.large` one with its template's. After an Actuator restart it resized nothing, and a new pod still got its type's requests.
- **Revert** put the three pods back to cpu 500m and memory 64Mi.
- **Found:** a put back's reason read "back from by instance type". It now reads "back from each instance type's requests: ...".
- Setup mistakes, not code: the webhook's namespace selector was left on the shipped `demo`, and new pods were made while a catch-up's canary waited. The webhook gives nothing while a hold rolls.
- **Not checked live:** the risk where the pooled CPU asks less than a type needs (here the pooled Sizing was the busy type's), a pre-check failing for one type, Automation's churn rule by type, a Step-up on such a hold, a node joining (kind adds none), `approve_sizing` through the Hub MCP, and more than a few nodes.

## 15.3c The money by type

**Grilled 2026-11-15, all recommended:**
- Realized Savings: one drop per type. A container's drop, once its types are applied, becomes one per type. A type's drop ends with its type, or when types are no longer applied.
- The Hub remembers per cluster whether its Actuator applies types. Only there does a container with types go by them. Its pooled Sizing is shown, not applied, and raises no type risk. `approve_sizing` refuses it when no type changes a request. A cluster with an older Actuator stays as it was.
- Nodes that could go after the Sizings give a DaemonSet's pod its node's type's Sizing. This is in this slice.
- Decided without asking, said with the grill: a type still learning counts no Waste; pods on no known node cost the container's request; the first digest after the switch compares a week by type with a pooled one. No eval rerun.

**Built 2026-11-15:**
- Migration 0040: `cluster_actuators` holds each cluster's `types`, written only on a change. `realized.type` (null means the container's, as every earlier drop) joins the unique key. Nulls are not distinct on Postgres 15 and later; the compose stack runs 17.
- `sizing.rows(..., typed)`: on those clusters a row with groups is `by_type`, its Waste the sum of its ready types' own. Each type is stored with its `waste` and `live`. `sizing.requested` sums each type's pods' requests, counting pods on no known node at the row's requests, never below zero.
- `realize` runs per type, matching new drops against that type's previous run and shown Sizings. A type not live leaves its drop as it is.
- `savings` values a type's drop by that type's pods and names its `type` (`Drop.type`).
- `blockers.after` gives a DaemonSet pod of a `by_type` row its node's type's targets, or its own request on a type with none.
- Automation sends nothing to a typed Actuator while every type of a container is learning (15.3b fell back to the pooled Sizing).
- The console opens "By instance type" on such a container and says the line above is not applied. The Hub MCP's `sizings` says `applied`.

**Chosen beyond the grill:**
- A container's drops, once converted, keep their run, request and matched Sizing.
- Of a chain of them, only the one with the lowest `after` goes down, to what each type's pods ask at the conversion. The others never go below their own, so the chain adds up to the first request less the type's. A type's lowering since the last run is in it, and no second drop is made that run.
- A run that cannot read the pods' instance types (`Collected.untyped`) leaves the DaemonSets' drops as they are and makes none. That hour's Waste is the pooled one.
- A type's pods not read live keep its drop, as a container's do.
- The Triage agent's `sizing` tool is unchanged in shape. Adding the types to it would be a model-visible change and an eval rerun.
- `squero sizing savings` names a drop's type.

**Tests:** every branch. 45 mutations: 44 caught, one equivalent (a learning type left in `blockers.after`'s map has no targets, so its pods keep their own request either way). Two Contracts' flags and drops kept apart.

**Checked live 2026-11-15, 04:19 to 05:49 UTC:** the 15.3b set-up on a throwaway kind cluster, the DaemonSet busy only on `m5.4xlarge`; the `c5.large` pod started 56 minutes in. Synthetic prices (0.04 USD a vCPU-hour, 0.005 a GiB-hour). Later Sizing runs were taken through `sizing.store_run`, the hourly tick's own function. The Actuator's first pull stored its cluster as applying types, so every run was by type.
- **Found:** a type's pods were averaged over that type's own time, so a 12-minute-old pod counted as a whole pod over the window, and the cost of requests read 2.0 cores for 3.2 pods. Fixed: each type keeps `pods` as shown and a `share` of the container's pods, averaged over the container's span. Waste, cost of requests and drops are counted on the share, so the types add up to the container. Tested and mutation-checked.
- **Before approval:** Waste 1.131 cores (33.37 USD a month); the pooled Sizing alone would have counted about 0.48. Cost of requests 1.607 cores (47.66 USD a month).
- **Approved:** the Actuator rolled out canary first, then one at a time. Each agent pod then sat at its node's type's requests (10m, 10m, 349m), and the learning type's 500m was kept.
- **Next run:** Waste 0; cost of requests 0.569 cores (17.05 USD a month). Six realized drops (cpu and memory per ready type) totalled 1.131 cores and 96 MiB, 33.37 USD a month: the Waste the approval took away. Before this slice no drop would have been seen.
- **Not checked live:** a container's drop converted into one per type (and a chain), a failed read of the types, a type's drop ending, the console page in a browser, `approve_sizing` through the Hub MCP, and `squero sizing savings`. Each is unit-tested.

## 15.4 Nodes that could go

**Built 2026-11-14**, on the phase's grill. Checked live with 15.1c, below. No new round: its answers, 15.1a's "the Hub MCP tool comes with 15.4" and 15.1b's "an unknown node is never counted" covered it.

- `usage.rooms` also reads each node's capacity and pod slots, and each pod's containers' requests, with the Workload the owner series resolve to (`resolve`).
- `blockers.could_go`: a clear node, not a control-plane one, whose moving pods each fit whole (CPU, memory and a slot) on one other, uncordoned node of its taint group. It goes by fewest requests first, largest pod first, then the first node by name.
- `blockers.after` gives the same read with the ready Sizings applied, container by container.
- `sizing.nodes_report` prices the nodes at capacity and counts the verdicts; `sizing.nodes_line` is the digest's line. `webapp.nodes_of` serves the page and the Hub MCP's new `nodes` tool: blocked nodes first, `top` of them, each node's blockers capped at 5. No migration; no eval rerun.

**Changed from the grill:** computed when asked, not each hour and stored, so it always agrees with the verdicts beside it, which depend on the report's age. Measured: 300 nodes of 30 pods, both scenarios, in 0.18 s.

**Chosen beyond the grill:**
- Every gap means the node stays. A node without its allocatable or slots neither goes nor receives. A read from before 15.4 says nothing.
- A pod fits pod by pod, not by a sum.
- The taints Kubernetes sets itself, and `PreferNoSchedule`, are no group, so a cordoned node's pods can move.
- A cordoned node may go and receives nothing. A node under a condition taint (not ready, under pressure) neither goes nor receives.
- A control-plane node never goes (clear and empty, it would head the list).
- A node that received pods and then goes must place those too. A bug its test caught.
- After the Sizings, a container without a ready Sizing, or of a pod whose Workload isn't known, keeps its own request. One that asks nothing today asks its Sizing. Held Sizings and raises count as applied.
- Priced at capacity, as the Bill check counts nodes; allocatable where capacity wasn't read.
- The digest's line appears only when a node could go, and counts the nodes not judged.
- The `nodes` tool is the first path from an Actuator's strings to a model. So a report whose namespace, pod, node or budget name isn't shaped like a Kubernetes name is refused on arrival. A kind or volume name that isn't is counted, not repeated.

**Tests:** 80 mutations, all killed after twelve tests were sharpened.

**Limits:**
- An upper bound. Affinity, spread, tolerations and the autoscaler's settings are unseen. (The [audit's A3](audit.md) later added node constraints to the fit.)
- It uses the Hub's own unit prices, not a node pool's actual price.

**Checked live 2026-11-14, 20:35 to 21:10 UTC** (with 15.1c): the 15.1b throwaway kind cluster, one Contract with typed prices, no Hub process, no model calls.
- The capacity, pod slots, and each pod's Workload and requests equalled the API server's. Workloads came through the owner series (`Deployment/web` behind its ReplicaSet, `DaemonSet/agent`, `Pod/solo`). The budget `web` read as allowing no disruption.
- **Found:** kube-state-metrics gives a pod with no controller no `created_by_kind` label, while the test fixture had `<none>`. So without an Actuator `solo` was unknown, not blocked. Fixed by reading `kube_pod_owner`, and the fixture was changed to what the cluster showed. After the fix that worker was blocked under `kubectl drain`'s and Cluster Autoscaler's rules, and unknown under Karpenter's.
- **Could go:** with the Actuator's report read through a ServiceAccount bound in `shop` and posted in process, one node was clear (the cordoned worker), two blocked, the control plane unknown. The clear node could go now, priced at 406.21 EUR a month, which matched its capacity (10 cores, 31.29 GiB) at the Contract's prices by hand. The digest carried the line with "1 not judged".
- A pod asking 9.6 cores was then put on that worker: nothing could go now (the others had 9.44 and 9.39 cores of Room), and the digest had no line. With a made-up ready Sizing of that Workload at 100m, it could go after the Sizings.
- The Hub MCP's cluster view listed blocked nodes first, with `more_nodes` counted.
- **Not checked live:** Sizings from real usage changing the list (the containers were all still learning, so that Sizing was synthetic), several taint groups, masking through the MCP, and the page in a browser.

## 15.5 Compaction with the descheduler

**Deferred** (grilled 2026-11-12). The reasons are in the decisions above.

## 15.6 The change that removes a Blocker

**Grilled 2026-11-14 (one question, the recommended answer):** a Blocker someone set on purpose gets words and no manifest: a PodDisruptionBudget allowing no disruption, `safe-to-evict: "false"`, and `do-not-disrupt`. *Why:* a budget at 0 is also a live status (a pod that is down), and a manifest that removes a protection is easy to paste in the middle of an incident. The others get a manifest.

**Built 2026-11-14:**
- `blockers._found` gives each reason with its rule, so `reasons` and `changes` come from one source. `blockers.changes` gives, per reason, the change in words, what it costs, and a manifest where it adds something. Examples: `safe-to-evict: "true"` for a pod with no controller or a custom one, under Cluster Autoscaler; `safe-to-evict-local-volumes` naming every local volume; a PodDisruptionBudget for a kube-system pod; under `kubectl drain`'s rules, the flag that does it and what it deletes.
- `blockers.SETTINGS` names the autoscaler's own settings to check.
- Shown on each blocking pod of a verdict, with its Workload from the Hub's read: on the Nodes page ("To remove it") and in the Hub MCP's `nodes`, where a Workload's replicas blocking for the same reasons are one entry.
- No migration and no new fact in the report, so an older Hub keeps taking a newer Actuator's report. No eval rerun.

**Changed from the grill:** "the Operator turns one into a pull request with the Hub MCP server's existing tool" does not hold. `propose_change` edits only a Contract's Sizing policy in the contracts repo, so the change is to copy. Writing to a client's repo would be a decision of its own.

**Chosen beyond the grill:**
- What a change costs is in the manifest's comments, with what changing a pod template does: it restarts every pod of the Workload. A CronJob's change reaches only later Jobs. A Job's template cannot be changed.
- An annotation goes on the Workload's pod template where the read resolved a kind that has one; for a CronJob, under `jobTemplate`. Otherwise it goes on the pod, and the manifest says so. A pod with no controller is annotated itself.
- The kube-system budget's selector is a placeholder that selects no pod until replaced. It is never an empty selector, which in `policy/v1` covers the whole namespace.
- The local-volumes annotation names every local volume, including those a partial annotation had.
- A pod's, Workload's or volume's name not shaped like a Kubernetes name never enters a manifest, including on the path without a report, where those names are Prometheus' labels.
- For a Contract that masks, the Hub MCP gives a manifest with placeholders; the page has the real names.
- A duration of `do-not-disrupt` needs no change.
- The settings carry their documented defaults (looked up 2026-11-12), not the cluster's own values.
- Each manifest is tested as a round trip (applied to the pod's facts, the reason is gone) and as YAML.

**Tests:** 50 mutations, all caught.

**Checked live 2026-11-14, 22:25 to 22:50 UTC:** a throwaway kind cluster on the Mac (Kubernetes 1.37) with a bare pod, a pod owned by a ConfigMap as its controller, a CronJob with an emptyDir, and the Actuator's ServiceAccount also bound in `kube-system`. A Contract named `cluster-autoscaler` over `shop` and `kube-system`. No Hub process, no model calls.
- The Hub made five manifests and words for two more (the budget at 0 on `web`, and `safe-to-evict: "false"` on a pod). The API server accepted all five as server-side dry runs.
- Three were applied for real: the budget, with its selector set to CoreDNS's label (then it allowed 1 disruption); the Deployment, which rolled out; and the bare pod. The next report no longer gave those pods a reason; the four not applied remained.
- A Job's template was refused by the API server as immutable, as its comment says.
- **Found:** "from its pod template" was said of a bare pod too. It now reads "where it is set (its Workload's pod template, or the pod itself)".
- **Not checked live:** a StatefulSet's and a DaemonSet's template, a Contract that masks, the page in a browser, and what Cluster Autoscaler does next.
