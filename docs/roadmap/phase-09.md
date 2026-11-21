# Phase 9: Read-only Sizings

**In short:** Phase 9 added Sizings. A Sizing is the machine's suggestion of CPU and memory requests for a Workload. The Hub reads usage from each Direct Contract's Prometheus, scores waste and risk, and shows them in the console and the agent. Nothing in a client cluster changes in this phase.

*What it adds:* a suggested CPU and memory request for each Workload, with its waste and risk, read from Prometheus. Nothing in a client cluster changes in this phase.

**Status:** built (7 slices).

| Slice | What | Status |
|---|---|---|
| [9.1](#91-metric-contract-and-workloads) | Metric contract and Workloads | ✅ built |
| [9.2](#92-recommender) | Recommender | ✅ built, checked on kind |
| [9.3](#93-sizing-policies) | Sizing policies | ✅ built, checked on the lab |
| [9.4](#94-waste-and-risk) | Waste and risk | ✅ built, checked on kind |
| [9.5](#95-console-app-and-slack) | Console app and Slack digest | ✅ built, checked live |
| [9.6](#96-the-agent-uses-sizings) | The agent uses Sizings | ✅ built |
| [9.7](#97-a-sizings-history-is-covered-time) | A Sizing's history is covered time | ✅ built, checked live |

Touches nothing in client clusters. Sizings come from each Direct Contract's Prometheus.

## 9.1 Metric contract and Workloads

**What:**

- The Prometheus queries a Sizing needs. These cover container CPU and memory usage, requests, limits, OOM kills, and throttling. They are checked per Contract.
- Workloads are found through their pod owners. The owners are: ReplicaSet to Deployment, Jobs, StatefulSets, DaemonSets, Rollouts, and ownerless pods.

**Grilled 2026-10-21:**

- Owners come from kube-state-metrics. The Kubernetes API covers the pods it misses.
- A cluster's own Prometheus URL and token are used. Its label matchers too. Otherwise the integration's are used.
- Memory is the working set.
- The lab runs kube-prometheus-stack.

**Parts:**

- Part a: the code, a fake Prometheus, and `aiops sizing usage`. Built 2026-10-21.
- Part b: the persistent kind lab. Run `make lab`. It has kube-prometheus-stack and nine Workload shapes, in namespace `lab`. Built 2026-10-21. The Mac Hub's image reads it.

**Deferred:**

- A synthetic backfill of weeks of history comes with 9.2, which needs it. Later, it waits for the seasonality work in Phase 14.
- The agent's `prometheus` tools move to per-cluster URLs in 9.6.

**Done when:** a fake Prometheus, in the style of `fake_cluster` (CI has no kubeconfig), gives the right Workloads and usage for these series: flat, diurnal, spike, slow-leak and OOM.

## 9.2 Recommender

**What:** a Sizing is worked out per container, for CPU and memory separately. The settings are:

- the percentile;
- the window;
- the headroom;
- the minimum and maximum;
- the change threshold;
- a node-capacity cap.

**Grilled 2026-10-21 (all recommended):**

- CPU: p95 plus 15%. Memory: peak plus 20%.
- Recent usage counts more, with a 24 h half-life. This applies to CPU only. A maximum can't decay.
- Limits are kept. Conflicts are flagged.
- A Sizing needs 24 hours of data before it is first made.

**Added from review:**

- CPU is never lowered while throttled over 10%.
- Memory is never lowered after an OOM kill.

**Built (2026-10-21):** `aiops sizing show`.

**Moved out:**

- The node-capacity cap moved to 9.3's per-policy maximum, and to Phase 15.
- *Why:* the read-only RBAC cannot list nodes. And node series on a shared store without labels would cross Contracts.

**Checked on kind (2026-10-23, 00:20 UTC, after the first 24 hours):**

- `api` (1000m and 1Gi requested) was sized at 116m and 92Mi.
- Its real p95 was 100m, and its peak was 76Mi. So it was within its 15% and 20% headroom.
- `throttled` CPU and `leaky` memory went up to their limits. The risk was listed.

**Done when:**

- every rule has a test that fails when the rule is removed;
- a 4x over-requested kind Workload gets a Sizing within headroom of its real p95.

## 9.3 Sizing policies

**What:**

- Built-in policies: production, batch, and high-availability.
- Overrides in `contract.yaml`, validated by `aiops contracts validate`.

**Grilled 2026-10-22 (all recommended):**

| Policy | Percentile | Memory headroom | Window | Other |
|---|---|---|---|---|
| production | from 9.2 | from 9.2 | from 9.2 | from 9.2 |
| high-availability | p99 + 30% | + 35% | 14 d | 72 h half-life and history; floors 50m and 64 MiB |
| batch | p90 + 10% | + 15% | 14 d | |

- A policy owns: the window, the percentile, the half-life, the headroom, the floors, an optional max, and the threshold.
- A capped Sizing is listed as a risk.
- The holds are not settings.
- Limits only `keep` until Phase 12.
- Your own policy names a built-in `base:`.
- Built-in names cannot be reused. A standing approval names a policy.

**Rules:**

- A rule matches on cluster, namespace, kind, name and container. Namespace and name accept globs.
- The first matching rule wins.
- Then Jobs and CronJobs get batch.
- Then the default applies.
- `off` excludes a Workload.

**Built (2026-10-22).** The `--window` option was left out of `sizing show`. `sizing show` names the policy, and shows a short history.

**Changed from the grilling:** each window the Contract's policies can reach is read in full. It is not read once at the longest and then trimmed. *Why:* OOM kills, throttling and requests are read over the whole window. This was found in review.

**Done when:**

- every setting, rule and bound has a test that fails when it is removed;
- on the lab, `report` is batch with no rule, `db` is high-availability, and `agent` uses a Contract's own policy through rules. `idle` is excluded, and the rest use production.
- Checked 2026-10-22. All were still learning.

## 9.4 Waste and risk

**What:** for each Workload:

- **waste** is the requests above the Sizing;
- **risk** covers OOM kills, throttling, and requests below the Sizing.

Both are stored in Postgres, per Contract.

**Grilled 2026-10-22 (all recommended):**

- One row per container per run. It holds the policy, the state, the current value and the Sizing, the average pods, the waste and the risks. Learning and excluded containers are included.
- The Hub's scheduler runs each Contract that has Prometheus every hour. `aiops sizing refresh` runs one on demand.
- Waste = the request above the Sizing, times the average pods.
- Risks are `{kind, text}`. The kinds are: `oom_killed`, `throttled`, `under_requested`, `limit_below_need`, `capped_by_policy`, and `request_unset`.
- Rows are kept hourly for 14 days, then the last one of each day for 400 days.
- Status is per cluster. A failed cluster stays at its last good run.
- `aiops sizing waste` ranks Workloads. A core weighs 4 GiB until Phase 10's prices.

**Built (2026-10-22).**

**Changed from the grilling:**

- A run is due when the Contract's last run started an hour ago. This is read from the runs table. There is no `next_due` row.
- Average pods counts only the steps where the cluster has any sample. *Why:* the lab's Prometheus misses scrapes while the Mac sleeps. The lab check found `api` averaging 2.5 of its 3 pods.

**Checked on kind (2026-10-23, 01:12 UTC, into a scratch `aiops_lab` database):**

- `api` topped the list: 2.63 cores and 2.70 GiB over 3 pods.
- `leaky` was oom_killed. `throttled` was throttled. `db` was learning. `idle` was excluded.

**Found and fixed:**

- Pods were first counted from CPU samples. The `rate` over 5 minutes keeps a pod counted for about 5 minutes after it ends.
- So `report` (a minute of work every 10) averaged 0.5 pods, and ranked second on waste.
- On the Operator's call (2026-10-22), pods now come from kube-state-metrics' Running phase, not from CPU samples.
- After the fix, `report` had 0.1 pods and 0.10 cores. `leaky` had 1.0.
- The ranking key (a core weighs 4 GiB) stays until Phase 10.

**Done when:** the isolation test proves that Contract A's Sizings never appear for B.

## 9.5 Console app and Slack

**What:**

- The React console's foundation: build, serving, login, and the JSON API pattern.
- Its first page shows Workloads per Contract: Sizing against current over time, waste, and risk.
- A weekly digest goes to the Contract's channel.

**Grilled 2026-10-23 (all recommended).** It has two parts.

### 9.5a: the app

- Vite, React and TypeScript. npm, with Node 22 pinned. It is built in the Docker build. There is no Node at runtime.
- uPlot charts.
- Served at `/app/`, beside the Jinja `/console`.
- A JSON API at `/api/v1/`, with pydantic models. TypeScript types are generated from its OpenAPI, with a drift check.
- The page needs the identity. The API needs the identity and the approver cookie. A 401 links to the token page.
- A strict CSP.
- Text only. This is enforced by ESLint, a source scan, and a component test.
- Workloads are ranked by waste. A container's chart shows the 14-day hourly view, or the 400-day daily view. It reads stored runs only.
- `make console-dev` proxies a local Hub. It adds the identity in development only.
- A live check with screenshots over `tailscale serve`. Then v0.4.0.

**Built 2026-10-23, and checked live over `tailscale serve`:**

- demo-shop's table showed `api` on top, at 2.62 cores.
- `api`'s 14-day chart showed the request, the Sizing and the p95.
- `leaky`'s risk texts showed as text.
- No CSP errors appeared in the browser.

**Changed from the grilling:**

- The approver cookie's path is now `/`. It was `/console`, which the browser never sends to `/api/v1/`.
- TypeScript is 5.9, not 6. `npm ci` refuses the peer range of `openapi-typescript`.
- ESLint bans raw HTML with its own `no-restricted-syntax` rule. `eslint-plugin-react` does not yet support ESLint 10.

### 9.5b: the weekly digest

- Monday 09:00 UTC, in the operator channel, by default.
- `sizing.digest:` overrides the cron and the zone. It can also be `off`.
- It runs on the Checks' schedule rows.
- It shows the totals and the week's change, the top 5, new risks, stale clusters, and the learning count.
- It has a link when `AIOPS_CONSOLE_URL` is set.
- `aiops sizing digest [--post]` runs it by hand.
- Live post, then v0.5.0.

**Built 2026-10-23:**

- `sizing_digests` holds one row per Contract that gets a digest. It has the due time and the schedule key.
- A row is claimed with one conditional update before posting.

**Changed from the grilling:**

- The digest has its own table. The grilling used the Checks' rows, but the console lists those as Checks, and a Check could take the same names.
- The week's change and new risks compare with each cluster's latest run, up to 7 days before. This is the same per-cluster rule as `sizing waste`.
- If there is no run that old, the digest says so. It lists every risk under "Risks".
- A Contract that no longer gets a digest loses its row. So turning digests back on starts at the next due time, not at a missed one.

**Done when:** the console tests that guard the Jinja pages still pass against the app. These cover identity, the token cookie, the 404 through Caddy, and text-only model output.

## 9.6 The agent uses Sizings

**Plan:** a `sizing_for_workload` tool. A Triage of an OOM kill cites the Sizing.

**Done when:** a new Scenario passes on Haiku. It is an OOM-killed Triage whose Report recommends a memory request above the observed peak.

**Grilled 2026-10-23 (all recommended). It has two parts.**

### 9.6a: the sizing tool

- A listed built-in integration, `sizing`. It is refused without a cluster that has a Prometheus. It is absent in the CLI, which has no database.
- Its tool is `sizing_for_workload(cluster, namespace, name, kind?)`. It reads stored Sizings, by Workload or pod name.
- After an OOM kill, it suggests memory at the policy's headroom above the limit. With no limit, it suggests memory above the peak. This is a stopgap until Reflex 13.1.
- The Scenario is recorded from the lab's `leaky`, with `expect.memory_above`.
- One Haiku run.

**Built 2026-10-23.** `oom-killed-sizing` passed on Haiku (run 2, 4 calls).

**Changed from the grilling:**

- Stored rows now hold each resource's limit. The suggestion needs it. Older rows get none.
- A Triage Scenario can set `expect.evidence`. So this Scenario must cite the Sizing as well as name the memory.
- It expects `app_error` or `resource_limits`. In the Haiku run, the model called `leaky` a leak. Its env var shows that, and the categories define it as `app_error`.

### 9.6b: per-cluster Prometheus for the agent

- The agent's `prometheus` tools take a `cluster`. They use that cluster's own URL and token.
- A cluster with `prometheus_labels` gets no PromQL at all. Its store is shared, and free PromQL could read another Contract's series.
- A Contract without clusters keeps today's tools.

**Built 2026-10-23, as grilled, plus one change:**

- The Operator's call: no PromQL on a cluster whose Prometheus another Contract also reads in other namespaces. The old single-URL tools had this gap too.
- No Scenario lists `prometheus`, so no eval was rerun.
- The grilling also set v0.6.0 and a live Triage on the Mac Hub as next steps.
- On the Mac Hub, demo-shop's agent queries the lab's Prometheus, through its cluster's `prometheus_url`. Before, it used the integration's `localhost:9090`, which the Hub's container never reached.

## 9.7 A Sizing's history is covered time

**Found 2026-11-13 on the lab:**

- The lab's Prometheus held one hour of data after a three-day gap. The kind node had been stopped.
- Seven Workloads still read as ready. A container's history was its newest sample minus its oldest sample.

**Grilled 2026-11-13 (all recommended):**

- A container's history is the covered time between its first and its last sample, in its policy's window.
- The steps counted are those where its cluster has any sample of a container in the Contract's namespaces. This is what `avg_pods` already counts.
- So a gap in Prometheus counts for nothing. A CronJob between its runs still counts.
- The history is (steps - 1) times the window's own step. A cluster without a gap gets the number it has today.
- A ready Sizing whose window now covers less than its policy's history is **learning** again:
  - Automation applies nothing new to it.
  - The Actuator keeps what it holds. It gives a container back only on a template change or when the Workload is gone.
  - Reflexes go on.
  - Its row moves to the learning list of waste and the digest.
- There is no rule that a Sizing needs a recent sample. A CronJob between runs, and a Workload scaled to zero, have none.
- `history_h` stays the one field. It now holds the covered hours. Its texts stay as they are, so no eval reruns.
- There is no separate read of scrape health (`up`). Its labels differ between Prometheus setups.
- Phase 14's Floor and Weekly profile count their own minimums of history with the same helper.

**Known limit, for the guide:** where a Contract's namespaces on a cluster hold only periodic Workloads, only their run times count.

- For example, one CronJob that runs for 5 minutes a day, under batch, covers about 70 minutes of its 14 days. It stays learning. Today it is ready after its second run.
- One container that runs all the time there ends the learning. An excluded container also ends it.
- A policy with a shorter `history` also ends it.

**Built 2026-11-13, as grilled.** `sizing.history` is called by `rows`, with the window's own step.

**Chosen beyond the grill:**

- The steps counted are those with a usage sample (CPU or memory). Running pods are not used. So kube-state-metrics scraped through a gap in the kubelet's is not history.
- `size` on a container alone, with no cluster to tell a gap by, keeps the span.
- The fake Prometheus can leave samples out.

**Changed in the Actuator:** no Actuator code changed. A test now holds that a Sizing Automation no longer lists keeps its hold. A Step-up that reaches its term while its Sizing is learning is kept for another term. The Hub lists it without a peak, as 13.1c has it for a peak not yet known.

**Savings:** Realized Savings compare requests, whatever the state. So they do not move. The available Savings and the digest's waste go down for a container that is learning again. The digest's count of learning containers sits beside them.

**Tests:** 18 mutations, each failing a test.

**Checked live (2026-11-13, 17:23 UTC, read-only):**

- The working tree's hourly read ran (`usage.collect`, `sizing.rows`). There was no Hub, no database, no Actuator, and no model call.
- It used a synthetic Contract, against the lab's Prometheus.
- The lab's Prometheus held 3 minutes of 10 November. Then nothing for 70 hours. Then three hours with a 12-minute hole of its own. Every target was missing from 16:33 to 16:44 UTC.
- Seven containers read as ready on 73 hours by the old rule: `api`, `web`, `spiky`, `leaky`, `throttled`, `agent`, and `db` under high-availability. Now they read learning, with 2.9 hours of 24. `db` read 3.0 of 72.
- The wall clock said 3.0 hours since the node came back.
- The CronJob `report` was learning, as before. `idle` was excluded.
- The real samples sit on the grid the count assumes. In both windows (7 d at 120 s, and 14 d at 180 s), every stretch of data had exactly the steps of a full grid. There was no spacing but one step.

**Not checked live:** a Sizing going from ready back to learning under Automation. This is covered by unit tests only. The lab has no ready Sizing until it has run for a day.

**Done when:**

- the helper's rule and its (steps - 1) count each have a test that fails when broken;
- a container that was ready is learning after a gap, and the Actuator keeps its hold;
- on the lab, the Workloads that read ready on an hour of data read learning with their real hours.
