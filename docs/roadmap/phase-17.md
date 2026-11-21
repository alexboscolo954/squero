# Phase 17: GPU and LLM inference

**In short:** The Hub reads what each GPU is used for, and flags *Idle GPUs* with their cost. It also reads vLLM's KV cache and queue, to size GPU replicas and warn about cache reservations. This phase only reports. Nothing is actuated, since an in-place resize covers CPU and memory only. It is built on fixtures. A real GPU check is Deferred.

**Status:** built on fixtures. 17.1, 17.2a to 17.2c, 17.3, 17.4a, 17.4b, 17.5a and 17.5b are built on 2026-11-20. The one paid check on a real GPU is Deferred. 17.6 (GPU sharing) and 17.7 (batch inference) are Deferred.

**Goal:** show which GPUs are idle, what they cost, and how vLLM's cache and queue should change the replica floor. Report only.

**Done when:** an idle synthetic GPU Workload is flagged with its cost from fixtures. Real GPUs only in a paid sandbox that the Operator approves.

| Slice | What | Status |
|---|---|---|
| 17.1 | DCGM ingestion, and each GPU attributed to its pod's container | built 2026-11-20, not checked live |
| 17.2a | Verdict, stored, priced by a typed `gpu`, in `sizing waste` and the digest | built 2026-11-20 |
| 17.2b | API, console Workloads and Cost pages, Hub MCP server | built 2026-11-20 |
| 17.2c | A GPU's price from list prices, and GPU-hours in the Bill check | built 2026-11-20 |
| 17.5a | GPU node consolidation: the read, the fit, the money, the digest | built 2026-11-20 |
| 17.5b | GPU nodes on the API, the Nodes page and the Hub MCP server | built 2026-11-20 |
| 17.3 | vLLM KV cache reservation risk | built 2026-11-20 |
| 17.4a | vLLM Needs, in place of the HPA's | built 2026-11-20 |
| 17.4b | Floor for a vLLM Workload without an HPA | built 2026-11-20 |
| 17.6 | GPU sharing | Deferred |
| 17.7 | Batch inference | Deferred |

## Decisions (grill, 2026-11-20)

The first grill covered 17.1 and 17.2 only. It had three rounds, all recommended. 17.3 to 17.5 were first planned for after real DCGM data had been seen. The same day, they were grilled on vLLM's docs instead, since the paid GPU check is Deferred. That grill had three more rounds, all recommended.

### First grill: 17.1 and 17.2

- **NVIDIA only.**
  - A container's request comes from kube-state-metrics' requests (`resource="nvidia_com_gpu"`).
  - Its use comes from NVIDIA's DCGM exporter.
  - Other vendors' exporters wait for a Contract that has them.
  - Both forms of a metric are read. The current default counters give 0 to 1 ratios: `DCGM_FI_DEV_GPU_UTIL_RATIO` and `DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO`. Older builds use `DCGM_FI_DEV_GPU_UTIL`, as 0 to 100.
  - The pod, namespace and container labels are read as written, and with the `exported_` prefix, as kube-state-metrics' are. The paid check confirms their exact names.

- **Idle GPU: a term of its own, not Waste.**
  - An Idle GPU is one a container requests, whose busiest hour in its Sizing policy's window stays under 5% busy.
  - The profiling metric (graphics engine active) is used where the exporter has it. GPU utilization is used otherwise.
  - Memory used is shown beside it, never counted.
  - Measured on the busiest hour, so a nightly batch is not called idle.
  - The window and history are the container's policy's. `off` excludes it.
  - Waste keeps its meaning (requests above Sizings). So CPU and memory totals don't move. An Idle GPU's cost is a line of its own, beside it.

- **Counted per GPU, grouped by Workload.** For example: "3 of 4 GPUs idle, 1,240 USD a month". So a partly idle Workload shows.
  - A GPU shared by time-slicing, and a MIG instance, is not judged. It is said so: "shared: not judged".
  - Pricing a pod's share belongs with GPU sharing (17.6).
  - GPUs on a node that no container requests are 17.5's.

- **A GPU's price.**
  - A typed `gpu` (per GPU-hour, one per cluster) wins.
  - Otherwise it comes from list prices. The node's price less its vCPUs and GiB, at the rates of the cluster's nodes without GPUs, divided by its GPUs. So GPU nodes leave the CPU and memory blend.
  - A cluster of GPU nodes only, with no typed `gpu`, shows its GPUs unpriced. The note says to add `prices.gpu`.
  - A cluster with several GPU models uses list prices.
  - The Bill check's estimate adds the GPU-hours at that price. So each node's price still adds back exactly.

