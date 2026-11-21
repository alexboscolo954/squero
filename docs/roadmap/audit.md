# Audit fixes

**In short:** On 2026-11-16 the roadmap was audited for defects found and left open, with Part A's findings from the AWS checks. The Operator asked for every defect to be fixed before work continued. These 14 slices fix them: a Prometheus counted twice, a title cut before masking, late Blockers, a memory suggestion that could raise nothing, and more.

**Status:** A1 to A13 are done (built 2026-11-16 and 2026-11-17). A14 was built 2026-11-20 and reaches the mini PC's Hub with the next release and upgrade.

| Slice | What | Status |
|---|---|---|
| [A1](#a1-one-prometheus-under-two-names) | One Prometheus under two names | built 2026-11-16 |
| [A2](#a2-a-title-cut-before-masking) | A title cut before masking | built 2026-11-16 |
| [A3](#a3-the-node-fit-sees-node-constraints) | The node fit sees node constraints | built 2026-11-16 (A3a, A3b) |
| [A4](#a4-blockers-reported-when-they-change) | Blockers reported when they change | built 2026-11-16, not checked live |
| [A5](#a5-no-node-could-go-without-the-pinned-nodes-label) | No node could go without the Pinned nodes' label | built 2026-11-16 (A5b decided), not checked live |
| [A6](#a6-gcps-bill-still-settling) | GCP's bill still settling; the Agenda | built 2026-11-16 (A6a, A6b), A6a not checked live |
| [A7](#a7-the-reports-hardened) | The Actuator's reports hardened | built 2026-11-17 (A7a, A7b) |
| [A8](#a8-reflex-leftovers) | Reflex leftovers | built 2026-11-17 |
| [A9](#a9-a-daemonsets-cpu-fits-the-busiest-node) | A DaemonSet's CPU fits the busiest node | built 2026-11-17 |
| [A10](#a10-a-contract-takes-at-most-half-the-triage-slots) | A Contract takes at most half the Triage slots | built 2026-11-17 |
| [A11](#a11-ceilings-whose-code-names-their-fix) | Ceilings whose code names their fix | built 2026-11-17 (A11a, A11b) |
| [A12](#a12-the-eval-regression-on-the-default-model) | The eval regression on the default model | built 2026-11-17 |
| [A13](#a13-the-weeks-resizes-in-the-weekly-digest) | The week's resizes in the weekly digest | built 2026-11-17 |
| [A14](#a14-stale-notices) | Stale notices | built 2026-11-20 |

## Decisions (grill, 2026-11-16)

Three rounds, all recommended. The slices run in this order, one slice each.

- **Splits:** A3 into A3a (the Hub) and A3b (the Actuator). A6 into A6a (the bill) and A6b (the Agenda). A7 into A7a (the reports) and A7b (what supersedes an approval). A11 into A11a and A11b.
  - *Why* A7 has a stop between its halves: A7b keeps an approval current for longer, and the Operator saw that before it was built.
- A4 was grilled further on 2026-11-16, and A14 was grilled on 2026-11-20 in two rounds.

## A1. One Prometheus under two names

**What:** 9.6b's guard compared URLs as text (`contracts._store`), so one Prometheus under two names passed.

**Built (2026-11-16):**
- At start, and in `cli.contract`, the Hub asks each Direct cluster's Prometheus for `/api/v1/status/runtimeinfo`. All asks run at once, each with a 5-second limit and its own cluster's token. Redirects are not followed.
- `startTime` is the store's identity. Prometheus and Thanos Query give it to the nanosecond.
- Mimir and VictoriaMetrics serve no runtime info (read in their sources and docs, 2026-11-16). So they, and a store that is down, are compared by the addresses their host resolves to, with the same scheme, port and path.
- `contracts.regroup` checks again. A match by identity is refused, as a URL written the same is. A match by address only marks the stores shared.
- Asking only joins. URLs written the same stay one store, whatever they answer. `contracts validate` stays offline and compares URLs.
- A failure is logged as `listprices.why` writes it (no URL, no token). Stores joined by asking are logged by cluster, since an HA pair's answer can change between starts.

**Decided:**
- *Why* an address match marks stores shared rather than refusing them: one ingress address often fronts several stores (the Operator, 2026-11-16).
- External labels and `hostname` were dropped. They are often the same across clusters built from one chart, and `startTime` alone tells processes apart.

**Tests:** every branch, and the two Contracts' tokens each sent only to their own store. 26 mutations, all caught. With the real ask made to fail, nothing in the full suite reached it.

**Known limits:**
- Replicas behind one name answer for themselves, so two names for an HA pair can still look like two.
- A Relay's cluster isn't asked.

No tool description changed.

## A2. A title cut before masking

**What:** `api.py` cut a stored title at 200 characters before masking, so a term cut in two went out as an unmasked fragment. Fix: mask first, then cut.

**Built (2026-11-16):**
- A Question's title is the whole Question, up to 4,000 characters, cut where it is shown.
- The Hub MCP's `investigations` and `investigation` mask first and cut at 200 (an Answer is cut at 500, as before, after masking). The console's list and heading cut at 200, with nothing masked there, and its exchange shows the first question whole.
- A Teaching's draft for an Investigation with no Report sends the title masked whole.
- Migration 0043 gives stored cut Questions their whole title back from their input.
- Agenda items keep the title whole, masked whole on the Hub MCP.

**Tests:** 7 mutations, 6 caught. The seventh is equivalent: the migration's `kind` guard, since no other kind's input has a question. Masking per Contract is unchanged, so the isolation tests stand.

No tool description changed.

## A3. The node fit sees node constraints

**What:** the nodes-that-could-go fit ignored node constraints, so squero said a node could go where Karpenter refused ([Phase 16](phase-16.md) Part A, finding 1).

**Decided:**
- The Actuator reports each pod's `nodeSelector` and required node affinity, plus the labels of the nodes those keys name. Under Karpenter it also reports the preferred node affinity, held as required.
  - *Why:* the Hub cannot see whether a NodePool could meet a preference, so it errs strict.
- A label the Hub cannot see counts as no match.
- **Split (the Operator's call, 2026-11-16):** the Actuator could not read nodes, since its ClusterRole had pods and workloads only. A3b asks for an optional ClusterRole. Phase 15 had promised no new permission, so it was asked of the Operator.
- Without that ClusterRole, a cluster lists no node that could go, and says why. Placement's restart advice goes by the pods' requirements.

### A3a: the Hub's side

**Built (2026-11-16):**
- When the Hub asks (`Desired.constraints`, also under the Kill switch), a report may carry `constraints` (a code: `read`, `cut` or `nodes_refused`; the Hub writes the sentence), each distinct `Need` once (its `nodeSelector`, its required terms and its preferred terms with their weights), `needed` (`namespace/pod` to its need) and each node's `labels`, only for the keys the needs name. These are facts, not a verdict.
- `sent()` leaves them out unless read, so an older Hub takes the report. `unreal` refuses any key, value, node or pod name of no shape Kubernetes allows, or a need that isn't there.
- `blockers.fit` reads them as the scheduler does:
  - one term of several, every expression of it, with the nodeSelector;
  - a missing label meets `NotIn` and `DoesNotExist` only;
  - `Gt` and `Lt` on integers; `matchFields` on the node's name; an empty term meets nothing.
- Under Karpenter the heaviest preferred terms are added as required, held while any node, a new one too, could meet them. *Why:* `NewPodRequirements` (read 2026-11-16) sorts by weight with the unstable `sort.Slice`, so every term of the heaviest weight is held. Its `matchFields` are kept, since the scheduler binds by them.
- A node whose labels weren't sent takes no pod that needs something.
- 15.4's fit places a pod only where it fits. The restart advice goes by the pod's requirements alone. A restarted pod is placed by the scheduler, where a preference only scores, whoever consolidates.
- A report without these counts no node (an Actuator from before A3b, one cut short, or one that refused the nodes). The Nodes page, the Hub MCP's `nodes` and the console say why (`could_go.note`).

**Tests:** Part A's three refusals first (a pod preferring spot under Karpenter and not Cluster Autoscaler; a `nodeSelector` on Karpenter's NodePool label; `keeper`'s `role NotIn [system]`), then every operator, and two Contracts' reports stored apart. 48 mutations, all caught. The Hub MCP's `nodes` description changed; no Scenario uses the Hub MCP, so no eval rerun.

**Known limits:**
- Until A3b ships, the Nodes page lists no node that could go, and says its Actuator doesn't report what its pods need.
- Karpenter's setting to check: `--feature-gates SpotToSpotConsolidation`. It is alpha and false by default. When on, a single spot node can be replaced by a cheaper spot node, when 15 cheaper types could take its pods.

### A3b: the Actuator's side

**Built (2026-11-16):**
- When the Hub asks, the hourly report carries what each pod that would move needs of a node (`actuator.need_of`, the webhook's own among them), as written: `nodeSelector`, required terms with their `matchFields`, and preferred terms with their weights. `stays` is now the one test of a pod that stays with its node, shared with the Blocker facts.
- Each distinct need is sent once. Every node's values of the label keys those needs name are sent (`constrained`), listed a page at a time through the optional ClusterRole `squero-actuator-nodes`. No need, no node read. Nothing else of a node is read.
- Codes, never text:
  - `nodes_refused` (403);
  - `nodes_unread` (any other failure; the Hub words it "its last read of the nodes' labels failed");
  - `cut`: over 10,000 pods' needs, 1,000 distinct needs, 5,000 nodes, 100 label keys, a need over its own limits, or the megabyte. The needs are dropped before any pod.
- A namespace whose read failed sends none of its needs.
- The Nodes page counts nodes that could go again.
- [`client-cluster.md`](../client-cluster.md) and [`safety.md`](../safety.md) say what a Node object holds, and what leaves the cluster.

**Decided:** the grill named `get` on the ClusterRole. It is built with `list` only, since listing is all it does.

**Tests:** every branch, Part A's NodePool case end to end, the RBAC role and binding, and the integration test listing a real cluster's nodes a page at a time. 29 mutations, 28 caught (one by hanging the fake API's paging, which fails the run), 1 equivalent (reading needs the Hub didn't ask for).

## A4. Blockers reported when they change

**What:** the hourly Blocker report was late. A rollout's new pods went unseen for up to an hour, and the Actuator's own pod showed as unknown (Part A findings 2 and 4).

**Decided (grilled further 2026-11-16, the Operator's calls, all recommended):**
- The Actuator cannot read its own pod, since its pod reads are bound in the Contract's namespaces only. It reads it through a new optional Role in its own namespace (`get pods`).
- Its pod is judged by the same rules as any pod's, not as "never a Blocker". Its `tmp` emptyDir blocks under Cluster Autoscaler and `kubectl drain`, so the manifest names it in `safe-to-evict-local-volumes`.
  - This replaces the first fix's rule that its own pods are reported as known.

**Built (2026-11-16):**
- The Actuator checks its Blockers every 5 minutes (`BLOCKERS_CHECK`). It sends them when they differ, times aside, from the last report the Hub answered, and once an hour regardless. A report that did not reach the Hub goes again 5 minutes on. A refused report, or an older Hub's refusal, is not repeated on each read.
- A rollout's new pods reach the Hub within 5 minutes.
- When the Hub asks, the report carries `itself`: the Actuator's own pod (`SQUERO_ACTUATOR_POD`, from the downward API), read through the optional Role `squero-actuator-self`, with the facts any pod's has (its containers' requests, and its need as an index into `needs`). It is never a key of `needed`, whose namespaces must be the cluster's. The Hub refuses one of no shape an Actuator sends.
- `blockers.known` counts the Actuator's pod on its node, where that node has a pod the Hub can't see, but only for a read made at least 5 minutes after the Actuator started (`SEEN`: an older read can't hold it, and counting it there would hide another pod). An older read cannot hold it. Never counted for a pod of the cluster's own namespaces.
- `could_go` moves the pod with its requests and need. Under Cluster Autoscaler its node can go; under `kubectl drain` it blocks (15.6's change says how).
- Without the Role, its node stays unknown.

**Tests:** every branch; two Contracts' own pods stored apart; the manifests' Role, binding, env and annotation. 31 mutations, all caught. A test joins the halves: the Actuator's pod object through to a clear verdict, and without the Role to the same unknown. No eval rerun, since the Hub MCP's descriptions are unchanged.

**Known limits:**
- A PodDisruptionBudget a client adds in the Actuator's namespace isn't read, since the Role reads pods only.
- A read twelve times an hour lists the namespaces' pods and PodDisruptionBudgets, and the nodes where a pod needs something of one.
- `started` is the cluster's clock and the read's time is the Hub's, so `SEEN` absorbs a skew under 5 minutes only.
- A report up to two hours old places its pod on the node it last named, if the Actuator has since gone down.
- Clients must apply the manifests again (env, Role, annotation). A new image alone leaves its node unknown.

**Not checked live:** owed on kind (free): the real Role and env, kube-state-metrics' real counts, and a rollout seen by the Hub within 5 minutes.

## A5. No node could go without the Pinned nodes' label

**What:** with the Pinned nodes' label not allowlisted in kube-state-metrics, the pinned node was listed as one that could go (Part A finding 5).

**Built (2026-11-16):**
- Where a cluster has Placement and its read shows no Pinned node (no node carries the label as kube-state-metrics shows it, or the read was made before the label, or with another), `nodes_report` counts no node as one that could go, now or after the Sizings. It says why (`could_go.note`, on the Nodes page and in the Hub MCP's `nodes`), beside the Placement note on how to fix it. This reason replaces A3's where both apply.
- Finding 3 (a CronJob running more often than `consolidateAfter` keeps its node) is a setting to check in [`optimizer.md`](../optimizer.md). Karpenter's v1.15 docs say the timer resets "whenever a pod is added to or removed from the node". The Hub can't read a NodePool, so it names the setting and warns on no CronJob's node.

**Tests:** both causes, and a cluster whose pinned node is read. 4 mutations, all caught.

**Not checked live:** the tests build the read by hand, with no node carrying the label, as Part A's read was.

### A5b: decided and built

**Decided (2026-11-16, the Operator's call, the first of three options after A5's review):**
- *Why:* with the label unseen, a Pinned node cannot be told apart, so it read `blocked` and offered the change that would undo what keeps its pods there.
- Now, where Placement is on and no node shows the label, a Blocker of a Workload the Hub places gets no change. It gets the note "its node may be a Pinned node, which keeps it on purpose: no node shows <label>". Other Blockers keep their changes, and the states stay as they are. The note shows on the Nodes page and in the Hub MCP's `nodes`. The restart advice already said why not.
- Rejected: every node unknown, which hides ordinary nodes' real Blockers; and words in the docs only.

**Tests:** with the label unseen, seen, and with Placement off. 4 mutations, all caught.

## A6. GCP's bill still settling

**What:** a day with fewer billed hours than sampled hours showed as "outside", not as still settling (10.5c's possible follow-up).

**Decided (grilled further 2026-11-16, the Operator's calls, all recommended):**
- The hours compared are RAM GiB-hours, not cores. *Why:* a shared-core E2 machine bills a fraction of a core, while Kubernetes reports 2 vCPU.
- The Agenda leaves out every Workload with a current Approved Sizing on a cluster that runs an Actuator, applied or not.

### A6a: the bill

**Built (2026-11-16):**
- GCP's export sums each day's RAM GiB-hours (`usage.amount_in_pricing_units` of the `Instance Ram running` SKUs; 192 for an e2-standard-2's day in 10.5c's live check), stored per day (`billed_costs.ram_hours`, migration 0044; null for AWS, Azure and earlier reads). `usage.node_hours` gives each UTC day's sampled GiB-hours from the same read.
- A window with a day billed under 95% of its sampled GiB-hours is still settling if the cluster's last good read came less than 3 days after that day ended (`SETTLES`). Then the gap is shown with no verdict, and is not on the Agenda. The CLI says "no verdict", which is not a failure. A note names each such day with both figures.
- Not settling when every day is short. That is a real gap, such as a label missing from nodes, and is judged as usual. Nor is a short day read later settling.
- This is narrower than the grill's "a day under 95%" in those two guards, so a missing label cannot hide a gap for good. *Why:* the first build looked only at the newest day. The bill is read once a day, so two days read soon after they ended can both be short.

**Tests:** each case, at the edges (exactly 95% was caught by a test added for it), and one from BigQuery's answer through the daily read to the report. 10 mutations, all caught.

**Known limits:**
- A window one day short is every day short, so it is judged.
- Hourly samples count a short-lived node whole or not at all, so churn alone can make a fresh day read short. There is no verdict until the next read.

**Not checked live:** the RAM SKUs' hours, read from a real export. Owed with 16.4a's GCP check.

### A6b: the Agenda

**What:** the Agenda (11.2b) ranked Workloads by waste cost alone. An approved Workload waiting for its Actuator now leaves the waste ranking, counted on one line.

**Built (2026-11-16):**
- The waste ranking leaves out each Workload with a current Approved Sizing of any of its containers (`store.approved_sizings`, so until the next hourly run reads its cluster). This applies on a cluster with an `actuator_token_secret`, applied or not. The next ones rise into the three shown.
- One line comes last, kind `approved`, with no link: "2 Workloads approved: their waste is read again within the hour". It counts only wasting Workloads left out.
- `counts()` (the Hub MCP's `now()` across Contracts) leaves that line out, since it counts Workloads, not something to look at.
- The API's Agenda kind gained `approved`; the console types were regenerated.

**Tests:** each branch, and one with two Contracts. 10 mutations, all caught. Only the Hub MCP's `now` description changed, which the Investigation agent never sees.

**Known limits:**
- An approval the Actuator won't apply still leaves the ranking until the next hourly run. Examples: its container was Reverted since, a kind the Actuator doesn't know, or a change of only types to an Actuator that applies none.
- A Contract whose only item is that line no longer says "Nothing needs you now".

## A7. The reports hardened

**What:** from 12.9c's offer: check `kind` against the kinds the Actuator resizes, and cap the stored Actuations per cluster per hour.

**Decided:** split 2026-11-17 into A7a and A7b, with a stop between: A7b keeps an approval current for longer, which the Operator saw before it was built.

### A7a: the reports

**Built (2026-11-17):**
- `/actuator/reports` refuses the whole report (422, naming the kinds) when it has a kind the Actuator doesn't resize (`actuator.KINDS`). An honest Actuator reports only the kinds the Hub sent it.
- It stores at most 1,000 reports from a cluster an hour (`HOURLY_REPORTS`), counted by `received_at`, the Hub's clock, never the Actuator's `at`.
  - A report sent again is not a new one, and is never refused.
  - Past 1,000, the Hub stores a POST's first ones, then answers 429 with a warning line. The Actuator keeps them all and sends them again.
  - A POST of only stored ones is taken (204), so a lost answer can't stall it for the hour.
- An index serves the count (migration 0045).

**Tests:** at the edges (the 1,000th report, the hour exactly); each cluster and Contract has its own hour. 11 mutations, all caught. No eval rerun.

**Known limits:**
- Two POSTs at once can each see room, so a few more than 1,000 may be stored.
- An Actuator keeps its newest 1,000 unsent, so an honest burst above 2,000 an hour (a mass approval or Revert on a big cluster) loses its oldest reports.

### A7b: what supersedes an approval

**Decided (grilled 2026-11-17, the Operator's call):** Sizing runs are pruned (hourly kept 14 days, then each day's last for 400), and approvals never are. *Why:* a supersession computed from stored Sizings would come back once its run was pruned. So it is kept on the approval.

**Built (2026-11-17):**
- An Approved Sizing is superseded by a later Sizing of its container, never by a run that read its cluster without that container.
- Each run marks the earlier approvals of the containers it sized (`approved_sizings.superseded_run`, migration 0046). An approval taken after a later run had sized its container is stored superseded.
- Approvals made before this keep their answer: the migration marks each with the first later run that read its cluster.
- Current approvals are listed in a fixed order: approved, cluster, namespace, kind, name, container.
- An approval whose container no run sizes again stays current for good. So A6b's Agenda matches approvals per container: one of a container its Workload no longer has hides nothing.
- The Actuator says such an approval once, and keeps it as done by its run.

**Tests:** each run that supersedes nothing (going, failing, lacking the container, the same name in another cluster, another Contract's), pruning, an older run approved late, the migration's backfill, and the Actuator over four minutes with the Workload deleted and the container renamed. 13 mutations, all caught; 2 equivalent (`run_id <` against `<=`, and keeping the first superseding run, since only whether it is null is read). No eval rerun.

**Known limits:**
- An approval committed while a run's rows are being stored can miss both marks. It stays current until the next run that sizes its container.
- An approval whose container is never sized again (its Workload deleted or renamed, or the container gone from its template) stays current and in its Actuator's desired state. The Actuator reports it once ("no such Deployment") and keeps it as done, so it is never applied twice. Nor is it applied to a Workload made later under the same name while that Actuator keeps its ConfigMap.

## A8. Reflex leftovers

**What:** closes what 13.x left "not done".

**Built (2026-11-17):**
- A Step-up's pod whose node had no room (its resize left pending) is put back and left, as 13.1c made it. It is now tried again every `STEP_EVERY` (10 minutes, counted from when it was left) while the Step-up lasts, within its cap and before its term ends.
- A retry is unsaid. It is said once, when first left.

**Tests:** 9 mutations, all caught. `>=` against `>` at the 10 minutes is equivalent at a clock's resolution.

**Not built: a Triage's line said once.** This was the Operator's call (Q27, recommended).
- *Why:* no path ends a Triage twice. A Triage leaves `running` only paused or ended, and is saved before it is told. A follow-up is a Question's only, and the store's claim checks the kind. A Triage that did end twice would post its Report twice, so a guard, if ever needed, belongs around the whole reply.

No eval rerun.

## A9. A DaemonSet's CPU fits the busiest node

**What:** a DaemonSet's pooled CPU target was a percentile over every pod's samples, so one busy node among many was ignored (15.3's finding).

**Decided (grilled 2026-11-17, all recommended):**
- Each node's percentile is taken by node, not by pod. The source is kube-state-metrics' `kube_pod_info`, the Contract's own pod series, so none of the node reads' guards applies. A pod whose node is unknown counts as its own.
- 15.3a's risk `type_needs_more_cpu` is removed: a type can no longer need more than the pooled Sizing. 15.3c's per-type Sizings keep the quiet types' share small.

**Built (2026-11-17):**
- The CPU target is the largest of each node's own weighted percentile. Memory already fits the busiest peak. Every other kind is pooled as before.
- The pod-to-node read is made only where a DaemonSet has samples, once per window, scoped to the Contract's namespaces.
- `type_needs_more_cpu` and `_short_of` are removed. Stored rows that carry the risk show it until the next run.
- 15.3a's fixture of thirty nodes, one busy, now sizes the pooled CPU at 1150m. It was 58m.

**Tests:** by node against by pod and pooled: a quiet fleet, a busier node, a pod a rollout replaced after minutes at a core, and a pod with no node. Each instance type is tested the same way. 9 mutations, all caught. The agent's `sizing` tool shows the new numbers, with its description and schema unchanged, so no eval rerun.

**Known limits:** a node that ran a DaemonSet's pod for only minutes (one just added) sets the target if its minutes were busy. That is the busiest node's, by design.

## A10. A Contract takes at most half the Triage slots

**What:** one Contract may hold at most half of `SQUERO_MAX_CONCURRENT_TRIAGES`; its others wait in their own queue, so one Contract's alert storm can't hold every slot.

**Decided (grilled 2026-11-17, the Operator's call):** the half applies only on a Hub with two Contracts or more, counted at start when Contracts load. *Why:* at the default of 3, a single Contract's Triages and Checks would otherwise run one at a time, with no other Contract to keep slots for.

**Built (2026-11-17):**
- Each Contract has its own semaphore of half the slots, rounded down and at least one. It is taken before a shared slot, so a Contract's Investigations beyond its share hold no slot another Contract could use.
- This covers Triages, Questions and Checks alike.

**Tests:** at 4, 3, 2 and 1 slots with two Contracts, and 3 slots with one. 6 mutations, 5 caught. The equivalent one: without "at least one", one slot gives a share of none, which turns the share off. The single slot already holds every Contract to one.

**Known limits:** it is per process, like the slots themselves.

## A11. Ceilings whose code names their fix

**What:** five ceilings the audit listed, each with a fix named in the code: the agent's memory suggestion (`servers/sizing.py`), deploy verification's Workload (`deploys.workload`), a rollback's time (`servers/kubernetes.py`), a CronJob's realized Savings (`sizing.py`), and [`development.md`](../development.md)'s stale line on choosing between clusters.

**Decided (grilled 2026-11-17, all recommended):**
- A memory suggestion with a `max` at or below the limit it was killed at gives no numbers, only why. *Why:* a suggestion that raises nothing after an OOM kill is wrong.
- A pod's Workload comes from its owners, through a new `workload` field on `list_pods`. The name rule is kept for an older Relay.
- A CronJob between runs takes its newest run's requests and limits for everything (Waste, cost and Realized Savings), as live pods already do.

### A11a: the memory cap, the owner, the CronJob and the stale line

**Built (2026-11-17):**
- The memory suggestion after an OOM kill is capped at the policy's memory `max`, and the cap is said in its `why`. With the `max` at or below the limit (or peak) it was killed at, it says only "raise the max, or look for a leak".
- `list_pods` names each pod's `workload` as kind/name, from its owners, using the Actuator's `workload_of`. A ReplicaSet's pod is its Deployment's or Rollout's, and a CronJob's run is its CronJob's. `deploys.unhealthy` uses it, so a StatefulSet's or DaemonSet's pod is no longer misnamed.
- A CronJob between runs asks what its newest run asked (by `kube_pod_created`, then name), counted as read from live pods. A request lowered from now on realizes after its next run. One lowered before is not counted: no backfill. Any other container with no live pod keeps the window's largest.
- The run 4 to 5 note in `development.md` now names `two-clusters-alert-picks-one`, which passed in every run of the 2026-11-10 comparison.

No tool's name, description or schema, and no prompt, changed. `list_pods` returns one more field.

**Tests:** over the cap (below, at and above, with and without a limit); each owner shape; a CronJob's newest run against a lone Job's and a Deployment's; never another namespace's or cluster's pod. 17 mutations, 16 caught. The equivalent one: `to > cap` against `to >= cap + 1`, which differ only under a byte.

### A11b: a rollout undo in `recent_changes`

**Decided:** the value is `rollout undo`, not the grilled `rollback`. *Why:* Rollback is the Actuator's own undo of a Sizing ([`CONTEXT.md`](../../CONTEXT.md)). The undo is proven by a real `kubectl rollout undo` in CI's kind job, since fakes can't show which condition time moves on one.

**Built (2026-11-17):**
- A rollout undo shows as `change: "rollout undo"` under its new revision, with its diff from the revision it replaced.
- The reused ReplicaSet is found by the `deployment.kubernetes.io/revision-history` annotation, not by age, since an older one may be pruned. It keeps its first creation time.
- The current one is timed by its Deployment's `Progressing` condition's update time, while that condition names it. The transition time stays put while the condition stays True.
- The Deployment is matched by owner UID, listed one allowed namespace at a time.
- An undo superseded since keeps its creation time, as does one whose condition names another ReplicaSet or has no update time.
- Deploy verification gets a new revision, so it queues a Check for an undo.
- [`contracts.md`](../contracts.md)'s known limit is gone.

No tool, schema or prompt changed (one more `change` value), so no eval rerun.

**Tests:** with fakes (the condition naming it, another ReplicaSet, a name that only starts with it, another Deployment of the same name, none, no status, a superseded undo, no update time, and the allowlist and Contract boundaries). A kind test in CI's integration job: a real `kubectl rollout undo` shows in a one-minute window as revision 3 (passed on the Mac's kind cluster, 112 s). 9 mutations, all caught.

## A12. The eval regression on the default model

**What:** `aws-lambda-timeout-cut` failed 3 of 3 on Sonnet 5.5, the Hub's default, which also missed `gcp-who-deployed` and the two Questions. Opus 5.5 at medium passed all of them.

**Decided (the grill's rule):** diagnose from the recorded runs first (free). Fix the prompt or tool descriptions. Then rerun Sonnet, with the cost asked first. Opus at medium becomes the default only if that cannot close the gap. The Operator's calls were all recommended, then effort medium, then Opus.

**Built (2026-11-17):**
- Diagnosed for free from the 2026-11-10 recordings. Sonnet named the Lambda's timeout cut but not why a 1 s timeout fails (the 1.2 s lookup sits past the log result's cut, behind `read_more`). On Questions it kept calling tools after it could answer. One run sent `read_more`'s `fields` as a string, and one ended without a report.
- Fixes: a Triage prompt line (say why a changed setting's new value fails, from what the workload needed before); two Question prompt lines (runbooks and past incidents only for a fix or a past incident; answer once you can); `read_more` takes `fields` as one comma-separated string; `TriageError` carries its call log, so the eval keeps it.
- Sonnet's four failing Scenarios, rerun 3 times: at high effort Lambda and `question-unhealthy-workloads` passed 3 of 3, but `question-checkout-rollout` passed 0 and `gcp-who-deployed` 1, using up to 28 calls. At medium, 5 of 12.
- So **Opus 5.5 at medium is the Hub's default** (`config.py`, `.env.example`, `deploy/base`, and `hub.env.example` with its price). This is the roadmap's fallback. 22 Scenarios × 3 runs passed 66 of 66, with 5.4 calls on average and 18 s.

**Cost:** about $45 to $65 on evals. No token counts were recorded.

**Tests:** `read_more` with a string of fields, and a Run without a report keeping its calls through `squero eval`. 6 mutations, all caught.

**Known limits:**
- Why Sonnet loops was not found. Two efforts were measured.
- Effort is not pinned: Opus runs at its own default, medium. `SQUERO_MODEL_EFFORT` would apply to any model a Hub names.
- A Hub whose env names Sonnet keeps it until changed. A Hub on the default runs Opus after upgrading, and refuses to start while a budgeted Contract has no Opus price.

## A13. The week's resizes in the weekly digest

**What:** 12.4's "resizes in the daily digest", decided on 2026-11-10 as one line in the weekly digest and not built since (added 2026-11-16).

**Built (2026-11-17):** the weekly digest has one line on the week's Actuations:

    Actuator this week: 5 resized, 1 rolled back.

- Each outcome that changed something counts the containers it reached in the 7 days up to the digest's time: applied (as resized), rolled back, reverted, failed, a Step-up and a Burst.
- An Actuation reported again, or by a later run, counts once. The same name in another cluster is kept apart.
- Skipped, an OOM kill or throttling seen, a Step-up's or Burst's end, and handed back are left out.
- With none of the counted outcomes, there is no line.
- It reads the Contract's own stored Actuations (`store.actuations`).

**Tests:** every outcome, a repeat, another cluster, the line's order, none at all, the week's edges, and another Contract's Actuations. 11 mutations, all caught. No tool, prompt or API changed, so no eval rerun and no console check.

## A14. Stale notices

**What:** added 2026-11-20 from a live gap. The lab cluster that the mini PC's Hub reads was down from 15 to 21 November, and only the weekly digest of 16 November said so.

**Decided (grilled 2026-11-20, two rounds, all recommended):**
- When a stale kind has lasted 3 hours without a break (three missed hourly runs), the Hub posts a **Stale notice** to the Contract's `operator_channel`. The stale kinds are a cluster's Sizing reads failing, a Relay offline, and a cluster's Bill reads failing. Without Slack, nothing is posted, and the Agenda stays.
- One thread per cluster and kind, so each closes on its own.
- The first post names the Contract, the cluster, what is failing, since when, and the error as the Agenda shows it. The error never includes a secret or a credential URL. The Now panel's link is included when `SQUERO_CONSOLE_URL` is set.
- A reminder goes in the thread each day while it fails ("still failing, 3 days now"). A reply goes when it reads again ("reads again, after 5 days 8 hours").
- Each post is claimed in Postgres first, so a restart neither repeats nor loses one.
- A new glossary term, *Stale notice*, since an Alert is what the Hub receives.
- The Hub itself going down is not covered. That is Deferred.

**Built (2026-11-20):**
- `notices.py`: `failing()`, which is also the Agenda's source of stale items, so the Now panel and the notices agree.
- `tick()` runs per Contract with Slack, every 10 minutes beside the scheduler's tick, while the Hub has a bot token. One Contract failing stops none.
- Migration 0047, `stale_notices`: Contract, cluster, kind, since, the thread's ts, and when last told.
- The first notice goes once 3 hours have passed (its text escaped, with the console link), a reminder in its thread a day later, and a reply when it reads again. A row whose cluster is no longer read ends quietly.
- A Bill's failure counts from when the Hub first saw it, since its store keeps no start. Sizing and Relay failures count from their own time.
- Each post is claimed with a conditional update before it is sent. A refused post is undone and tried at the next look.
- No API change.

**Tests:** isolation: two Contracts failing on a cluster of one name keep their channels, threads and rows apart, and each statement's Contract clause was mutated on its own. 23 mutations, all caught. Not tested: the 10-minute cadence of the scheduler loop.

**Known limits:**
- As built, not "never lost": a crash between a claim and its post delays it a day, and a refused recovery reply is not tried again.

## Not code: owed live checks

The owed live checks are the "not checked live" and "owed" lines on these pages. They are batched per cloud after the audit slices, and each comes with its cost.

- A4, on kind (free): the real Role and env, kube-state-metrics' real counts, and a rollout seen by the Hub within 5 minutes.
- A5: a live read with no node carrying the label.
- A6a, with 16.4a's GCP check: the RAM SKUs' hours, read from a real export.
- The Backtest waits for its date ([14.4](phase-14.md), 12 December).
