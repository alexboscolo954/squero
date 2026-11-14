# Resource optimizer

The Hub reads each cluster's usage, sizes every container (a *Sizing*), prices it, allocates cost by namespace and team, reports waste and Savings, and checks its estimate against the cloud Bill. Sizings are read-only until an Operator approves one; the Actuator ([client-cluster.md](client-cluster.md#running-an-actuator)) then applies it in the client's cluster.

- [Workload usage](#workload-usage)
- [Sizings](#sizings)
- [Sizing policies](#sizing-policies)
- [Waste and risk, stored](#waste-and-risk-stored)
- [Unit prices](#unit-prices)
- [Cloud list prices](#cloud-list-prices)
- [Bill check](#bill-check)
- [Cost by namespace](#cost-by-namespace)
- [Cost by label and team](#cost-by-label-and-team)
- [Savings, and comparing periods](#savings-and-comparing-periods)
- [Realized Savings](#realized-savings)
- [The weekly Sizing digest](#the-weekly-sizing-digest)
- [Sizings in a Triage](#sizings-in-a-triage)
- [The optimizer lab](#the-optimizer-lab)

## Workload usage

The Hub reads each cluster's Prometheus itself, with no model call, to learn what every Workload's containers use: `squero sizing usage --contract <id> --window 7d` prints it (`--json` too). A Workload is found by following pod owners in kube-state-metrics (a ReplicaSet up to its Deployment or Argo Rollout, a Job up to its CronJob, an ownerless pod as itself, any other owner as it is); pods kube-state-metrics doesn't list are looked up once through the Kubernetes API, which sees live pods only. CPU (cAdvisor's `rate`, so restarts don't skew it) and memory (working set, peak per step) are pooled across a Workload's pods per container, so a Sizing is per replica whatever the replica count; requests, limits, OOM-killed pods and CPU throttling come with them. Every query carries the cluster's namespaces and its label matchers. On a store shared by several clusters or Contracts (Thanos, Mimir), `prometheus_labels` must tell them apart: `squero contracts validate` and the Hub refuse two clusters, in one Contract or two, reading the same Prometheus (however its URL is written) with a namespace in common unless one label has different values in their `prometheus_labels`: `{cluster: eu}` also matches every `{cluster: eu, env: prod}` series, and no labels match every cluster's. Two DNS names for one store still look like two. Required: cAdvisor and kube-state-metrics (`kube_pod_owner`, `kube_pod_container_resource_requests`); the command names what is missing, and also reads kube-state-metrics scraped without `honor_labels` (the pod's namespace, pod and container in `exported_*`). A cluster uses the Contract's `prometheus` integration unless it names its own:

```yaml
clusters:
  - name: prod-eu
    context: prod-eu
    namespaces: [shop]
    prometheus_url: https://thanos.acme.example      # optional: else the prometheus integration's
    prometheus_token_secret: thanos-token            # optional, a name in secrets.sops.yaml
    prometheus_labels: {cluster: prod-eu}            # optional: added to every query
```
The agent's own `prometheus` tools take a `cluster` in a Contract with clusters, and query that cluster's Prometheus: its `prometheus_url` and token, else the integration's. A cluster with `prometheus_labels` gets none: labels mark a store shared with other clusters on purpose, and the Hub can't force them into the agent's PromQL as it does into its own queries. The tool's description says so; the agent keeps its Kubernetes tools there, and `sizing_for_workload` when the Contract lists `sizing`. The same goes for a cluster whose Prometheus another Contract also reads, even in other namespaces (which loading allows: the Hub's own queries are scoped by namespace); store URLs are compared normalized, as in that check. A Contract without clusters keeps the tools without `cluster`, on the integration's URL. The health check queries each cluster that has them, so a Contract whose clusters all have none has no Prometheus check.

## Sizings

`squero sizing show --contract <id>` prints each container's Sizing under its Sizing policy: what its requests should be, and whether that means set, raise, lower or keep. Nothing is applied. Under `production`, the default, CPU is the 95th percentile of the last 7 days, recent hours weighted more (half-life 24 hours: a drop is followed in about 4 days, a rise within hours), plus 15%. Memory is the peak, plus 20%, so it follows a drop only once the old peak leaves the window. Then a floor of 10m and 32 MiB, then the policy's maximum if it has one (a capped Sizing is listed as a risk), then the limit: limits are kept, a request never goes above one, and a limit below what the container needs is listed as a risk. Requests are rounded up to a millicore and a MiB, and change only when the difference is at least 10% and at least 10m or 16 MiB. Throttling over 10% of CPU periods or an OOM kill hides the real demand, so CPU is never lowered while throttled, and memory never after an OOM kill; both are listed as risks. A container with under 24 hours of history is still learning.

A container's history is covered time: the time its cluster's Prometheus has usage for (of any container in the Contract's namespaces there), from the container's first sample in the window to its last. A gap in Prometheus counts for nothing, so an hour of data after three days without a scrape is an hour of history. A CronJob between its runs, or a Workload scaled to zero beside others that run, keeps its history. After a long gap a Sizing can be learning again until the window holds what its policy needs: Automation applies nothing new to it meanwhile, the Actuator keeps what it holds, Reflexes go on (a Step-up that reaches its term then is kept for another, its memory peak not being known), and its waste leaves the totals, so the digest's waste goes down by it. One limit: where a Contract's namespaces on a cluster hold only periodic Workloads, only their run times count, so a CronJob of 5 minutes a day stays learning there. Give it a policy with a shorter `history`, or it is ready once anything in those namespaces runs all the time.

## Sizing policies

A policy sets the window, the CPU percentile and its half-life, the headroom, floors and maximums, the change threshold and the history needed before a first Sizing; the throttling and OOM holds are not settings, no policy turns them off. Built-ins:

| | production | high-availability | batch |
|---|---|---|---|
| CPU | weighted p95 + 15% | weighted p99 + 30% | weighted p90 + 10% |
| Memory | peak + 20% | peak + 35% | peak + 15% |
| Window, half-life, history needed | 7 d, 24 h, 24 h | 14 d, 72 h, 72 h | 14 d, 24 h, 24 h |
| Floors | 10m, 32 MiB | 50m, 64 MiB | 10m, 32 MiB |

Your own policies and the rules that pick one go in `contract.yaml`; `squero contracts validate` checks them (ranges, a known `base:`, known policy names in rules):
```yaml
sizing:
  default: production                  # optional; off sizes only what a rule names
  policies:
    lean-agent:                        # a built-in with the settings it names replaced
      base: production                 # production, high-availability or batch
      window: 3d                       # 1h to 90d; half_life 1h to 30d; history up to the window
      cpu: {headroom: 0.10, max: "2"}  # percentile 0.5 to 1, headroom 0 to 3, floor, max
      memory: {floor: 64Mi, max: 4Gi}  # Kubernetes quantities
      threshold: 0.05                  # 0 to 0.5
      replicas: {percentile: 0.25}     # 0 to 0.5: the Floor of a Workload with an HPA
  rules:                               # top to bottom, the first match wins
    - {namespace: payments, policy: high-availability}
    - {namespace: "team-*", name: "checkout-*", policy: lean-agent}   # globs: namespace, name
    - {container: istio-proxy, policy: off}                          # also cluster, kind
    - {namespace: shop, policy: production, automate: true}          # Automation (Phase 12)
```
After the rules, a Job or CronJob gets `batch`, everything else the default. `automate: true` on a rule is Automation: the Actuator applies its containers' Sizings without an approval ([Automation](client-cluster.md#automation)); it needs a policy other than `off` and a cluster with an `actuator_token_secret` (the one the rule names, if it names one). `startup_boost: 2` beside it (1.5 to 4, refused without `automate`) starts that rule's containers with twice their CPU limit until their pod is Ready ([the Startup boost](client-cluster.md#the-startup-boost)). Built-in names mean the same in every Contract (a standing approval names a policy, ADR 0004), so they and `off` can't be reused. `off` lists the container as excluded. The output names each container's policy, and `history 3.0d of 14d` when Prometheus holds less than the policy's window. Each window a Contract's policies use is read in full, so a policy's OOM kills and throttling are from its own window.

## Waste and risk, stored

Every hour the Hub sizes each Contract with a Prometheus and stores the run in Postgres: a row per container with its policy, state, Sizing, average pods and risks. Waste is the request above the Sizing times the average pods, in cores and bytes: what the Sizings would free with headroom kept, nothing for a container that is learning, raised or held. Average pods counts Running pods from kube-state-metrics' `kube_pod_status_phase` (or, without it, the CPU samples, which `rate` keeps about 5 minutes past a pod's end, so short Jobs read high) over the steps between a container's first and last on which its cluster has any sample: a CronJob averages over the time it doesn't run, and a gap in Prometheus counts for no one. Each risk has a kind: `oom_killed`, `throttled`, `under_requested`, `limit_below_need`, `capped_by_policy`, `request_unset`. Runs are kept hourly for 14 days, then the last of each day for 400. A cluster that fails is stored with its error, and readers keep its last good run, so one dead cluster never blanks another. `squero sizing refresh --contract <id>` stores a run now; `squero sizing waste --contract <id>` (`--json` too) ranks the Workloads by waste, a core weighing as much as 4 GiB without prices, with their risk kinds, what is learning or excluded and any stale cluster. Both need `SQUERO_DATABASE_URL`.

## Unit prices

With `prices:` a Contract's Sizings show money as well as cores and GiB:

```yaml
prices: {currency: EUR, cpu: 0.0316, memory: 0.0042}   # per vCPU-hour and per GiB-hour
clusters:
  - name: eks-prod
    prices: {cpu: 0.040, memory: 0.0053}               # this cluster's own; the currency stays the Contract's
```

Each Workload gets the monthly cost (730 hours) of its waste and of its requests, and the Contract gets both totals. The console's Workloads page, the weekly digest and `squero sizing waste` rank by the waste's cost and show it as `41.20 EUR/month`. Requests count every container that has one, learning or excluded too, while waste counts only sized ones. It is the cost of what pods reserve, not of the nodes: idle node capacity isn't in it. Money is worked out when shown from the stored quantities, so a corrected price applies to every past run, and the digest's change since last week is a change of usage at today's prices. Rows stored before 10.1 hold requests only for ready containers. One currency per Contract, never converted. A price can be 0 (hardware already paid for). A cluster's prices need the Contract's, so every cluster of a priced Contract is priced and they rank together. Without `prices:` nothing changes: cores and GiB, and a core ranks like 4 GiB. The agent's `sizing_for_workload` never shows money.

## Cloud list prices

Instead of typing `prices:`, a Contract can take them from what its clusters' nodes cost on demand:

```yaml
list_prices: {weight: 7, discount: 0.6, gcp_key_secret: gcp-pricing, currency: USD}   # all optional
clusters:
  - name: eks-prod          # provider, region and instance types come from kube-state-metrics
  - name: on-prem
    prices: {cpu: 0.02, memory: 0.003}                   # typed prices always win
    provider: aws           # only for a cluster that can't say (kube_node_info's provider_id)
```

Each hourly run reads the cluster's nodes (`kube_node_info`, `kube_node_labels`, `kube_node_status_capacity`); kube-state-metrics must allowlist the node labels (`--metric-labels-allowlist=nodes=[node.kubernetes.io/instance-type,topology.kubernetes.io/region]`). Once a day the Hub looks up each (provider, region, instance type) in the cloud's public price list: AWS's offer file (streamed, Linux, shared tenancy), Azure's Retail Prices API (pay-as-you-go Linux, no Spot) and, only with `gcp_key_secret` naming a Billing API key, GCP's catalog (cores and RAM of the machine family, on demand, not custom). A failed lookup is tried again after an hour and keeps the last good price and its age. A cluster's prices are its nodes' total price over their total vCPU and GiB, one vCPU worth `weight` GiB (default 7), so the CPU and GiB prices add back up to the nodes' price exactly; `discount` (0 to 1) multiplies them for a committed-use deal. The currency is USD unless `list_prices.currency` (three capitals, e.g. `BRL`) says otherwise: only Azure's and GCP's catalogs offer one, so every cluster must then set `provider: azure` or `provider: gcp`; prices are never converted. On demand only, no Spot. A cluster without labels (kind, bare metal) or without a price yet is not priced, says why, and leaves the whole Contract in cores and GiB, since money for one cluster and cores for the other can't be ranked. `squero sizing prices --contract <id> [--refresh]` shows where each cluster's price comes from, the Cost and Workloads pages and `sizing cost` and `sizing waste` print the same line. `list_prices` and a Contract-level `prices` can't both be set.

## Bill check

A cluster may name its bill lines with `billing: {tag: "squero-cluster=kind"}` (the Contract needs a Prometheus and the `aws`, `azure` or `gcp` integration; with more than one, the cluster sets `provider:`). AWS: a cost-allocation tag activated in AWS Billing, read from Cost Explorer (`GetCostAndUsage`: EC2 compute, usage before credits, unblended, a day at a time; the `aws` integration's IAM user needs `ce:GetCostAndUsage`). Azure: any resource tag, read from Cost Management (`type: ActualCost`, `PreTaxCost`, the Virtual Machines service, usage charges, a day at a time) with the `azure` integration's service principal (tenant, client id and subscription from its `env`, the client secret from `env_secrets`; it needs Reader or Cost Management Reader); an AKS node pool carries the tag to its scale set, and a day without usage reads as 0. GCP: a label (lowercase letters, digits, `_` and `-`, the key starting with a letter), read from the billing account's BigQuery export, so the cluster also names its table: `billing: {tag: "squero-cluster=kind", table: "my-project.billing.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX"}`. Turn the export on in the Cloud Console (Billing, Billing export, standard usage cost); it holds data only from then on. The `gcp` integration's service account needs BigQuery Data Viewer on the dataset and BigQuery Job User on its own project (`GOOGLE_CLOUD_PROJECT`, where the query runs); for this read alone the Hub mints a token with only the `bigquery.readonly` scope. It sums `cost` (before credits) of Compute Engine's on-demand core and RAM SKUs per UTC day: shared-core machines (e2-micro, e2-small, e2-medium), Spot and committed-use lines are not counted, nor disks and network. The Hub reads once a day on its hourly run (a failure, a 429 included, is tried again after an hour, about one call per billed cluster a day) and keeps the last 30 full days. `squero sizing bill --contract <id> [--days N] [--refresh] [--json]` and a line on the Cost page set the bill of the last N (default 7, up to 30) full UTC days ending 2 days ago, while the cloud settles them, beside the estimate: the cluster's node capacity-hours (kube-state-metrics, an hourly sample each) at its unit prices, typed or from `list_prices`. The gap is the estimate's distance from the bill as a fraction of the bill; within 5% is reconciled, else flagged (the CLI exits 1). The estimate counts the node-hours Prometheus sampled: when some hours of the window are missing (a laptop that slept) the row says "the estimate rests on N of M sampled hours", and below 90% of the hours it shows the gap but no verdict (and the CLI doesn't exit 1 for that alone). A bill in another currency than the prices (an Azure subscription billing in BRL needs `list_prices: {currency: BRL}`), a day not yet settled, a failed read or a cluster without a price says why and shows no gap; nothing is converted. Reconcile only: no shown cost changes.

## Cost by namespace

The console's **Cost** page and `squero sizing cost --contract <id> [--json]` group a Contract's latest stored Sizings by namespace (each cluster's own: `kind/lab`, not `lab`): each namespace's requests (every container, learning and excluded ones too), its waste (sized containers), what both cost a month with the Contract's prices (cores and GiB without them), and how many Workloads it has, ranked by what its requests cost. A namespace links to the Workloads page filtered to it. The namespaces add up to the Contract's totals on the Workloads page. It is the cost of what pods reserve in the Contract's namespaces, not of the nodes.

## Cost by label and team

List pod labels and team rules in `contract.yaml`, and the Cost page gets a **Group by** picker (`squero sizing cost --by team` or `--by label:<key>`):

```yaml
allocation:
  labels: [team]                    # read from kube-state-metrics' kube_pod_labels
  teams:                            # first match wins, else "(no team)"
  - {team: payments, namespace: "pay-*"}
  - {team: storefront, labels: {team: storefront}}
  - {team: platform, cluster: kind}
```

kube-state-metrics exports no pod labels unless allowlisted: `--metric-labels-allowlist=pods=[team]` (kube-prometheus-stack: `kube-state-metrics.metricLabelsAllowlist: ["pods=[team]"]`). The Hub reads only the listed keys, takes a Workload's labels from its newest pod (by `kube_pod_created`), and stores them with each hourly run. Team rules are matched when shown, on the cluster, a namespace glob and label values, so a changed rule applies to past runs too. Runs stored before a label was listed have no labels, so only cluster and namespace rules match them. A team or a label value spans clusters, unlike a namespace. Workloads without the label go to `(no <key>)`. When no pod in a cluster has the label, the page and the CLI warn that kube-state-metrics probably doesn't allowlist it.

## Savings, and comparing periods

The Cost page and `sizing cost` name the waste's cost the **available Savings**: what the requests above the Sizings cost, per group and for the whole Contract (cores and GiB without prices). What under-sized containers need on top stays a risk on the Workloads page, never netted against it. **Compare with** (1, 7, 30 or 90 days ago; `squero sizing cost --compare 30d`, any `m`, `h` or `d`) adds each group's change in requests and Savings since then: each cluster's latest run now against its latest run at or before then, both at today's prices, so a price change alone changes nothing. The page names the run each cluster is compared with: after 14 days only one run a day is kept, so it can be a little older than asked. Only what both runs know is compared. A cluster with no run that old is left out, named with its oldest run. A container whose requests weren't stored then (learning or excluded before 0.8.0) is left out. So, when the grouping reads labels, is every container of a cluster where no pod had them then but some do now (stored before the Contract listed them or before kube-state-metrics exported them), which would otherwise seem to move from `(no team)` into its team. Both are listed as left out. A Workload new since counts as new cost, and a group gone since stays listed with its whole drop and no Workloads.

## Realized Savings

Each hourly run compares every container with its cluster's previous good run. A request lowered to within 10% of a Sizing the Hub showed in the 14 days before (one below the old request) is stored as a drop, CPU and memory apart. It is worth (old request − new request) × the container's pods in its latest run, at today's prices (cores and GiB without prices): a monthly run rate, and an amount accrued since the drop, which is that rate × the hours since. A later rise takes the drop back, the newest first, and a drop back at its old request is gone; so a partial rise lowers the accrued amount too. A container gone from a cluster that was read, or whose request was removed, ends its drops; a failed read ends nothing. Fewer pods and raised requests never count as savings, and a drop that matched no Sizing isn't stored. An HPA's minimum lowered toward its [Floor](#floors) counts here too. `squero sizing savings --contract <id> [--json]` lists each drop; the Cost page shows the totals, and the weekly digest adds *Realized this week* (left out while nothing was realized). Requests and limits are read from the pods seen in the last 5 minutes, so a lowered request shows the next run; a container with no live pod (a CronJob between runs, a Workload scaled to 0, kube-state-metrics down) keeps the largest of its window, and such a run neither starts, takes back nor ends a drop. So only a drop between two runs of 0.12.0 or later counts (there is no backfill), and a CronJob whose pods are cleaned up between runs rarely realizes. The pods are the latest run's average over its window, as for its waste.

## The weekly Sizing digest

Each Monday at 09:00 UTC the Hub posts a digest of each Contract's stored Sizings to its operator channel, never the client channel: total waste and its change since 7 days before, the top 5 Workloads by waste, risk kinds a Workload didn't have a week before (every risk, under "Risks", while there is no week-old run), stale clusters, how many containers are still learning, and a link to the Workloads page when `SQUERO_CONSOLE_URL` is set. On for every Contract with a `slack:` and a Prometheus when the Hub has `SQUERO_SLACK_BOT_TOKEN`. `sizing: {digest: {schedule: "30 8 * * 5", timezone: Europe/Rome}}` moves it, `sizing: {digest: off}` turns it off. Each due time posts once, also across restarts and missed weeks (claimed in Postgres before posting, so a post Slack refuses is lost for that week, never sent twice). `squero sizing digest --contract <id>` prints it now; `--post` also posts it, leaving the schedule as it is.

## Sizings in a Triage

`- name: sizing` gives the agent `sizing_for_workload`: one Workload's stored Sizing (the Hub's latest run, as the console shows), by the Workload's name or one of its pods' (the longest Workload name the pod name starts with, followed by `-`; `kind` only when two share a name). Per container: the request and limit now, the Sizing and the usage it came from (CPU's weighted p95, memory's peak), the risks, average pods and waste, when the run was and whether its cluster's reads are failing. After an OOM kill a Sizing keeps memory as it is, since the peak is the limit and not the demand, so the tool adds `suggested`: memory request and limit at the policy's memory headroom above the limit (64Mi gets 77Mi), or, with no limit, a request that far above the peak, the node having run out of memory. The guide asks a Triage of a resource alert to call it once and cite it. It reads only the Contract's own Sizings, of its clusters with a Prometheus and their allowed namespaces; `contracts validate` refuses the integration without such a cluster. The CLI's `squero triage` has no database, so it leaves the tool and its guide out. Rows stored before 0.6.0 have no limits, so they get no suggestion until the next hourly run.

## Needs of autoscaled Workloads

For every Workload with an HPA (a KEDA ScaledObject's `keda-hpa-<name>` is one) the hourly run also reads and stores its **Need**: the replicas it needed, whatever the HPA's minimum and maximum allowed. Nothing is shown yet and nothing is ever written to an HPA: the Need is what the Floor and the Weekly profile (Phase 14) will be computed from, and it is stored now because a profile needs more weeks than most Prometheus keep.

- **The figure.** The HPA's replicas at a time, times the current value of the metric it scales on over that metric's target (4 replicas at 75% of their requests against a target of 50% needed 6); with several metrics, the largest. It works for any metric kind (resource, one container's, pods, object, external), each current value compared with the target of its own type. With no replicas the Need is 0. The replicas counted are the smaller of that moment's and the sample before's: right after a scale-up an HPA reports its new replica count with the utilization of the pods that already have metrics, the old ones, which would read as a Need never there (3 replicas at 83% going to 5 needed 5, not 8).
- **Read from** kube-state-metrics, in the Contract's namespaces only, like every other read: `kube_horizontalpodautoscaler_info` (the Workload an HPA scales), `_status_current_replicas`, `_spec_target_metric` and `_status_target_metric`. No new permission and no Kubernetes API call. A cluster without an HPA costs one query an hour.
- **Unknown**, so nothing is stored for that time: an HPA whose target or current value kube-state-metrics doesn't have (a metric the HPA can't read, or a kube-state-metrics that doesn't export `_spec_target_metric`), a target of zero, and a Workload two HPAs scale.
- **Stored** as each UTC hour's peak per Workload, for 8 weeks. The first read goes back over the 8 weeks Prometheus still has, sampled every 9 minutes; after that each run reads on from the newest hour stored, every minute, so a Hub that was down catches up. An hour read twice keeps the larger figure. A peak between two samples is missed. A failed read is logged and never fails the Sizing run.

## Floors

A **Floor** is the minimum replicas the Hub computes for a Workload with an HPA, from its stored Needs. Each hourly run reads the HPA's minimum and maximum as they are (`kube_horizontalpodautoscaler_spec_min_replicas` and `_spec_max_replicas`) and stores each cluster's Floors, replacing the last. `squero sizing show`, the console's Workloads page (Autoscaled Workloads), the Hub MCP's `sizings` and one line of the weekly digest show them. Nothing is written to an HPA: lowering a minimum is the client's manifest change.

- **The Floor.** A low percentile of the Workload's hourly Needs over its Sizing policy's window, rounded up as an HPA rounds: the 10th under `production` and `batch`, the 25th under `high-availability`, a policy's `replicas: {percentile: 0.10}` (0 to 0.5). Never below 2, unless the HPA's minimum already is. With containers under different policies the highest percentile decides, with that policy's window and history; a Workload whose containers are all `off` gets none.
- **History.** Only the Workload's own stored hours count, each an hour, never the hour under way. Under the policy's `history` (24 hours under `production`) the Floor is learning. Needs are kept 8 weeks, so a Floor looks back 8 weeks at most and never asks for more than 4 weeks of history, whatever the policy says.
- **Against the HPA's minimum.** A Floor below it is a saving: for each stored hour, the replicas the lower minimum would have dropped (the minimum less the larger of the Floor and that hour's Need rounded up, when positive), averaged, each replica at its containers' Sizings (at its request for a container without one). It is its own figure, on top of the Waste: Waste is what every pod asks above its Sizing, this is the pods no longer held. With prices it has a monthly cost. A Floor above the minimum is a risk (`minimum_below_floor`): the Workload needed more in most hours than its minimum guarantees. Equal, nothing is said.
- **The maximum.** No maximum is recommended. Where the Need went above it, one risk says how often: `needed more than its maximum of 5 in 6% of hours`.
- **No Floor**, with the reason shown: two HPAs on the Workload; a minimum kube-state-metrics doesn't have; and a Workload that scales to zero, which is left alone. KEDA gives its HPA a minimum of 1 and scales to zero itself, so scaling to zero is read from the HPA's replicas: 0 at some time in the Contract's longest policy window (8 weeks at most), or an HPA minimum of 0. A Need of 0 is not that: an idle Workload held at its minimum has one, and it is where a Floor saves most. One scaled to zero by hand in that time reads the same and gets no Floor until it is out of the window.
- **Realized.** Each hourly run compares every HPA's minimum with the Floor the last run showed. A minimum lowered while that Floor was below it is stored as a drop, down to the new minimum and never below that Floor: lower is not the Hub's advice, and shows as the `minimum_below_floor` risk. It counts in the [Realized Savings](#realized-savings) with the request drops, listed apart: worth the replicas no longer held (per stored hour since the drop, over the last 7 days at most, the old minimum less the larger of what it counts down to and that hour's Need rounded up, averaged) times the pod's requests as they are now, every container's, at today's prices, as a monthly rate, and accrued as that rate × the hours since. Here a replica counts at what it really asked, not at its Sizing as in the available saving. A minimum raised again takes the drop back, the newest first, and at the old minimum it is gone; an HPA gone, or a second HPA on the Workload, ends it; a failed read or an unknown minimum ends nothing. It is worth nothing until an hour since the drop is stored. No backfill: only a lowering between two runs of 0.49.0 or later counts.
- **Not yet.** The available saving is not in the Cost page's groups.

## Weekly profiles

A Workload with a Floor also gets a **Weekly profile**: its Floors by hour of the week, for load that follows the week. It is shown under the Floor in the console, in `squero sizing show` and in the Hub MCP's `sizings`, and it is stored with the Floor by each hourly run.

- **Each hour of the week** is the median of that hour's Need across the weeks stored, rounded up, never below the Floor. With three weeks, one odd week changes nothing. An hour of the week stored in fewer than three weeks (a gap in Prometheus at that hour in the others) is at the Floor, since one week would decide it; a block can be split there.
- **Three weeks first.** The Workload's own complete stored hours count, over the 8 weeks Needs are kept; under 504 of them the profile is learning.
- **Blocks.** Each run of hours that asks the same, above the Floor, is a block: `Mon 08:45 to Mon 12:00: 7 replicas`. The Floor is the baseline and not listed. A block that asks more than the hour before it starts `sizing.lead_time` early, so the replicas are there when the load is; a block after a higher one starts on the hour. All end on the hour. A week that asks the same in every hour has no pattern, and says so. Blocks are not smoothed, so a noisy week gives many, and a block is not capped at the HPA's maximum: where it asks more, the maximum holds it down, and the Floor's risk line already says the maximum was too low.
- **Settings**, per Contract: `sizing.timezone`, an IANA name the hours are shown and scheduled in (UTC by default), and `sizing.lead_time`, whole minutes from 0 to 2h (15 minutes by default).

  ```yaml
  sizing:
    timezone: Europe/Rome
    lead_time: 30m
  ```

- **KEDA.** For an HPA named `keda-hpa-<name>` each block comes as a `cron` trigger for the ScaledObject `<name>`, to paste beside its other triggers. KEDA holds at least `desiredReplicas` in each window and takes the trigger that asks for most:

  ```yaml
  - type: cron
    metadata:
      timezone: Europe/Rome
      start: 45 8 * * 1
      end: 0 12 * * 1
      desiredReplicas: "7"
  ```

  One trigger per block, each with a single weekday (Sunday is 0): KEDA's docs don't say that the weekday field takes lists or ranges.
- **A plain HPA** has no schedule of its own. It gets the profile with a note saying so: something else has to hold those replicas. The Hub writes nothing and generates no CronJobs.
- **Limits.** In a zone with daylight saving the hours of the two weeks around a change are an hour off against the rest. There is no backtest yet (slice 14.4): the profile says what the last weeks' median was, not how well it would have held.

## Sizings held by an HPA

An HPA with a `Utilization` target on cpu or memory divides the pods' usage by their requests. Changing such a request changes the percentage and so the replica count, which is why the Actuator leaves it alone ([the pre-checks](client-cluster.md#running-an-actuator)). The Hub marks a Sizing that would change such a request as **held by its HPA**: in `squero sizing show`, in the console's Workloads page under the container, and in the Hub MCP's `sizings`. Its Waste still counts, because one manifest change frees it.

- **The change.** The HPA's target as an `AverageValue`: the same threshold in absolute terms. 50% of pods that ask 1200m of CPU in all is 600m per pod. After it the HPA reads no requests, the replica count follows usage as before, and the Actuator resizes that request like any other. The Hub shows it ready to copy:

  ```yaml
  # HorizontalPodAutoscaler shop/api, its cpu metric:
  # 50% of the pod's cpu requests now (1200m), as a value.
  # Apply it before any request changes: a request cut first scales it out.
  target:
    type: AverageValue
    averageValue: 600m
  ```

- **Whose requests.** For a metric of the whole pod, every container's request summed (sidecars too, sized or not), and every container whose Sizing would change it is held. For a per-container metric, that container's own request, and only it is held. The value is computed from the requests as they are now, so apply it before any of them changes: with a request already cut, the HPA has scaled out and the value shown is smaller.
- **KEDA.** An HPA named `keda-hpa-<name>` is taken for the ScaledObject `<name>`'s, and the change is given for its trigger (`metricType: AverageValue`, `value` as a quantity, `containerName` for a per-container trigger). A wrong guess only shows the wrong form.
- **Read from** kube-state-metrics as it is at the run (`kube_horizontalpodautoscaler_info` and `_spec_target_metric`), in the Contract's namespaces; a failed read marks nothing and fails nothing. A target switched to `AverageValue` stops holding at the next hourly run.
- **Limits.** A kube-state-metrics before 2.19 doesn't say which container a per-container metric is of: it reads as the whole pod's, and the comment above the change says which requests the value was taken from. Where a container of the pod has no request for that resource, the HPA's `Utilization` target isn't working at all, so the value shown (from the requests there are, or none) is no equivalent of it. With two HPAs on one Workload the first by name is shown. Nothing is ever written to an HPA or a ScaledObject: the client's GitOps applies the change.

## The optimizer lab

`make lab` turns the kind cluster into a persistent lab: kube-prometheus-stack (chart pinned in the Makefile, the setup most client clusters run; Prometheus keeps 30 days, Grafana via `make lab-grafana`) and, in namespace `lab`, Workloads with the usage shapes a Sizing must get right: a 24-hour diurnal Deployment with two replicas, an over-requested one, a spiky one, a slow memory leak that its limit OOM-kills about hourly, a CPU-throttled one, an idle one, a StatefulSet, a DaemonSet, a CronJob, and `weekly`, a Deployment under an HPA (CPU at 50%, 2 to 5 replicas; metrics-server is installed for it) whose load follows the week and is shared by its pods: it needs 1 replica at night and at weekends, 3 on weekdays from 08:00 to 18:00 UTC and 7 on Mondays from 09:00 to 12:00, so its Need reads below the minimum and above the maximum. Prometheus is reachable from the Hub on kind's Docker network at `http://squero-control-plane:30090`, so a Contract's cluster can set it as `prometheus_url` with `lab` in its namespaces. Leave the lab running: real history builds up. Its history lives inside the kind node container, so `kind delete cluster` erases it.

A laptop that sleeps leaves gaps in that history, and a Weekly profile needs weeks without them. The lab can run on another host that stays up: `make lab-remote LAB_HOST=<ssh name> LAB_ADDR=<address>` creates a kind cluster `lab` there (the host needs Docker and kind) whose API server and Prometheus listen on that address only. Use a private one, the host's Tailscale address: Prometheus has no login and the kubeconfig, written to `~/k8s_clusters/lab.yaml`, is an admin's. The node container is capped (2.5 CPUs and 6 GiB by default, `LAB_CPUS` and `LAB_MEMORY`) so the host's own services keep theirs, and restarts with Docker. Then `make lab LAB=small CLUSTER=lab KUBECONFIG=~/k8s_clusters/lab.yaml` installs the lab from here. `LAB=small` (`demo/lab/small`) is the lab for a 4-CPU host: the full one asks about 8 CPUs of its node, so there the big Workloads ask and burn a quarter to a third of the CPU, each in the same proportion; the shapes, the ratios of request to usage, `weekly` and all memory are unchanged. A Contract's cluster then sets `prometheus_url: http://<address>:30090`. That is for a Hub on another machine. A Hub on the same host cannot use it: Docker gives one container no way to another's published port through the host's own address, and the read times out. There the Hub joins kind's Docker network (a `compose.override.yaml` with `networks: [default, kind]` for the `hub` service and `kind` as an external network) and the Contract names the node: `http://lab-control-plane:30090`, and `https://lab-control-plane:6443` as the kubeconfig's server, which the API server's certificate covers. For its kubeconfig, apply `demo/lab/hub-read.yaml`: the Hub's read-only role, bound in namespace `lab` only, with a token Secret (`squero-token` in namespace `squero`). A kubeconfig with that token and the cluster's server and CA goes into the Contract's SOPS file (`kubeconfig_secret`); kind's own kubeconfig never does. The same cluster can hold the Triage demo (`make demo CLUSTER=lab`) and a second Contract: `demo/lab/bank.yaml` is a small over-requested Workload in namespace `bank`, and `demo/lab/hub-read-demo.yaml` binds the first account in `demo` and `orders` and gives the second Contract an account of its own in `bank` only, so two Contracts share one cluster and one Prometheus and neither's kubeconfig reads the other's namespaces.