- **Report only.**
  - Idle GPUs, with their cost, appear on the Workloads and Cost pages, in the digest, and through the Hub MCP server.
  - Nothing is actuated, since an in-place resize covers CPU and memory only.
  - A Proposed change that lowers a GPU request waits until the flags are trusted.
  - The agent's `sizing_for_workload` gets them in a slice of its own, with its eval rerun.

- **Checked** on fixtures (`FakePrometheus` with DCGM's metric names and labels). Then one short paid check confirms the labels before the phase counts as done: a spot `g4dn.xlarge` with the GPU Operator for about 2 hours, about 2 USD, asked for separately. That check is Deferred.

### Second grill: 17.3 to 17.5 (on vLLM's docs)

vLLM's metrics: `vllm:num_requests_running`, `vllm:num_requests_waiting`, and `vllm:kv_cache_usage_perc`. The last is a 0 to 1 fraction, despite its name. Each has a `model_name` label. `vllm:cache_config_info` carries `gpu_memory_utilization`, which is what vLLM reserves of the GPU up front. So DCGM's memory used reads near it whatever the load.

- **Order:** 17.5, then 17.3, then 17.4. vLLM only. Other servers can follow when a Contract runs them. Report only. Checked on fixtures, and said "not checked live".

- **17.5, GPU node consolidation.**
  - GPUs are a third resource in the nodes that could go (15.4).
  - A node's GPU pods fit on other GPU nodes with free GPUs. Where nodes carry GPU Feature Discovery's `nvidia.com/gpu.product`, the same product is needed. Otherwise the fit is by count, and the line says "GPU model not checked".
  - A node that could go is priced with its GPUs.
  - **Unrequested GPUs** (a new term) are counted per node, and priced at the cluster's GPU rate. They are apart from Idle GPUs and Waste. They show on the Nodes page, in a digest line, and in the Hub MCP's `nodes`.
  - Still report only. The autoscaler's drain removes nodes.

- **17.3, the KV cache reservation.**
  - A Risk on the vLLM container, kind `kv_cache_light`. It applies when its busiest hour's KV cache usage over its Sizing policy's window (and history) stays under 50% of the blocks.
  - The text gives the usage, the current `gpu_memory_utilization` and the block count. It says that "a lower setting or a smaller GPU or MIG slice would likely hold it".
  - No new setting is computed. The model's weights sit in the reservation, and no metric gives their size.
  - Shown wherever Risks show. Nothing is applied. It is an argument, which the Actuator never changes.

- **17.4, Floors from vLLM.**
  - A vLLM Workload's hourly Need is: that hour's replicas x its busiest KV cache usage / 0.8, rounded up. At least one replica more in an hour when requests waited.
  - Phase 14's Floor, Weekly profile and Backtest are reused whole. Only the Need's source changed.
  - A Workload without an HPA gets a Floor too, against its replica count. It is never below 2, unless it already runs fewer.
  - Its saving is the replicas above the Floor, times their GPUs, times the GPU price.
  - Its Weekly profile gives the blocks. The cron triggers come with a note that they need a KEDA ScaledObject.

- **Glossary:** *Unrequested GPU* is added. *Need* and *Floor* are extended. A vLLM Workload's Need comes from its KV cache and queue. A Floor can exist without an HPA.

**Chosen while building, and each new term:** see the slices below.

## 17.1 DCGM ingestion

**Built 2026-11-20.** GPU use is read, with each GPU attributed to its pod's container. That attribution moved here from 17.2, since scoping the read by namespace needs it. 17.2 keeps the verdict, the cost, the storing and the surfaces.

- **A container's GPU request** comes from the requests read already made: the window's largest. Nothing more is asked where none requests one.
  - It was widened while building from the grilled `nvidia_com_gpu` to every resource that counts GPUs:
    - `nvidia_com_gpu_shared` (time-sliced, renamed by the device plugin, and marked shared);
    - MIG profiles, `nvidia_com_mig_*`, one instance each. They are marked shared by DCGM's `GPU_I_ID`.
  - So those containers say "shared: not judged" rather than going unseen.
  - A sharing scheduler's memory and core quantities (HAMi's `nvidia_com_gpumem` and `gpucores`) are not GPUs, and stay out.
- **DCGM's hourly means:** one range query per metric name and label case, at a one-hour step.
  - The busiest hour is taken in code. It is judged by the graphics engine where DCGM has it, else by GPU utilization.
  - The deprecated percent and MiB forms are scaled.
  - Peak memory is shown only.
  - The hours read and the pod last seen on each GPU are kept.
  - MIG instances and virtual GPUs are marked shared.
