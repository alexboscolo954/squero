# Resource optimizer

**In short:** the Hub reads each cluster's usage and sizes every container. That Sizing is priced, split by namespace and team, and checked against the cloud Bill. It also reports waste and Savings.

Sizings stay read-only until an Operator approves one. Then the [Actuator](client-cluster.md#running-an-actuator) applies it in the client's cluster.

## Contents

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

### What the Hub reads

The Hub reads each cluster's Prometheus itself, with no model call. It learns what every Workload's containers use.

```bash
squero sizing usage --contract <id> --window 7d    # add --json for JSON
```

A Workload is found by following pod owners in kube-state-metrics:

- a ReplicaSet up to its Deployment or Argo Rollout;
- a Job up to its CronJob;
- an ownerless pod as itself;
- any other owner as it is.

Pods that kube-state-metrics doesn't list are looked up once through the Kubernetes API. That API sees live pods only.

Usage is pooled across a Workload's pods, per container:

- **CPU:** cAdvisor's `rate`, so restarts don't skew it.
- **Memory:** working set, peak per step.

So a Sizing is per replica, whatever the replica count. Requests, limits, OOM-killed pods and CPU throttling come with them.

Every query carries the cluster's namespaces and its label matchers.

### Required metrics

- cAdvisor and kube-state-metrics (`kube_pod_owner`, `kube_pod_container_resource_requests`).
- The command names what is missing.
- It also reads kube-state-metrics scraped without `honor_labels` (the pod's namespace, pod and container in `exported_*`).

### Shared stores and `prometheus_labels`

On a store shared by several clusters or Contracts (Thanos, Mimir), `prometheus_labels` must tell them apart.

- `squero contracts validate` and the Hub refuse two clusters that read the same Prometheus, with a namespace in common. This holds in one Contract or two.
- It applies however the URL is written.
- The refusal doesn't apply if one label has different values in their `prometheus_labels`.
- Labels match by subset. `{cluster: eu}` also matches every `{cluster: eu, env: prod}` series. No labels match every cluster's.
- `contracts validate` compares URLs as written. It works offline.
- The Hub, at start, asks each store who it is. The `squero` commands that load one Contract do too.
  - They read `/api/v1/status/runtimeinfo`'s `startTime`. Prometheus and Thanos Query give it.
  - They use that cluster's own token.
  - So two names for one store count as one, and are refused the same way.
- A store that can't say (Mimir, VictoriaMetrics, or a store that is down) is compared differently:
  - by the addresses its host resolves to, with the same scheme, port and path;
  - an address in common only marks the stores shared ([below](#prometheus-for-the-agents-tools)).
- One ingress address often fronts several stores.
- Replicas behind one name answer each for itself. So two names for an HA pair can still look like two.
- A Relay's cluster isn't asked.

### Which Prometheus a cluster uses

A cluster uses the Contract's `prometheus` integration unless it names its own:

```yaml
clusters:
  - name: prod-eu
    context: prod-eu
    namespaces: [shop]
    prometheus_url: https://thanos.acme.example      # optional: else the prometheus integration's
    prometheus_token_secret: thanos-token            # optional, a name in secrets.sops.yaml
    prometheus_labels: {cluster: prod-eu}            # optional: added to every query
```

| Field | Default | What it does |
|---|---|---|
| `prometheus_url` | the `prometheus` integration's URL | The cluster's own Prometheus. Optional. |
| `prometheus_token_secret` | the integration's token | A name in `secrets.sops.yaml`. Optional. |
| `prometheus_labels` | not set | Labels added to every query. Optional. |
| `prometheus_auth` | `bearer` | `bearer` or `basic`. |
| `prometheus_user` | not set | Basic only: the user id. It is not a secret. |

The `prometheus` integration takes the same `auth` and `user` beside its `url` and `token_secret`.

#### Bearer and Basic

- The token goes as a Bearer token.
- A Prometheus behind Basic auth takes `prometheus_auth: basic` and `prometheus_user`. The token goes in `prometheus_token_secret` as the password. This is slice 19.1a. Grafana Cloud's is one example.
- Basic needs three things: a user, a token and an `https://` URL. So the password never crosses the network in clear.
- A user under `bearer` is refused.
- A URL with a path works. Grafana Cloud's ends in `/api/prom`.
- A cluster's token, auth and user always come from where its URL does. A cluster with its own `prometheus_url` never takes the integration's user.
- A Relay's cluster sets none of them. Its Relay's own Prometheus takes a Bearer token or none.

```yaml
    prometheus_url: https://prometheus-prod-01.grafana.example/api/prom
    prometheus_auth: basic                           # optional: bearer (the default) or basic
    prometheus_user: "123456"                        # basic only: the user id
    prometheus_token_secret: grafana-cloud-token     # the access policy token
```

### Prometheus for the agent's tools

The agent's own `prometheus` tools query a cluster's Prometheus. They take a `cluster` in a Contract with clusters.

- They use that cluster's `prometheus_url` and token, else the integration's.
- A cluster with `prometheus_labels` gets none of these tools.
  - Labels mark a store shared with other clusters on purpose.
  - The Hub can't force those labels into the agent's PromQL, as it does into its own queries.
  - The tool's description says so.
  - The agent keeps its Kubernetes tools there.
  - It keeps `sizing_for_workload` there when the Contract lists `sizing`.
- The same goes for a cluster whose Prometheus another Contract also reads. This applies even in other namespaces. Loading allows that, because the Hub's own queries are scoped by namespace.
- Stores are compared as in the [shared stores](#shared-stores-and-prometheus_labels) check: by URL, by what each store says it is, or by address where one can't say.
- A Contract without clusters keeps the tools without `cluster`. They use the integration's URL.
- The health check queries each cluster that has them. So a Contract whose clusters all have none has no Prometheus check.

### GPUs

This is slice 17.1, NVIDIA only.

- **Which containers.** A container that requests a GPU is read from kube-state-metrics. The window's largest request counts, of:
  - `nvidia.com/gpu`;
  - `nvidia.com/gpu.shared`, which the device plugin uses for time-sliced GPUs;
  - or a MIG profile, one instance each.
- **Not GPUs.** A sharing scheduler's memory or core quantities are not GPUs. HAMi's `nvidia.com/gpumem` is one example.
- **Where they come from.** NVIDIA's DCGM exporter. It runs with its Kubernetes mapping on.
  - The exporter chart's `kubernetes: auto` turns it on.
  - The binary's own flag is off by default.
- **What is read per GPU.** Each GPU its pods were given, with:
  - its busiest hour;
  - the hours read;
  - its peak memory;
  - the pod last seen on it.
- **Which GPU.** By DCGM's `UUID`. Without one, by its node and index. A MIG instance is also keyed by its `GPU_I_ID`.
- **Busiest hour.** The highest of the window's hourly means of the busy share.
  - Where DCGM has it, the busy share is the graphics engine's: `DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO`, or its older name `DCGM_FI_PROF_GR_ENGINE_ACTIVE`.
  - Otherwise it is the GPU's utilization: `DCGM_FI_DEV_GPU_UTIL_RATIO`, or the deprecated `DCGM_FI_DEV_GPU_UTIL` in percent.
- **Memory.** The framebuffer used: `DCGM_FI_DEV_FB_USED_BYTES`, or the deprecated `DCGM_FI_DEV_FB_USED` in MiB. It is shown, never judged.
- **Marked shared.** These are marked shared:
  - a MIG instance;
  - a virtual GPU (DCGM's `vgpu` label);
  - a GPU requested as `nvidia.com/gpu.shared`.
- **Label names.** The exporter's chart scrapes without `honor_labels`. So a pod's own namespace, name and container are `exported_*`.
  - Its old names are read too: `pod_namespace`, `pod_name` and `container_name`.
  - A scrape with `honor_labels` is read too.
  - Plain `namespace` is read only when neither of the others is on a series. Otherwise it is the exporter's own.
  - So a Contract that owns the exporter's namespace gets no other pod's GPUs.
- **Nothing read where none request.** Nothing is asked of a cluster where no container requests a GPU.
- **Coverage gaps.** A cluster whose GPUs DCGM doesn't cover is noted: "no GPU use from DCGM for N containers that request GPUs".
- **A failed read** costs only the GPUs. It never costs the Sizings.
- **Where shown.** `squero sizing usage` shows them (`gpus`, and `gpu` in `--json`).
- **Through a Relay.** A Relay's cluster is read the same way. The Relay passes the Hub's queries on.

> ponytail: a GPU time-sliced without renaming its resource, and without DCGM's virtual-GPU mode, looks like one pod's.

### Idle GPUs

This is slice 17.2a. Each hourly run stores a verdict for each GPU of a GPU-requesting container. The verdict sits with that container's Sizing.

| Verdict | When |
|---|---|
| `idle` | Its busiest hour, in the window of the container's Sizing policy, stays under 5% busy. |
| `busy` | Otherwise, once judged. |
| `learning` | It has fewer hours read than the policy's history. Under `production` that is a day. |
| `shared` | Not judged. A MIG instance, or a virtual or time-sliced GPU. |
| `unread` | Not judged. No use from DCGM. |

#### Counting for a Workload

- A Workload counts its ready containers' judged GPUs.
- Each GPU counts by the share of the window it was held: hours read over the window's hours, at most 1.
- So a pod that moved across three GPUs in the week counts as one GPU, idle or not.
- The count reads like this: `1 of 4 GPUs idle`.
- GPUs not judged are said beside it: `; 2 not judged (1 shared, 1 unread)`.
- This keeps "none idle" from reading as "all busy" when DCGM doesn't cover them.

#### The money

- With a GPU price, the idle share is priced monthly (730 hours).
- The price is `gpu`, per GPU-hour. It sits in the Contract's or a cluster's `prices`. See [Unit prices](#unit-prices).
- The idle cost goes on its own line beside Waste. It is never in Waste.
- Waste, its cost, the ranking, Savings and the Bill check are all unchanged.

#### Where it shows

- `squero sizing waste` gives the Contract's total and each Workload's.
  - A Workload with idle or unjudged GPUs and no waste is listed too.
  - `--json` adds `gpus` to each Workload and to the report.
- The weekly digest has a line while some are idle or not judged:
  - `Idle GPUs: 1 of 4 GPUs idle, 1,240.00 USD/month.`
- A priced Contract with a cluster that has no GPU price says so. It doesn't show a partial total.
- The console's Workloads page shows:
  - the total under the waste;
  - an Idle GPUs column;
  - in a Workload's detail, each GPU with its verdict, busiest hour, hours, share of the window held and peak memory.
- The Cost page shows the total beside the Savings.
- The Hub MCP server's `sizings` gives `idle_gpus` for the Contract and for each Workload.
- Its `cost` gives `gpus`. This is slice 17.2b.
- Counts show up to two decimals. The share is what their money is worked out from.

#### Price source and limits

- Nothing is actuated.
- A GPU's price comes from a typed `gpu`. On a list-priced cluster it comes from the GPU nodes' list prices. See [Cloud list prices](#cloud-list-prices). That is slice 17.2c.
- The agent's `sizing_for_workload` doesn't show GPUs. That is a slice of its own, with its eval rerun.

### A light KV cache

This is slice 17.3, vLLM only.

**Why it matters.** vLLM reserves `gpu_memory_utilization` of its GPU up front. The default is 0.9. It fills that reservation with the model's weights and its KV cache. So DCGM's memory used reads near that whatever the load.

What a vLLM server actually uses is its KV cache.

#### What the hourly run reads

Where a container requests a GPU, the hourly run reads two vLLM series, in the Contract's namespaces:

- `vllm:kv_cache_usage_perc`: a 0 to 1 share of its cache blocks, despite its name. The highest of the window's hourly peaks is used.
- `vllm:cache_config_info`: what it reserves, and its block count.

A series counts for its own container, where the scrape sets `container`. Otherwise it counts for the pod's one container that requests a GPU.

#### When it is flagged

When that peak stays under 50% of the blocks over the container's Sizing policy's window, the Sizing carries a Risk. The policy's history must be read first. The Risk is `kv_cache_light`:

```text
the KV cache peaked at 30% of its 4,096 blocks in the window, vLLM reserving 0.9 of the GPU: a lower gpu_memory_utilization or a smaller GPU or MIG slice would likely hold it
```

- No new setting is computed. The model's weights sit in the reservation, and no metric gives their size.
- Nothing is applied. It is an argument of the container, which the Actuator never changes.
- It shows wherever Risks show:
  - `sizing show`;
  - the Workloads page;
  - the digest's new risks;
  - the Hub MCP server;
  - the agent's `sizing_for_workload`, in the tool's existing `risks`.
- A failed read is a note. The Sizings stay.
- Other inference servers are not read.

## Sizings

**In short:** a Sizing is the Hub's suggested requests for one container. Under its Sizing policy it says set, raise, lower or keep. Nothing is applied until an Operator approves it.

```bash
squero sizing show --contract <id>
```

The command prints each container's Sizing under its Sizing policy. It shows what its requests should be, and whether that means set, raise, lower or keep. Nothing is applied.

### How a Sizing is worked out

Under `production`, the default policy, the steps run in this order:

1. **CPU.** The 95th percentile of the last 7 days, plus 15%.
   - Recent hours are weighted more. The half-life is 24 hours.
   - A drop is followed in about 4 days. A rise is followed within hours.
2. **Memory.** The peak, plus 20%.
   - So memory follows a drop only once the old peak leaves the window.
3. **Floor.** A floor of 10m and 32 MiB.
4. **Maximum.** The policy's maximum, if it has one.
   - A capped Sizing is listed as a risk.
5. **Limit.** Limits are kept. A request never goes above one.
   - A limit below what the container needs is listed as a risk.
6. **Rounding and change threshold.**
   - Requests are rounded up to a millicore and a MiB.
   - They change only when the difference is at least 10% and at least 10m or 16 MiB.

#### Holds

- Throttling over 10% of CPU periods hides the real demand. So CPU is never lowered while throttled.
- An OOM kill hides the real demand too. So memory is never lowered after an OOM kill.
- Both are listed as risks.

#### Learning

A container with under 24 hours of history is still learning.

### Covered history

A container's history is covered time. It is the time its cluster's Prometheus has usage for. That usage is from any container in the Contract's namespaces there.

- It runs from the container's first sample in the window to its last.
- A gap in Prometheus counts for nothing. So an hour of data after three days without a scrape is an hour of history.
- A CronJob between its runs keeps its history.
- A Workload scaled to zero beside others that run keeps its history.

#### After a long gap

After a long gap, a Sizing can be learning again. That lasts until the window holds what its policy needs.

- Automation applies nothing new to it meanwhile.
- The Actuator keeps what it holds.
- Reflexes go on. A Step-up that reaches its term is kept for another, since its memory peak is not known.
- Its waste leaves the totals. So the digest's waste goes down by it.

#### One limit

Where a Contract's namespaces on a cluster hold only periodic Workloads, only their run times count. So a CronJob of 5 minutes a day stays learning there.

To fix it, give it a policy with a shorter `history`. Or it is ready once anything in those namespaces runs all the time.

## Sizing policies

**In short:** a Sizing policy sets how much history, headroom, floor and maximum a Sizing gets. Three built-ins exist. You can add your own, and rules pick which policy each container uses.

A policy sets:

- the window;
- the CPU percentile and its half-life;
- the headroom;
- floors and maximums;
- the change threshold;
- the history needed before a first Sizing.

The throttling and OOM holds are not settings. No policy turns them off.

### Built-in policies

| | production | high-availability | batch |
|---|---|---|---|
| CPU | weighted p95 + 15% | weighted p99 + 30% | weighted p90 + 10% |
| Memory | peak + 20% | peak + 35% | peak + 15% |
| Window, half-life, history needed | 7 d, 24 h, 24 h | 14 d, 72 h, 72 h | 14 d, 24 h, 24 h |
| Floors | 10m, 32 MiB | 50m, 64 MiB | 10m, 32 MiB |

### Your own policies

Your own policies, and the rules that pick one, go in `contract.yaml`. `squero contracts validate` checks them. It checks ranges, a known `base:`, and known policy names in rules.

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
    # a Workload's spot comes from the first rule without `container` that matches it: the
    # specific rules go above a namespace-wide one, which would match first
    - {namespace: shop, name: cart, policy: production, automate: true, spot: {ratio: 0.7, on_demand: 2}}  # 16.1c
    - {namespace: shop, kind: CronJob, policy: batch, spot: true}    # its runs on spot (16.3a)
    - {namespace: shop, policy: production, automate: true}          # Automation (Phase 12)
```

| Field | Values | What it does |
|---|---|---|
| `default` | a policy name | Optional. `off` sizes only what a rule names. |
| `base` | `production`, `high-availability` or `batch` | The built-in a policy starts from. |
| `window` | 1h to 90d | How far back the policy reads. |
| `half_life` | 1h to 30d | The CPU weighting's half-life. |
| `history` | up to the window | The history needed before a first Sizing. |
| `cpu` | percentile 0.5 to 1; headroom 0 to 3; floor; max | The CPU percentile, headroom, floor and maximum. |
| `memory` | Kubernetes quantities | The memory floor and maximum. |
| `threshold` | 0 to 0.5 | The change threshold. |
| `replicas.percentile` | 0 to 0.5 | The percentile for the Floor of a Workload with an HPA. |

### Rules

Rules run top to bottom. The first match wins.

Each rule matches on these keys:

| Key | Matches |
|---|---|
| `namespace` | A namespace. Globs work. |
| `name` | A Workload's name. Globs work. |
| `container` | A container's name. |
| `cluster` | A cluster. |
| `kind` | A Workload kind, such as `CronJob`. |

Each rule then sets its `policy`, and may add the keys below.

- A Workload's spot comes from the first rule without `container` that matches it.
- So the specific rules go above a namespace-wide one. Otherwise the namespace-wide one matches first.

### What a rule can add

- **Batch default.** After the rules, a Job or CronJob gets `batch`. Everything else gets the default.
- **Automation.** `automate: true` on a rule is Automation. The Actuator applies its containers' Sizings without an approval. See [Automation](client-cluster.md#automation).
  - It needs a policy other than `off`.
  - It needs a cluster with an `actuator_token_secret`. That is the one the rule names, if it names one.
- **Startup boost.** `startup_boost: 2` sits beside it. The value runs from 1.5 to 4. It is refused without `automate`.
  - It starts that rule's containers with twice their CPU limit.
  - It does that until their pod is Ready. See [the Startup boost](client-cluster.md#the-startup-boost).
- **Built-in names.** Built-in names mean the same in every Contract. A standing approval names a policy (ADR 0004).
  - So a custom policy can't reuse a built-in's name.
  - And `off` can't be reused.
- **`off`.** Lists the container as excluded.
- **Spot.** `spot` on a rule is covered in [Spot ratios and candidates](#spot-ratios-and-candidates) and in [CronJobs on spot](#cronjobs-on-spot).

### Reading the output

- The output names each container's policy.
- It shows `history 3.0d of 14d` when Prometheus holds less than the policy's window.
- Each window a Contract's policies use is read in full. So a policy's OOM kills and throttling are from its own window.

## Waste and risk, stored

**In short:** every hour the Hub stores each Contract's sizing run in Postgres. Waste is what the Sizings would free, and each run carries risks. Old runs are thinned out over time.

### What a run stores

- Every hour the Hub sizes each Contract that has a Prometheus. It stores the run in Postgres.
- A run has a row per container. The row holds its policy, state, Sizing, average pods and risks.
- **Waste** is the request above the Sizing times the average pods. It is counted in cores and bytes.
  - It is what the Sizings would free, with headroom kept.
  - A container that is learning, raised or held has no waste.
- **Average pods** counts Running pods.
  - It reads kube-state-metrics' `kube_pod_status_phase`.
  - Without that metric, it reads the CPU samples instead.
  - Those samples come from `rate`, which keeps them about 5 minutes past a pod's end. So short Jobs read high.
  - It averages over the steps between a container's first and last sample in its cluster.
  - A CronJob averages over the time it doesn't run.
  - A gap in Prometheus counts for no one.
- A cluster that fails is stored with its error. Readers keep its last good run. So one dead cluster never blanks another.

### Risk kinds

Each risk has a kind:

- `oom_killed`
- `throttled`
- `under_requested`
- `limit_below_need`
- `capped_by_policy`
- `request_unset`
- `needs_larger_node`, see [Nodes](#nodes)
- `kv_cache_light`, see [A light KV cache](#a-light-kv-cache)

### Retention and commands

- Runs are kept hourly for 14 days.
- After that, the last run of each day is kept for 400 days.

```bash
squero sizing refresh --contract <id>            # stores a run now
squero sizing waste --contract <id>              # ranks the Workloads by waste; --json too
```

- `squero sizing waste` ranks the Workloads by waste.
  - A core weighs as much as 4 GiB, when there are no prices.
  - The output shows each Workload's risk kinds.
  - It shows what is learning or excluded.
  - It shows any stale cluster.
- Both commands need `SQUERO_DATABASE_URL`.

## Unit prices

**In short:** with a `prices:` block, Sizings show money as well as cores and GiB. Prices are per vCPU-hour, per GiB-hour and per GPU-hour. One currency applies per Contract, and it is never converted.

```yaml
prices: {currency: EUR, cpu: 0.0316, memory: 0.0042}   # per vCPU-hour and per GiB-hour
clusters:
  - name: eks-prod
    prices: {cpu: 0.040, memory: 0.0053}               # this cluster's own; the currency stays the Contract's
```

| Field | Unit | What it does |
|---|---|---|
| `currency` | one per Contract | The money's currency, as in `EUR`. Never converted. |
| `cpu` | per vCPU-hour | The price of CPU. |
| `memory` | per GiB-hour | The price of memory. |
| `gpu` | per GPU-hour | The price of a GPU. See [GPU price](#gpu-price). |

### What the money covers

- Each Workload gets the monthly cost (730 hours) of two things: its waste, and its requests.
- The Contract gets both totals.
- The console's Workloads page, the weekly digest and `squero sizing waste` rank by the waste's cost. They show it as `41.20 EUR/month`.
- Requests count every container that has one. That includes learning and excluded containers.
- Waste counts only sized containers.
- It is the cost of what pods reserve, not of the nodes. Idle node capacity isn't in it.

### How prices are applied

- Money is worked out when shown, from the stored quantities.
- So a corrected price applies to every past run.
- The digest's change since last week is a change of usage, at today's prices.
- Rows stored before 10.1 hold requests only for ready containers.
- A price can be 0. That means the hardware is already paid for.
- A cluster's prices need the Contract's. So every cluster of a priced Contract is priced, and they rank together.

### With no prices

- Without `prices:`, nothing changes.
- Costs stay in cores and GiB. A core ranks like 4 GiB.
- The agent's `sizing_for_workload` never shows money.

### GPU price

- `gpu:` sits beside `cpu` and `memory`. It is a price per GPU-hour, from slice 17.2a.
- It prices [Idle GPUs](#idle-gpus).
- A cluster's own prices without a `gpu` take the Contract's.
- A cluster priced from list prices gets its GPU price from them. See [below](#cloud-list-prices).
- Typing its `prices` in full (`cpu`, `memory` and `gpu`) wins over that, as typed prices do.
- A typed `gpu` is not blended by a spot discount.
- The [Bill check](#bill-check)'s estimate adds the cluster's GPU-hours at its `gpu`.

## Cloud list prices

**In short:** instead of typing prices, a Contract can take them from the cloud's public list prices for its nodes. Each cluster's node prices are split so CPU and GiB add back up to the node price.

Instead of typing `prices:`, a Contract can take them from what its clusters' nodes cost on demand:

```yaml
list_prices: {weight: 7, discount: 0.6, gcp_key_secret: gcp-pricing, currency: USD}   # all optional
clusters:
  - name: eks-prod          # provider, region and instance types come from kube-state-metrics
  - name: on-prem
    prices: {cpu: 0.02, memory: 0.003}                   # typed prices always win
    provider: aws           # only for a cluster that can't say (kube_node_info's provider_id)
```

### Settings

| Field | Default | What it does |
|---|---|---|
| `list_prices.weight` | 7 | How many GiB one vCPU is worth when a node's price is split. |
| `list_prices.discount` | 1.0 (no discount) | 0 to 1. Multiplies the CPU and GiB prices for a committed-use deal. |
| `list_prices.gcp_key_secret` | not set | Names a Billing API key. Without it, GCP's catalog is not read. |
| `list_prices.currency` | `USD` | Three capitals, such as `BRL`. Only Azure and GCP offer one. |
| cluster `provider` | not set | Only for a cluster that can't say. Read from `kube_node_info`'s `provider_id`. |

All the `list_prices` fields are optional.

### How the Hub reads list prices

- **Node labels.** Each hourly run reads the cluster's nodes.
  - It reads `kube_node_info`, `kube_node_labels` and `kube_node_status_capacity`.
  - kube-state-metrics must allowlist the node labels:

```bash
--metric-labels-allowlist=nodes=[node.kubernetes.io/instance-type,topology.kubernetes.io/region]
```

- **Price lookups.** Once a day the Hub looks up each (provider, region, instance type) in the cloud's public price list:
  - AWS: the offer file, streamed. Linux, shared tenancy.
  - Azure: the Retail Prices API. Pay-as-you-go Linux, no Spot.
  - GCP: the catalog. Cores and RAM of the machine family, on demand, not custom. It is read only with `gcp_key_secret` naming a Billing API key.
- **Failed lookups.** A failed lookup is tried again after an hour. It keeps the last good price and its age.
- **Split.** A cluster's prices are its nodes' total price over their total vCPU and GiB.
  - One vCPU is worth `weight` GiB. The default is 7.
  - So the CPU and GiB prices add back up to the nodes' price exactly.
- **Discount.** `discount` (0 to 1) multiplies the prices for a committed-use deal.
- **On demand only.** A cluster's spot nodes are priced from these at its own discount. See [Spot nodes](#spot-nodes).

### Currency

- The currency is USD unless `list_prices.currency` says otherwise.
- Only Azure's and GCP's catalogs offer another currency.
- So every cluster must then set `provider: azure` or `provider: gcp`.
- Prices are never converted.

### When a cluster isn't priced

- A cluster without labels (kind, bare metal) is not priced.
- A cluster without a price yet is not priced.
- Either way, it says why.
- It leaves the whole Contract in cores and GiB. Money for one cluster and cores for the other can't be ranked.
- `list_prices` and a Contract-level `prices` can't both be set.

### Checking where prices come from

```bash
squero sizing prices --contract <id> [--refresh]
```

- It shows where each cluster's price comes from.
- The Cost and Workloads pages print the same line.
- So do `sizing cost` and `sizing waste`.

### GPUs in list prices

This is slice 17.2c.

- A node's GPUs come from its `nvidia.com/gpu` capacity.
- **Mixed clusters.** In a cluster with GPU nodes and GPU-less nodes:
  - The CPU and GiB prices, and the spot shares, come from the GPU-less nodes.
  - What the GPU nodes cost above their vCPU and GiB, at those prices, is their GPUs' cost. Spot ones are priced at the spot rate.
  - That cost is shown per GPU-hour, as the `gpu` that prices [Idle GPUs](#idle-gpus).
  - The price note names it, for example `1 GPUs at 0.334 a GPU-hour`.
  - So the nodes' prices still add back exactly, with their GPUs, in the Bill check.
- **One price for the cluster.** It is one price for the cluster's GPUs.
  - With several GPU models, each is priced at their average.
  - So an idle cheap GPU reads dearer than it is.
  - Type the cluster's prices to set it.
- **GPUs on every node.** Nothing sets a CPU and GiB price apart from the GPUs then.
  - Those prices are the whole nodes' prices, as before.
  - The GPUs are unpriced.
  - The note says to type the cluster's prices in full: `cpu`, `memory`, `gpu`.
  - The same happens for GPU nodes that cost no more than their vCPU and GiB at the others' prices.
- **GCP.** A machine's list price is its cores and RAM, its GPUs apart.
  - GCP's GPUs stay unpriced. The note says so.
- **Effect.** A mixed cluster's CPU and GiB prices drop once its GPUs are priced apart.
  - With them, its waste, requests and Savings in money drop too.
  - The GPUs' share was in them before.

> ponytail: one GPU price for the cluster's GPU nodes, spot or not, the latest mix's; MIG profiles' capacity isn't counted, and time-sliced GPUs advertised as replicas count each replica, so their price is a replica's.

### Spot nodes

**In short:** a cluster that runs spot nodes names their label and their discount. The Hub then prices spot capacity at the spot rate, and says where that rate came from.

A cluster that runs spot nodes names their label and what they cost, as a share of on-demand (slice 16.1a):

```yaml
clusters:
  - name: prod
    spot:
      label: karpenter.sh/capacity-type=spot   # key=value; a node without it is on-demand
      discount: 0.35                           # a spot node pays 35% of its on-demand price
      taint: kubernetes.azure.com/scalesetpriority=spot:NoSchedule   # optional: AKS taints its spot nodes
```

| Field | What it does |
|---|---|
| `label` | `key=value`. The label the cluster's nodes carry. A node without it is on-demand. |
| `discount` | A multiplier. A spot node pays this share of its on-demand price. |
| `taint` | Optional. The taint the spot nodes carry. AKS taints its spot nodes. |

#### The label

The label is the one the cluster's nodes carry. Examples:

- `karpenter.sh/capacity-type=spot` (Karpenter)
- `eks.amazonaws.com/capacityType=SPOT` (EKS managed node groups)
- `cloud.google.com/gke-spot=true` (GKE)
- `kubernetes.azure.com/scalesetpriority=spot` (AKS)

`discount` is a multiplier, as `list_prices.discount` is. It must be more than 0 and less than 1.

#### What a spot node costs

- **Spot list price first.** Each spot node costs its spot list price where the cloud gives one. This is from 0.59.0, slice 16.4a.
- **Discount otherwise.** Where there is no spot list price, a node costs its on-demand list price at the discount.
- **Node by node.** This is worked out before the cluster's CPU and GiB prices are made. So they still add back up to what the nodes cost.
- **The price note** says which applies. For example:
  - `spot list on 2 of 3 nodes, x0.35 on 1 (HTTP 403 from ec2.us-east-1.amazonaws.com)`
  - `spot x0.35 on 2 of 8 nodes` where none has one.
- **A deal is for on-demand only.** `list_prices.discount` applies to an on-demand node only.
  - Committed-use deals don't cover spot capacity.
  - So a spot node pays its spot list price, or its discount of the list price. It pays no more.

#### Where spot prices come from

Spot prices are read daily, with the on-demand ones, and kept. Each keeps its last good price and its age, in a table of its own.

- **Azure.** The Retail Prices API's meters named "... Spot". These are Linux, not Low Priority. They come from the read on-demand prices already make.
- **GCP.** The catalog's `Preemptible` SKUs, such as "Spot Preemptible N2 Instance Core running in ...". A legacy "Preemptible" SKU is used only where no Spot one exists. They come from the same read.
- **AWS.** EC2's `DescribeSpotPriceHistory`, Linux/UNIX.
  - It is read with the Contract's own `aws` integration's key.
  - It is kept for that Contract alone. It is never priced into another's.
  - The IAM user needs `ec2:DescribeSpotPriceHistory`.
  - Each instance type of the cluster's spot nodes is read, in a region among the integration's `regions`.
  - Each type is averaged in each zone over the last day. Then the zones are averaged.
  - A refused call (no permission) falls back to the discount. The price note says why.

#### Clusters with typed prices

- A cluster with typed prices keeps its discount.
- A negotiated on-demand rate and a public spot price are two kinds of number.

#### Typed prices

- Typed prices (the cluster's or the Contract's) are read as the on-demand rate.
- Each hourly run blends them by the cluster's spot and on-demand capacity.
- The blend is by size, as a price is split. One vCPU is worth `list_prices.weight` GiB. Without list prices, the weight is 7.
- An example note: `typed prices, spot x0.35 on 1 of 2 nodes: x0.675 of them`.
- A cluster without `spot` keeps its typed price as it is.

#### One rate per cluster

- Every pod costs the cluster's blended price, wherever it runs.
- Cost by namespace and team, Waste and Savings keep one rate per cluster.
- That rate moves as the cluster's mix of spot and on-demand nodes moves.
- A node that could go is priced at its own rate:
  - an on-demand node above the blended price;
  - a spot node at the discount.

#### Seeing spot nodes

- Allowlist the label in kube-state-metrics:

```bash
--metric-labels-allowlist=nodes=[karpenter.sh/capacity-type,...]
```

- It is named the way kube-state-metrics names it: `label_karpenter_sh_capacity_type`, with a capital letter in snake case.
- Where no node shows the label, every node is priced on demand. The price note and the Nodes page say so.
- A label set or changed in the Contract counts from the next hourly run.

#### No Actuator needed

- Pricing only reads Prometheus. It needs no Actuator.
- The Nodes page marks each spot node.
- The Hub MCP's `nodes` says `spot` per node and per cluster.

#### The Bill check and spot

- The Bill check prices each hour's spot capacity at a spot node's rate.
- It prices the rest at an on-demand node's rate.
- These are the rates the cluster's pricing applies.
- So its estimate follows the mix the bill charged, not the latest one.
- The bill counts its spot lines. See [Bill check](#bill-check).

### Spot ratios and candidates

**In short:** a Spot ratio sends a share of a Workload's replicas to spot nodes. An On-demand minimum keeps some replicas off spot. Candidates are the Workloads that would save money on spot.

`spot: {ratio, on_demand}` on a Sizing rule is a [Spot ratio](../CONTEXT.md). This is slice 16.1c.

- `ratio` is the share of its Deployments' and Rollouts' replicas meant for spot nodes.
- `on_demand` is its On-demand minimum. These replicas are kept off spot nodes, whatever the ratio.

```yaml
spot: {ratio: 0.7, on_demand: 2}
```

| Field | Range | What it does |
|---|---|---|
| `ratio` | more than 0, at most 1 | The share of replicas meant for spot. Required. |
| `on_demand` | 1 or more | The On-demand minimum. Required. |

#### How the replicas split

- On-demand replicas are the larger of the minimum and the share that isn't spot, rounded up.
- `{ratio: 0.7, on_demand: 2}` gives 10 replicas: 3 on-demand and 7 on spot.
- The same rule gives 4 replicas: 2 on-demand and 2 on spot.

#### Which Workloads get a ratio

- A Workload's ratio is the first rule without `container` that matches it.
- That holds whatever its policy, `off` too, and with or without `automate`.

#### When a rule is refused

- A rule is refused when it names a `container`.
- It is refused for a kind that never runs on spot. Those kinds are StatefulSet, DaemonSet and Job.
- A CronJob has a rule of its own. See [CronJobs on spot](#cronjobs-on-spot).
- It is refused when it reaches no cluster that names its spot label and has an `actuator_token_secret`.
- On a cluster without both, its Workloads are under no ratio.

#### What the Hub tells each Actuator

The Hub tells each cluster's Actuator:

- its Workloads under a ratio. These are live ones of a kind that can run on spot, in the cluster's namespaces.
  - A Workload whose pods block a node, by its latest report, gets no share on spot.
  - Every replica of it is kept off spot nodes.
  - The list waits for a usable report and read. The Actuator keeps the last one until then.
- the spot label, and the `taint` (the toleration its spot replicas get). Only AKS taints spot nodes by default.
- the nodes carrying the label, as last read.

The webhook places them from slice 16.2b. Until the Actuator says so in its report, the Workloads show "its Actuator doesn't place them yet".

#### What the Nodes page shows

The Nodes page and the Hub MCP's `nodes` show, for each live Workload under a ratio:

- its replicas meant on-demand, and its replicas meant on spot;
- against where they run. Where they run is not known in a read made without the spot label.
- why none is meant for spot, for a kind that can't run on spot;
- or fewer than 2 replicas;
- or a Workload whose pods block a node in its Actuator's latest report.

#### Risks once placement works

Once its Actuator's latest usable report says it places them, a Workload can show a risk. The risk applies when it has fewer replicas on on-demand nodes than meant.

- "1 on-demand, below its minimum of 2: restart it to repair".
- "2 of its 3 on-demand, where all are meant to be". This is where every replica is meant on-demand. That happens when it blocks a node, or when it has no more replicas than its minimum.
- A spot replica that fell back to on-demand is never a risk.
- Nothing evicts to repair it.

#### Candidates

Candidates are shown on every cluster with Prometheus. They are:

- each Deployment or Rollout with 2 or more replicas on average in the latest Sizings;
- with no pod claiming a persistent volume (`kube_pod_spec_volumes_persistentvolumeclaims_info`);
- none blocking a node in its Actuator's latest usable report;
- and under no ratio.

Each candidate has a what-if: 70% of its replicas on spot, and 2 on-demand at least.

- The 70% is of the average, rounded half up.
- A Workload the what-if moves nothing of is left out. That is one with 2 replicas, or an average under 2.5.

#### What a candidate saves

Where the cluster names its spot nodes and has a price, the saving is shown per month:

- its spot replicas, times one pod's current requests, at the cluster's on-demand rate;
- times one less a spot node's rate against it.
- A spot node's rate is the discount, or a deal over a list price.
- The saving is never below 0.

This is an upper bound. It is a figure of its own. It is never summed with Savings. Nothing is saved unless a spot pool exists and the autoscaler moves nodes to it.

- Candidates are ranked by saving.
- The first 20 are shown. The total counts every one.
- Without `spot`, the replicas and requests show. The text says "set the cluster's spot to price it".
- CronJobs are covered below.

#### CronJobs on spot

This is slice 16.3a. The Hub's side is here. Its Actuator steers them from 16.3b.

A CronJob's runs go on spot whole. A rule of their own does that: `{kind: CronJob, ..., spot: true}`.

- That rule is refused without `kind: CronJob`.
- It is refused with a ratio or a minimum. A batch run that starts again from zero is a judgement of its own.
- Each run's first tries prefer spot nodes.
- Once the run has a failed pod, its retries are required off spot nodes.
- So a run is interrupted once at most.

Only a CronJob whose Jobs meet both of these is steered on spot:

- its Jobs allow a retry: `backoffLimit` of 1 or more. The default is 6.
- its Jobs count an interrupted pod as failed. There is no `podFailurePolicy` that ignores `DisruptionTarget`.

kube-state-metrics has no `backoffLimit`. So the Actuator reads them. It reports the ones it skips, with why. The Nodes page shows those.

#### How the Hub finds CronJobs

- The Hub lists a cluster's CronJobs from `kube_cronjob_info`, running or not.
  - A CronJob between its runs has no pod to be seen by.
- It sends its Actuator those under `spot: true` in its namespaces.
  - This holds whether a run blocks a node or not.
  - A run blocks only while it runs.
  - A spot node it holds is the cloud's to take anyway.
  - They go in a list of their own. An Actuator of 0.57.0 ignores that list.
- The Nodes page says "its runs on spot, a retry off it", with its live pods beside it.
  - Or it says why not: "its Actuator doesn't steer its runs yet", until its report says it does.
  - Or it gives the reason its Actuator skips a run.
- There is no intended split and no risk. A run that falls back to on-demand is the design.
- A CronJob under a ratio, by a rule without `kind`, is told it needs a rule of its own.

#### CronJob candidates

- Candidates include every CronJob of the latest Sizings that is under no rule.
- Its what-if is a run entirely on spot.
- Its average pods are used with no split. The figure is kept to three decimal places for a batch run. An hour a night is 0.04.
- The saving is one less a spot node's rate. The note says "if its Jobs allow a retry", unless its Actuator reported they don't.
- They are ranked with the Deployments, by saving.

## Bill check

**In short:** the Bill check compares the Hub's estimate of a cluster's cost with the cloud's tagged bill. Within 5% it reconciles. Otherwise it is flagged. It never changes a shown cost.

The estimate is the cluster's node capacity-hours, at its unit prices. The bill is the cloud's compute bill for the cluster's tag. The gap between them is the check.

### Naming the bill lines

A cluster may name its bill lines with `billing`:

```yaml
billing: {tag: "squero-cluster=kind"}
```

- The Contract needs a Prometheus.
- It needs the `aws`, `azure` or `gcp` integration.
- With more than one of those, the cluster sets `provider:`.

#### AWS

- The tag is a cost-allocation tag. It must be activated in AWS Billing.
- It is read from Cost Explorer, with `GetCostAndUsage`.
- It covers EC2 compute, usage before credits, unblended, a day at a time.
- The `aws` integration's IAM user needs `ce:GetCostAndUsage`.

#### Azure

- The tag can be any resource tag.
- It is read from Cost Management: `type: ActualCost`, `PreTaxCost`, the Virtual Machines service, usage charges, a day at a time.
- It uses the `azure` integration's service principal.
  - Tenant, client id and subscription come from its `env`.
  - The client secret comes from `env_secrets`.
  - It needs Reader or Cost Management Reader.
- An AKS node pool carries the tag to its scale set.
- A day without usage reads as 0.

#### GCP

- The tag is a label. Its key starts with a letter. It uses lowercase letters, digits, `_` and `-`.
- It is read from the billing account's BigQuery export.
- So the cluster also names its table:

```yaml
billing: {tag: "squero-cluster=kind", table: "my-project.billing.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX"}
```

- Turn the export on in the Cloud Console: Billing, Billing export, standard usage cost.
- It holds data only from then on.
- The `gcp` integration's service account needs:
  - BigQuery Data Viewer on the dataset;
  - BigQuery Job User on its own project, the `GOOGLE_CLOUD_PROJECT`, where the query runs.
- For this read alone, the Hub mints a token with only the `bigquery.readonly` scope.
- It sums `cost` (before credits) of Compute Engine's core and RAM SKUs per UTC day.
- Spot ones ("Spot Preemptible ...") are included.
- Not counted: shared-core machines (e2-micro, e2-small, e2-medium), committed-use lines, disks and network.

#### Reading schedule

- The Hub reads the bill once a day, on its hourly run.
- A failure, a 429 included, is tried again after an hour. That is about one call per billed cluster a day.
- It keeps the last 30 full days.

### Reading the bill

```bash
squero sizing bill --contract <id> [--days N] [--refresh] [--json]
```

- The command, and a line on the Cost page, set the bill beside the estimate.
- They cover the last N full UTC days. The default is 7. The maximum is 30.
- The days end 2 days ago, while the cloud settles them.

### The estimate

- It is the cluster's node capacity-hours, from kube-state-metrics: an hourly sample each.
- It is priced at the cluster's unit prices, typed or from `list_prices`.
- Where the cluster has a GPU price, its nodes' GPU-hours are added. These come from `nvidia.com/gpu` capacity, at that price. This is slice 17.2c.
- Without a GPU price, GPU-hours aren't read.
- On GCP, the bill is read as its core and RAM lines. So GPU-hours are left out there.

### Spot lines

Spot lines count on every cluster, whether or not it sets [`spot`](#spot-nodes).

- GCP's spot lines are counted by their SKUs.
- Azure's and AWS's spot lines are counted as usage of the same service.
- That is believed from the clouds' docs. The docs don't settle it for AWS. It is unconfirmed until a live check on an AWS cluster.

Where the cluster sets `spot`, the estimate prices each hour's capacity like this:

- nodes that carried its label are priced at a spot node's rate;
- the rest are priced at an on-demand node's rate.

The row then says what share of the vCPU-hours ran on spot. For example, `spot: 33% of the vCPU-hours, at x0.35`.

- The rate is a spot node's rate against an on-demand one's.
- With a deal, it is the discount over the deal. For example, x0.583 for 0.35 and a deal of 0.6.
- With no spot hours in the window, the row says to allowlist the label, if spot nodes ran.
- A cluster on spot without `spot` shows its estimate above its bill.
- An Azure day billed in two currencies fails the read, and the failure says so. An EA account's spot rows are priced in USD whatever its own currency is.

### The bill's spot part

This is slice 16.4b, from 0.59.0.

- It shows what a cluster's spot lines cost. This is `spot_billed`.
- Beside it, it shows what the estimate prices its spot hours at. This is `spot_estimated`.
- That spot price is typed or from the list.
- The two are checked against what the cloud charged.

#### How the spot part is read

- AWS's and Azure's come from a read of their own. It runs after the total's.
  - Cost Explorer groups by `USAGE_TYPE`, in its `...SpotUsage:<type>` lines.
  - Cost Management groups by `PricingModel`, in its `Spot` rows.
- A reply not shaped as expected costs the spot part only.
  - The total and its verdict are saved as before.
  - The last known spot part is kept.
  - The row says "its spot lines failed to read: ...".
- That is one more call a day per billed cluster. Cost Explorer charges 0.01 USD a request.
- GCP's export query sums its `Spot Preemptible` and legacy `Preemptible` SKUs with the total.
  - It reads only fields it already reads.
  - It makes one scan.
- The spot part has no verdict of its own. The total's gap is the check.

#### Days read before 16.4b

- A day read before 16.4b has no spot part. Its row waits for the next daily read.
- A bill with spot lines on a cluster that doesn't name its spot nodes says so.

### The verdict

- The gap is the estimate's distance from the bill, as a fraction of the bill.
- Within 5% is reconciled.
- Otherwise it is flagged. The CLI exits 1.

#### Missing node-hours

- The estimate counts the node-hours Prometheus sampled.
- When some hours of the window are missing, the row says "the estimate rests on N of M sampled hours". For example, a laptop that slept.
- Below 90% of the hours, it shows the gap but no verdict.
- In that case the CLI doesn't exit 1 for that alone.

#### GCP days still settling

On GCP, the export says how many RAM GiB-hours a day's lines charged for.

- A day is billed for fewer than 95% of the GiB-hours its nodes were sampled for.
- It is read less than 3 days after that day ended.
- Such a day is **still settling**. The row shows the gap and no verdict.
- The note names each such day.
- Audit A6a: a newly enabled export was seen still filling a day 28 hours after it ended. It was complete 30 to 38 hours after.
- That check's estimate of the nodes' memory was 3% under what GCP billed. So a complete day reads at or above its samples.
- Every day short is a real gap, such as a label missing from nodes.
- A short day read later is also a real gap. Both are judged as usual.
- The next daily read of the bill settles it.

#### When there is no verdict

These give no verdict. They say why and show no gap:

- a bill in another currency than the prices;
- a day not yet settled;
- a failed read;
- a cluster without a price.

For example, an Azure subscription billing in BRL needs `list_prices: {currency: BRL}`. Nothing is converted.

The Bill check is reconcile-only. No shown cost changes.

## Cost by namespace

**In short:** the Cost page and `sizing cost` group the Contract's latest Sizings by namespace. Each namespace shows what its pods reserve, what that costs, and what could be saved.

```bash
squero sizing cost --contract <id> [--json]
```

- The console's **Cost** page and `squero sizing cost` group the latest stored Sizings by namespace.
- Each cluster's namespaces are its own. For example, `kind/lab`, not `lab`.
- Each namespace shows:
  - its requests: every container, learning and excluded ones too;
  - its waste: sized containers only;
  - what both cost a month with the Contract's prices. Without prices, cores and GiB are shown instead;
  - how many Workloads it has.
- Namespaces are ranked by what their requests cost.
- A namespace links to the Workloads page, filtered to it.
- The namespaces add up to the Contract's totals on the Workloads page.
- It is the cost of what pods reserve in the Contract's namespaces. It is not the cost of the nodes.

## Cost by label and team

**In short:** list pod labels and team rules in `contract.yaml`. The Cost page then gets a Group by picker, to group costs by team or by a label.

List pod labels and team rules in `contract.yaml`. The Cost page then gets a **Group by** picker.

```bash
squero sizing cost --by team
squero sizing cost --by label:<key>
```

```yaml
allocation:
  labels: [team]                    # read from kube-state-metrics' kube_pod_labels
  teams:                            # first match wins, else "(no team)"
  - {team: payments, namespace: "pay-*"}
  - {team: storefront, labels: {team: storefront}}
  - {team: platform, cluster: kind}
```

### Getting the labels into the read

- kube-state-metrics exports no pod labels unless they are allowlisted.
- Allowlist them with `--metric-labels-allowlist=pods=[team]`.
- In kube-prometheus-stack, set `kube-state-metrics.metricLabelsAllowlist: ["pods=[team]"]`.

### How the Hub uses them

- The Hub reads only the listed keys.
- It takes a Workload's labels from its newest pod, by `kube_pod_created`.
- It stores them with each hourly run.
- Team rules are matched when shown, on the cluster, a namespace glob and label values.
- So a changed rule applies to past runs too.
- Runs stored before a label was listed have no labels. Only cluster and namespace rules match them.
- A team or a label value spans clusters. A namespace does not.
- Workloads without the label go to `(no <key>)`.
- When no pod in a cluster has the label, the page and the CLI warn. They say kube-state-metrics probably doesn't allowlist it.

## Savings, and comparing periods

**In short:** Savings is what the requests above the Sizings cost. You can compare any group's requests and Savings with an earlier period. Prices are kept the same, so only usage changes count.

### Available Savings

- The Cost page and `sizing cost` name the waste's cost the **available Savings**.
- That is what the requests above the Sizings cost.
- It is shown per group and for the whole Contract.
- Without prices, it is shown in cores and GiB.
- What under-sized containers need on top stays a risk on the Workloads page.
- That need is never netted against the available Savings.

### Compare with

```bash
squero sizing cost --compare 30d
```

- The **Compare with** option takes 1, 7, 30 or 90 days ago.
- The `--compare` flag accepts any `m`, `h` or `d` value.
- It adds each group's change in requests and Savings since then.
- Each cluster's latest run now is compared with its latest run at or before then.
- Both are at today's prices. So a price change alone changes nothing.
- The page names the run each cluster is compared with.
- After 14 days, only one run a day is kept. So that run can be a little older than asked.

#### What is compared

- Only what both runs know is compared.
- A cluster with no run that old is left out. It is named with its oldest run.
- A container whose requests weren't stored then is left out.
  - Those are containers that were learning or excluded before 0.8.0.
- When the grouping reads labels, one case needs care.
  - Every container of a cluster is left out where no pod had the labels then, but some do now.
  - This happens when labels were stored before the Contract listed them, or before kube-state-metrics exported them.
  - Without this, such containers would seem to move from `(no team)` into their team.
  - Both are listed as left out.
- A Workload new since counts as new cost.
- A group gone since stays listed with its whole drop, and no Workloads.

## Realized Savings

**In short:** Realized Savings are request cuts the Hub has seen happen. Each cut is worth its old request less its new one, times the pods, at today's prices. A later rise takes the cut back.

```bash
squero sizing savings --contract <id> [--json]
```

- The command lists each drop.
- The Cost page shows the totals.
- The weekly digest adds *Realized this week*. That line is left out while nothing was realized.

### How a drop is found

- Each hourly run compares every container with its cluster's previous good run.
- A request lowered to within 10% of a Sizing is stored as a drop.
  - The Hub must have shown that Sizing in the 14 days before.
  - The old request must be one above the new one.
- CPU and memory are kept apart.
- A drop that matched no Sizing isn't stored.
- Fewer pods and raised requests never count as savings.

### What a drop is worth

- A drop is worth (old request − new request) × the container's pods in its latest run.
- It is worth that at today's prices. Without prices, it is in cores and GiB.
- That gives a monthly run rate.
- It also gives an amount accrued since the drop. That is the rate × the hours since.
- A later rise takes the drop back, newest first.
- A drop back at its old request is gone.
- So a partial rise lowers the accrued amount too.

### When a drop ends

- A container gone from a cluster that was read ends its drops.
- So does a container whose request was removed.
- A failed read ends nothing.
- A DaemonSet applied per instance type realizes per type. See [below](#a-daemonsets-sizing-per-instance-type).
- An HPA's minimum lowered toward its [Floor](#floors) counts here too.

### Pods and CronJobs

- Requests and limits are read from the pods seen in the last 5 minutes.
- So a lowered request shows in the next run.
- A CronJob between runs takes its newest run's request.
  - Its newest run is found by `kube_pod_created`.
  - It asks what it asks now.
  - So its lowered request realizes after its next run. This is audit A11.
- Any other container with no live pod keeps the largest of its window.
  - That covers a Workload scaled to 0, or kube-state-metrics down.
  - Such a run neither starts, takes back nor ends a drop.
- The pods are the latest run's average over its window, as for its waste.

### No backfill

- Only a drop between two runs of 0.12.0 or later counts. There is no backfill.
- A CronJob's request lowered before audit A11 is never counted.
- Its first run after the upgrade compares with a row from the window's largest.

## The weekly Sizing digest

**In short:** each Monday the Hub posts a digest of each Contract's stored Sizings to its operator channel. It shows waste, nodes that could go, the week's Actuations, and the top Workloads.

Each Monday at 09:00 UTC the Hub posts a digest of each Contract's stored Sizings. It goes to the operator channel, never the client channel.

### What the digest contains

- Total waste, and its change since 7 days before.
- The nodes that could go. See [Nodes that could go](#nodes-that-could-go).
- The week's Actuations, in one line. For example: `Actuator this week: 5 resized, 1 rolled back, 1 with a Step-up.`
  - Each of these counts the containers it reached in the 7 days up to the digest's time: resized, rolled back, reverted, failed, with a Step-up and with a Burst.
  - A container reported again is counted once.
  - Nothing that changed nothing is counted.
  - There is no line without an Actuation.
- Idle GPUs, while some are idle.
- Unrequested GPUs, while there are some.
- The top 5 Workloads by waste.
- Risk kinds that a Workload didn't have a week before.
  - While there is no week-old run, every risk is listed, under "Risks".
- Stale clusters.
- How many containers are still learning.
- A link to the Workloads page, when `SQUERO_CONSOLE_URL` is set.

### Switching it on, moving it, or off

It is on for every Contract that has a `slack:` and a Prometheus. That also needs the Hub's `SQUERO_SLACK_BOT_TOKEN`.

- `sizing: {digest: {schedule: "30 8 * * 5", timezone: Europe/Rome}}` moves it. This example posts on Friday at 08:30 in Rome time.
- `sizing: {digest: off}` turns it off.

### Posting

- Each due time posts once. That holds across restarts and missed weeks.
- Each due time is claimed in Postgres before posting.
- So a post that Slack refuses is lost for that week. It is never sent twice.

### Commands

```bash
squero sizing digest --contract <id>           # prints it now
squero sizing digest --contract <id> --post    # also posts it; the schedule stays as it is
```

## Sizings in a Triage

**In short:** an agent's Triage can call `sizing_for_workload` to read one Workload's stored Sizing. The tool also suggests a memory fix after an OOM kill.

```yaml
- name: sizing
```

This line gives the agent `sizing_for_workload`.

### What the tool returns

- It returns one Workload's stored Sizing. This is the Hub's latest run, as the console shows it.
- It finds the Workload by its name, or by one of its pods' names.
  - A pod matches the longest Workload name its pod name starts with, followed by `-`.
  - `kind` is used only when two Workloads share a name.
- Per container, it returns:
  - the request and limit now;
  - the Sizing, and the usage it came from: CPU's weighted p95, memory's peak;
  - the risks;
  - average pods and waste;
  - when the run was, and whether its cluster's reads are failing.

### The suggested memory fix

After an OOM kill, a Sizing keeps memory as it is. The peak is the limit, not the demand. So the tool adds `suggested`.

- It suggests a memory request and limit, at the policy's memory headroom above the limit. For example, 64Mi gets 77Mi.
- With no limit, it suggests a request that far above the peak. The node had run out of memory.
- The suggestion is never past the policy's memory `max`. This is audit A11.
- If a `max` is at or below what it was killed at, there are no numbers. Only the reason is given:
  - raise the max, or look for a leak.

### Use and scope

- The guide asks a Triage of a resource alert to call it once and cite it.
- It reads only the Contract's own Sizings.
  - Those are of its clusters with a Prometheus, and their allowed namespaces.
- `contracts validate` refuses the integration without such a cluster.
- The CLI's `squero triage` has no database. So it leaves the tool and its guide out.
- Rows stored before 0.6.0 have no limits. So they get no suggestion until the next hourly run.

## Needs of autoscaled Workloads

**In short:** for each Workload with an HPA, the hourly run stores its Need. A Need is the number of replicas it really needed, whatever the HPA's limits were. Nothing is shown or written to an HPA yet.

For every Workload with an HPA, the hourly run also reads and stores its **Need**. A KEDA ScaledObject's `keda-hpa-<name>` is one such HPA.

- A Need is the replicas it needed, whatever the HPA's minimum and maximum allowed.
- Nothing is shown yet.
- Nothing is ever written to an HPA.
- The Need is what the Floor and the Weekly profile (Phase 14) will be computed from.
- It is stored now, because a profile needs more weeks than most Prometheus keep.

### The figure

- The figure is the HPA's replicas at a time, times the current value of the metric it scales on, over that metric's target.
- For example, 4 replicas at 75% of their requests, against a target of 50%, needed 6.
- With several metrics, the largest figure counts.
- It works for any metric kind: resource, one container's, pods, object, or external.
- Each current value is compared with the target of its own type.
- With no replicas, the Need is 0.
- The replicas counted are the smaller of two: that moment's, and the sample before's.
  - Right after a scale-up, an HPA reports its new replica count.
  - It reports the utilization of the pods that already have metrics, the old ones.
  - That would read as a Need that was never there.
  - For example, 3 replicas at 83% going to 5 needed 5, not 8.

### Where it is read from

- The read comes from kube-state-metrics, in the Contract's namespaces only.
- It works like every other read of Workloads and pods. The exception is the sums behind a node's Room. See [Nodes](#nodes).
- The metrics are:
  - `kube_horizontalpodautoscaler_info`, which gives the Workload an HPA scales;
  - `_status_current_replicas`;
  - `_spec_target_metric`;
  - `_status_target_metric`.
- It needs no new permission and no Kubernetes API call.
- A cluster without an HPA costs one query an hour.

### Unknown Needs

Nothing is stored for that time when the Need is unknown. That happens in these cases:

- an HPA whose target or current value kube-state-metrics doesn't have;
  - this covers a metric the HPA can't read;
  - and a kube-state-metrics that doesn't export `_spec_target_metric`;
- a target of zero;
- a Workload that two HPAs scale.

### Storage

- A Need is stored as each UTC hour's peak per Workload.
- It is kept for 8 weeks.
- The first read goes back over the 8 weeks Prometheus still has. It samples every 9 minutes.
- After that, each run reads on from the newest hour stored. It reads every minute. So a Hub that was down catches up.
- An hour read twice keeps the larger figure.
- A peak between two samples is missed.
- A failed read is logged. It never fails the Sizing run.

### A vLLM Workload's Need

Where a container of the cluster requests a GPU, a vLLM Workload's Need is read from vLLM's own metrics. This applies with or without an HPA, and in place of its HPA's.

- The metrics are read in the Contract's namespaces.
- Each hour's Need is the replicas that reported a KV cache that hour (`vllm:kv_cache_usage_perc`).
- That count is multiplied by the busiest one's peak, over 0.8.
  - A replica's cache 80% full is as busy as it should be.
- The result is rounded up.
- When requests waited (`vllm:num_requests_waiting` above 0), the hour needs at least one replica more.
- A Workload with an HPA gets its Floor, Weekly profile and Backtest from these. See [Floors](#floors). This is slice 17.4a.
- A Workload without an HPA gets a Floor against its replica count. See [Without an HPA](#without-an-hpa). This is slice 17.4b.
- A failed read is logged. It keeps the HPA's Need.

> ponytail: the replicas are the pods that reported in the hour, so an hour a rollout replaces pods counts both.

## Floors

**In short:** a Floor is the minimum number of replicas the Hub computes for a Workload with an HPA. It comes from the Workload's stored Needs. The Hub never writes it to the HPA. It shows what lowering the minimum would save.

A **Floor** is the minimum replicas the Hub computes for a Workload with an HPA. It comes from the Workload's stored Needs.

- Each hourly run reads the HPA's minimum and maximum as they are.
  - `kube_horizontalpodautoscaler_spec_min_replicas`
  - `kube_horizontalpodautoscaler_spec_max_replicas`
- It stores each cluster's Floors, replacing the last.
- These commands and pages show Floors:
  - `squero sizing show`;
  - the console's Workloads page, under Autoscaled Workloads;
  - the Hub MCP's `sizings`;
  - one line of the weekly digest.
- Nothing is written to an HPA. Lowering a minimum is the client's manifest change.

### The Floor

- The Floor is a low percentile of the Workload's hourly Needs over its Sizing policy's window.
- It is rounded up, as an HPA rounds.
- The percentile is:
  - the 10th under `production` and `batch`;
  - the 25th under `high-availability`;
  - or a policy's `replicas: {percentile: 0.10}`, which is 0 to 0.5.
- It is never below 2, unless the HPA's minimum already is.
- With containers under different policies, the highest percentile decides. That policy's window and history are used.
- A Workload whose containers are all `off` gets no Floor.

### History

- Only the Workload's own stored hours count.
- Each counts as an hour. The hour under way never counts.
- Under the policy's `history`, the Floor is learning. Under `production`, the history is 24 hours.
- Needs are kept 8 weeks. So a Floor looks back 8 weeks at most.
- It never asks for more than 4 weeks of history, whatever the policy says.

### Against the HPA's minimum

A Floor below the HPA's minimum is a saving. It is worked out like this:

- For each stored hour, take the replicas the lower minimum would have dropped.
  - That is the minimum, less the larger of the Floor and that hour's Need rounded up.
  - It counts only when positive.
- Average those across the hours.
- Each replica is priced at its containers' Sizings. A container without one uses its request.
- This saving is its own figure. It sits on top of the Waste.
  - Waste is what every pod asks above its Sizing.
  - This figure is the pods no longer held.
- With prices, it has a monthly cost.

A Floor above the minimum is a risk: `minimum_below_floor`. The Workload needed more, in most hours, than its minimum guarantees. When they are equal, nothing is said.

### The maximum

- No maximum is recommended.
- Where the Need went above the maximum, one risk says how often. For example: `needed more than its maximum of 5 in 6% of hours`.

### No Floor

A Floor is not given, and the reason is shown, in these cases:

- two HPAs on the Workload;
- a minimum that kube-state-metrics doesn't have;
- a Workload that scales to zero, which is left alone.

A Workload that scales to zero is found from the HPA's replicas.

- KEDA gives its HPA a minimum of 1. It scales to zero itself.
- So scaling to zero shows as 0 replicas at some time in the Contract's longest policy window. That window is 8 weeks at most.
- Or it shows as an HPA minimum of 0.
- A Need of 0 is not that. An idle Workload held at its minimum has a Need of 0, and that is where a Floor saves most.
- A Workload scaled to zero by hand in that time reads the same. It gets no Floor until it is out of the window.

### Realized Floor savings

- Each hourly run compares every HPA's minimum with the Floor the last run showed.
- A minimum lowered while that Floor was below it is stored as a drop.
  - The drop goes down to the new minimum.
  - It never goes below that Floor.
  - Lower is not the Hub's advice. It shows as the `minimum_below_floor` risk.
- The drop counts in [Realized Savings](#realized-savings), with the request drops. It is listed apart.
- Its worth is the replicas no longer held. This is per stored hour since the drop.
  - It uses the last 7 days at most.
  - The old minimum, less the larger of what it counts down to and that hour's Need rounded up.
  - Averaged across the hours.
- That is multiplied by the pod's requests as they are now. It is every container's requests, at today's prices.
- The result is a monthly rate. It accrues as that rate × the hours since.
- Here a replica counts at what it really asked. In the available saving, it counts at its Sizing.
- A minimum raised again takes the drop back, newest first.
  - At the old minimum, the drop is gone.
- An HPA gone, or a second HPA on the Workload, ends it.
- A failed read, or an unknown minimum, ends nothing.
- It is worth nothing until an hour since the drop is stored.
- There is no backfill. Only a lowering between two runs of 0.49.0 or later counts.

### Not yet

The available saving is not in the Cost page's groups.

### Without an HPA

This is slice 17.4b. A GPU Workload with vLLM Needs, and no HPA, gets a Floor. It must be a Deployment or a StatefulSet. The Floor is against its replica count.

- The replica counts come from `kube_deployment_spec_replicas` and `kube_statefulset_replicas`, in the Contract's namespaces.
- The rules are the same as for an HPA's minimum and maximum:
  - never below 2, unless it already runs fewer;
  - none for one at 0 in the lookback;
  - none for another kind, shown as `its replica count is not known`;
  - none without stored Needs. That covers a GPU Workload no vLLM reports for.
- Its saving is Phase 14's. It is what an autoscaler holding the Floor would save. Each hour counts the replicas less the larger of the Floor and that hour's Need.
- The line says so: `(with an autoscaler holding the Floor, such as KEDA)`.
- Its risks talk about replicas. For example:
  - `its 2 replicas are below the 5 replicas it needed in 40% of hours`;
  - `needed more than its 2 replicas in 10% of hours`.
- Its Weekly profile gives the cron triggers a KEDA ScaledObject for it would need. A note says it needs one.
- A replica count lowered by hand is never realized Savings. Fewer pods never are.
- The console, `sizing show`, the digest (`1 can run fewer replicas`) and the Hub MCP's `sizings` say "no HPA" where an HPA's minimum is said otherwise.

### GPUs in Floors

- A Floor's saving counts the replicas' GPUs too. So does a Backtest's too many.
  - These are `gpu_saving` and `excess_gpus`.
  - This is true whether or not there is an HPA.
  - They are priced at the cluster's GPU price.
- Without a GPU price, there is no cost. It is never their CPU and memory alone, and the total has none either.
- A replica held above its Floor holds its GPUs. So this overlaps with [Idle GPUs](#idle-gpus). This is slice 17.4b.

## Weekly profiles

**In short:** a Weekly profile is a Workload's Floors by hour of the week. It suits load that follows the week. Its Backtest checks the profile against the newest week. A profile that misses by over 20% is not shown, and its blocks are not stored.

A Workload with a Floor also gets a **Weekly profile**. It holds its Floors by hour of the week, for load that follows the week.

- It is shown under the Floor in the console.
- It is shown in `squero sizing show`, and in the Hub MCP's `sizings`.
- It is stored with the Floor by each hourly run. It is stored with its **Backtest**.

### Each hour of the week

- Each hour of the week is the median of that hour's Need across the weeks stored.
- The median is rounded up, and is never below the Floor.
- With three weeks, one odd week changes nothing.
- An hour of the week stored in fewer than three weeks sits at the Floor.
  - That happens with a gap in Prometheus at that hour in the others.
  - One week would decide it, so it isn't used.
  - A block can be split there.

### Three weeks first

- The Workload's own complete stored hours count, over the 8 weeks Needs are kept.
- Under 504 of them, the profile is learning.

### Blocks

- Each run of hours that asks the same, above the Floor, is a block. For example: `Mon 08:45 to Mon 12:00: 7 replicas`.
- The Floor is the baseline. It is not listed.
- A block that asks more than the hour before it starts `sizing.lead_time` early. So the replicas are there when the load is.
- A block after a higher one starts on the hour.
- All blocks end on the hour.
- A week that asks the same in every hour has no pattern. The profile says so.
- Blocks are not smoothed. So a noisy week gives many blocks.
- A block is not capped at the HPA's maximum.
  - Where it asks more, the maximum holds it down.
  - The Floor's risk line already says the maximum was too low.

### Timezone and lead time

Both settings are per Contract.

| Setting | Default | What it does |
|---|---|---|
| `sizing.timezone` | UTC | An IANA name. The hours are shown and scheduled in it. |
| `sizing.lead_time` | 15 minutes | Whole minutes, from 0 to 2h. How early a rising block starts. |

```yaml
sizing:
  timezone: Europe/Rome
  lead_time: 30m
```

### KEDA triggers

For an HPA named `keda-hpa-<name>`, each block comes as a `cron` trigger. The trigger is for the ScaledObject `<name>`. It is pasted beside its other triggers.

KEDA holds at least `desiredReplicas` in each window. It takes the trigger that asks for most.

```yaml
- type: cron
  metadata:
    timezone: Europe/Rome
    start: 45 8 * * 1
    end: 0 12 * * 1
    desiredReplicas: "7"
```

- There is one trigger per block.
- Each trigger has a single weekday. Sunday is 0.
- KEDA's docs don't say that the weekday field takes lists or ranges.

### A plain HPA

- A plain HPA has no schedule of its own.
- It gets the profile, with a note saying so. Something else has to hold those replicas.
- The Hub writes nothing.
- The Hub generates no CronJobs.

### Backtest

A profile is checked against a week it has not seen. This is the Backtest.

- The newest week is replayed hour by hour. That is the 168 complete hours before the run.
- It is replayed against a profile built from the weeks kept before it. There are three to seven of them.
- It needs four stored weeks: 504 stored hours before the newest week, and at least half of that week (84 hours).
- Until then, the profile shows with `not backtested yet (needs four weeks)`.

#### Needed

- In an hour, the Workload needed the larger of two figures: its Floor, and its Need rounded up.
- That is what its HPA would run with the Floor as its minimum.
- So a profile at its Floor in a quiet hour is right.
- The profile is judged only on what it adds above the Floor.

#### Error

- The error is the replica-hours the profile held too few, plus those it held too many.
- It is shown as a share of the replica-hours needed.
- An hour off by less than a tenth of what it needed counts for nothing.
  - This is like the HPA's own tolerance.
  - With whole replicas, that only applies from 11 replicas up.
- An hour never stored is skipped.
- The lead time plays no part. The replay is by the hour.

#### Within the budget

The budget is 20%. That is fixed for now. Exactly 20% is within.

- The profile shows, with both parts and what the too many would cost a month.
- The cost is at the Sizings, like the Floor's saving.
- For example: `backtest: off by 12% last week (too few 4%, too many 8%: 3.10 EUR/month)`.
- That cost is not in the Floors' total. Nothing holds those replicas until the profile is applied.

#### Over the budget

- The Workload shows `load isn't weekly enough: off by 34% last week (too few 21%, too many 13%); it keeps its Floor`.
- Its blocks and its `cron` triggers are not stored and not shown anywhere.
- The Floor and its saving stand.

### Limits of the profile

- In a zone with daylight saving, the hours of the two weeks around a change are an hour off against the rest.
- The Backtest replays one week. So one unusual newest week, such as a holiday, can fail a good profile.
  - The profile is failed until the next week replaces that one.
  - The Backtest says how the weeks before predicted the newest one.
  - It does not say how the profile shown will do next week, since that profile includes the newest week.
- The Floor used on both sides is today's.

## Sizings held by an HPA

**In short:** an HPA that scales on a percentage of requests blocks some request changes. The Hub shows the HPA change to make first, and marks those Sizings as held. The Waste still counts.

An HPA with a `Utilization` target on cpu or memory divides the pods' usage by their requests. So changing such a request changes the percentage. That changes the replica count.

That is why the Actuator leaves such a request alone. See [the pre-checks](client-cluster.md#running-an-actuator).

The Hub marks a Sizing that would change such a request as **held by its HPA**. It shows this in three places:

- `squero sizing show`;
- the console's Workloads page, under the container;
- the Hub MCP's `sizings`.

Its Waste still counts. One manifest change frees it.

### The change

The change turns the HPA's target into an `AverageValue`. That is the same threshold, in absolute terms.

For example, 50% of pods that ask 1200m of CPU in all is 600m per pod.

After the change, the HPA reads no requests. The replica count follows usage, as before. The Actuator then resizes that request like any other.

The Hub shows the change ready to copy:

```yaml
# HorizontalPodAutoscaler shop/api, its cpu metric:
# 50% of the pod's cpu requests now (1200m), as a value.
# Apply it before any request changes: a request cut first scales it out.
target:
  type: AverageValue
  averageValue: 600m
```

### Whose requests

- For a metric of the whole pod, every container's request is summed. Sidecars count, sized or not.
- Every container whose Sizing would change that is held.
- For a per-container metric, only that container's own request counts. Only it is held.
- The value is computed from the requests as they are now.
- So apply it before any of them changes.
- With a request already cut, the HPA has scaled out. The value shown is then smaller.

### KEDA ScaledObjects

- An HPA named `keda-hpa-<name>` is taken for the ScaledObject `<name>`'s.
- The change is given for its trigger.
  - `metricType: AverageValue`.
  - `value` as a quantity.
  - `containerName` for a per-container trigger.
- A wrong guess only shows the wrong form.

### Where the read comes from

- The read comes from kube-state-metrics, as it is at the run.
- The metrics are `kube_horizontalpodautoscaler_info` and `_spec_target_metric`.
- It is read in the Contract's namespaces.
- A failed read marks nothing and fails nothing.
- A target switched to `AverageValue` stops holding at the next hourly run.

### Limits of the held check

- A kube-state-metrics before 2.19 doesn't say which container a per-container metric is of.
  - Such a metric reads as the whole pod's.
  - The comment above the change says which requests the value was taken from.
- Where a container of the pod has no request for that resource, the HPA's `Utilization` target isn't working at all.
  - So the value shown is no equivalent of it.
  - It comes from the requests there are, or from none.
- With two HPAs on one Workload, the first by name is shown.
- Nothing is ever written to an HPA or a ScaledObject. The client's GitOps applies the change.

## Nodes

**In short:** each hourly run reads the cluster's nodes and stores the latest read. The Nodes page shows each node's Room, its Blockers and whether it could be removed. Sizings are capped so they fit on a real node.

Each hourly run also reads the cluster's nodes from kube-state-metrics. It stores the latest read per cluster.

- The console's **Nodes** page shows them.
- The Hub MCP's `nodes` gives them to the Operator's client.
- The weekly digest has a line on the nodes that could go.

### Room

A node's Room is its allocatable CPU and memory, less the requests of every pod on it that is Running or Pending.

- A finished pod holds nothing. Its series stay until it is deleted.
- Init containers are left out.

### What is read

Per node, the Hub reads:

- allocatable: `kube_node_status_allocatable`;
- taints: `kube_node_spec_taint`;
- whether it is cordoned: `kube_node_spec_unschedulable`;
- its instance type, where the label is allowlisted: `kube_node_labels`;
- its pods' requests: `kube_pod_container_resource_requests`. These are joined to `kube_pod_status_phase` and `kube_pod_info` on the pod's `uid`. That label is on every pod series, however it was scraped.

### Pods outside the Contract's namespaces

- Pods outside the Contract's namespaces are part of a node's Room. So they are read.
- They are read only as Prometheus sums them: one total and one count per node.
- They are shown as "not visible".
- No name and no namespace of such a pod reaches the Hub.
- The Contract's own pods are listed by name, with their requests.
- A cluster behind a Relay is read the same way, through the Relay.
- On a cluster two Contracts share, each Contract sees how much the other's pods ask per node. It sees that as one number.

### A shared Prometheus

- Node series carry no namespace.
- Where another Contract reads the same Prometheus, and the cluster has no `prometheus_labels`, nothing of this is read.
- That is the same guard as for list prices. The page says why.
- To lift it, give the cluster's series a label, such as `cluster: <name>`. Then name it in `prometheus_labels`.

### The cap on Sizings

No Sizing asks more than the largest Room after DaemonSets in its cluster.

- The Room is per resource: the most any node has once its DaemonSets' pods are in.
- Only nodes not cordoned count.
- A Sizing that needs more is capped there. It carries the risk `needs_larger_node`.
- The cap never lowers a request. A container already asking more than the cap keeps what it has.
  - That happens when its node is cordoned, or it is missing from the read.
- A DaemonSet's own Sizing is not capped. Its Room already leaves its requests out.
- When a pod's containers together ask more than that Room, each carries the risk. None is cut.
- A cluster whose nodes were not read, or only in part, has no cap.
  - A node without its allocatable counts as only in part.

### Tainted nodes

- Tainted nodes count toward the largest Room.
- The Hub can't see a Workload's tolerations.
- A cap that is too generous changes nothing. One too strict would shrink a correct Sizing.
- CPU and memory may come from different nodes, for the same reason.

### The autoscaler

The autoscaler is named per cluster in the Contract. It is never guessed. It decides whose rules a node's Blockers go by.

```yaml
clusters:
  - name: prod
    autoscaler: karpenter   # or cluster-autoscaler; left out: none
```

### A DaemonSet's Sizing per instance type

**In short:** a DaemonSet runs one pod per node, and one request has to fit them all. Pooling makes the Sizing follow the busiest pods. Where an Actuator runs, each instance type can get its own Sizing.

A DaemonSet runs one pod on every node. One request has to do for all of them.

A log collector idles on a small node and works on a large one. The Hub pools a container's pods into one Sizing. So for a DaemonSet, that Sizing follows the many.

- **Memory** is a peak. So the pooled Sizing fits the busiest pod.
- **CPU** fits the busiest node too. Its target is the largest of each node's own percentile.
  - That node's pods over the window count. So the pod a rollout replaced counts with its successor.
  - The node is read from kube-state-metrics' `kube_pod_info`. A pod whose node isn't there is its own.
- A percentile over every pod's samples would sit below a busy minority. Say one node in thirty. It would ask far too little for that node's pod.
- So the pooled Sizing is too large on the quiet nodes.

#### The example

On a cluster that runs an Actuator, the Hub also sizes a DaemonSet's container **per instance type**.

- Its pods on nodes of one type get a Sizing of their own.
- That Sizing comes from their own usage, against their own requests, under the container's policy.
- It shows under the container:
  - in the console, as "By instance type";
  - in `squero sizing show`;
  - in the Hub MCP's `sizings`, as `by_instance_type`. That shows ten types at most, and counts the rest.

```
kind/shop/DaemonSet/agent/app [production]: cpu lower 2000m -> 1150m, memory raise 256Mi -> 1080Mi
  on m5.4xlarge (1 pods): cpu lower 2000m -> 1150m, memory raise 256Mi -> 1080Mi
  on m5.large (2 pods): cpu lower 500m -> 58m, memory lower 256Mi -> 77Mi
```

Here one node of three is busy, and the pooled Sizing follows it. The two small nodes' pods would be asked 1150m, where 58m does.

#### Applied per type

This is from 0.54.0. It needs an Actuator of that version or later.

Where a DaemonSet's container has Sizings per instance type, the Actuator applies those. It does not apply the pooled one. Each pod gets the Sizing of its node's type.

- **A type with no Sizing yet.** This is a type still learning, or a node whose type the Hub doesn't know. Its pods get their template's requests. That holds even where an earlier pooled Sizing had changed them.
- **One approval covers every type** of the run it approves.
  - `approve_sizing` is called with the container's pooled values, as `sizings` showed them.
  - It answers with the types it approved, as `by_instance_type`.
  - A container whose pooled Sizing keeps its requests can still be approved, when a type's changes them.
  - A container with more than the ten types `sizings` shows is refused. Nothing is approved unseen. Automation still applies it.
- **Automation** sends a container when some type's Sizing changes a request.
  - Its churn rule is 10% and one lowering a day.
  - That rule is applied to each type, against what that type's pods hold.
- **An older Actuator** (before 0.54.0) keeps getting the pooled Sizing for every pod.
  - It gets nothing for a container where only a type's changes count.

#### The money by type

This is from 0.54.0. The Hub remembers, per cluster, whether its Actuator applies types. Each pull of the desired state says so. The value is stored when it changes.

On such a cluster, a container with types goes by them only.

- **Waste** is each ready type's pods, against that type's Sizing. A type still learning has none.
- **The cost of its requests** is each type's pods' requests.
  - Its pods on no known node are counted at the container's, the largest any pod asks.
- **Realized Savings** are each type's pods' request lowered to within 10% of that type's Sizing. They are worth that type's pods.
  - A drop the container had before its types were applied becomes one per type.
  - It comes from the same run and request.
  - It is priced at what each type's pods ask now.
  - A chain of drops still adds up to the first request, less what the type asks.
  - A type's drop ends when the type is gone, or the types are no longer applied.
  - An hour whose read of the nodes' types failed changes none.
- **The pooled Sizing** is shown, not applied.
  - The console opens "By instance type" and says so.
  - The Hub MCP's `sizings` says `applied: by_instance_type`.
  - It raises no risk about a type.
  - `approve_sizing` refuses it when no type changes a request.
  - Automation sends nothing while every type is still learning.
- **Nodes that could go after the Sizings** give each of its pods its node's type's Sizing.

#### On a cluster whose Actuator doesn't apply types

On a cluster whose Actuator is older than 0.54.0, all of this stays on the pooled Sizing.

- `squero sizing show` reads Prometheus without the Hub's store.
- It shows the pooled Waste too.
- The first weekly digest after a cluster's Actuator starts applying types compares a week by type with a pooled one.

#### Which nodes and when

- **Which nodes.** Nodes are grouped by `node.kubernetes.io/instance-type`. This is the label that list prices need, allowlisted in kube-state-metrics.
  - The grouping is over the Sizing's window.
  - So the pods of nodes since replaced still count.
  - Nodes without the label are one type of their own.
  - With fewer than two types there is nothing to split. Nothing shows.
  - A pod whose node isn't found is in no type. It still counts in the pooled Sizing.
- **Only with an Actuator.** A DaemonSet has one pod template.
  - So nothing but an Actuator resizing each pod in place can give two nodes different requests.
  - On a cluster without one, the read isn't made.
  - Nor is it made from a Prometheus another Contract reads without cluster labels. Node series carry no namespace.
- **Each type is sized like a container.**
  - It gets the same policy.
  - It gets a day of its own history. A newly added type is still learning.
  - What hides the container's real demand keeps every type from being lowered. That means throttling or an OOM kill.

#### Applying them

The Actuator applies each type's Sizing from 0.54.0 (slice 15.3b): see [Applied per type](#applied-per-type).

### Blockers: could a node be removed

**In short:** a Blocker is a pod that keeps its node from being removed. The Nodes page gives each node a verdict: blocked, clear or unknown. The verdict is read-only. The Hub evicts nothing for it.

A **Blocker** is a pod that keeps its node from being removed. The Nodes page gives each node a verdict:

| Verdict | Meaning |
|---|---|
| **blocked** | Shows the pods and why. |
| **clear** | "yes: nothing on it blocks". |
| **unknown** | Shows why. |

- It is read-only. The Hub evicts nothing for it.
- It does not yet say whether a node's pods would fit elsewhere. That comes with the nodes that could go.

#### Where the facts come from

- A cluster's [Actuator](client-cluster.md#blockers-what-the-actuator-reports) reports the facts of pods that may block.
  - These are pods in the Contract's namespaces.
  - It reads them every five minutes, and hourly at least.
  - Its own pod's facts are included. This is audit A4, judged by the same rules. So its node isn't unknown for a pod the Hub can't see.
- The facts are:
  - its controller;
  - its emptyDir and hostPath volumes, by name;
  - three annotations, as they are;
  - the PodDisruptionBudget covering it that allows the fewest disruptions.
- The Actuator sends facts and no conclusion.
- The Hub applies the rules. So the rules can change without redeploying an Actuator.

#### Whose rules

The rules are those of the autoscaler the Contract names for the cluster. Each comes from that autoscaler's own documentation.

| Removes nodes | A pod blocks when |
|---|---|
| all three | a PodDisruptionBudget covering it allows no disruption |
| `cluster-autoscaler` | it is annotated `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`; or, unless annotated `"true"`: it has no controller or a custom one, it has local storage (a hostPath, or an emptyDir not in memory) not all named in `safe-to-evict-local-volumes`, or it is a kube-system pod with no PodDisruptionBudget |
| `karpenter` | it is annotated `karpenter.sh/do-not-disrupt: "true"`, or with a duration (`"30m"`) and started less than that long ago; any other value is ignored, as Karpenter ignores it |
| none named | what stops `kubectl drain` without force flags: a pod not managed by a ReplicationController, ReplicaSet, Job or StatefulSet, or a pod using emptyDir |

A DaemonSet's pods and a node's own static pods stay with their node. They block no one.

#### When a node is clear, and when it is unknown

- **Clear needs evidence.** A node is clear only when every pod on it is known, and none blocks.
- **Unknown** when any of these is true:
  - the report was cut at 1000 pods, and names no Blocker there;
  - the Actuator could not read a namespace that has pods on the node;
  - the node holds pods the Hub can't see.
- The pods the Hub can't see are pods outside the Contract's namespaces. They are not a DaemonSet's or static.
  - CoreDNS in kube-system is one example.
  - They are counted from Prometheus, and never named.

#### Without a usable report

A usable report is missing when the cluster runs no Actuator, or has sent none yet. It is also missing when its last one reached the Hub more than two hours ago, by the Hub's clock.

In that case the Hub goes by what Prometheus shows of the Contract's pods.

- A pod with no controller, or a custom one, still blocks its node. This holds under `kubectl drain`'s and Cluster Autoscaler's rules.
- Under Cluster Autoscaler's rules, the reason adds "unless annotated safe-to-evict: true, which only an Actuator can see".
- Karpenter has no such rule. So nothing blocks there.
- A node is never clear this way. Volumes, annotations and which pods a PodDisruptionBudget covers stay unseen.
- Where a budget in a namespace with pods on the node allows no disruption, the unknown says so. It reads `kube_poddisruptionbudget_status_pod_disruptions_allowed`.
  - For example: "the cluster runs no Actuator; PodDisruptionBudget web of shop allows no disruption".

#### Limits of the verdict

- The autoscaler's own figures are not shown.
  - Cluster Autoscaler's `cluster_autoscaler_unremovable_nodes_count` labels its reasons with numbers that differ between its releases.
  - It counts nodes that are simply in use.
  - It keeps a reason's last count once that falls to zero.
- Karpenter's `do-not-disrupt` on a Node is not read. The Actuator reads no nodes.
- Cluster Autoscaler's flags are taken at their defaults. Those are `skip-nodes-with-system-pods`, `-local-storage` and `-custom-controller-pods`, all true.
  - A cluster that changed them is judged more strictly than it behaves.
- A pod that fits nowhere else is not a Blocker here.

### The change that removes a Blocker

Under each blocking pod, the Nodes page gives the change that would remove that Blocker. It is headed "To remove it".

- The change comes from the same rules that found the Blocker.
- It says what to change and what it costs.
- Where the change adds something, a manifest is given to copy.
- The Hub writes it nowhere. It belongs in the client's own manifests.
- The Hub MCP's `nodes` hands the same changes to the Operator's client.
- For a Contract that masks, the names inside a manifest are placeholders there. The page has the real ones.

| Blocker | The change | Manifest |
|---|---|---|
| a PodDisruptionBudget allows no disruption | check first that its pods are healthy (a budget also allows none while one is down); then more replicas than it requires, or let it allow one disruption | none: someone set it on purpose |
| annotated `safe-to-evict: "false"`, or `karpenter.sh/do-not-disrupt: "true"` | remove the annotation if the pods may be evicted | none: someone set it on purpose |
| `do-not-disrupt` with a duration | it ends by itself | none |
| no controller, under Cluster Autoscaler | run it under a Deployment, StatefulSet or Job; or accept losing it | the annotation `safe-to-evict: "true"` on the pod, which says in its comment that an evicted pod is gone for good |
| a custom controller's pod, under Cluster Autoscaler | the same annotation, if its controller brings an evicted pod back | the annotation, wherever that controller sets its pods' annotations |
| local storage, under Cluster Autoscaler | accept losing the data when the pod moves | `safe-to-evict-local-volumes` naming every local volume of the pod, on its Workload's pod template |
| a kube-system pod with no budget, under Cluster Autoscaler | give it a PodDisruptionBudget | one allowing one disruption, whose selector is a placeholder to replace with the pod's labels (the Hub reads no labels; as it stands it selects no pod and changes nothing) |
| no controller, a custom controller, emptyDir data, under `kubectl drain`'s rules | the flag of the drain that does it (`--force`, `--delete-emptydir-data`) and what it deletes | none |

#### Where an annotation goes

- On the pod template of the pod's Workload, where the Hub's read resolved one.
  - This applies to a kind that has a template: a Deployment, StatefulSet, DaemonSet, Job or ReplicaSet.
  - A CronJob's template is under `jobTemplate`.
- Otherwise it goes on the pod itself. The manifest says so.

#### What the manifest says about cost

- The cost is in the manifest's comments. A manifest gets pasted without the page around it.
- That includes what changing a pod template does. It restarts every pod of the Workload in one rollout.
  - So local data is lost then, not at some later eviction.
- A CronJob's change reaches only the Jobs started after it.
- A Job's template can't be changed once created.

#### Names

- A pod's, a Workload's or a volume's name that isn't shaped like a Kubernetes name never enters a manifest.
- That holds even when the name came from Prometheus and not from an Actuator.
- A placeholder, or "this pod", stands there instead.

#### Each change lifts its Blocker

- Each change lifts its Blocker under the Hub's own rules.
- It is applied to the pod's facts, and the reason is gone. This is tested as a round trip.
- A budget is lifted by no annotation.

#### The autoscaler's own settings

- The autoscaler's own settings decide the same for the whole cluster.
- The Hub can't read them. They live in kube-system, or in objects of no namespace.
- The page names the ones to check. It shows their documented defaults when looked up, not the cluster's values.
  - Cluster Autoscaler's `--skip-nodes-with-local-storage`, `--skip-nodes-with-system-pods`, `--skip-nodes-with-custom-controller-pods` and `--scale-down-utilization-threshold`.
  - Karpenter's NodePool `spec.disruption.consolidationPolicy`, `consolidateAfter` and `budgets`.
- `consolidateAfter` counts from the last pod added to a node or removed from it. Karpenter resets it then. Its docs, v1.15, were read 2026-11-16.
  - So a node where a CronJob's runs start more often than that is never consolidated. Part A saw one every 3 minutes under `5m`.
  - The Hub can't read the NodePool. So it can't tell which nodes that holds back.
  - It names the setting, rather than warn on every CronJob's node.

#### Not proposed

- The descheduler is not proposed. Its `HighNodeUtilization` needs a scheduler scoring `MostAllocated`, and the default scores `LeastAllocated`.
- Nothing is turned into a pull request.
- The Hub MCP's `propose_change` edits a Contract's Sizing policy in the contracts repo. It does not edit a client's manifests.

### Nodes that could go

**In short:** a Sizing frees capacity, but money is saved only when a node leaves. The Nodes page says which nodes could go, now and after the Sizings. The figure is an upper bound, not Savings.

A Sizing frees capacity. Money is saved only when a node leaves.

For each cluster, the Nodes page says which nodes could go:

```
Could go now: up to 1 (worker3), 120.00 EUR/month. After the Sizings: up to 2 (worker3, worker2), 240.00 EUR/month.
```

- It is the Hub's arithmetic, on what its hourly run and the cluster's Actuator stored.
- The Hub removes no node.

#### Which nodes

A node goes when both of these hold:

- its verdict is clear. It has no Blocker, and every pod on it is known;
- each of its pods that would move fits whole on one other node. That means its CPU, its memory, and a pod slot there.

Its DaemonSets' and static pods go with it. They need no room.

- A blocked or unknown node never goes, though it can receive.
- A control-plane node never goes.
- A cordoned node may go, and receives nothing.
- A node under a taint Kubernetes set for its condition receives nothing. Not ready and under pressure are examples.

#### Taints

- Pods move only between nodes carrying the same taints.
- That is because a Workload's tolerations can't be seen.
- The taints Kubernetes sets itself don't count as a difference. Those are cordon and node conditions.
- `PreferNoSchedule` doesn't count as a difference either.

#### What a pod needs of a node

- A moving pod fits only on a node that meets its nodeSelector.
- It also fits only on a node that meets its required node affinity: one of its terms, every expression of it.
- These are read as the scheduler reads them:
  - a label the node lacks meets `NotIn` and `DoesNotExist`;
  - `Gt` and `Lt` compare integers;
  - `matchFields` compares the node's name.
- Under Karpenter, its heaviest preferred node affinity term counts too. Every one of that weight counts.
  - Karpenter sorts them unstably, so which one it holds isn't fixed.
  - Its scheduler holds it as required while any node could meet it. A new one included.
  - So a pod preferring spot keeps its spot node, "SpotToSpotConsolidation is disabled", unless another spot node has room.
- The cluster's Actuator reports these from each pod's spec. It sends the nodes' values of the labels they name.
  - A node whose labels it didn't send takes none of the pods that need something of a node.
- Where the Actuator doesn't report them, no node is counted as one that could go. The page says why.
  - That is the case for an Actuator from before this, or one that may not read nodes.
- The restart advice of [Placement](#placement) goes by the pod's requirements alone.
  - A restarted pod is placed by the scheduler. A preference only scores.
  - So a pod requiring what no Pinned node has is never advised onto one.

#### The order

- The node asking least goes first.
- Its largest pod is placed first.
- Each pod goes on the first node by name with room.
- What a node receives is no longer free.
  - A node that goes receives nothing more.
  - A node that received pods, and then goes itself, must find room for those too.
- So the list is one consistent plan, the same from run to run.

#### Twice

The list is worked out twice:

1. with today's requests;
2. with the requests after the Sizings.

In the second, each container with a ready Sizing asks its Sizing.

- A DaemonSet's Sizing is applied per instance type. It uses its node's type's Sizing.
- Any other container keeps its own request. This covers one still learning, and one of a pod whose Workload the owner series don't resolve.
- A container that asks nothing today asks its Sizing.
- Each node's requests move by what its own pods' did.
- A Sizing that asks more can also take a node off the list.

#### An upper bound

- The figure is shown as an upper bound: "up to".
- These are not seen: spread, pod affinity, volume zones and tolerations.
- The autoscaler's own configuration is not seen either.
  - Karpenter's `SpotToSpotConsolidation` gate is named among its settings to check.

#### Price

- A node that could go is priced at its capacity, from `kube_node_status_capacity`.
- That is multiplied by the cluster's unit prices, typed or from list prices, for a month.
- Where the cluster names [spot nodes](#spot-nodes), it is priced at its own rate.
- Without prices, it is shown in cores and GiB.
- This is a figure of its own. It is never added to Waste, Savings or the Floors' total.
  - After the Sizings, it is partly the same money.

#### What "none" says

- The page, the digest and the tool count how many nodes were not judged.
- A cluster without an Actuator's report has no clear node. So none could go.
  - That is nobody having looked, not a finding.
- A cluster with [Placement](#placement) lists none either. This happens in two cases:
  - no node shows its label, because it is not allowlisted in kube-state-metrics, or no node is labelled yet;
  - its nodes weren't read with its label yet.
  - The page and the tool say why. The digest has no line where none could go.
  - Without this, a Pinned node would look like any other, and be counted as one that could go. This is audit A5.

#### Gaps

- Every gap means the node stays.
- A node read without its allocatable, or without its pod slots, neither goes nor receives.
- A cluster's read from before this slice says nothing until the next hourly run.

#### The digest line

The digest has one line when a node could go, now or after the Sizings:

```
Nodes that could go: up to 1 now (120.00 EUR/month), up to 2 after the Sizings (240.00 EUR/month); 3 not judged. An upper bound, not Savings.
```

#### Computed when asked

- The figure is computed when asked.
- It uses the stored read, the Actuator's stored report and the stored Sizings.
- So it always agrees with the verdicts shown beside it.
- For scale: 300 nodes of 30 pods take about 0.2 seconds.

#### The Hub MCP's nodes

- The Hub MCP's `nodes` is the first place an Actuator's strings reach a model.
- They go out redacted and masked, like everything else of the Contract's.
- A report whose namespace, pod, node or budget name is not shaped like a Kubernetes name is refused when it arrives.
- A controller's kind, or a volume's name that isn't shaped like one, is counted. It is not repeated.

#### Pinned nodes

- Pinned nodes, from [Placement](#placement), never go.
- No moving pod is counted to land on one.

#### GPUs in the could-go plan

This is slice 17.5a, for NVIDIA's `nvidia.com/gpu`.

- A moving pod's GPUs need as many free GPUs on the node it lands on.
  - Free means allocatable, less every pod's requests.
- Where both nodes carry GPU Feature Discovery's `nvidia.com/gpu.product` label, the product must match.
  - Allowlist it in kube-state-metrics: `nodes=[...,nvidia.com/gpu.product]`.
- Where one node doesn't carry it, the match is by count alone.
  - The could-go line then says "GPU model not checked".
- A pod asking a time-sliced GPU (`nvidia.com/gpu.shared`) or a MIG profile isn't placed by count. So its node stays.
- A node that could go is priced with its GPUs. The GPU price is the cluster's, typed or [from list prices](#cloud-list-prices).
- A GPU node without a GPU price shows no cost. It never shows its CPU and memory alone.
- GPUs are read apart from CPU and memory. A node's `gpus` covers allocatable, capacity, requested and product. A pod's `gpus` covers its own GPUs.
- The Nodes page shows each GPU node's GPUs asked of all, and its product as a chip.
- It also shows the GPUs of the nodes that could go, and the "GPU model not checked" note.
- The Hub MCP server's `nodes` gives each node's `gpus`. It gives could_go's `gpus` and `gpu_note`. This is slice 17.5b.

#### Unrequested GPUs

This is slice 17.5a. On each cluster, these are shown:

- the GPUs its nodes have that no pod asks for;
- how many nodes have some;
- their cost a month at the cluster's GPU price.

They are apart from [Idle GPUs](#idle-gpus), which are requested but unused. They are also apart from Waste.

- The digest has a line while there are some. For example: `Unrequested GPUs: 3 on 2 nodes, 980.00 USD/month.`
- The Nodes page has a line under the nodes that could go.
- The Hub MCP server gives `unrequested_gpus`. This is slice 17.5b.
- A node that could go and has unrequested GPUs counts them in both places.
  - Both are upper bounds, not Savings.
- Time-sliced GPUs advertised as replicas count each replica as a GPU. This is true here as in list prices.

### Placement

**In short:** pods that can't move keep whatever node they land on. Placement gathers them onto a few nodes kept for them, the Pinned nodes. That leaves the rest free to go. Placement is always a preference, never a requirement.

Pods that can't move keep whatever node they land on. **Placement** gathers them on a few nodes the client keeps for them. Those are the **Pinned nodes**. The rest can then go.

The Contract names the nodes' label per cluster, and the taint they carry if any:

```yaml
clusters:
  - name: prod
    actuator_token_secret: prod-actuator
    placement:
      label: squero.io/pinned=yes          # key=value, on the nodes kept for them
      taint: squero.io/pinned=yes:NoSchedule   # optional: tolerated by the pods placed only
```

#### Only with an Actuator

- Placement needs an Actuator on that cluster.
- Its webhook adds two things to each new pod of a placed Workload:
  - a preference for the label. This is one preferred node affinity term, weight 100, after the pod's own;
  - with a taint set, its toleration.
- It is a preference, never a requirement. A pod with no room on a Pinned node starts elsewhere.
- [How the Actuator does it](client-cluster.md#placement-gathering-the-pods-that-cant-move). This is from 0.55.0.
- With an older Actuator, the Hub shows the Pinned nodes and which Workloads it would place. It says nothing about restarting.

#### Which Workloads

A Workload is placed when its pods block a node for a reason set in their spec. The reasons are:

- `safe-to-evict: "false"`;
- `do-not-disrupt: "true"`;
- local storage;
- emptyDir data.

Each of these is judged under the rules of the cluster's autoscaler, as for [Blockers](#blockers-could-a-node-be-removed).

- This applies on any node, Pinned ones included. So a Workload that has moved keeps the preference at its next rollout.
- A Workload is not placed for a PodDisruptionBudget that allows no disruption. Nor is it placed for a timed `do-not-disrupt`.
  - Both come and go. A budget allows none during any rollout.
  - Placed for them, a Workload would fill the Pinned nodes with pods that can move.
- A pod with no controller, or a custom one's, is not placed. The webhook can't name a Workload for it.
- A DaemonSet's pods are not placed.
- The kinds placed are Deployment, StatefulSet, Argo Rollout and CronJob. They are in the Contract's namespaces.
  - Each pod's Workload is as the Actuator names it, from its last report.
  - That is none for two hours: the Actuator keeps what it last got.

#### No approval per Workload

- No approval is needed per Workload.
- The Contract's field is the consent, as for Reflexes.
- The pause annotation and the Kill switch stop it.

#### A Pinned node's verdict

A Pinned node's verdict is pinned. Its Blockers are shown as expected. No change is offered to remove them.

#### What frees a node

- A running pod's affinity can't be changed.
- The Actuator never evicts for Placement.
- So a pod already running moves at its Workload's next rollout or restart.

Once the cluster's Actuator places (its report says so), a blocked node can say which Workloads to restart. The page then says "Restart shop/Deployment/api to free it". This happens when both of these hold:

- every Blocker on the node is a placed Workload's pod;
- the Pinned nodes have room for every pod of those Workloads not on one already. A restart moves them all.

The room is checked like this: each pod whole, on CPU, memory and a pod slot, largest first. It goes on a Pinned node that takes pods, whatever its taints.

Otherwise it says why not. For example:

- "pod shop/web-1 would still hold it";
- "the pinned nodes lack room: about 1.2 cores and 3.0 GiB short".

Each node is judged on its own. So two nodes' advice may count on the same room.

A placed pod that started off the Pinned nodes says so. For example: "placed, on worker2: the pinned nodes had no room when it started".

#### Seen through kube-state-metrics

- A node label reaches Prometheus only when it is allowlisted. Use `--metric-labels-allowlist=nodes=[squero.io/pinned]`, or `[*]`.
- Where no node shows it, the Nodes page and the Hub MCP's `nodes` say so.
  - No node is counted as one that could go. A Pinned node would look like any other.
  - A Blocker of a Workload it places gets no change to remove it. Its node may be a Pinned one that keeps it on purpose. The page says so.
- A label set or changed in the Contract counts from the next hourly run's read.
  - Until then, no node is pinned, and none could go.
  - The page says the nodes were not read with it yet.

#### With Karpenter

With Karpenter, a preference is treated as required when it picks a node to launch. It is relaxed only when that can't be met. So put the label on a NodePool's template.

#### Where placement shows

- On the Nodes page: the cluster's label, how many nodes carry it, the Workloads placed, and whether its Actuator places them.
- In the Hub MCP's `nodes`:
  - `placement`;
  - `placed_by` on a blocker;
  - `restart` on a node.
- The digest's line is unchanged.

## The optimizer lab

**In short:** `make lab` turns the kind cluster into a persistent lab. It has Workloads with usage shapes the Sizings must get right. It can also run on another host, so the history is not broken by sleep.

`make lab` turns the kind cluster into a persistent lab.

- It installs kube-prometheus-stack. The chart is pinned in the Makefile.
  - This is the setup most client clusters run.
  - Prometheus keeps 30 days.
  - Grafana comes via `make lab-grafana`.
- In namespace `lab`, it installs Workloads with the usage shapes a Sizing must get right:
  - a 24-hour diurnal Deployment with two replicas;
  - an over-requested one;
  - a spiky one;
  - a slow memory leak, which its limit OOM-kills about hourly;
  - a CPU-throttled one;
  - an idle one;
  - a StatefulSet;
  - a DaemonSet;
  - a CronJob;
  - `weekly`.
- `weekly` is a Deployment under an HPA. The HPA targets CPU at 50%, with 2 to 5 replicas. metrics-server is installed for it.
  - Its load follows the week, and is shared by its pods.
  - It needs 1 replica at night and at weekends.
  - It needs 3 replicas on weekdays, from 08:00 to 18:00 UTC.
  - It needs 7 replicas on Mondays, from 09:00 to 12:00.
  - So its Need reads below the minimum, and above the maximum.

Prometheus is reachable from the Hub on kind's Docker network, at `http://squero-control-plane:30090`. So a Contract's cluster can set it as `prometheus_url`, with `lab` in its namespaces.

Leave the lab running. Real history builds up.

Its history lives inside the kind node container. So `kind delete cluster` erases it.

### A lab on another host

A laptop that sleeps leaves gaps in that history. A Weekly profile needs weeks without them. So the lab can run on another host that stays up.

```bash
make lab-remote LAB_HOST=<ssh name> LAB_ADDR=<address>
make lab LAB=small CLUSTER=lab KUBECONFIG=~/k8s_clusters/lab.yaml
```

- `make lab-remote` creates a kind cluster `lab` on that host. The host needs Docker and kind.
- Its API server and Prometheus listen on that address only.
- Use a private address: the host's Tailscale address. Prometheus has no login.
- The kubeconfig is written to `~/k8s_clusters/lab.yaml`. It is an admin's.
- The node container is capped at 2.5 CPUs and 6 GiB by default. The variables are `LAB_CPUS` and `LAB_MEMORY`.
  - The cap keeps the host's own services with theirs.
  - The node container restarts with Docker.
- `make lab LAB=small ...` then installs the lab from here.
  - `LAB=small` (`demo/lab/small`) is the lab for a 4-CPU host.
  - The full lab asks about 8 CPUs of its node. So there, the big Workloads ask for and burn a quarter to a third of the CPU, each in the same proportion.
  - In `small`, the shapes, the ratios of request to usage, `weekly` and all memory are unchanged.

### Pointing a Contract at the lab

- A Contract's cluster then sets `prometheus_url: http://<address>:30090`.
- That is for a Hub on another machine.
- A Hub on the same host cannot use it. Docker gives one container no way to another's published port through the host's own address. The read times out.

#### A Hub on the same host

The Hub joins kind's Docker network. This takes a `compose.override.yaml`:

- `networks: [default, kind]` for the `hub` service;
- `kind` as an external network.

The Contract then names the node:

- `http://lab-control-plane:30090` for Prometheus;
- `https://lab-control-plane:6443` as the kubeconfig's server. The API server's certificate covers that name.

#### The Hub's read access

- Apply `demo/lab/hub-read.yaml` for the Hub's kubeconfig.
  - It is the Hub's read-only role.
  - It is bound in namespace `lab` only.
  - It uses a token Secret, `squero-token`, in namespace `squero`.
- A kubeconfig with that token, and the cluster's server and CA, goes into the Contract's SOPS file. It is named by `kubeconfig_secret`.
- kind's own kubeconfig never does.

### Two Contracts on one lab

- The same cluster can hold the Triage demo: `make demo CLUSTER=lab`.
- It can also hold a second Contract.
  - `demo/lab/bank.yaml` is a small over-requested Workload in namespace `bank`.
  - `demo/lab/hub-read-demo.yaml` binds the first account in `demo` and `orders`.
  - It gives the second Contract an account of its own, in `bank` only.
- So two Contracts share one cluster and one Prometheus.
- Neither's kubeconfig reads the other's namespaces.
