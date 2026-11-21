# Phase 10: Cost and Savings

**In short:** Phase 10 put money on Sizings. It covers unit prices, cost by namespace, label and team, available and realized Savings, cloud list prices, and reconciliation against the cloud bill.

*What it adds:* money. Unit prices, costs by namespace, label and team, Savings, cloud list prices, and a Bill check against the cloud's own bill.

**Status:** built (10.1 to 10.5d). Two facts bear on the done-when:

- The GCP check of 2026-11-02 reconciled at -1.0%, within 5% (see 10.5c).
- The AWS sandbox could not show a non-zero bill, so 10.5a waits for an account that bills (see 10.5a).

The later slice (the billed rate) is not built.

**Done when:** namespace cost on kind matches a hand computation, and one sandbox bill reconciles within a stated tolerance.

| Slice | What | Status |
|---|---|---|
| [10.1](#101-unit-prices-per-contract) | Unit prices per Contract | ✅ built, checked live |
| [10.2](#102-allocation-by-namespace-label-and-team) | Allocation by namespace, label and team | ✅ built, checked live |
| [10.3](#103-available-and-realized-savings) | Available and realized Savings | ✅ built, checked live |
| [10.4](#104-cloud-list-prices) | Cloud list prices | ✅ built, checked live |
| [10.5](#105-read-only-billing-readers) | Read-only billing readers: AWS, Azure, GCP | ✅ built, checked live (see notes) |
| [Later](#later-slice-the-billed-rate-feeds-prices) | The billed effective rate feeds prices | Not built |

## 10.1 Unit prices per Contract

**Grilled 2026-10-24 (all recommended):**

- CPU and memory only. GPU and spot arrive with Phases 17 and 16, which measure them.
- `prices: {currency, cpu, memory}` per Contract. The prices are per vCPU-hour and per GiB-hour.
- A cluster's own `cpu` and `memory` win over the Contract's.
- There is one currency per Contract. Currencies are never converted.
- Money is a monthly run rate: 730 hours, from the latest run. It is worked out when shown. So a corrected price applies to history.
- Accrued totals and periods are in 10.3.
- Each Workload shows its waste cost and its request cost. Requests count every container that has a request.
- The console, the digest and `sizing waste` rank by the waste's cost. The agent's tool shows no cost.
- Refused: a negative price; a cluster's prices without the Contract's; a currency on a cluster.
- No prices means no change.
- *Done when:* hand-computed tests and an isolation test pass. The Mac Hub's demo-shop shows its costs. Then v0.8.0.

**Built (2026-10-24).**

**Changed from the grilling:** every stored row now keeps its requests and average pods. Learning rows used to keep no requests. Excluded rows kept none either. So their cost could not be counted. Rows stored before this still count only the ready containers' requests.

**Checked live (2026-10-24, Mac Hub, v0.8.0):**

- demo-shop was priced at USD 0.0316 and 0.0042.
- `api` costs 69.46 USD a month of waste, and 78.40 of requests.
- A hand computation from its 2.652 cores and 2.701 GiB of waste, and from 3 pods asking 1 core and 1 GiB, gives the same figures.

## 10.2 Allocation by namespace, label and team

**Grilled 2026-10-24 (all recommended). It has two parts.**

### 10.2a: by namespace

- A console Cost page, and `aiops sizing cost --by namespace [--json]`.
- Each namespace shows its request cost (every container) and its waste cost (sized containers only).
- Namespaces with unpriced cores or GiB are shown in those units.
- They are ranked by requests, and link to their Workloads.
- The digest is unchanged.
- Then v0.9.0.

**Built (2026-10-24).** A namespace is identified by its cluster: `kind/lab`. So two clusters' `shop` namespaces stay apart.

**Checked live (2026-10-24, Mac Hub, v0.9.0):**

- `kind/lab` requests 147.29 USD a month, and its waste is 75.29.
- The waste equals its ranked Workloads' waste.
- The requests are the sum of three parts: the ready containers' requests (117.32), the learning `db`'s (3.84), and the excluded `idle`'s (26.13).
- The namespaces add up to the Contract's 148.50.

### 10.2b: labels and teams

- Config: `allocation: {labels: [...], teams: [...]}`.
- Label values come from kube-state-metrics' `kube_pod_labels`. Only the keys listed are read.
  - A key that is not allowlisted gets a warning, per cluster.
  - When pods disagree, the newest pod's value is used.
- Teams come from first-match rules. A rule matches on cluster, namespace glob, and label values. Anything unmatched goes to "(no team)".

**Built (2026-10-24).** Changes from the grilling:

- A team rule names its team in `team:`.
- A team, or a label value, can span clusters.
- The newest pod is the latest `kube_pod_created`. Pods without one are sorted by name.
- The warning is worked out when shown. It comes from the stored rows: a cluster where no pod has the label. It appears on the Cost page and in `sizing cost`.
- `--by team` exists only when team rules are set.
- A Workload without the label goes to `(no <key>)`.
- Team and label groups do not link to the Workloads page. That page filters by namespace only.
- The lab's pods carry `team`: one, `idle`, and none.
- The lab allowlists `team`. Then v0.10.0.

**Checked live (2026-10-24, Mac Hub, v0.10.0), with demo-shop's four team rules on the `team` label:**

- storefront: 103.00 USD a month of requests, 70.25 of waste, 2 Workloads.
- payments: 9.22. data: 8.61. platform: 0.33.
- (no team): 27.34. This covers `idle`, and the 4 Workloads of `demo` and `orders`.
- These add up to the Contract's 148.50. Their waste adds up to the namespaces' 74.65.
- `--by label:team` gives the same groups, with no warning.

## 10.3 Available and realized Savings

**Grilled 2026-10-24 (all recommended, 2 rounds). It has two parts.**

### 10.3a: available Savings and comparisons

- **Available Savings** is the waste's cost, under its glossary name. It is totalled per Contract and per group, on the Cost page and in the CLI.
- What under-sized containers need stays a risk. It is never netted against the Savings.
- **Compare periods** are snapshots. Each group's monthly request and waste cost now is compared with N days ago (7, 30 or 90). This uses `latest_sizings(before=)`.
- If there is no run that old, the comparison says so and gives the oldest date.
- Runs before 0.8.0 compare only containers whose requests are known.
- The Cost page has a picker and a Δ column. `aiops sizing cost --compare 30d` does the same on the command line.
- Then v0.11.0.

**Built (2026-10-24). Changes from the grilling:**

- The picker also offers 1 day, for the live check.
- The CLI takes any `m`, `h` or `d`. The API takes 1 to 400 days.
- Each cluster is compared on its own. A cluster with no run that old is left out of both sides. It is named with its oldest run. So the comparison is never empty for the others.
- When grouping reads labels (`label:<key>`, or team rules with labels), every container of a cluster is also left out. This applies where no pod had those labels then, but some do now. Such containers were stored before the Contract listed the labels, or before kube-state-metrics exported them. Containers whose requests were not stored are left out too. Both are listed.
- A group that is gone since is still listed, with its whole drop.
- Only the Cost page and `sizing cost` say "savings available". The Workloads page, the digest and `sizing waste` still say "waste".

**Checked live (2026-10-24, Mac Hub, v0.11.0):**

- `sizing cost --compare 1d` compared the 23:09 UTC run with 2026-10-23 23:45 UTC. That was the latest run at or before 00:09.
- It left out the 4 containers stored before 0.8.0: `db`, `idle`, `checkout` and `reports`.
- Its changes equal a hand computation from both runs' rows in Postgres.
- `kind/lab` requests +0.00 USD a month, and Savings +0.73 USD a month.
- `kind/demo` and `kind/orders` show 0.
- The team comparison waits for a day of labelled runs.

### 10.3b: realized Savings

- Each hourly run compares a container with the previous run. It stores a drop when the new request is within 10% of a Sizing shown in the 14 days before. CPU and memory are checked apart.
- The migration stores the container, the time, and the CPU and memory before and after. There is no backfill.
- A drop is worth (before - after) × the pods now × the price. It is priced when shown.
- It is shown as a monthly run rate, and as accrued since the change (run rate × hours, at today's prices).
- A later rise cancels that much. A deleted Workload ends it.
- Fewer pods, and raised requests, never count.
- `aiops sizing savings [--json]` shows it. The Cost page has a Savings summary (available and realized).
- The digest has one line: "Realized this week". Where a price is missing, cores and GiB are shown unpriced.
- The agent's tool is unchanged.
- Then v0.12.0.

**Built (2026-10-24). Changes from the grilling:**

- Requests and limits are now read from the live pods. These are the pods seen in the last 5 minutes. The window's largest value is used only for a container with no live pods.
- *Why:* the window's largest value across pods hid a lowered request for up to the whole window. This is a correction to 9.1.
- The matched Sizing must be below the old request. A rise cannot make a saving.
- A drop is stored once per previous run. So a `sizing refresh` that overlaps the hourly run cannot store it twice.
- Each row says whether its requests come from its live pods.
- Only a live row starts, takes back, or ends a drop. A run with kube-state-metrics down, or a Workload scaled to 0, reads the window's old request. So only drops between two 0.12.0 runs count.
- "The pods now" are the latest run's average pods, the same as the waste's.
- A removed request ends its drops.
- The digest leaves the line out while nothing is realized.
- The Cost page's summary comes with the cost API. It has no API of its own.

**Checked live (2026-10-25, Mac Hub, v0.12.0):**

- `kubectl set resources` lowered lab `api` to its Sizing: 116m, 102Mi, 3 pods. The next run stored it.
- `sizing savings` showed 61.18 + 8.28 = 69.46 USD a month.
- That equals a hand computation: ((1 - 0.116) × 3 × 0.0316 × 730) and ((1 GiB - 102Mi) × 3 × 0.0042 × 730).
- The digest said "Realized this week: 69.46 USD/month".
- `make lab` raised `api` back to 1 core and 1Gi. The run 5 minutes later ended the drop ("no realized Savings").

## 10.4 Cloud list prices

**Grilled 2026-10-25 (all recommended, 2 rounds):**

- The Hub reads on-demand list prices from the public APIs.
  - AWS: the price list.
  - Azure: the Retail Prices API.
  - GCP: its catalog. This is used only when the Contract names an API key secret. Otherwise GCP is skipped, with a message.
- Prices are refreshed daily and stored with their date. The hourly run reads the stored copy.
- A cluster's provider, region and instance types come from kube-state-metrics' `kube_node_labels`. This needs no new RBAC. The KSM allowlist must expose these labels.
- A node's hourly price is split into a CPU price and a GiB price, by one fixed weight. The default is 1 vCPU = 7 GiB, per Contract. The two parts sum to the node price.
- A cluster with mixed node types gets the capacity-weighted average. It is the total node price over total vCPU and GiB. It gets one price per cluster, as `prices` does.
- An optional per-Contract `discount` factor multiplies the list price. It applies to on-demand only. Spot waits for Phase 16.
- Typed `prices` win. The list fills only the gaps. So upgrading changes no shown cost.
- The console says which source each price comes from.
- A cluster with no labels (kind, or bare metal), or a failed fetch, gets no list price. It says why. It keeps the last good price with its age. It never makes up a price.
- *Done when:* hand-computed tests pass (split, average, discount, precedence, fallbacks). The AWS price file is parsed as a stream, for only the instance types in use. The Mac Hub's kind node, labelled by hand as an m5.large in us-east-1, shows the real list price, equal to a hand computation. Then v0.13.0.

**Built (2026-10-25). Changes from the grilling:**

- A cluster may declare `provider: aws|azure|gcp`. Kind and bare metal have a `kind://` provider_id. The check needs a cloud name.
- `list_prices: {weight, discount, gcp_key_secret}` and a Contract-level `prices` are refused together. The Contract's prices already price every cluster, so the list would have nothing to fill. A cluster's own `prices` are allowed next to `list_prices`.
- If one cluster cannot be priced from the list, the whole Contract stays in cores and GiB. Money for one cluster and cores for another cannot be ranked. A note is given per cluster.
- The daily lookup rides on the hourly Sizing tick. A price is looked up again after 24 h. A failed lookup is retried after 1 h. `aiops sizing prices [--refresh]` shows or forces it.
- The AWS offer file is streamed to disk, and parsed with `ijson`. This is a new dependency, because the file is hundreds of MB.
- GCP's key goes in the `X-Goog-Api-Key` header, never in the URL. A failed lookup stores only `HTTP <status> from <host>`.
- A cluster on a store shared with another Contract, and without labels, reads no nodes.
- The Cost and Workloads pages, `sizing cost` and `sizing waste` say where each price comes from.

**Checked live (2026-10-25, the local v0.13.0 image on the Mac Hub):**

- The kind node was labelled by hand as an m5.large in us-east-1.
- `aiops sizing prices --refresh` read AWS's real offer file. The real m5.large Linux price is 0.096 USD/h.
- With the node's real capacity of 10 vCPU and 31.29 GiB, the prices came to 0.00094776 USD per GiB-hour, and 7 times that per vCPU-hour.
- demo-shop's total requests are 5.70 cores and 5.55 GiB. They cost 31.45 USD a month. A hand computation, ((5.7 × 7 + 5.55) × 0.00094776 × 730), gives the same figure.
- `sizing cost` printed its price line.

**GCP note:** GCP is checked against recorded fixtures only. There was no Billing API key yet.

## 10.5 Read-only billing readers

**Grilled 2026-10-26 (all recommended, 3 rounds).**

- This is reconciliation only. It shows "estimated vs billed" per cluster and period. It never changes a shown cost.
- Three slices, one release each: 10.5a for AWS, 10.5b for Azure, 10.5c for GCP.
- 10.5d was added later (see 10.5d).

### 10.5a: AWS

**Plan:**

- Reads Cost Explorer `GetCostAndUsage`, with the Contract's `aws` integration key. The key needs `ce:GetCostAndUsage`.
- A cluster names its bill lines with `billing: {tag: "key=value"}`. The bill lines are EC2 compute, usage before credits, unblended, daily.
- The estimate is the cluster's node-hours, from a Prometheus range query, at its effective price (typed or list).
- The period is 7 full UTC days, ending 2 days ago. `--days 1` to `30` is allowed.
- The bill is fetched daily on the hourly Sizing tick. It is stored per cluster and day. A failure is retried after 1 h. `--refresh` forces a read.
- 5% is the tolerance.
- It is shown by `aiops sizing bill [--json]`, and as a Cost page line per cluster. The digest and the agent are unchanged.
- A currency that differs from the Contract's is shown with no gap. It is never converted.
- Live check: one t3.small in us-east-1, tagged `aiops-cluster=kind`, for about 48 hours. This costs about 1 USD from credits, with the Operator's yes. The kind node is labelled t3.small. The bill should equal 0.0208 × hours, within 5% of the estimate.
- Then v0.14.0.

**Built (2026-10-26). Changes from the grilling:**

- The bill has its own endpoint: `GET /contracts/{id}/billing?days=`. It is not a field of the cost API. It asks Prometheus for node-hours. The Cost page's other requests should not wait for that.
- The estimate is node capacity-hours. These are hourly samples of `kube_node_status_capacity`, at the cluster's unit prices. So a changing node count is counted, not only the latest nodes.
- `billing:` is refused without the `aws` integration, or without a Prometheus.
- The read covers the last 30 full days, and leaves today out.
- A failed read keeps the stored days, and the time of the last good read.
- The CLI exits 1 when any cluster is outside 5%.
- *Tests:* 964 Python tests (36 new) and 12 console tests pass. 27 mutations caught.
- AWS GovCloud and China partitions are not read.
- The live check (the tagged t3.small for about 48 hours) waits for the Operator's yes.

**Checked live (2026-10-26, against the AWS sandbox's real Cost Explorer, API only):**

- `ce:GetCostAndUsage` was added to `aiops-readonly`.
- `billing.fetch` with that key read 30 days in USD and parsed them. It is signed by the Hub's own SigV4.
- An unknown tag answered with zero days.
- A wrong secret answered with `HTTP 400 from ce.us-east-1.amazonaws.com`. No secret was in it.

**Not proven live:** the sandbox cannot show a non-zero tagged bill.

- Its SCP allows EC2 only in us-east-2.
- Free-plan usage costs 0, even before credits. The Operator's one-minute instance of 2026-10-23 shows 0.0175 hours and 0 USD.
- A linked account cannot activate cost-allocation tags.
- So Phase 10's "one bill reconciles" waits for an account that bills. Reconciliation itself is proven by fixtures and hand computations.

### 10.5b: Azure

**Grilled 2026-10-26 (2 rounds, all recommended):**

- Same bill check, for Azure Cost Management. It uses the Contract's `azure` integration.
  - Tenant, client id and subscription come from its env.
  - The client secret comes from env_secrets.
  - The role is Reader, or Cost Management Reader.
- `billing: {tag}` only.
- It reads `ActualCost` and `PreTaxCost`, for Virtual Machines, usage charges, daily.
- A billed cluster reads its `provider`'s bill. Otherwise it reads the only cloud integration. If there are both, and no `provider`, it is refused.
- `list_prices.currency` is used. Azure's lookup passes `currencyCode`. A non-USD currency is refused unless every cluster sets `provider: azure`. So a BRL subscription compares with BRL prices.
- Migration 0022 keys `list_prices` by currency.
- Live check: one Standard_B1s Linux VM in centralus, in the Free Trial, tagged `aiops-cluster=kind`, for 24 to 48 hours. This costs about 3.1 BRL for 48 h, with the Operator's yes. The bill should be near 0.0652 × hours, and the estimate within 5%.
- Then v0.15.0.

**Built (2026-10-26):** `billing.aws_fetch` and `azure_fetch`. The token is read per read, and never stored. A day before the latest row without one reads as 0. Also `Contract.bill_provider`, `ListPrices.currency`, and migration 0022. The console needed no change.

**Checked live (2026-10-29 to 31, Azure sandbox, Free Trial, BRL):**

- A Standard_B2ps_v2 VM was used, in centralus, tagged `aiops-cluster=kind`. It was read with the sandbox service principal, and priced from the Retail Prices API in BRL, at 0.3962 per hour.
- The free 750 hours of B2pts_v2 bill 0.0 before credits. This is like AWS's free plan. So the first VM was resized to the paid size.
- B1s was unavailable in the trial. The paid size needed the `UseStandardSecurityType` feature registered. That was undone afterwards.
- For the full UTC day 2026-10-30, the bill was 9.5077 BRL. That is 24 h × 0.3962, to the cent.
- The partial day 2026-10-29 was 8.16. That is 20.6 h × 0.3962.
- The estimate did not reconcile for 10-30. It was 5.15 BRL, -45.8%. Prometheus held only 14 of 25 hourly points that day, because the lab's Mac slept.
- Per sampled hour, the estimate equals the bill.
- So the price, the query, and the BRL currency path are proven live.
- The 5% rule on a day with complete samples is proven by fixtures only.
- **Finding:** `sizing bill` did not show how many hours the estimate rests on. Showing node-hour coverage is a follow-up. It was done in 10.5d.
- The VM, the resource group and the demo-shop edits were removed. The cost was about 26 BRL.

### 10.5c: GCP

**Grilled 2026-10-31 (3 rounds, all recommended). Built after 10.5d.**

- The GCP bill comes from the BigQuery billing export. This is the only route.
- The client enables the export. The client also grants a dataset, BigQuery Data Viewer on it, and Job User on the project.
- The cluster config is `billing: {tag: "key=value", table: "project.dataset.table"}`.
  - `table` is required for GCP. It is refused for AWS and Azure.
  - The tag must be a valid GCP label.
- The provider comes from `provider`. Otherwise it is the only cloud integration of aws, azure or gcp.
- Live check afterwards (an e2-standard-2 labelled `aiops-cluster=kind`, for 24 to 48 hours, about 3.2 USD, after the Operator links billing in the sandbox and enables the export, with the Operator's yes).
- The `gcp` integration's key mints a separate `bigquery.readonly` token for each read. The agent's token is unchanged.
- The query runs in the integration's project. The table name is checked against a strict pattern. The label goes in as query parameters.
- It reads `cost` before credits, per UTC usage day, and the `currency` column.
- It covers core and RAM SKUs of Compute Engine only. Spot is excluded.
- Days without rows, before the latest day, read as 0.
- The same daily read, hourly retry and 2-day lag as AWS.
- `list_prices.currency` also applies to GCP. It uses the catalog's `currencyCode`. AWS stays USD.
- Not matched, and said in the docs:
  - shared-core VMs (e2-micro, small, medium);
  - Spot;
  - committed-use lines;
  - disks and network.
- Ships on fixtures first, as v0.17.0. The live check came afterwards.

**Built (2026-10-31, v0.17.0), as grilled:**

- `billing.gcp_fetch` mints a `bigquery.readonly` token from the `gcp` integration's key. `GoogleKey` takes a scope. The MCP servers keep theirs.
- It runs one `jobs.query` in `GOOGLE_CLOUD_PROJECT`, and sums `cost` per UTC day and currency.
- A query that does not finish in 60 s, comes in pages, or bills a day in two currencies is refused with a message. It is not read in part.
- `Billing.table` has a strict pattern, because it goes into the SQL.
- `Contract.bill_provider` covers aws, azure and gcp. GCP label rules apply.
- A GCP catalog price in another currency is refused.
- No migration.

**Checked live (2026-11-01 to 04, GCP sandbox, USD):**

- An e2-standard-2 VM ran in us-central1, labelled `aiops-cluster=kind`. It ran k3s and Prometheus.
- kind on the Mac would have reported its own 10 vCPU and 31 GiB. GCP's list price is capacity × core and RAM rates. So the estimate would have been 4.6 times the bill.
- It was read with the sandbox service account. That account had BigQuery Job User on the project, and reader on the export dataset.
- It was priced from the Cloud Billing catalog, with an API key.
- For the full UTC day 2026-11-02, with 24 of 24 hours sampled:
  - the bill was 1.61 USD (48 vCPU-hours 1.0469 + 192 GiB-hours 0.5613, at the list rates, to the cent);
  - the estimate was 1.59;
  - the gap was -1.0%, within 5%.
- **Finding:** a newly enabled export was still filling that day 28 hours after it ended. The first read saw 20.6 of 24 VM-hours, 1.38 USD, +15.3%, and was "outside". It was complete 30 to 38 hours after the day ended.
- The 2-day lag counts from the end of the day. So it usually covers this. But a day with fewer billed hours than sampled hours could be shown as outside, when it is still settling. This is a possible follow-up.
- The VM, the firewall rule, the API key, the service account's BigQuery grants and the demo-shop edits were removed. The sandbox's billing was unlinked.
- Cost: 2.59 USD before credits, and 0 after.

### 10.5d: coverage of the bill check

**Built (2026-10-31, v0.16.0, from the 10.5b live finding):**

- `sizing bill` rows carry `hours`, `sampled` and `coverage`.
- Under 100%, the note says "the estimate rests on N of M sampled hours".
- Under 90% (`billing.MIN_COVERAGE`), `within` is null. The gap is still shown. The CLI prints "no verdict", and does not exit 1 for that alone. The Cost page shows it as a notice.
- `usage.node_hours` also returns how many hourly samples there were.

## Later slice: the billed rate feeds prices

*Requested by the Operator, to be remembered.* This slice is not built.

After reconciliation shows the gap, a billed rate can replace the list price. This covers discounts and savings plans. It needs its own precedence rules against typed `prices` and `list_prices`.