- **Facts from the exporter's code and chart, not recall:**
  - Current counters are 0 to 1 ratios (`_RATIO`) and bytes. The deprecated ones are percent and MiB.
  - The engine's older name, `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, comes from the docs' example. It is a ratio too.
  - Its pod labels are `pod`, `namespace` and `container`. The old names were `pod_name`, `pod_namespace` and `container_name`.
  - Its chart's ServiceMonitor has `honorLabels: false`, so the labels arrive as `exported_*`.
  - The chart turns the Kubernetes mapping on (`kubernetes: auto`). The binary's flag is off by default.
- **Isolation:** three label cases. Each is scoped by its own namespace label. Plain `namespace` counts only with neither of the others on the series. Tested with both the `exported_*` and the old names, and with a Contract that owns the exporter's namespace and a GPU pod there.
- A failed read is a note. The Sizings are kept.
- A Relay passes the queries on as they are.
- `squero sizing usage` shows them.

**Tests:** 22 mutations, all caught. One, the request's largest value, survived until a test asked for 2 GPUs. Each isolation matcher was mutated on its own.

Not stored and not in the API. So no console types and no eval rerun. **Not checked live:** the label names on a real exporter. That is the paid check, and it is Deferred.

## 17.2a The verdict, stored and priced

**Built 2026-11-20.** Each GPU of a GPU-requesting container is stored with its Sizing row. It is jsonb, so there is no migration. It has a verdict:
- `idle`: busiest hour under 5%;
- `busy`;
- `learning`: fewer hours read than the policy's history;
- `shared`;
- `unread`.

It is computed under the container's policy and window. It is never computed for an excluded container, and counted only for ready containers.

**Decided while building:** a Workload's "N of M" and its money count each judged GPU by the share of the window it was held. Hours read are counted over the window's hours, capped at 1. So a pod that moved across three GPUs counts as one GPU, as Waste counts average pods.

- `prices.gpu` (per GPU-hour, at least 0, optional) sits on the Contract's or a cluster's prices. A cluster's own price, without one, takes the Contract's.
- It survives the spot blend, unblended. (ponytail: until 17.2c.)
- Its monthly cost is a line beside Waste. Waste, its cost and the ranking are unchanged. This is tested.
- Savings and the Bill check read no GPU field.
- GPUs that are not judged (shared, unread, or learning) are said beside the count, by the same held share. So "none idle" never reads as "all busy".
- A priced Contract with one cluster unpriced for GPUs shows no total. It never shows a partial one.
- `squero sizing waste` lists a Workload with idle or unjudged GPUs, and no waste, too. `--json` gives `gpus`. The digest has a line.
- The agent's `sizing_for_workload` is unchanged, and tested. So no eval rerun. The OpenAPI is unchanged, so no console types.

**Until 17.2c:** a list-priced cluster prices its GPUs only by typing its prices in full. `resolve` passes them through `_typed`, which keeps `gpu`. This was checked by reading.

**Phase 17's done-when, on fixtures:** a test runs from DCGM's series, through `sizing refresh` and a stored run, to `sizing waste`'s idle GPU and its cost. Two Contracts' idle GPUs are kept apart, through the report and the digest.

**Tests:** 22 mutations, all caught. One, which counted every verdict, survived until a test summed a mix.

## 17.2b The API and the surfaces

**Built 2026-11-20:**
- The API's `Workloads` shows the Contract's `gpus` and each Workload's.
- Each `Container`'s GPUs appear with their verdicts (`GpuUse`).
- The Cost page's `Allocation.gpus` (the report's, beside the Savings) is kept when comparing.
- `report()` writes each count's `text` once, as `sizing waste` says it. So the console and the Hub MCP server never word it themselves.
- Console: the total is under the waste. An Idle GPUs column appears only on a Contract with GPUs. Each GPU shows in a Workload's detail: verdict, busiest hour, hours, share held, and peak memory. The total is on the Cost page. All of it is text.
- Hub MCP: `sizings`' `idle_gpus` for the Contract and each Workload. `cost`'s `gpus`. Both descriptions say so.
- No node or pod name is in that text, so masking has nothing new to cover.

**Decided while building:** counts show up to two decimals. 0.25 of a GPU had read as 0.2, beside a cost worked out from 0.25.

The OpenAPI and console types were regenerated. **Isolation:** one Contract's idle GPU reaches its own `sizings`, `cost` and Workloads page, and never another's. **Tests:** 9 mutations, 6 in the Hub and 3 in the console, all caught.

