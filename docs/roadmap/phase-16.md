# Phase 16: Spot

**In short:** A spot ratio sends a share of a Workload's replicas to spot nodes. An On-demand minimum keeps the rest on normal nodes. The Hub prices spot nodes and shows where to look, and the Actuator's webhook places each new replica. Nothing in a manifest changes. CronJobs run on spot only if their Jobs allow a retry.

**Status:** built, and the done-when was met on kind on 2026-11-15. Checked on AWS in two parts (2026-11-16, and 2026-11-17 to 21). Still owed: GCP's spot list prices and spot bill query, and Azure's grouped spot bill read.

**Goal:** keep a set share of each Workload on spot nodes, and never let the on-demand minimum fall below its floor, even when a spot node is lost.

**Done when:** replicas keep the ratio and the floor when a "spot" kind node is deleted. ✅ (16.2b, on kind, 2026-11-15)

| Slice | What | Status |
|---|---|---|
| [16.1a](#161a-the-spot-label-and-the-discount) | The spot label and a typed discount; spot nodes priced | built 2026-11-15 |
| [16.1b](#161b-the-bill-check-with-spot) | The Bill check with spot | built 2026-11-15, checked on AWS 2026-11-17 to 21 |
| [16.1c](#161c-the-spot-rule-candidates-and-intended-against-actual) | `spot:` on the Sizing rule, candidates, intended against actual | built 2026-11-15 |
| [16.2a](#162a-the-hubs-side-of-steering) | The Hub's side of steering | built 2026-11-15 |
| [16.2b](#162b-the-webhook-steers-replicas) | The webhook places replicas | built 2026-11-15, checked live 2026-11-15 (done-when) |
| [16.3a](#163a-cronjobs-on-spot-the-hubs-side) | CronJobs on spot: the Hub's side | built 2026-11-15 |
| [16.3b](#163b-cronjobs-on-spot-the-actuator) | CronJobs on spot: the Actuator | built 2026-11-15, checked live 2026-11-16 |
| [16.4a](#164a-spot-list-prices) | Spot list prices of the three clouds | built 2026-11-16, checked live on Azure and AWS; GCP owed |
| [16.4b](#164b-the-bills-spot-part) | The bill's own spot part, beside the estimate | built 2026-11-16, checked on AWS; Azure and GCP owed |

## Decisions (grill, 2026-11-12)

Four rounds, all recommended, except CronJobs on spot, which the Operator chose to build in this phase rather than defer. "Floor" is [Phase 14](phase-14.md)'s term, so it is the *On-demand minimum* here.

- **Scope.**
  - The Hub prices spot and shows candidates. The Actuator's webhook places each new replica.
  - Nothing in a manifest changes. A topology spread balances two groups evenly, so it cannot hold a 70/30 split. Karpenter's documented recipe for a ratio says it "is not identical" to one.
  - The Operator sets it on the Sizing rule: `spot: {ratio: 0.7, on_demand: 2}`. The Hub only shows where to look.
  - It needs an Actuator and the cluster's spot label. It is not tied to Automation.
  - It covers Deployments and Argo Rollouts with 2 or more replicas, and CronJobs under their own rule. Never a StatefulSet (its state and volumes) or a DaemonSet. A rule naming a kind that cannot take it is refused.
- **Numbers.** On-demand replicas are the larger of the On-demand minimum and the non-spot share, rounded up. "70% on spot, never fewer than 2 on-demand": 10 replicas give 3 and 7; 4 give 2 and 2. The minimum is 1 or more, so no Workload is all spot.
- **Placing (16.2).**
  - The On-demand minimum is a requirement, "not on a spot node". It is the one requirement the webhook adds. Placement's rules are preferences.
  - The spot share is a preference. A spot replica with no spot room lands on on-demand by itself, as Karpenter does when a NodePool allowing both has no spot capacity.
  - Cluster Autoscaler ignores preferences when it picks a node group to grow, so whether new nodes are spot depends on the client's expander setting. The guide says so.
  - The spot label is named per cluster: `karpenter.sh/capacity-type=spot`, `eks.amazonaws.com/capacityType=SPOT`, `cloud.google.com/gke-spot=true` or `kubernetes.azure.com/scalesetpriority=spot`. On-demand is any node without it (`NotIn` matches a node lacking the label).
  - On AKS, whose spot nodes are tainted, spot replicas also get the toleration.
  - Replicas are counted per ReplicaSet, so each new version reaches the ratio by itself. Counted over the Workload, old pods would leave the new version short of on-demand ones.
  - On-demand replicas are placed first. The count is corrected against the cluster each minute. The docs do not say that a Deployment's pods are admitted one at a time.
  - A pod the webhook did not place counts by its node, from the spot nodes the Hub sends. A Pending pod without the label counts as neither.
  - A pod whose spec already settles its side (a `nodeSelector`, or a required affinity on the label's key) is left alone.
  - The risk line below the minimum shows on the Nodes page and in the Hub MCP only. It is not in the Agenda or the digest, and it is best effort until 16.3.
- **Keeping it.**
  - Scale-down: on-demand replicas get `controller.kubernetes.io/pod-deletion-cost` at creation, so spot ones go first. Best effort, as Kubernetes says: until pods are created again, the mix leans to on-demand. A Workload that sets deletion costs itself is left alone.
  - Drift (a spot replica that fell back, or pods created while the Actuator was down) is repaired by nobody until the next rollout or scale-up. The Hub shows intended against actual ("restart to repair"). The Actuator never evicts for it.
  - The webhook stays fail-open, so while the Actuator is down the ratio and minimum are best effort.
  - The Kill switch and the pause annotation stop new placements. Running pods stay.
  - A Workload that Phase 15's report names as blocking gets no spot replicas, and the Hub says why.
- **Interruptions.** The ReplicaSet creates new pods and the webhook places them again. Only 12.10 ([Phase 12](phase-12.md)) needs building: without it, an interruption inside a Sizing's watch hour is a false Rollback. Notice: 2 minutes on AWS, 30 seconds on Azure, 0, 30 or 120 seconds on GCP.
- **CronJobs (16.3).**
  - Only through a rule naming `kind: CronJob`, written `spot: true`. A rule with a ratio or a minimum is refused. An empty map reads as a mistake, so it is not used.
  - *Why:* a batch run that starts again from zero is a judgement of its own.
  - Only a CronJob whose Job allows a retry (`backoffLimit` of 1 or more; the default is 6) is steered. Others are skipped, with the reason shown.
  - A Job that ignores disruptions (`podFailurePolicy` `Ignore` on `DisruptionTarget`) is skipped, since its retries would keep preferring spot.
  - Every pod of a run prefers spot on its first try. Once the run has a failed pod, its retries are required off spot, so a run is interrupted once at most.
  - A Job with no CronJob stays skipped.
  - Decided 2026-11-12: a run's pod Failed by a drain eviction (`EvictionByEvictionAPI`, how Karpenter empties a spot node it is about to lose) is no Rollback. 12.10 had left it counting.
- **Money.**
  - Spot nodes were priced as on-demand, so a cluster with spot showed too high a cost. First: a discount typed per cluster, for every cloud. Later: spot list prices. Azure's Retail Prices API (public) and GCP's catalog (with the API key it already needs), and AWS's `DescribeSpotPriceHistory` with the Contract's own credentials, never AWS's undocumented public feed.
  - kube-state-metrics must allowlist the spot label.
  - The Bill check counts spot nodes at their discount and includes the bill's spot lines for all three clouds. Otherwise a cluster that moves to spot stops reconciling.
  - Candidates: a Deployment or Rollout with 2 or more replicas on average, no persistent volume, not named as blocking; and CronJobs. Each shows a what-if (70% spot with a minimum of 2; a CronJob run entirely on spot), an upper bound never summed with Savings. Nothing is saved unless a spot pool exists and the autoscaler shifts nodes to it.
  - No realized spot saving is computed. The cost pages show spot nodes at their real price.
- **Terms** (in [`CONTEXT.md`](../../CONTEXT.md)): *Spot ratio*, the share of a Workload's replicas the webhook steers to spot; *On-demand minimum*, the replicas it keeps off them.
- **Live check on AWS.** One small, time-boxed Karpenter cluster for Phases 15 and 16, to test a real interruption and Karpenter's handling of a preference. Estimate under 5 USD for a few hours. The exact estimate and the Operator's yes were needed before anything was created.

**Looked up before the grill (2026-11-12):**
- Only AKS taints spot nodes by default.
- Karpenter prefers reserved capacity, then spot, then on-demand.
- A ReplicaSet's scale-down order: Pending first, then the lower deletion cost, then nodes with more replicas, then newer pods.
- Argo Rollouts sets a ReplicaSet's replicas and leaves pod choice to its controller (inferred from its source).
- "CA does not consider soft constraints like `preferredDuringSchedulingIgnoredDuringExecution` when selecting node groups."
- CAST AI's webhook applies its spot percentage per ReplicaSet.

## 16.1a The spot label and the discount

**What:** `spot: {label, discount}` per cluster, and spot nodes priced at their own rate.

**Decided (grill, 2026-11-15):**
- The discount is the share of on-demand a spot node costs (0.35 means 35%). It is a multiplier like `list_prices.discount`, and must be more than 0 and less than 1.
- Typed prices are read as the on-demand rate and blended hourly by the cluster's spot and on-demand capacity, since a typed blended rate would go stale. Only where a cluster sets `spot`.
- Every pod costs the cluster's blended price, not its node's. Cost, Waste and Savings keep one rate.
- A node that could go is priced at its own rate.
- Where no node shows the label, every node is on-demand, and the price note and Nodes page say to allowlist it.
- No Actuator is needed, since pricing reads Prometheus.

**Built (2026-11-15):**
- `Cluster.spot` (`label`, `discount`). `usage.carries` reads a node's labels as kube-state-metrics names them; each node's `spot` is marked in both reads, and each read says which label it used.
- `listprices.spot_paid` gives each node's share of its on-demand price, by size. `_from_list` and `_typed` apply the discount or the blend.
- A read made with another label, or before 16.1a, prices every node on-demand and says the next run reads it.
- The Nodes page and the Hub MCP's `nodes` show `spot` per node and per cluster, with a note where no node shows the label.

**Asked of the Operator (2026-11-15):** a deal (`list_prices.discount`) and the spot discount do not compound. A deal applies only to on-demand nodes, since committed-use deals do not cover spot capacity. So a spot node pays its own discount of the list price and no more.

**Chosen beyond the grill:**
- A node that could go is priced from what its cluster's pricing applied (`Contract._spot_shares`), never from a second count of the mix. So an on-demand node costs the on-demand rate and a spot node its own.
- A list price's on-demand rate is the cluster's average. A failed read says so in the price note.
- `sizing prices` prints the Contract's average for list prices only. A typed Contract-level price stays the on-demand rate.

**Tests:** every branch; two Contracts' nodes kept apart; 34 mutations, all caught.

**Checked:** not live. The read was checked live with Placement's node labels.

**Known limits:** until 16.1b, a cluster that sets `spot` may show its estimate below its bill. The guide says so.

## 16.1b The Bill check with spot

**What:** the Bill check counts spot lines, and spot node-hours at their own rate.

**Decided (grill, 2026-11-15):**
- Every cluster's bill counts its spot lines, with or without `spot:`. A cluster on spot without `spot:` shows its estimate above its bill, which points at the setting.
- GCP's query stops leaving out `Spot Preemptible` SKUs.
- AWS's and Azure's usage filters are believed to take spot lines already. Unconfirmed until the AWS live check.
- The estimate splits the window's node-hours by the spot label, hour by hour, at the rates 16.1a applied. It follows the mix the bill charged, not the latest one.
- The bill stays one total. The row shows the share of vCPU-hours on spot. The bill's own spot part is deferred to 16.4.
- A cluster that sets `spot` with no spot node-hours keeps its verdict, and the row says to allowlist the label.
- An Enterprise Agreement account's Azure spot rows are priced in USD whatever its currency. So a day billed in two currencies is refused, and neither currency is kept.

**Built (2026-11-15):**
- GCP's query includes `Spot Preemptible` SKUs.
- `usage.spot_hours`: the window's capacity-hours of nodes carrying the spot label, hour by hour. It is joined in Python, not PromQL, since a node's name may be in `exported_node` (found in review).
- `billing.report` undoes the blend (`Contract._spot_shares`) and prices spot hours at a spot node's rate.
- The row's note gives the spot share of vCPU-hours and the rate, or asks for the allowlist.
- `azure_parse` refuses a day in two currencies.

**Chosen beyond the grill:**
- A cluster that sets `spot` keeps its rates whenever it is priced. The share is 1.0 where its latest read priced no spot node, so the Bill check prices a window's spot hours even then.
- The Nodes page prices a spot node its own read shows at its rate, not the cluster's price.

**Tests:** every branch; two clusters with same-named nodes kept apart; 22 mutations, all caught. 16.1a's 34 rerun, all caught.

**Checked:**
- On the Mac lab's Prometheus, read-only, 2026-11-15. With no allowlisted label on its node, a matcher every node series meets (`label_absent=""`) stood in for a spot label. Spot hours equalled node-hours exactly (180 vCPU-hours, 563.2 GiB-hours over 18 sampled hours). A label no node carries gave none. So the join by node and hour holds on a real Prometheus's answers.
- On AWS, 2026-11-17 to 21: 19 November billed 1.2674 USD against 1.2826 estimated (+1.2%, 24 of 24 hours sampled). 18 to 19 November: 2.5112 against 2.5652 (+2.1%). The on-demand node's BoxUsage was 0.9984, exactly 24 hours at its list price, so the gap is the spot price.

**Known limits:** Azure's spot lines are not checked live.

## 16.1c The spot rule, candidates and intended against actual

**What:** `spot:` on a Sizing rule, the candidates, and intended against actual.

**Decided (grill, 2026-11-15):**
- `spot: {ratio, on_demand}`, both required. `ratio` is more than 0 and at most 1. `on_demand` is a whole number, 1 or more.
- Refused on a rule with `container:` (spot is a Workload's), on StatefulSet, DaemonSet or Job, and where no cluster the rule reaches has both the spot label and an Actuator. CronJobs follow in 16.3.
- A rule without `kind` touches only its Deployments and Rollouts. A Workload's spot rule is the first rule without `container` that matches it.
- Any policy applies, `off` too, with or without `automate`.
- Candidates appear on every cluster with Prometheus, as where to look: a Deployment or Rollout with 2 or more replicas on average, no persistent volume (`kube_pod_spec_volumes_persistentvolumeclaims_info`), not named as blocking, and not under a `spot` rule.
- A candidate is a what-if: its spot replicas times one pod's requests, at the on-demand rate, times the difference to a spot node's rate. It is a month, an upper bound, never summed with Savings. Totals are per cluster, ranked.
- Money appears only where the cluster sets `spot`. Otherwise the page says "set the cluster's `spot` to price it".
- Intended against actual: intended from live replicas, actual from the Nodes page's read of pods' nodes. A Workload is listed with its reason if it blocks a node, has fewer than 2 replicas, or is of a kind that cannot take it. "Placed from 16.2" shows until the webhook places, and no risk line until then.
- Shown on the Nodes page and the Hub MCP's `nodes`, not in the digest. Nothing new reaches the Actuator.

**Built (2026-11-15):**
- `SizingRule.spot` (`SpotRule`), with its refusals. `SizingConfig.spot` and `Contract.spot_for` find the rule.
- `usage.rooms` reads which pods claim a volume, stamped on the read, so a read from before says it cannot list candidates.
- `sizing.spot_split` takes the ceiling after rounding, since 10 times 0.3 is 3.0000000000000004.
- The Hub MCP's `nodes` shows `spot_candidates`, and `spot_workloads` capped at 20.

**Asked of the Operator (2026-11-15):** a Workload with 2 replicas on average meets the rule, but its what-if keeps both on-demand. So it is left out. A candidate always moves one replica, which means 3 or more. That made the "2 or more" check redundant, so it was dropped.

**Chosen beyond the grill:** a candidate's average replicas are rounded half up (2.5 is 3), its pods the most of its containers' and its requests their sum. A rule naming a container matches no Workload as a whole.

**Found in review (fixed):** the pricing note showed on a cluster with no candidate (now only with one); a live CronJob under a rule without `kind` was said never to run on spot (now "with slice 16.3"); the isolation test stored no Sizings (now each Contract has its own candidate).

**Tests:** every branch; two Contracts' nodes kept apart; 48 mutations, all caught after dropping one equivalent survivor.

**Checked:** not live. Nothing is placed until 16.2.

## 16.2a The Hub's side of steering

**What:** `Desired.spot`: each Workload under a spot ratio, its ratio and minimum, the cluster's spot nodes, and an optional spot taint. Plus the risk line.

**Built (2026-11-15):**
- `Spot.taint` (`key[=value]:effect`, as Placement's). `actuator.Spotted` and `Spotting` on `Desired.spot`. An older Actuator ignores it.
- The Workloads: live, in the cluster's namespaces now, under a ratio the cluster can place, of a kind that can take it. A Workload that blocks a node gets ratio 0, so all its replicas are kept off spot.
- The spot nodes come from `sizing.spot_nodes`, None without the label.
- No list is sent without a usable read and report; the Actuator keeps its last list. Nothing is sent under the Kill switch. None for a cluster without `spot`.
- The report's `spotting` is sent only when true. On the Nodes page and in the Hub MCP, `risk` appears where fewer replicas run on on-demand nodes than meant (none where the read cannot say). Once a usable report says `spotting`, the Nodes page drops its "doesn't place them yet" note.

**Asked of the Operator (2026-11-15):** a Workload that blocks a node is sent with no share, not left out. Otherwise nothing would keep its pods off spot.

**Found in review (fixed):**
- A stale report counted as placing; now only a usable one does.
- The read's namespaces were not filtered by the Contract's; now they are.
- The risk compared on-demand replicas with the intended share, not the minimum: 2 of 10 under 0.7 with a minimum of 2 said "below its minimum of 2". Now it applies only below the minimum, or below every replica where all are meant on-demand.
- A Workload with fewer replicas than its minimum said "below its minimum of 1"; now "0 of its 1 on-demand, where all are meant to be".

**Tests:** every branch; two Contracts' Actuators each sent only their own; 24 mutations, all caught after fixtures for two survivors (25 after the risk fix).

**Checked:** not live by itself. 16.2b's live check covers it.

## 16.2b The webhook steers replicas

**What:** the webhook places each new replica on-demand first, then spot.

**Built (2026-11-15):** [ADR 0006](../adr/0006-placement-is-a-preference.md) is extended with its one requirement first.
- `contracts.on_demand_of` gives the split. A ratio of 0 keeps every replica on-demand.
- On-demand first: while fewer pods than the on-demand count exist, a new pod is required off spot (`NotIn`). It gets the label `squero.io/capacity` (`on-demand` or `spot`) and a deletion cost of 1, unless it sets its own.
- Then spot: a preferred term of weight 100, plus the taint's toleration.
- Built against the pod as the webhook's other changes leave it. jsonpatch was not used, since it was only installed as another package's dependency. So a pod both placed and kept off spot gets both.
- Only where there is a list. A cluster without spot pays nothing.
- Counts are per ReplicaSet, under a lock (the webhook runs in threads). Replicas are read at the first admission in the minute and again once counted pods reach them, since a rollout grows a ReplicaSet within the minute. Both reads share a 1.2-second budget (the API server gives the webhook 2 seconds). A failure places and counts nothing. A dry run is never counted. A count read outside the lock is never laid over admissions counted meanwhile (`spot_seq`).
- **Asked of the Operator (2026-11-15):** a ReplicaSet counted full is listed again at admission, by its template's hash. A replacement for a pod lost to an interruption is decided by the fresh count, not always made on-demand.
- Each minute, not under the Kill switch, one list per namespace recounts the steered ReplicaSets. A namespace that cannot be listed keeps its counts.
- Events `SpotPlaced` and `SpotEnded`. The pause annotation is read.
- The report says `spotting` only with spot nodes from the Hub, never under the Kill switch.
- No new permission: ReplicaSet `get`, pod `list`, Events.

**Tests:** every branch, including two admissions racing for the last on-demand place; 36 mutations, all caught. The lock itself is a reviewed line, not a tested one, since under the GIL a race rarely shows in a test. A review before the live check found the stale-count race, the budget, the patch counted before it was built, and the hook running without a list.

**Checked live 2026-11-15, the phase's done-when.** A throwaway kind cluster with two `karpenter.sh/capacity-type=spot` workers and two on-demand ones, kube-prometheus-stack allowlisting the label. The Actuator was built from the tree, and the Hub ran on the Mac with a scratch database. All deleted afterwards.
- The Hub sent `cart` (0.5, 2), `web` (0.7, 2) and the two spot nodes.
- Before steering, `cart`'s 4 pods were all spot and showed "0 on-demand, below its minimum of 2: restart it to repair". `SpotPlaced` appeared on both. Before steering, `web`'s 6 and 4 showed no risk.
- `web` restarted: 3 on-demand (deletion cost 1) and 7 spot. Scaled to 5, it lost only spot replicas; back to 10, it gained 5 spot.
- A spot node was deleted. Its 4 replicas came back as spot on the other, recounted at admission: 3 and 7, on real nodes.
- The last spot node went. The minimum held, and the 7 spot replicas fell back to on-demand nodes, none Pending. The Nodes page then showed both nodes all on-demand, with no risk.
- Paused, `web`'s new replicas were left alone. Under the Kill switch, `cart`'s too. After that, 11 replicas gave 4 and 7.
- The Actuator logged no error.

**Found after the check (fixed):**
- Its report said `spotting` while its list was still unknown. Now only once it knows its list.
- With every spot node gone, the price note said only to allowlist the label. Now it says both: "none is on spot now, or the label isn't in its --metric-labels-allowlist".

## 16.3a CronJobs on spot: the Hub's side

**What:** the CronJob rule, its candidates, the CronJobs sent to the Actuator, and their skips shown.

**Built (2026-11-15):**
- `SizingRule.spot` takes `true` for a CronJob, refused without `kind: CronJob`, and a CronJob rule with a ratio is refused. A CronJob has no ratio.
- The node read lists the Contract's CronJobs (`kube_cronjob_info`). `sizing.cronjobs_of` adds any whose run's pods it saw.
- `sizing.spot_runs` takes those under `spot: true` that their cluster can place. They are sent in `Spotting.cronjobs`, a list of their own. An Actuator of 0.57.0 ignores it.
- The report's `spot_runs` (sent only when true) says it steers runs. `spot_skipped` gives each skipped run as `namespace/name` with a code, `no_retry` or `ignores_disruptions` (sent only when there are some).
- The Nodes page and the Hub MCP show the Hub's sentence for each code; a stale report's skips show nothing. Until a usable report says `spot_runs`, the page says "its Actuator doesn't steer its runs yet".
- On the Nodes page, a CronJob under `spot: true`, running or not, reads "its runs on spot, a retry off it", with its live pods by side or its skip reason. It has no intended split and no risk line. Under a ratio of a rule without `kind`, it is told it needs a rule of its own.
- Candidates are CronJobs under no rule, in the latest Sizings, not skipped, with a run seen. Each is a run entirely on spot, ranked with the Deployments by saving. Its what-if is its average pods' requests at the on-demand rate, times one less a spot node's rate: a month, an upper bound, never summed with Savings. (The grill had shown them "if its Jobs allow a retry"; a skipped one is left out.)

**Asked of the Operator (2026-11-15):** a CronJob whose run blocks a node is never left out of the list or the candidates. A run blocks only while it runs, the only time the Hub can see it, so leaving it out would flip the list run by run.

**Chosen beyond the grill:** the report's skips are looked up only from the read's own CronJobs. So there is no namespace filter, and another namespace's key shows nowhere.

**Found in review (fixed):**
- The page took runs as on spot from an Actuator 0.57.0's `spotting`, which steers none. It now uses `spot_runs`.
- A CronJob's average pods were stored to one decimal, so an hour a night (0.04) was nothing. Now three decimals.
- The reason was free text reaching the Hub MCP's model. Now a code.
- The guide's example put the spot rules below a namespace-wide rule, which matches first. Now above it, and checked.

**Tests:** every branch; two Contracts' Actuators each sent only their own; 25 mutations, all caught.

**Checked:** not live. 16.3b's live check covers it.

## 16.3b CronJobs on spot: the Actuator

**What:** the webhook steers each run's pods, and the Actuator reads each listed CronJob's Job for its retry setting.

**Built (2026-11-15):** [ADR 0006](../adr/0006-placement-is-a-preference.md)'s requirement is extended to a run's retry first.
- `Actuator.check_runs`, each minute, reads each listed CronJob (kube-state-metrics has no `backoffLimit`) and marks it:
  - `no_retry` where `backoffLimit` is 0, where an Indexed Job's `backoffLimitPerIndex` is 0, or where a `podFailurePolicy` fails the Job on `DisruptionTarget`. An interruption ends the run, so it gets no retry either (chosen beyond the grill).
  - `ignores_disruptions` where a rule ignores it.
  - An Event `SpotSkipped` on a change. A CronJob it cannot read keeps what it knew; one not read yet is not steered.
- `steer_run` at admission reads the run's Job within the 1.2-second budget. A failed pod in its status (counted, or `uncountedTerminatedPods`) makes the pod required off spot. Otherwise it prefers spot, with the toleration. No deletion cost, since a Job scales by none, and no count. A failed read places nothing.
- Events `SpotPlaced` and `SpotEnded` go on the CronJob. Its pause annotation is read. The hook runs with CronJobs alone.
- The report lists skipped CronJobs and says `spot_runs`. Never sent under the Kill switch or before its list is known.
- `drained`: a run's pod Failed by `EvictionByEvictionAPI` is no Rollback. A preemption, or a condition not True, still is.
- No new permission.

**Chosen beyond the grill:** the report is hourly, but a change in what it tells (steering starts, a list becomes known, a skip) brings the next one forward.

**Found in review (fixed):**
- The client guide still said a drain eviction counted as before. Fixed, with the per-pod reading of "once at most" where `parallelism` is above 1.
- The report's codes were not validated before sending. Now they are, and a test pins the three lists equal.

**Tests:** every branch; 29 mutations, all caught. 31 after a fix to the skip events (below).

**Checked live 2026-11-16, the 16.3 done-when.** A throwaway kind cluster as in 16.2b, with the rule `{kind: CronJob, spot: true}`. Deleted afterwards.
- `kube_cronjob_info` carries `cronjob`, as 16.3a read it.
- The Hub sent `nightly` (every minute, 150 s, `backoffLimit: 3`) and `fragile` (`backoffLimit: 0`). `SpotSkipped` ("its Jobs allow no retry") appeared on `fragile`, whose runs were left alone.
- `nightly`'s first run was on a spot node, which was deleted mid-run. The Job counted the pod failed, and the retry was admitted required off spot (not a fallback: the Job's status showed the failed pod by then), on an on-demand node although the other spot node had room. The run completed (1 failed, 1 succeeded).
- The next run preferred spot again. The Nodes data showed `fragile`'s reason and the `nightly` retry's pod on-demand, and the report arrived within minutes of each change.
- Paused, and under the Kill switch, runs were left alone. After it, steered again. The Actuator logged no error.
- **Found after the check (fixed):** a skipped CronJob got `SpotPlaced` a minute before its `SpotSkipped`, because the list arrives before the minute's read. Now the read says one or the other, once.

**Known limits:** the drain eviction's no-Rollback is not checked live. The unit tests cover it.

## 16.4a Spot list prices

**What:** spot list prices of the three clouds, stored beside the on-demand ones. The typed discount stays as the fallback.

**Decided (grill, 2026-11-16):**
- Spot list prices win where found. The discount is the fallback when a price is missing, a read failed, or the cluster is typed, so every Contract stays valid and priced. The price note says which applied, for example "spot list on 2 of 3 nodes, x0.35 on 1".
- Only for clusters priced from `list_prices`. A typed price is a negotiated on-demand rate, so setting it against a public spot price would compare two kinds of number.
- AWS: `DescribeSpotPriceHistory`, with the `aws` integration's own credentials (the client grants `ec2:DescribeSpotPriceHistory`; the guide names it). The price is the region's average over the last 24 hours, across its zones. A node's zone is not read; per-zone waits until a Bill check gap shows it matters. A refused call falls back to the discount, and the note says why.
- Azure's Retail Prices API and GCP's catalog provide on-demand prices too.
- Spot prices are stored as rows of their own, with the spot kind. They are read daily; the last good price is kept, and its age shown.
- **Split:** 16.4a is the spot list prices; 16.4b is the bill's own spot part, deferred from 16.1b so a spot price can be checked against what the cloud charged.

**Split:** 16.4a is the spot list prices; 16.4b is the bill's own spot part, deferred from 16.1b so a spot price can be checked against what the cloud charged.

**Built (2026-11-16):**
- Table `spot_list_prices` (migration 0041), with `contract_id`. A spot row can never take an on-demand one's place. An older Hub reads `list_prices` as it did.
- `listprices.azure` and `gcp` return spot prices from the read they already make. Azure's "... Spot" meters count; Low Priority and Windows do not. GCP's `Preemptible` SKUs count, and a "Spot Preemptible" one wins over a legacy one. A group is read again when either of its rows is due, so a Hub upgraded with fresh on-demand prices reads its spot ones the next hour.
- `aws_spot` calls `DescribeSpotPriceHistory`, signed with the Contract's key (SigV4), for Linux/UNIX from a day back. It follows the pages, then averages each zone, then the zones. It reads the price with a pattern, not an XML parser, so no parser error escapes the tick.
- It runs only for a Contract on list prices with an `aws` integration. Its spot clusters are read untyped in the integration's regions. Otherwise it is refused, and stored with why.
- `_from_list` prices each spot node at its spot list price, or its discount, node by node. The spot rate is the effective one for the Bill check, the candidates and a node that could go.
- An AWS spot price is read with each Contract's own key and kept for it alone; its CloudTrail shows only its own calls. Azure's and GCP's are shared, as their on-demand prices are. `store.spot_prices` never gives one Contract another's rows.

**Asked of the Operator (2026-11-16):** a refused AWS spot read (401 or 403) waits a day, not an hour, so a client's audit trail gets one denied call a day. Other failures wait an hour.

**Chosen beyond the grill:**
- The "AWS in USD only" fallback was dropped. A Contract on AWS prices in USD, and its validation says so.
- No key is ever both shared and a Contract's.

**Found in review (fixed):**
- A size the cloud has no spot price for was retried hourly, and so was its group (GCP's read is the whole catalog). Now it waits the day.
- AWS's spot read skips a node read with a problem, as `wanted_by_group` does.

The IAM permission is in the [Contract reference](../contracts.md).

**Tests:** every branch. Two Contracts in one region, the second's key refused: it falls back, and no request carries the first's key. 28 mutations, all caught.

**Checked live:**
- **2026-11-16, Azure** (eastus, the real Retail Prices API): D2s_v3 0.096 on-demand, 0.01919 Spot (0.20 of on-demand); D4s_v5 0.192 and 0.040531 (0.21). B2s has no Spot price (not sold as Spot), so it is stored as none and priced at its discount. westeurope answered 429; the Hub stores that and retries within the hour.
- **2026-11-16, AWS Part A:** m6a.large 0.02968, c6a.large 0.025071 and m5.large 0.026932 USD an hour. These equalled the CLI's read of the same day's history, averaged per zone and then across zones.
- **2026-11-17 to 21, AWS Part B:** the live read gave 0.01184 USD an hour for `t3.medium` in us-east-2, with the sandbox's administrator user. The Hub's read-only user lacked `ec2:DescribeSpotPriceHistory` until the Operator added it on 2026-11-21. IAM's simulator then allowed it in us-east-2 and denied it in us-east-1, where a region-restricting SCP applies.

**Still owed (GCP):** the catalog read, which needs the sandbox's key, so the Operator runs it: `listprices.fetch(http, "gcp", "us-central1", {"e2-standard-2": (2, 8), "n2-standard-4": (4, 16), "n1-standard-2": (2, 7.5)}, key)`, printing both maps (a Spot price per family, and the Spot SKU's naming).

## 16.4b The bill's spot part

**What:** the bill's own spot part, beside the estimate.

**Built (2026-11-16):**
- Each billed day keeps what its spot lines cost (`billed_costs.spot_amount`, migration 0042). It is null for days read before, and the report waits for the next daily read.
- Each cluster's last spot read keeps its failure (`billing_tries.spot_error`).
- The spot part is its own read, after the total, and fails softly. **Asked of the Operator (2026-11-16):** the docs settle neither Azure's `PricingModel` grouping nor AWS's usage-type keys for spot. One grouped read for both would have darkened the Bill check that works live today.
  - AWS Cost Explorer, grouped by `USAGE_TYPE`, with a day's groups added across pages.
  - Azure Cost Management, grouped by `PricingModel`. A reply without that column is refused.
  - A failure is stored and said ("its spot lines failed to read: ..."). The total is saved as before, and the last known spot part is kept. It costs one more call a day per billed cluster (0.01 USD each on Cost Explorer).
- GCP's export query sums its `Spot Preemptible` and legacy `Preemptible` SKUs with the total, using only fields it already reads. This was chosen beyond the Operator's answer, for GCP only.
- `billing.report` sets `spot_billed` beside `spot_estimated`, in the API, the console, `sizing bill` and the Hub MCP's `bill`. There is no verdict of its own.
- A bill with spot lines on a cluster that does not name its spot nodes says so.

**Tests:** every branch; two Contracts' spot parts kept apart; 21 mutations, all caught.

**Checked on AWS, Part B (2026-11-17 to 21):**
- AWS's grouped lines are `USE2-SpotUsage:<type>` (both `t3.medium` and `t3a.medium` seen) and `USE2-BoxUsage:<type>`, as the code reads them.
- Spot billed 0.2690 against 0.2842 estimated on 19 November (+5.7%); over two days, 0.5144 against 0.5684 (+10.5%).
- Cost Explorer still marked the days Estimated when read.

**Known limits:**
- Owed: the grouped replies of Azure's spot read and GCP's query. A wrong shape now costs only the spot part.
- The estimate takes today's spot price, 0.01184 USD an hour for `t3.medium`, where AWS billed about 0.0112 that day. It prices every spot hour at the latest spot node's type. 18 November ran partly on `t3a.medium`, and node facts keep only the latest nodes.

## AWS checks, Part A (2026-11-16)

Phases 15 and 16 were checked live on AWS from 04:12 to 12:32 UTC on 2026-11-16, for about 2.3 USD of the Operator's credits (ceiling 5). The setup was a throwaway EKS 1.37 cluster in a sandbox account: two `t3.medium` system nodes, Karpenter 1.15.2 with one NodePool for spot and on-demand 2-vCPU types, kube-prometheus-stack, and the Hub 0.59.0 with its Postgres in the cluster, on a synthetic Contract. Everything was tagged, torn down and swept empty.

**Results:**
- **16.2b:** `web` (10 replicas, ratio 0.7, minimum 2) had Karpenter put all ten on two spot nodes when unsteered. After a restart, three pods required off spot made Karpenter launch an on-demand node, and seven kept the preference. Scaled to 4, only spot replicas went.
- **Real spot interruption, twice** (AWS FIS, two minutes' notice): Karpenter read its queue about 25 seconds after the notice, then tainted, drained and deleted the node.
  - 16.3b's evicted run was retried required off spot and succeeded. Its pod's `DisruptionTarget` reason was `EvictionByEvictionAPI` (Karpenter's Event said "Forceful Termination"), the reason the no-Rollback matches.
  - `web`'s evicted pods fell back to on-demand; no spot node was launched. The result was 5 and 5, against 3 and 7 intended. The Nodes page shows this honestly ([ADR 0006](../adr/0006-placement-is-a-preference.md): a running pod is never moved).
- **15.1b:** squero and Karpenter's `DisruptionBlocked` named the same `do-not-disrupt` pod.
- **15.2b, Phase 15's done-when on Karpenter:** a pinned, tainted node group. The Hub said "restart keeper to free" the on-demand node; the webhook gave the restarted pod the preference and the toleration, and it landed on the pinned node, whose verdict was `pinned`.
- **16.4a:** the AWS spot price read is in its checks above.

**Found:**
1. 15.4's node fit saw no node constraints, so squero said a node could go where Karpenter refused, twice: a spot node whose pod preferred spot ("SpotToSpotConsolidation is disabled"), and an on-demand node whose pods had a `nodeSelector` ("Can't replace with a cheaper node"). Karpenter's consolidation holds a preference as required while a new node could meet it (its `consolidation.go` and `scheduler.go`). Spot-to-spot replacement needs an alpha gate, off by default, and 15 cheaper instance types.
2. The Actuator's own node read "unknown (1 pod not visible)": its pod was outside the Contract's namespaces.
3. A CronJob every 3 minutes under `consolidateAfter: 5m` kept its node from ever consolidating.
4. After a Contract change and a rollout, the hourly Blocker report named a pod already gone, until the Actuator reported again.
5. With the Pinned nodes' label not allowlisted in kube-state-metrics (a setup slip, as [`optimizer.md`](../optimizer.md) warns), the pinned node was listed as one that could go.

**Not covered:** a drain with a held Sizing. There was no usage history to make one.

## AWS checks, Part B (2026-11-17 to 21)

A throwaway EKS 1.37 cluster in `us-east-2` (no NAT), with one on-demand `t3.medium` and one spot node (`t3a.medium` or `t3.medium`), every instance and volume carrying a cost-allocation tag the Operator had activated. Prometheus was snapshotted and the cluster deleted after about 50 hours (about 8.5 USD of the Operator's 15). The Hub's own code read the bill through the snapshot. The key was the Operator's and never reached the session.

**Results:** 16.1b reconciled, and 16.4b's spot lines were confirmed (see those slices). 16.4a's live read works once the permission was granted.

**Found:**
1. kube-state-metrics was not allowlisted `topology.kubernetes.io/region`, so nodes had no region and list prices had none. The check added the region from the zone label, for that read only.
2. AWS replaced the spot node twice in the 50 hours. Prometheus kept the history.
3. The bill check's command broke when the Operator's terminal wrapped it over two lines. It is now run from an executable file.