## 17.2c GPU prices from list prices

**Built 2026-11-20:**
- Node facts carry each node's GPUs, from its `nvidia.com/gpu` capacity. The key appears only on a GPU node.
- With list prices, in a cluster with both GPU nodes and nodes without:
  - the CPU and memory rates, and the spot shares, are those of the GPU-less nodes;
  - each GPU node's price above its vCPUs and GiB, at the rates the Bill check prices them at (unblended, and a spot one at the spot rate), is its GPUs' price;
  - that is summed over the GPU nodes and divided by their GPUs;
  - the price note names it. This applies to AWS and Azure, whose instance prices include the GPUs.
- GPUs on every node: the rates are the whole nodes' as before. The GPUs are unpriced, and the note says to type the cluster's prices in full.
- GPU nodes no dearer than their CPU and memory: unpriced too, never priced at 0.
- The Bill check's estimate adds the cluster's GPU-hours at its `gpu` (typed or listed). It is read only where there is one, so each node's price still adds back whole.
- Tested with nodes on spot and on-demand, with and without GPUs, and with a GPU node's spot list price at another ratio from the others'.

**ponytail:** one GPU rate for spot and on-demand GPU nodes, the latest mix's, outside the spot part (`spot_estimated`). MIG profiles' capacity is not counted.

**The grill's "typed wins":** on a list-priced cluster, that means typing its prices in full, as before. A `gpu` alone in `list_prices` would be a new setting, which was not asked for.

**Decided while building, against the grill's Q10 ("several GPU models use list prices"):** one GPU rate per cluster. So with several models, each is priced at their average. The nodes' total adds back, not each node's.

**GCP is gated.** Its list price is a machine's cores and RAM. Its bill is read as its core and RAM lines. So its GPUs stay unpriced (noted), and its Bill check leaves GPU-hours out, typed `gpu` or not.

**Known effects, recorded in the docs:**
- A mixed list-priced cluster's CPU and memory rates drop on upgrade. The GPUs' share was in them. So its money for waste, requests and Savings drops too.
- Time-sliced GPUs advertised as replicas are priced per replica.
- The nodes that could go are priced without their GPUs.

**Tests:** through stored node facts, `effective()`, and the Bill check itself. An on-demand m5, a spot m5 and a GPU node reconcile whole. `gpu_hours` is kept to the cluster's labels, tested with two clusters on one store. Nothing new is stored except the node facts' key, and there is no API change.

**16 mutations, all caught.** One, the GPU nodes in the spot shares, survived until a GPU node's spot list price was at another ratio.

## 17.5a GPU nodes that could go, and unrequested GPUs

**Built 2026-11-20.**
- GPUs are read apart from CPU and memory. A node's `gpus` holds its allocatable, its capacity, and its requests summed over every pod, by the cluster's labels only. It also holds GPU Feature Discovery's `nvidia.com/gpu.product`.
- A Contract pod's GPUs are read in its namespaces. A pod with a time-sliced GPU or a MIG profile is `gpus_unread`.
- No reader of the CPU and memory dicts changes. This is tested.
- The nodes that could go:
  - a moving pod's GPUs need as many free GPUs where it lands. The product must match where both nodes name theirs. Otherwise the match is by count, and the could-go line says "GPU model not checked".
  - what lands is no longer free.
  - a pod moved twice keeps the product of the node it first left.
  - a `gpus_unread` pod lands nowhere, so its node stays. This means a GPU pod once packed by CPU and memory alone. Some clusters' could-go lists shrink as a result.
  - it holds after the Sizings. `after` keeps a pod's GPUs (tested).
- A node that could go is priced with its GPUs, at the cluster's GPU price. Its rate is the same as the Bill check's, spot or not, so a list-priced GPU node is worth its list price. Without a GPU price, there is no cost, and never a partial one.
- Unrequested GPUs are counted per cluster: allocatable, less every pod's requests, for the nodes with some. Their cost and the digest's line are shown. Where they overlap a node that could go, the docs say so.
- A read from before 17.5a goes as before.
- **Isolation:** the GPU node sums are by the cluster's labels (two clusters on one store). The pods' GPUs are by the Contract's namespaces (the query count test).
- The OpenAPI is unchanged.

**Tests:** 18 mutations, all caught.

**Not checked live** (the paid GPU check is Deferred).

## 17.5b GPU nodes on the API and the surfaces

**Built 2026-11-20:**
- The API's `Node.gpus` (`NodeGpus`): allocatable, capacity, requests and product.
- A pod's `gpus` and `gpus_unread`.
- `could_go`'s `gpus` and `gpu_note`.
- Each cluster's `unrequested_gpus`.
- The OpenAPI and the console types are regenerated.
- **The Nodes page:**
  - a chip on each GPU node, for example `1 of 4 GPUs asked, Tesla-T4`;
  - the GPUs of the nodes that could go, and "no GPU price" where a cost is missing for them;
  - the GPU note;
  - a line of Unrequested GPUs.
  - All of it is text.
- **The Hub MCP's `nodes`:** each node's `gpus`, `could_go`'s, and `unrequested_gpus`. The description says so.
- **Isolation:** one Contract's GPU node reaches its own Nodes page and `nodes`, and never another's.

**Tests:** 9 mutations, 5 in the Hub and 4 in the console, all caught.

## 17.3 The KV cache reservation

**Built 2026-11-20.** Where a container requests a GPU, the Hub reads vLLM's `vllm:kv_cache_usage_perc` as the window's hourly peaks. It keeps the highest, and the hours read. It also reads `vllm:cache_config_info` (`gpu_memory_utilization`, `num_gpu_blocks`). Both are read in the Contract's namespaces.
- A series counts for its own container (`container`, which asks a GPU). Or it counts for the pod's one container that requests a GPU. With two such containers and no `container` label, neither counts.

**Decided while building:** the hour's peak rather than its mean. A cache that fills for minutes in an hour is not light.

The Risk `kv_cache_light` applies when the peak stays under 50%, with the policy's history read. Its text gives the peak, the blocks, and the reservation. No new setting is computed.

It reaches every Risk's surface, including the agent's `sizing_for_workload`, as data in the tool's existing `risks`. No tool name, description or schema changed. No Scenario runs vLLM, so no eval rerun.

A failed read is a note, and the Sizings are kept. There is no API change, since a Risk's kind is a string. **Isolation:** another namespace's vLLM series is never read.

**Tests:** 12 mutations, all caught. **Not checked live** (the paid GPU check is Deferred).

## 17.4a vLLM Needs

**Built 2026-11-20.** `usage.vllm_needs` covers a container of the cluster that requests a GPU. For each Workload and UTC hour:
- take the pods that reported `vllm:kv_cache_usage_perc` that hour;
- times the busiest one's hourly peak;
- over 0.8, rounded up;
- and one replica more in an hour when `vllm:num_requests_waiting` went above 0.

Pods are matched to their owners as Sizings resolve them, in the Contract's namespaces. Another namespace's pod of the same name never counts (tested).

These are merged over the HPA's Needs in `collect`. A vLLM Workload's own win. They are read from the same hour on. A failure is logged, and the HPA's Needs are kept.

**ponytail:** replicas are the pods that reported in the hour. So a rollout's hour counts the old and new pods.

Not checked live. **Tests:** 12 mutations, all caught.

## 17.4b A Floor without an HPA

**Grilled further while building (2026-11-20, all recommended):**
- Q16: a fixed-replica Workload's saving is Phase 14's. Each hour, its replicas less the larger of the Floor and that hour's Need. That is what an autoscaler holding the Floor would save. It is said on its line. It is not Q12's every-hour count.
- Q17: a replica count lowered by hand is never realized Savings.
- Q18: GPUs count in every Floor's saving, and in its Backtest's too many, whether there is an HPA or not. They are never priced in part.

**Built 2026-11-20:**
- `usage.fixed`: a GPU Workload without an HPA joins the HPAs' bounds as `fixed`. That is a Deployment's or StatefulSet's replica count, in the Contract's namespaces. Another kind is not known. A Workload at 0 in the lookback scales to zero. An HPA wins.
- `floors` gives it a Floor only with stored Needs. It does not use this run's read. So an hour missed keeps it. Its texts say replicas.
- Every Floor's saving and Backtest carry their replicas' GPUs (`gpu_saving`, `excess_gpus`).
- `floors_report` prices them with the GPU price, or gives no cost. The total does too.
- `floor_text`, the digest line (`can run fewer replicas`, or both kinds), `scheduled` (the cron triggers a KEDA ScaledObject would need, with a note), and the console's Floors section say "no HPA".
- The realized drops leave fixed Floors out. Tested both ways.
- API: `Floor.fixed`, `gpu_saving`, `Backtest.excess_gpus`, and the total's `gpus` and `fixed`. The console types are regenerated.

**Tests:** 24 mutations, 20 in the Hub and 4 in the console, all caught.

**Not checked live.**
